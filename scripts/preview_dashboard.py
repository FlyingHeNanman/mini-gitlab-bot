#!/usr/bin/env python3
"""本地预览 Dashboard：临时 SQLite + 假 SCM + 一组样例任务。

用法：

    uv run python scripts/preview_dashboard.py

默认地址 http://127.0.0.1:8099/dashboard ，HTTP Basic 用户名 admin、密码 preview2026。
可用 PREVIEW_PORT / PREVIEW_HOST / DASHBOARD_PREVIEW_PASSWORD 覆盖。
"""

from __future__ import annotations

# ruff: noqa: E402  （先修 sys.path，再导入仓库模块；仅此脚本需要）
import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# `python scripts/...` 的 sys.path[0] 是 scripts/，显式把仓库根加回来。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn

from adapters.fakes import FakeAgent, FakeEnvPreparer, FakeSCM, FakeVCS, FakeWorkspace
from adapters.sqlite_store import SqliteStore
from core.models import ProjectRef, Trigger, TriggerActor, TriggerKind
from core.states import TaskKind, TaskState
from gateway.app import create_app
from gateway.config import Settings
from gateway.runtime import build_runtime


class _IdleWorker:
    """让页面显示“Worker 已启用”，但不真的消费队列，保证预览数据稳定。"""

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(3600)


def _trigger(
    *,
    delivery_key: str,
    project_id: int,
    path: str,
    issue_iid: int,
    kind: TriggerKind,
    actor: str = "alice",
    tag_name: str | None = None,
    mr_iid: int | None = None,
    subject_sha: str | None = None,
) -> Trigger:
    return Trigger(
        kind=kind,
        project=ProjectRef(project_id=project_id, path=path),
        issue_iid=issue_iid,
        actor=TriggerActor(user_id=7, username=actor),
        delivery_key=delivery_key,
        tag_name=tag_name,
        mr_iid=mr_iid,
        subject_sha=subject_sha,
    )


def _claim(store: SqliteStore, task_id: str, at: datetime) -> None:
    claimed = store.claim_next(owner="preview", now=at, lease_seconds=86400)
    assert claimed is not None and claimed.id == task_id


def _success(
    store: SqliteStore,
    task_id: str,
    started: datetime,
    states: list[TaskState],
    fields_at: dict[TaskState, dict[str, object]],
) -> None:
    at = started
    for state in states:
        at = at + timedelta(seconds=3)
        store.transition(task_id, state, now=at, **fields_at.get(state, {}))


def _failure(
    store: SqliteStore,
    task_id: str,
    started: datetime,
    *,
    stage: str,
    reason: str,
    evidence: str,
) -> None:
    at = started + timedelta(seconds=3)
    store.transition(task_id, TaskState.RUNNING, now=at)
    store.fail(
        task_id,
        stage=stage,
        reason=reason,
        evidence=evidence,
        now=at + timedelta(seconds=4),
    )


def seed(store: SqliteStore, now: datetime) -> None:
    """造 9 条任务，覆盖成功/失败/执行中/排队与四种 kind。"""
    payments = (101, "acme/payments")
    console = (102, "acme/web-console")
    docs = (103, "acme/docs")
    edge = (104, "acme/edge-worker")

    # 1. 成功修复
    created = now - timedelta(minutes=8)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-fix-128",
            project_id=payments[0],
            path=payments[1],
            issue_iid=128,
            kind=TriggerKind.LABEL,
        ),
        now=created,
        kind=TaskKind.FIX,
    ).task
    assert task is not None
    _claim(store, task.id, created + timedelta(seconds=1))
    _success(
        store,
        task.id,
        created,
        [
            TaskState.RUNNING,
            TaskState.VALIDATING,
            TaskState.PUSHING,
            TaskState.OPENING_MR,
            TaskState.SUCCEEDED,
        ],
        {
            TaskState.RUNNING: {"branch": "ai-fix/128-empty-labels"},
            TaskState.OPENING_MR: {
                "mr_iid": 91,
                "mr_url": "http://127.0.0.1:8929/acme/payments/-/merge_requests/91",
            },
        },
    )

    # 2. 失败修复
    created = now - timedelta(minutes=24)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-fix-131",
            project_id=payments[0],
            path=payments[1],
            issue_iid=131,
            kind=TriggerKind.LABEL,
        ),
        now=created,
        kind=TaskKind.FIX,
    ).task
    assert task is not None
    _claim(store, task.id, created + timedelta(seconds=1))
    _failure(
        store,
        task.id,
        created,
        stage="validating",
        reason="diff 超限：src/parser.py 单文件改动 420 行，超过 150 行阈值",
        evidence=(
            "pytest -q\n"
            "FAILED tests/test_parser.py::test_empty_labels\n"
            "AssertionError: expected [] but got None\n"
            "changed files: src/parser.py (+318/-102)"
        ),
    )

    # 3. 成功巡检
    created = now - timedelta(hours=2, minutes=15)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-triage-127",
            project_id=docs[0],
            path=docs[1],
            issue_iid=127,
            kind=TriggerKind.ISSUE_OPEN,
        ),
        now=created,
        kind=TaskKind.TRIAGE,
    ).task
    assert task is not None
    _claim(store, task.id, created + timedelta(seconds=1))
    _success(
        store,
        task.id,
        created,
        [TaskState.RUNNING, TaskState.FINALIZING, TaskState.SUCCEEDED],
        {},
    )

    # 4. 成功巡检
    created = now - timedelta(hours=1, minutes=55)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-triage-130",
            project_id=edge[0],
            path=edge[1],
            issue_iid=130,
            kind=TriggerKind.ISSUE_OPEN,
        ),
        now=created,
        kind=TaskKind.TRIAGE,
    ).task
    assert task is not None
    _claim(store, task.id, created + timedelta(seconds=1))
    _success(
        store,
        task.id,
        created,
        [TaskState.RUNNING, TaskState.FINALIZING, TaskState.SUCCEEDED],
        {},
    )

    # 5. 成功解释（MR 合并后）
    created = now - timedelta(hours=1, minutes=10)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-explain-87",
            project_id=payments[0],
            path=payments[1],
            issue_iid=124,
            kind=TriggerKind.MERGE,
            mr_iid=87,
            subject_sha="preview-merge-sha",
        ),
        now=created,
        kind=TaskKind.EXPLAIN,
    ).task
    assert task is not None
    _claim(store, task.id, created + timedelta(seconds=1))
    _success(
        store,
        task.id,
        created,
        [TaskState.RUNNING, TaskState.FINALIZING, TaskState.SUCCEEDED],
        {
            TaskState.SUCCEEDED: {
                "mr_iid": 87,
                "mr_url": "http://127.0.0.1:8929/acme/payments/-/merge_requests/87",
            }
        },
    )

    # 6. 失败巡检
    created = now - timedelta(minutes=46)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-triage-129",
            project_id=console[0],
            path=console[1],
            issue_iid=129,
            kind=TriggerKind.ISSUE_OPEN,
        ),
        now=created,
        kind=TaskKind.TRIAGE,
    ).task
    assert task is not None
    _claim(store, task.id, created + timedelta(seconds=1))
    _failure(
        store,
        task.id,
        created,
        stage="running",
        reason="模型返回空结果，未给出可定位的结论",
        evidence="codex.jsonl: completed event missing; last message empty",
    )

    # 7. 执行中的巡检（租约还未过期）
    created = now - timedelta(minutes=2)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-triage-134",
            project_id=console[0],
            path=console[1],
            issue_iid=134,
            kind=TriggerKind.ISSUE_OPEN,
        ),
        now=created,
        kind=TaskKind.TRIAGE,
    ).task
    assert task is not None
    _claim(store, task.id, now - timedelta(seconds=15))
    store.transition(task.id, TaskState.RUNNING, now=now - timedelta(seconds=10))

    # 8. 排队修复（最后两条保持 queued，供页面展示队列）
    created = now - timedelta(minutes=5)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-fix-136",
            project_id=docs[0],
            path=docs[1],
            issue_iid=136,
            kind=TriggerKind.LABEL,
        ),
        now=created,
        kind=TaskKind.FIX,
    ).task
    assert task is not None

    # 9. 排队 Release
    created = now - timedelta(minutes=3)
    task = store.enqueue(
        _trigger(
            delivery_key="preview-release-140",
            project_id=edge[0],
            path=edge[1],
            issue_iid=0,
            kind=TriggerKind.TAG_PUSH,
            tag_name="v1.4.0",
        ),
        now=created,
        kind=TaskKind.RELEASE,
    ).task
    assert task is not None


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="mgb-dashboard-preview-"))
    password = os.environ.get("DASHBOARD_PREVIEW_PASSWORD", "preview2026")
    settings = Settings(
        webhook_secret="preview-secret-value-0123456789",
        repo_whitelist=["acme/payments", "acme/web-console", "acme/docs", "acme/edge-worker"],
        bot_username="mini-gitlab-bot",
        dashboard_username="admin",
        dashboard_password=password,
        tasks_page_enabled=False,
        worker_enabled=False,
        sweep_enabled=False,
        db_path=base / "tasks.db",
        work_dir=base / "work",
        log_dir=base / "logs",
        gitlab_url="http://127.0.0.1:8929",
        gitlab_token="glpat-preview-token",
        model_base_url="http://llm.internal/v1",
        model_name="glm-preview",
        llm_api_key="preview-llm-key",
        config_file=base / "config.json",
    )
    store = SqliteStore(settings.db_path)
    runtime = build_runtime(
        settings,
        store=store,
        scm=FakeSCM(),
        vcs=FakeVCS(),
        agent=FakeAgent(),
        workspace=FakeWorkspace(),
        env=FakeEnvPreparer(),
        with_worker=False,
    )
    runtime.worker = _IdleWorker()  # type: ignore[assignment]
    seed(store, datetime.now(UTC))

    runtime.metrics.inc("sweep_runs_total", 12)
    runtime.metrics.inc("sweep_triaged_total", 4)
    runtime.metrics.inc("sweep_resolved_total", 3)
    runtime.metrics.inc("sweep_errors_total", 1)
    runtime.metrics.inc("release_total", 2)
    runtime.metrics.inc("release_skipped_total", 1)

    app = create_app(settings=settings, runtime=runtime)
    host = os.environ.get("PREVIEW_HOST", "127.0.0.1")
    port = int(os.environ.get("PREVIEW_PORT", "8099"))
    print(f"Dashboard preview: http://{host}:{port}/login", flush=True)
    print(f"Login: admin / {password}", flush=True)
    print(f"SQLite: {settings.db_path}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
