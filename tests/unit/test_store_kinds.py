"""任务类型（kind）：分离的去重键、领取优先级、每类流转图、老库迁移。"""

from __future__ import annotations

import sqlite3

import pytest

from adapters.sqlite_store import SqliteStore
from core.errors import InvalidTransition
from core.models import TriggerKind
from core.states import TaskKind, TaskState
from tests.helpers import Clock, make_trigger


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    store = SqliteStore(tmp_path / "tasks.db")
    yield store
    store.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _enqueue(
    store: SqliteStore, clock: Clock, *, kind: TriggerKind, delivery: str, issue: int = 12
):
    return store.enqueue(
        make_trigger(delivery_key=delivery, kind=kind, issue_iid=issue), now=clock()
    )


def test_fix_and_triage_on_the_same_issue_can_coexist(store: SqliteStore, clock: Clock) -> None:
    fix = _enqueue(store, clock, kind=TriggerKind.LABEL, delivery="d-fix")
    triage = _enqueue(store, clock, kind=TriggerKind.ISSUE_OPEN, delivery="d-triage")
    assert fix.created and fix.task is not None
    assert triage.created and triage.task is not None
    assert fix.task.kind is TaskKind.FIX
    assert triage.task.kind is TaskKind.TRIAGE
    # 同一个 issue，但 kind 不同 → 两个活跃任务都合法
    assert fix.task.subject_key == triage.task.subject_key == "issue:12"
    assert store.count() == 2


def test_duplicate_triage_for_same_issue_is_rejected(store: SqliteStore, clock: Clock) -> None:
    first = _enqueue(store, clock, kind=TriggerKind.ISSUE_OPEN, delivery="d-1")
    second = _enqueue(store, clock, kind=TriggerKind.ISSUE_OPEN, delivery="d-2")
    assert first.created
    assert not second.created
    assert second.reason == "active_task_exists"
    assert second.task is not None and second.task.id == first.task.id


def test_claim_priority_prefers_triage_over_fix(store: SqliteStore, clock: Clock) -> None:
    _enqueue(store, clock, kind=TriggerKind.LABEL, delivery="d-fix")
    _enqueue(store, clock, kind=TriggerKind.ISSUE_OPEN, delivery="d-triage")
    claimed = store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    assert claimed is not None
    assert claimed.kind is TaskKind.TRIAGE


def test_claim_can_be_restricted_to_kinds(store: SqliteStore, clock: Clock) -> None:
    _enqueue(store, clock, kind=TriggerKind.ISSUE_OPEN, delivery="d-triage")
    assert store.claim_next(owner="w1", now=clock(), lease_seconds=60, kinds=[TaskKind.FIX]) is None
    claimed = store.claim_next(owner="w1", now=clock(), lease_seconds=60, kinds=[TaskKind.TRIAGE])
    assert claimed is not None and claimed.kind is TaskKind.TRIAGE


def test_triage_walks_its_own_graph(store: SqliteStore, clock: Clock) -> None:
    outcome = _enqueue(store, clock, kind=TriggerKind.ISSUE_OPEN, delivery="d-triage")
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    task_id = outcome.task.id
    store.transition(task_id, TaskState.RUNNING, now=clock())
    with pytest.raises(InvalidTransition):
        store.transition(task_id, TaskState.VALIDATING, now=clock())
    store.transition(task_id, TaskState.FINALIZING, now=clock())
    store.transition(task_id, TaskState.SUCCEEDED, now=clock())


def test_fix_graph_does_not_use_finalizing(store: SqliteStore, clock: Clock) -> None:
    outcome = _enqueue(store, clock, kind=TriggerKind.LABEL, delivery="d-fix")
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    store.transition(outcome.task.id, TaskState.RUNNING, now=clock())
    with pytest.raises(InvalidTransition):
        store.transition(outcome.task.id, TaskState.FINALIZING, now=clock())


def test_payload_roundtrip(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(
        make_trigger(kind=TriggerKind.ISSUE_OPEN),
        now=clock(),
        payload={"verdict": "confirmed", "repro": {"command": "pytest -q"}},
    )
    assert outcome.task is not None
    loaded = store.get(outcome.task.id)
    assert loaded is not None
    assert loaded.payload["verdict"] == "confirmed"
    assert loaded.payload["repro"]["command"] == "pytest -q"


# --------------------------------------------------------------------------- #
# 老库迁移：v0.1 的 tasks 表没有 kind / subject_key
# --------------------------------------------------------------------------- #

LEGACY_SCHEMA = """
CREATE TABLE tasks (
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
CREATE UNIQUE INDEX ux_tasks_active_issue
    ON tasks(project_id, issue_iid)
    WHERE state NOT IN ('succeeded', 'failed');
CREATE TABLE task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


def test_legacy_database_is_migrated_in_place(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, delivery_key, project_id, project_path, issue_iid,"
        " trigger_kind, trigger_actor, trigger_actor_id, command, state, attempts,"
        " max_attempts, report_json, created_at, updated_at)"
        " VALUES ('task-legacy', 'legacy-delivery', 42, 'group/project-a', 12,"
        " 'label', 'alice', 7, NULL, 'queued', 0, 3, '{}',"
        " '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')",
    )
    conn.commit()
    conn.close()

    store = SqliteStore(path)
    legacy = store.get("task-legacy")
    assert legacy is not None
    assert legacy.kind is TaskKind.FIX
    assert legacy.subject_key == "issue:12"
    # 新老任务共用同一个去重语义：老库里已有活跃 fix，新的 fix 会被拒绝
    second = store.enqueue(make_trigger(delivery_key="d-new"), now=Clock()())
    assert not second.created
    assert second.reason == "active_task_exists"
    store.close()
