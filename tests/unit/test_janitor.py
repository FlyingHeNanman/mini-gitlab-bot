"""保留策略：该删的删、不该删的一个都不碰（删错 = 删掉正在跑的任务）。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adapters.janitor import Janitor, RetentionSettings
from adapters.sqlite_store import SqliteStore
from core.metrics import Metrics
from core.states import TaskState
from tests.helpers import make_trigger

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "tasks.db")
    yield store
    store.close()


def age(path: Path, days: float) -> None:
    """把 mtime 改到 N 天前（janitor 只看 mtime）。"""
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def finish(store: SqliteStore, task_id: str, *, when: datetime) -> None:
    """沿合法路径把任务推到终态（状态机不允许跳步）。"""
    for state in (
        TaskState.PREPARING,
        TaskState.RUNNING,
        TaskState.VALIDATING,
        TaskState.PUSHING,
        TaskState.OPENING_MR,
        TaskState.SUCCEEDED,
    ):
        store.transition(task_id, state, now=when)


def make_janitor(store: SqliteStore, tmp_path: Path, **settings) -> Janitor:
    return Janitor(
        store=store,
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
        settings=RetentionSettings(**settings),
        clock=lambda: NOW,
        metrics=Metrics(),
    )


async def test_old_log_dirs_are_removed_recent_kept(store: SqliteStore, tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    old = logs / "task-old0001"
    fresh = logs / "task-new0002"
    other = logs / "not-a-task"
    for path in (old, fresh, other):
        path.mkdir(parents=True)
        (path / "codex.jsonl").write_text("x", encoding="utf-8")
    age(old, days=20)
    age(other, days=20)

    report = await make_janitor(store, tmp_path).run()

    assert report.removed_log_dirs == 1
    assert not old.exists()
    assert fresh.exists()  # 14 天内保留
    assert other.exists()  # 不是 task- 前缀，绝不动


async def test_orphan_job_dirs_are_removed_but_active_tasks_are_kept(
    store: SqliteStore, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    active = work / "task-active01"
    orphan = work / "task-orphan02"
    recent = work / "task-recent03"
    cache = work / ".uv-cache"
    for path in (active, orphan, recent, cache):
        path.mkdir(parents=True)
    # 一个仍在排队的任务（queued 也算非终态，绝不能被清）
    task = store.enqueue(make_trigger(), now=NOW - timedelta(days=5), max_attempts=3).task
    assert task is not None
    store.transition(task.id, TaskState.PREPARING, now=NOW - timedelta(days=4), workdir=str(active))
    for path in (active, orphan, cache):
        age(path, days=5)

    report = await make_janitor(store, tmp_path).run()

    assert report.removed_job_dirs == 1
    assert active.exists()  # 非终态任务的工作目录
    assert not orphan.exists()
    assert recent.exists()  # 太新
    assert cache.exists()  # 不是 task- 前缀


async def test_terminal_tasks_older_than_retention_are_purged(
    store: SqliteStore, tmp_path: Path
) -> None:
    old_task = store.enqueue(
        make_trigger(delivery_key="d-old", issue_iid=1), now=NOW - timedelta(days=120)
    ).task
    new_task = store.enqueue(
        make_trigger(delivery_key="d-new", issue_iid=2), now=NOW - timedelta(days=10)
    ).task
    assert old_task is not None and new_task is not None
    for task, days in ((old_task, 100), (new_task, 5)):
        finish(store, task.id, when=NOW - timedelta(days=days))

    report = await make_janitor(store, tmp_path).run()

    assert report.purged_tasks == 1
    assert store.get(old_task.id) is None
    assert store.get(old_task.id) is None
    assert store.get(new_task.id) is not None


def test_purge_keeps_non_terminal_tasks_even_if_ancient(store: SqliteStore, tmp_path: Path) -> None:
    """排队中/在飞的任务再老也不删：那是运维要看的现场。"""
    task = store.enqueue(make_trigger(), now=NOW - timedelta(days=400)).task
    assert task is not None
    store.transition(task.id, TaskState.PREPARING, now=NOW - timedelta(days=399))
    assert store.purge_terminal(before=NOW) == 0
    assert store.get(task.id) is not None


async def test_report_counts_and_metrics(store: SqliteStore, tmp_path: Path) -> None:
    metrics = Metrics()
    janitor = Janitor(
        store=store,
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
        clock=lambda: NOW,
        metrics=metrics,
    )
    logs = tmp_path / "logs" / "task-gone01"
    logs.mkdir(parents=True)
    age(logs, days=30)

    report = await janitor.run()

    assert report.total == 1
    assert metrics.value("janitor_runs_total") == 1
    assert metrics.value("janitor_removed_total", kind="log_dir") == 1


async def test_run_is_safe_when_directories_do_not_exist(
    store: SqliteStore, tmp_path: Path
) -> None:
    report = await make_janitor(store, tmp_path).run()
    assert report.total == 0
