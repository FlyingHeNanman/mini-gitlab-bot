"""SqliteStore：幂等入队、领取、流转守卫、租约、崩溃恢复、审计事件。"""

from __future__ import annotations

import pytest

from adapters.sqlite_store import SqliteStore
from core.errors import InvalidTransition
from core.models import DiffSummary, ProjectRef, TriggerKind
from core.states import TaskState
from tests.helpers import Clock, make_trigger


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    store = SqliteStore(tmp_path / "nested" / "tasks.db")  # 目录不存在，应自动创建
    yield store
    store.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


# --------------------------------------------------------------------------- #
# 入队幂等
# --------------------------------------------------------------------------- #


def test_enqueue_creates_queued_task(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.created
    assert outcome.reason == "created"
    assert outcome.task is not None
    task = store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert task.attempts == 0
    assert task.project == ProjectRef(project_id=42, path="group/project-a")
    assert task.trigger_kind is TriggerKind.LABEL
    assert task.max_attempts == 3


def test_duplicate_delivery_is_not_enqueued_twice(store: SqliteStore, clock: Clock) -> None:
    first = store.enqueue(make_trigger(delivery_key="d-1"), now=clock())
    second = store.enqueue(make_trigger(delivery_key="d-1"), now=clock())
    assert first.created
    assert not second.created
    assert second.reason == "duplicate_delivery"
    assert second.task is not None and second.task.id == first.task.id
    assert store.count() == 1


def test_same_issue_with_active_task_is_not_enqueued_again(
    store: SqliteStore, clock: Clock
) -> None:
    first = store.enqueue(make_trigger(delivery_key="d-1", issue_iid=12), now=clock())
    second = store.enqueue(make_trigger(delivery_key="d-2", issue_iid=12), now=clock())
    assert first.created
    assert not second.created
    assert second.reason == "active_task_exists"
    assert second.task is not None and second.task.id == first.task.id
    assert store.count() == 1


def test_retry_after_failure_creates_new_task(store: SqliteStore, clock: Clock) -> None:
    first = store.enqueue(make_trigger(delivery_key="d-1"), now=clock())
    assert first.task is not None
    store.fail(first.task.id, stage="running", reason="炸了", now=clock())
    second = store.enqueue(make_trigger(delivery_key="d-2"), now=clock())
    assert second.created
    assert second.task is not None and second.task.id != first.task.id
    assert store.count() == 2


# --------------------------------------------------------------------------- #
# 领取与租约
# --------------------------------------------------------------------------- #


def test_claim_next_sets_preparing_and_lease(store: SqliteStore, clock: Clock) -> None:
    store.enqueue(make_trigger(), now=clock())
    claimed = store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    assert claimed is not None
    assert claimed.state is TaskState.PREPARING
    assert claimed.attempts == 1
    assert claimed.lease_owner == "w1"
    assert claimed.lease_expires_at is not None
    assert store.claim_next(owner="w1", now=clock(), lease_seconds=60) is None


def test_claim_next_returns_none_on_empty_queue(store: SqliteStore, clock: Clock) -> None:
    assert store.claim_next(owner="w1", now=clock(), lease_seconds=60) is None


def test_renew_leases_extends_only_own_tasks(store: SqliteStore, clock: Clock) -> None:
    store.enqueue(make_trigger(delivery_key="d-1"), now=clock())
    claimed = store.claim_next(owner="w1", now=clock(), lease_seconds=10)
    assert claimed is not None and claimed.lease_expires_at is not None
    clock.advance(30)
    assert store.renew_leases(owner="w1", now=clock(), lease_seconds=60) == 1
    renewed = store.get(claimed.id)
    assert renewed is not None and renewed.lease_expires_at > claimed.lease_expires_at
    assert renewed.heartbeat_at is not None
    assert store.renew_leases(owner="someone-else", now=clock(), lease_seconds=60) == 0


# --------------------------------------------------------------------------- #
# 状态流转
# --------------------------------------------------------------------------- #


def test_transition_walks_normal_path_and_stores_fields(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    task_id = outcome.task.id
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)

    task = store.transition(
        task_id,
        TaskState.RUNNING,
        now=clock(),
        branch="ai-fix/12-fix",
        base_branch="main",
        workdir="/work/task-1",
    )
    assert task.state is TaskState.RUNNING
    task = store.transition(task_id, TaskState.VALIDATING, now=clock(), report={"fixes": ["a"]})
    assert task.report == {"fixes": ["a"]}
    task = store.transition(task_id, TaskState.PUSHING, now=clock(), commit_sha="deadbeef")
    task = store.transition(task_id, TaskState.OPENING_MR, now=clock())
    task = store.transition(
        task_id, TaskState.SUCCEEDED, now=clock(), mr_iid=45, mr_url="http://mr/45"
    )
    assert task.state is TaskState.SUCCEEDED
    assert task.mr_iid == 45
    assert task.finished_at is not None
    assert task.lease_owner is None  # 终态释放租约


def test_illegal_transition_is_rejected(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    with pytest.raises(InvalidTransition):
        store.transition(outcome.task.id, TaskState.PUSHING, now=clock())


def test_transition_rejects_unknown_fields(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    with pytest.raises(ValueError):
        store.transition(outcome.task.id, TaskState.RUNNING, now=clock(), exists=False)


def test_fail_records_reason_and_is_terminal(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    task = store.fail(
        outcome.task.id, stage="running", reason="codex 退出码 1", evidence="未通过", now=clock()
    )
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "running"
    assert task.failure_reason == "codex 退出码 1"
    assert task.failure_evidence == "未通过"
    assert task.finished_at is not None


def test_terminal_task_cannot_be_failed_again(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    store.fail(outcome.task.id, stage="running", reason="第一次", now=clock())
    with pytest.raises(InvalidTransition):
        store.fail(outcome.task.id, stage="running", reason="第二次", now=clock())


def test_requeue_rejects_terminal_state(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    store.fail(outcome.task.id, stage="running", reason="炸了", now=clock())
    with pytest.raises(InvalidTransition):
        store.requeue(outcome.task.id, reason="重来", now=clock())


# --------------------------------------------------------------------------- #
# 崩溃恢复与看门狗
# --------------------------------------------------------------------------- #


def test_requeue_inflight_recovers_after_restart(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    claimed = store.claim_next(owner="dead-worker", now=clock(), lease_seconds=600)
    assert claimed is not None and claimed.state is TaskState.PREPARING

    recovered = store.requeue_inflight(now=clock())
    assert [task.id for task in recovered] == [outcome.task.id]
    task = store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert task.lease_owner is None
    assert task.attempts == 1  # 尝试次数保留，用于重试上限
    # 可以再次被领取
    assert store.claim_next(owner="new-worker", now=clock(), lease_seconds=60) is not None


def test_requeue_inflight_ignores_still_waiting_tasks(store: SqliteStore, clock: Clock) -> None:
    """排队中的任务本来就该继续排队，不该被算作"崩溃恢复"。"""
    store.enqueue(make_trigger(), now=clock())
    assert store.requeue_inflight(now=clock()) == []
    assert store.count(state=TaskState.QUEUED) == 1


def test_reap_expired_requeues_when_attempts_remain(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=10)
    clock.advance(11)

    outcomes = store.reap_expired(now=clock())
    assert len(outcomes) == 1
    assert outcomes[0].requeued
    assert not outcomes[0].failed
    task = store.get(outcome.task.id)
    assert task is not None and task.state is TaskState.QUEUED


def test_reap_expired_fails_when_attempts_exhausted(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock(), max_attempts=1)
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=10)
    clock.advance(11)

    outcomes = store.reap_expired(now=clock())
    assert len(outcomes) == 1
    assert outcomes[0].failed
    task = store.get(outcome.task.id)
    assert task is not None
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "watchdog"
    assert "已达最大重试次数" in (task.failure_reason or "")


def test_reap_expired_ignores_live_leases(store: SqliteStore, clock: Clock) -> None:
    store.enqueue(make_trigger(), now=clock())
    store.claim_next(owner="w1", now=clock(), lease_seconds=600)
    assert store.reap_expired(now=clock()) == []


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #


def test_events_trace_the_whole_lifecycle(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    store.transition(outcome.task.id, TaskState.RUNNING, now=clock())
    store.fail(outcome.task.id, stage="running", reason="炸了", now=clock())

    kinds = [event.kind for event in store.events(outcome.task.id)]
    assert kinds == ["enqueued", "claimed", "state", "failed"]
    assert store.events(outcome.task.id)[0].detail["actor"] == "alice"


def test_list_and_count_filter_by_state(store: SqliteStore, clock: Clock) -> None:
    first = store.enqueue(make_trigger(delivery_key="d-1", issue_iid=1), now=clock())
    store.enqueue(make_trigger(delivery_key="d-2", issue_iid=2), now=clock())
    assert first.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    store.fail(first.task.id, stage="running", reason="炸了", now=clock())

    assert store.count() == 2
    assert store.count(state=TaskState.FAILED) == 1
    assert [task.id for task in store.list_tasks(state=TaskState.QUEUED)] == [
        task.id for task in store.list_tasks() if task.state is TaskState.QUEUED
    ]
    assert len(store.list_tasks(limit=1)) == 1


def test_store_handles_dict_report_round_trip(store: SqliteStore, clock: Clock) -> None:
    outcome = store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    store.claim_next(owner="w1", now=clock(), lease_seconds=60)
    store.transition(outcome.task.id, TaskState.RUNNING, now=clock())
    payload = {"fixes": ["修了 A", "修了 B"], "deps": ["x>=1"], "nested": {"k": 1}}
    store.transition(outcome.task.id, TaskState.VALIDATING, now=clock(), report=payload)
    task = store.get(outcome.task.id)
    assert task is not None
    assert task.report == payload
    assert task.report_list("fixes") == ("修了 A", "修了 B")


def test_diff_summary_model_is_used_by_store_contract(store: SqliteStore) -> None:
    # 防止有人在 core.models 里改坏 DiffSummary（策略层依赖 is_empty/changed_lines）
    diff = DiffSummary(files=("a.py",), insertions=2, deletions=3)
    assert not diff.is_empty
    assert diff.changed_lines == 5
    assert DiffSummary().is_empty
