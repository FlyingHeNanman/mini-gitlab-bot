"""SQLite 持久队列 + 状态机 + 租约 + 崩溃恢复（`ports.store.Store` 的实现）。

设计要点：

* **状态只以这张表为准**：进程内对象不是事实来源，所以重启不会丢单（对应 v0 缺陷）；
* `delivery_key` 唯一约束 ⇒ webhook 重复投递不会重复建单；
* **部分唯一索引** ⇒ 同一 issue 同时只能有一个非终态任务；
* 时间统一存带时区的 ISO8601 字符串（可读、可字典序比较）；
* 所有写操作都在 `BEGIN IMMEDIATE` 事务里，配合 `RLock` 支持多线程访问。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.models import (
    UPDATABLE_FIELDS,
    EnqueueOutcome,
    ProjectRef,
    ReapOutcome,
    Task,
    TaskEvent,
    Trigger,
    TriggerKind,
)
from core.states import ACTIVE_STATES, TERMINAL_STATES, TaskState, can_requeue, ensure_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    delivery_key      TEXT NOT NULL UNIQUE,
    project_id        INTEGER NOT NULL,
    project_path      TEXT NOT NULL,
    issue_iid         INTEGER NOT NULL,
    trigger_kind      TEXT NOT NULL,
    trigger_actor     TEXT NOT NULL,
    trigger_actor_id  INTEGER,
    command           TEXT,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    lease_owner       TEXT,
    lease_expires_at  TEXT,
    heartbeat_at      TEXT,
    branch            TEXT,
    base_branch       TEXT,
    commit_sha        TEXT,
    mr_iid            INTEGER,
    mr_url            TEXT,
    failure_stage     TEXT,
    failure_reason    TEXT,
    failure_evidence  TEXT,
    workdir           TEXT,
    log_path          TEXT,
    report_json       TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    finished_at       TEXT
);

-- 同一 issue 同时只能有一个非终态任务（字面量必须与 core.states 保持一致）
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_issue
    ON tasks(project_id, issue_iid)
    WHERE state NOT IN ('succeeded', 'failed');

CREATE INDEX IF NOT EXISTS ix_tasks_state ON tasks(state, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_lease ON tasks(lease_expires_at);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_events_task ON task_events(task_id, id);
"""

_TERMINAL_SQL = "(" + ", ".join(f"'{state.value}'" for state in sorted(TERMINAL_STATES)) + ")"
_ACTIVE_SQL = "(" + ", ".join(f"'{state.value}'" for state in sorted(ACTIVE_STATES)) + ")"
#: "在飞"= 已被 worker 领走的状态；**不含 queued** —— 还在排队的任务无需恢复
_INFLIGHT_STATES = ACTIVE_STATES - {TaskState.QUEUED}
_INFLIGHT_SQL = "(" + ", ".join(f"'{state.value}'" for state in sorted(_INFLIGHT_STATES)) + ")"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        delivery_key=row["delivery_key"],
        project=ProjectRef(project_id=row["project_id"], path=row["project_path"]),
        issue_iid=row["issue_iid"],
        trigger_kind=TriggerKind(row["trigger_kind"]),
        trigger_actor=row["trigger_actor"],
        trigger_actor_id=row["trigger_actor_id"],
        command=row["command"],
        state=TaskState(row["state"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=_dt(row["lease_expires_at"]),
        heartbeat_at=_dt(row["heartbeat_at"]),
        branch=row["branch"],
        base_branch=row["base_branch"],
        commit_sha=row["commit_sha"],
        mr_iid=row["mr_iid"],
        mr_url=row["mr_url"],
        failure_stage=row["failure_stage"],
        failure_reason=row["failure_reason"],
        failure_evidence=row["failure_evidence"],
        workdir=row["workdir"],
        log_path=row["log_path"],
        report=json.loads(row["report_json"] or "{}"),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        finished_at=_dt(row["finished_at"]),
    )


class SqliteStore:
    """线程安全的 SQLite 实现（单容器、单进程，够用且零外部依赖）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        # 懒连接：构造 Store 不产生任何文件系统副作用（导入 gateway.app 时也能安全构造）
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                if self.path.parent and str(self.path.parent) not in ("", "."):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self.path), check_same_thread=False, isolation_level=None
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.executescript(_SCHEMA)
                self._conn = conn
            return self._conn

    # ------------------------------------------------------------------ #
    # 入队（幂等）
    # ------------------------------------------------------------------ #

    def enqueue(self, trigger: Trigger, *, now: datetime, max_attempts: int = 3) -> EnqueueOutcome:
        with self._lock:
            duplicate = self._fetch_one(
                "SELECT * FROM tasks WHERE delivery_key=?", (trigger.delivery_key,)
            )
            if duplicate is not None:
                return EnqueueOutcome(False, "duplicate_delivery", _row_to_task(duplicate))

            active = self._fetch_one(
                "SELECT * FROM tasks WHERE project_id=? AND issue_iid=?"
                f" AND state IN {_ACTIVE_SQL} ORDER BY created_at DESC LIMIT 1",
                (trigger.project.project_id, trigger.issue_iid),
            )
            if active is not None:
                return EnqueueOutcome(False, "active_task_exists", _row_to_task(active))

            task = Task(
                id=f"task-{uuid4().hex[:10]}",
                delivery_key=trigger.delivery_key,
                project=trigger.project,
                issue_iid=trigger.issue_iid,
                trigger_kind=trigger.kind,
                trigger_actor=trigger.actor.username,
                trigger_actor_id=trigger.actor.user_id,
                command=trigger.command,
                max_attempts=max_attempts,
                created_at=now,
                updated_at=now,
            )
            values = {
                "id": task.id,
                "delivery_key": task.delivery_key,
                "project_id": task.project.project_id,
                "project_path": task.project.path,
                "issue_iid": task.issue_iid,
                "trigger_kind": task.trigger_kind.value,
                "trigger_actor": task.trigger_actor,
                "trigger_actor_id": task.trigger_actor_id,
                "command": task.command,
                "state": task.state.value,
                "attempts": task.attempts,
                "max_attempts": task.max_attempts,
                "report_json": "{}",
                "created_at": _iso(now),
                "updated_at": _iso(now),
            }
            try:
                with self._tx() as conn:
                    columns = ", ".join(values)
                    placeholders = ", ".join("?" for _ in values)
                    conn.execute(
                        f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
                        tuple(values.values()),
                    )
            except sqlite3.IntegrityError:
                # 竞态：并发投递刚建了单（同一 delivery，或同一 issue 的活跃任务）
                return self._describe_conflict(trigger)

        self.append_event(
            task.id,
            "enqueued",
            {
                "kind": trigger.kind.value,
                "actor": trigger.actor.username,
                "command": trigger.command,
                "delivery_key": trigger.delivery_key,
            },
            now=now,
        )
        return EnqueueOutcome(True, "created", task)

    def _describe_conflict(self, trigger: Trigger) -> EnqueueOutcome:
        """并发插入撞唯一约束时，判断到底撞的是哪个约束。"""
        same_delivery = self._fetch_one(
            "SELECT * FROM tasks WHERE delivery_key=?", (trigger.delivery_key,)
        )
        if same_delivery is not None:
            return EnqueueOutcome(False, "duplicate_delivery", _row_to_task(same_delivery))
        active = self._fetch_one(
            "SELECT * FROM tasks WHERE project_id=? AND issue_iid=?"
            f" AND state IN {_ACTIVE_SQL} ORDER BY created_at DESC LIMIT 1",
            (trigger.project.project_id, trigger.issue_iid),
        )
        return EnqueueOutcome(
            False, "active_task_exists", _row_to_task(active) if active is not None else None
        )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def get(self, task_id: str) -> Task | None:
        row = self._fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return _row_to_task(row) if row else None

    def list_tasks(self, *, state: TaskState | None = None, limit: int = 50) -> list[Task]:
        if state is None:
            rows = self._fetch_all(
                "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._fetch_all(
                "SELECT * FROM tasks WHERE state=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (state.value, limit),
            )
        return [_row_to_task(row) for row in rows]

    def count(self, *, state: TaskState | None = None) -> int:
        if state is None:
            row = self._fetch_one("SELECT COUNT(*) AS n FROM tasks")
        else:
            row = self._fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state=?", (state.value,))
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # 队列与租约
    # ------------------------------------------------------------------ #

    def claim_next(self, *, owner: str, now: datetime, lease_seconds: int) -> Task | None:
        expires = _iso(now + timedelta(seconds=lease_seconds))
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE state='queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            task_id = row["id"]
            cursor = conn.execute(
                "UPDATE tasks SET state='preparing', attempts=attempts+1, lease_owner=?,"
                " lease_expires_at=?, heartbeat_at=?, updated_at=? WHERE id=? AND state='queued'",
                (owner, expires, _iso(now), _iso(now), task_id),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

        task = _row_to_task(claimed)
        self.append_event(task.id, "claimed", {"owner": owner, "attempt": task.attempts}, now=now)
        return task

    def renew_leases(self, *, owner: str, now: datetime, lease_seconds: int) -> int:
        expires = _iso(now + timedelta(seconds=lease_seconds))
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET lease_expires_at=?, heartbeat_at=?, updated_at=?"
                f" WHERE lease_owner=? AND state NOT IN {_TERMINAL_SQL}",
                (expires, _iso(now), _iso(now), owner),
            )
            return cursor.rowcount

    def reap_expired(self, *, now: datetime) -> list[ReapOutcome]:
        rows = self._fetch_all(
            "SELECT * FROM tasks WHERE lease_expires_at IS NOT NULL AND lease_expires_at < ?"
            f" AND state NOT IN {_TERMINAL_SQL} ORDER BY created_at",
            (_iso(now),),
        )
        outcomes: list[ReapOutcome] = []
        for row in rows:
            task = _row_to_task(row)
            if task.attempts < task.max_attempts:
                outcomes.append(
                    ReapOutcome(self.requeue(task.id, reason="lease_expired", now=now), True)
                )
            else:
                outcomes.append(
                    ReapOutcome(
                        self.fail(
                            task.id,
                            stage="watchdog",
                            reason=(
                                "任务租约过期且已达最大重试次数"
                                f"（{task.attempts}/{task.max_attempts}）"
                            ),
                            evidence="worker 崩溃或心跳丢失，租约未被续期",
                            now=now,
                        ),
                        False,
                    )
                )
        return outcomes

    # ------------------------------------------------------------------ #
    # 状态流转
    # ------------------------------------------------------------------ #

    def transition(self, task_id: str, to: TaskState, *, now: datetime, **fields: Any) -> Task:
        to = TaskState(to)
        with self._lock:
            row = self._require_row(task_id)
            current = TaskState(row["state"])
            ensure_transition(current, to)
            updates = self._sanitize(fields)
            updates["state"] = to.value
            if to in TERMINAL_STATES:
                updates.setdefault("finished_at", _iso(now))
                updates["lease_owner"] = None
                updates["lease_expires_at"] = None
            self._apply(task_id, updates, now)
            self.append_event(
                task_id,
                "state",
                {"from": current.value, "to": to.value, "fields": sorted(updates)},
                now=now,
            )
            return _row_to_task(self._require_row(task_id))

    def fail(
        self,
        task_id: str,
        *,
        stage: str,
        reason: str,
        evidence: str = "",
        log_path: str = "",
        now: datetime,
    ) -> Task:
        with self._lock:
            row = self._require_row(task_id)
            current = TaskState(row["state"])
            ensure_transition(current, TaskState.FAILED)
            fields = {
                "state": TaskState.FAILED.value,
                "failure_stage": stage,
                "failure_reason": reason,
                "failure_evidence": evidence,
                "lease_owner": None,
                "lease_expires_at": None,
                "finished_at": _iso(now),
            }
            if log_path:
                # 失败任务的日志路径要留下：job 目录终态即清，但日志按 PLAN §9 M5 留 14 天
                fields["log_path"] = log_path
            self._apply(task_id, fields, now)
            self.append_event(
                task_id,
                "failed",
                {"stage": stage, "reason": reason, "evidence": evidence},
                now=now,
            )
            return _row_to_task(self._require_row(task_id))

    def requeue(self, task_id: str, *, reason: str, now: datetime) -> Task:
        with self._lock:
            row = self._require_row(task_id)
            current = TaskState(row["state"])
            if not can_requeue(current):
                from core.errors import InvalidTransition

                raise InvalidTransition(current, TaskState.QUEUED)
            self._apply(
                task_id,
                {
                    "state": TaskState.QUEUED.value,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                },
                now,
            )
            self.append_event(
                task_id, "requeued", {"reason": reason, "from": current.value}, now=now
            )
            return _row_to_task(self._require_row(task_id))

    def requeue_inflight(self, *, now: datetime) -> list[Task]:
        """只恢复**已被领走**的任务；仍在排队的 `queued` 任务原样不动。"""
        rows = self._fetch_all(
            f"SELECT * FROM tasks WHERE state IN {_INFLIGHT_SQL} ORDER BY created_at"
        )
        recovered: list[Task] = []
        for row in rows:
            recovered.append(self.requeue(row["id"], reason="worker_restart", now=now))
        return recovered

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #

    def append_event(
        self, task_id: str, kind: str, detail: dict[str, Any], *, now: datetime
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, at, kind, detail_json) VALUES (?,?,?,?)",
                (task_id, _iso(now), kind, json.dumps(detail, ensure_ascii=False, default=str)),
            )

    def events(self, task_id: str) -> list[TaskEvent]:
        rows = self._fetch_all(
            "SELECT at, kind, detail_json FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        return [
            TaskEvent(
                at=datetime.fromisoformat(row["at"]),
                kind=row["kind"],
                detail=json.loads(row["detail_json"] or "{}"),
            )
            for row in rows
        ]

    def purge_terminal(self, *, before: datetime) -> int:
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                f"SELECT id FROM tasks WHERE state IN {_TERMINAL_SQL}"
                " AND finished_at IS NOT NULL AND finished_at < ?",
                (_iso(before),),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            # 事件表没有外键（见 schema），必须显式清；分块避免 SQLite 变量上限
            for start in range(0, len(ids), 400):
                chunk = ids[start : start + 400]
                placeholders = ", ".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", chunk)
            return len(ids)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _fetch_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        conn = self._connection()
        with self._lock:
            return conn.execute(sql, params).fetchone()

    def _fetch_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._connection()
        with self._lock:
            return list(conn.execute(sql, params).fetchall())

    def _require_row(self, task_id: str) -> sqlite3.Row:
        row = self._fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise KeyError(f"任务不存在：{task_id}")
        return row

    @staticmethod
    def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"不允许写入的字段：{sorted(unknown)}")
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "report":
                updates["report_json"] = json.dumps(value or {}, ensure_ascii=False, default=str)
            elif isinstance(value, datetime):
                updates[key] = _iso(value)
            elif isinstance(value, TaskState):
                updates[key] = value.value
            else:
                updates[key] = value
        return updates

    def _apply(self, task_id: str, updates: dict[str, Any], now: datetime) -> None:
        updates = dict(updates)
        updates["updated_at"] = _iso(now)
        assignments = ", ".join(f"{column}=?" for column in updates)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=?",
                (*updates.values(), task_id),
            )
