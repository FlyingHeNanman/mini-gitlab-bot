"""崩溃恢复与看门狗：重启不丢单、租约过期能重跑或判失败。"""

from __future__ import annotations

from core.states import TaskState
from tests.helpers import make_trigger


async def test_restart_requeues_inflight_task_and_finishes_it(make_runtime, clock) -> None:
    runtime, scm = make_runtime(with_worker=True)
    outcome = runtime.store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    # 模拟上一次进程领了任务后崩溃（租约还没到期，SQLite 里仍是 preparing）
    claimed = runtime.store.claim_next(owner="dead-worker", now=clock(), lease_seconds=600)
    assert claimed is not None and claimed.state is TaskState.PREPARING

    # 新进程启动 → 恢复 → 跑完
    assert await runtime.worker.recover() == 1
    task = runtime.store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert task.attempts == 1

    assert await runtime.worker.drain() == 1
    task = runtime.store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.SUCCEEDED
    assert scm.merge_requests


async def test_watchdog_requeues_then_succeeds_on_a_retry_branch(make_runtime, clock) -> None:
    runtime, _ = make_runtime(with_worker=True)
    outcome = runtime.store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    # 第一次尝试被领走后"卡住"（不走编排器），随后租约过期
    runtime.store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    clock.advance(61)

    outcomes = await runtime.worker.reap()
    assert len(outcomes) == 1 and outcomes[0].requeued

    assert await runtime.worker.drain() == 1
    task = runtime.store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.SUCCEEDED
    assert task.attempts == 2  # 手动领取 + 恢复后领取

    # 这次真正跑编排器的是"第 2 次尝试"，分支名必须带任务后缀，
    # 否则会复用上一次可能已推送的分支 → non-fast-forward
    assert len(runtime.vcs.prepare_calls) == 1
    branch = runtime.vcs.prepare_calls[0][2]
    assert branch.startswith("ai-fix/12-")
    assert branch.endswith(task.short_id[:6])


async def test_watchdog_fails_task_when_attempts_exhausted_and_notifies(
    make_runtime, clock
) -> None:
    runtime, scm = make_runtime(with_worker=True, max_attempts=1)
    outcome = runtime.store.enqueue(make_trigger(), now=clock(), max_attempts=1)
    assert outcome.task is not None
    runtime.store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    clock.advance(61)

    outcomes = await runtime.worker.reap()
    assert len(outcomes) == 1 and outcomes[0].failed

    task = runtime.store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "watchdog"
    assert "已达最大重试次数" in (task.failure_reason or "")

    # 看门狗判失败也必须走收尾：切标签 + 贴失败评论（失败不静默）
    assert scm.current_labels == {"ai-fix:failed"}
    assert scm.comments, "看门狗判失败后应当有状态评论"
    assert "失败" in scm.comments[0]["body"]


async def test_heartbeat_keeps_lease_alive(make_runtime, clock) -> None:
    runtime, _ = make_runtime(with_worker=True, lease_seconds=10)
    outcome = runtime.store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    claimed = runtime.store.claim_next(owner=runtime.worker.owner, now=clock(), lease_seconds=10)
    assert claimed is not None

    clock.advance(9)
    assert await runtime.worker.heartbeat() == 1
    clock.advance(5)
    # 已续租，不应被看门狗回收
    assert await runtime.worker.reap() == []
    task = runtime.store.get(outcome.task.id)
    assert task is not None and task.state is TaskState.PREPARING


async def test_recover_leaves_waiting_tasks_alone(make_runtime, clock) -> None:
    """恢复只针对"已被领走"的任务；还在排队的任务不该被算作恢复。"""
    runtime, _ = make_runtime(with_worker=True)
    runtime.store.enqueue(make_trigger(), now=clock())
    assert await runtime.worker.recover() == 0
    assert runtime.store.count(state=TaskState.QUEUED) == 1
