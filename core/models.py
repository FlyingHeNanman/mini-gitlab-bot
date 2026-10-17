"""领域模型：纯数据结构，不含任何 IO、框架依赖。

放在 `core/` 而不是 `ports/`，是为了保持依赖方向 **core ← ports ← adapters**：
端口只声明协议，数据形状由领域层定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from core import layout
from core.states import TaskState, is_terminal


class TriggerKind(StrEnum):
    """触发来源：标签状态机（主要用法）或评论命令。"""

    LABEL = "label"
    COMMENT = "comment"


@dataclass(frozen=True, slots=True)
class ProjectRef:
    project_id: int
    path: str  # "group/project"（path_with_namespace）


@dataclass(frozen=True, slots=True)
class TriggerActor:
    user_id: int | None
    username: str


@dataclass(frozen=True, slots=True)
class Trigger:
    """一次通过校验的触发（已过 secret / 白名单 / 权限闸门）。"""

    kind: TriggerKind
    project: ProjectRef
    issue_iid: int
    actor: TriggerActor
    delivery_key: str
    command: str | None = None  # run | retry
    issue_labels: tuple[str, ...] = ()
    source: str = ""  # 触发处的 web_url，供追溯


@dataclass(slots=True)
class Task:
    """一个可持久化、可恢复的修复任务。"""

    id: str
    delivery_key: str
    project: ProjectRef
    issue_iid: int
    trigger_kind: TriggerKind
    trigger_actor: str
    created_at: datetime
    updated_at: datetime
    trigger_actor_id: int | None = None
    command: str | None = None
    state: TaskState = TaskState.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    mr_iid: int | None = None
    mr_url: str | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None
    failure_evidence: str | None = None
    workdir: str | None = None
    log_path: str | None = None
    report: dict[str, Any] = field(default_factory=dict)
    finished_at: datetime | None = None

    @property
    def short_id(self) -> str:
        return self.id.removeprefix("task-")

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    def report_list(self, key: str) -> tuple[str, ...]:
        """读取 agent 报告里的列表字段（fixes / tests / risks / deps / ...）。"""
        value = self.report.get(key) or ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)

    def report_text(self, key: str, default: str = "") -> str:
        value = self.report.get(key)
        return default if value is None else str(value)

    def elapsed_sec(self, now: datetime) -> float:
        end = self.finished_at or now
        return max(0.0, (end - self.created_at).total_seconds())


#: 允许通过 `Store.transition(**fields)` 写入的字段白名单。
UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "lease_owner",
        "lease_expires_at",
        "heartbeat_at",
        "branch",
        "base_branch",
        "commit_sha",
        "mr_iid",
        "mr_url",
        "failure_stage",
        "failure_reason",
        "failure_evidence",
        "workdir",
        "log_path",
        "report",
        "finished_at",
    }
)


# --------------------------------------------------------------------------- #
# 端口之间的数据契约
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IssueContext:
    """预取到的 issue 上下文（写进 workdir/context.md）。"""

    iid: int
    title: str
    description: str
    author: str
    web_url: str
    labels: tuple[str, ...] = ()
    state: str = "opened"
    comments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MergeRequestRef:
    iid: int
    url: str
    title: str
    draft: bool = True


@dataclass(frozen=True, slots=True)
class Workdir:
    """一个 job 的独立工作目录。"""

    path: str
    branch: str
    base_branch: str

    @property
    def context_path(self) -> str:
        return f"{layout.internal_dir(self.path)}/{layout.CONTEXT_FILENAME}"

    @property
    def report_path(self) -> str:
        return f"{layout.internal_dir(self.path)}/{layout.REPORT_FILENAME}"

    @property
    def temp_dir(self) -> str:
        return f"{layout.internal_dir(self.path)}/{layout.TMP_DIRNAME}"


@dataclass(frozen=True, slots=True)
class DiffSummary:
    files: tuple[str, ...] = ()
    insertions: int = 0
    deletions: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def changed_lines(self) -> int:
        return self.insertions + self.deletions


@dataclass(frozen=True, slots=True)
class AgentResult:
    """agent 一次运行的原始结果。

    `ok` 只覆盖四条件判定里的**前三条**；"diff 非空"由编排器在 validating 阶段自查，
    因为那是仓库状态而不是 agent 自述（PLAN §7 基线 5）。
    """

    exit_code: int
    completed_event: bool
    last_message_written: bool
    last_message: str = ""
    duration_sec: float = 0.0
    log_path: str | None = None
    report: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False
    #: 观测到的证据（`turn.failed` / `error` 事件、超时、解析告警……），进失败评论便于定位
    notes: tuple[str, ...] = ()
    #: 日志尾部（已截断），失败时直接贴进 issue 评论
    log_tail: str = ""
    log_lines: int = 0

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and self.exit_code == 0
            and self.completed_event
            and self.last_message_written
        )

    def failed_conditions(self) -> tuple[str, ...]:
        bad: list[str] = []
        if self.timed_out:
            bad.append("超时")
        if self.exit_code != 0:
            bad.append(f"exit_code={self.exit_code}")
        if not self.completed_event:
            bad.append("缺少 turn.completed")
        if not self.last_message_written:
            bad.append("未写入 --output-last-message 文件")
        return tuple(bad)


@dataclass(frozen=True, slots=True)
class EnvReport:
    """准备阶段（网关联网）的结果 —— 两阶段模型的前半段（PLAN §4）。

    失败即任务失败并写明原因：agent 阶段是**完全离线**的，环境没准备好就没有补救机会。
    """

    ok: bool
    python_version: str = ""
    steps: tuple[str, ...] = ()
    duration_sec: float = 0.0
    failure_reason: str = ""
    log_tail: str = ""
    log_lines: int = 0
    log_path: str | None = None


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    """入队结果。`reason` 取值：created / duplicate_delivery / active_task_exists。"""

    created: bool
    reason: str
    task: Task | None = None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    at: datetime
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReapOutcome:
    """看门狗处理一个过期租约任务的结果。"""

    task: Task
    requeued: bool

    @property
    def failed(self) -> bool:
        return not self.requeued


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    reason: str
    access_level: int | None = None
