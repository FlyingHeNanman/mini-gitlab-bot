"""状态机单测：合法边、非法边、终态不可变、恢复专用边。"""

from __future__ import annotations

from itertools import pairwise

import pytest

from core.errors import InvalidTransition
from core.states import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    NORMAL_ORDER,
    STAGE_LABELS,
    TERMINAL_STATES,
    TaskState,
    can_requeue,
    can_transition,
    ensure_transition,
    is_terminal,
)


def test_normal_path_is_fully_allowed() -> None:
    for src, dst in pairwise(NORMAL_ORDER):
        assert can_transition(src, dst), f"{src} → {dst} 应当合法"
        ensure_transition(src, dst)


def test_failed_is_reachable_from_every_active_state() -> None:
    assert ACTIVE_STATES, "至少要有非终态"
    for state in ACTIVE_STATES:
        assert can_transition(state, TaskState.FAILED), f"{state} 必须能失败"


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_are_dead_ends(state: TaskState) -> None:
    assert is_terminal(state)
    assert ALLOWED_TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (TaskState.QUEUED, TaskState.SUCCEEDED),  # 不能跳阶段
        (TaskState.PREPARING, TaskState.PUSHING),  # 不能跳阶段
        (TaskState.RUNNING, TaskState.QUEUED),  # 正常流转里不能回队列
        (TaskState.SUCCEEDED, TaskState.FAILED),  # 终态不可变
        (TaskState.FAILED, TaskState.QUEUED),  # 终态不可变
        (TaskState.VALIDATING, TaskState.RUNNING),  # 不能倒退
    ],
)
def test_illegal_transitions_are_rejected(src: TaskState, dst: TaskState) -> None:
    assert not can_transition(src, dst)
    with pytest.raises(InvalidTransition):
        ensure_transition(src, dst)


def test_requeue_is_only_for_active_states() -> None:
    for state in ACTIVE_STATES:
        assert can_requeue(state)
    for state in TERMINAL_STATES:
        assert not can_requeue(state)


def test_active_and_terminal_partition_every_state() -> None:
    assert set(TaskState) == ACTIVE_STATES | TERMINAL_STATES
    assert not (ACTIVE_STATES & TERMINAL_STATES)


def test_stage_labels_cover_every_state() -> None:
    assert set(STAGE_LABELS) == set(TaskState)
