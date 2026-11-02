"""任务状态机：纯逻辑、无 IO、可单测。

正常路径：

    queued → preparing → running → validating → pushing → opening_mr → succeeded

不变式（这几条是 M1 完成判据的一部分，别为了省事放宽）：

* `failed` 可以从**任何**非终态到达 —— 任何阶段都可能失败；
* 终态（`succeeded` / `failed`）**不可再流转**；
* 恢复专用边 `requeue`（非终态 → `queued`）不参与正常流转，用 `can_requeue()` 单独表达，
  只有崩溃恢复 / 租约过期才会用到它。
"""

from __future__ import annotations

from enum import StrEnum


class TaskKind(StrEnum):
    """任务类型。同一条 issue 上可以并存不同类型的活跃任务。"""

    FIX = "fix"
    TRIAGE = "triage"
    EXPLAIN = "explain"


class TaskState(StrEnum):
    """任务状态。取值同时是 SQLite 里存的字符串。"""

    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    VALIDATING = "validating"
    PUSHING = "pushing"
    OPENING_MR = "opening_mr"
    #: triage / explain 的收尾阶段：把结论落成标签与评论（fix 不使用这个状态）
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATES: frozenset[TaskState] = frozenset({TaskState.SUCCEEDED, TaskState.FAILED})

ACTIVE_STATES: frozenset[TaskState] = frozenset(s for s in TaskState if s not in TERMINAL_STATES)

NORMAL_ORDER: tuple[TaskState, ...] = (
    TaskState.QUEUED,
    TaskState.PREPARING,
    TaskState.RUNNING,
    TaskState.VALIDATING,
    TaskState.PUSHING,
    TaskState.OPENING_MR,
    TaskState.SUCCEEDED,
)

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.PREPARING, TaskState.FAILED}),
    TaskState.PREPARING: frozenset({TaskState.RUNNING, TaskState.FAILED}),
    TaskState.RUNNING: frozenset({TaskState.VALIDATING, TaskState.FAILED}),
    TaskState.VALIDATING: frozenset({TaskState.PUSHING, TaskState.FAILED}),
    TaskState.PUSHING: frozenset({TaskState.OPENING_MR, TaskState.FAILED}),
    TaskState.OPENING_MR: frozenset({TaskState.SUCCEEDED, TaskState.FAILED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
}

STAGE_LABELS: dict[TaskState, str] = {
    TaskState.QUEUED: "入队",
    TaskState.PREPARING: "准备环境",
    TaskState.RUNNING: "Agent 修复",
    TaskState.VALIDATING: "校验 diff",
    TaskState.PUSHING: "push 分支",
    TaskState.OPENING_MR: "开 draft MR",
    TaskState.FINALIZING: "应用结果",
    TaskState.SUCCEEDED: "已完成",
    TaskState.FAILED: "失败",
}

#: 不同 kind 的阶段名（`STAGE_LABELS` 是 fix 的默认值，保持向后兼容）。
STAGE_LABELS_BY_KIND: dict[TaskKind, dict[TaskState, str]] = {
    TaskKind.FIX: STAGE_LABELS,
    TaskKind.TRIAGE: {
        TaskState.QUEUED: "入队",
        TaskState.PREPARING: "准备材料",
        TaskState.RUNNING: "排查·定位",
        TaskState.FINALIZING: "打标·评论",
        TaskState.SUCCEEDED: "已完成",
        TaskState.FAILED: "失败",
    },
    TaskKind.EXPLAIN: {
        TaskState.QUEUED: "入队",
        TaskState.PREPARING: "准备上下文",
        TaskState.RUNNING: "生成解释",
        TaskState.FINALIZING: "发布评论",
        TaskState.SUCCEEDED: "已完成",
        TaskState.FAILED: "失败",
    },
}


def stage_label(state: TaskState, kind: TaskKind = TaskKind.FIX) -> str:
    labels = STAGE_LABELS_BY_KIND.get(kind, STAGE_LABELS)
    return labels.get(state, STAGE_LABELS.get(state, state.value))


#: 每种任务只允许自己的流转图。fix 沿用 M1 的完整流水线；triage/explain 是
#: `queued → preparing → running → finalizing → succeeded|failed` 的粗粒度流程。
KIND_TRANSITIONS: dict[TaskKind, dict[TaskState, frozenset[TaskState]]] = {
    TaskKind.FIX: ALLOWED_TRANSITIONS,
    TaskKind.TRIAGE: {
        TaskState.QUEUED: frozenset({TaskState.PREPARING, TaskState.FAILED}),
        TaskState.PREPARING: frozenset({TaskState.RUNNING, TaskState.FAILED}),
        TaskState.RUNNING: frozenset({TaskState.FINALIZING, TaskState.FAILED}),
        TaskState.FINALIZING: frozenset({TaskState.SUCCEEDED, TaskState.FAILED}),
        TaskState.SUCCEEDED: frozenset(),
        TaskState.FAILED: frozenset(),
    },
    TaskKind.EXPLAIN: {
        TaskState.QUEUED: frozenset({TaskState.PREPARING, TaskState.FAILED}),
        TaskState.PREPARING: frozenset({TaskState.RUNNING, TaskState.FAILED}),
        TaskState.RUNNING: frozenset({TaskState.FINALIZING, TaskState.FAILED}),
        TaskState.FINALIZING: frozenset({TaskState.SUCCEEDED, TaskState.FAILED}),
        TaskState.SUCCEEDED: frozenset(),
        TaskState.FAILED: frozenset(),
    },
}


def can_transition_for(kind: TaskKind, src: TaskState, dst: TaskState) -> bool:
    """按任务类型判断流转是否合法。"""
    return dst in KIND_TRANSITIONS.get(kind, ALLOWED_TRANSITIONS).get(src, frozenset())


def ensure_transition_for(kind: TaskKind, src: TaskState, dst: TaskState) -> None:
    from core.errors import InvalidTransition

    if not can_transition_for(kind, src, dst):
        raise InvalidTransition(src, dst)


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


def can_transition(src: TaskState, dst: TaskState) -> bool:
    """正常流转是否合法（不含恢复专用边）。"""
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def ensure_transition(src: TaskState, dst: TaskState) -> None:
    from core.errors import InvalidTransition

    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)


def can_requeue(state: TaskState) -> bool:
    """恢复专用：只有非终态可以被打回队列重跑。"""
    return state in ACTIVE_STATES
