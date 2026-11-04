"""真实适配器落地前的占位实现。

目的：网关**能启动**（webhook 立刻 2xx、`/healthz` 正常），但任何任务都会在明确的位置
失败并写明"适配器未实现"，而不是静默卡住 —— 失败不静默（PLAN 成功标准 3）。

M2 用 `GitCli` + `CodexCli` + `LocalWorkspace` 替换，M3 用 `GitLabClient` 替换。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from core.models import (
    AgentResult,
    CommitInfo,
    DiffSummary,
    EnvReport,
    IssueContext,
    IssueSummary,
    MergeRequestContext,
    MergeRequestRef,
    ProjectRef,
    ReleaseInfo,
    TagInfo,
    Task,
    Workdir,
)


class AdapterUnavailable(RuntimeError):
    def __init__(self, name: str, milestone: str) -> None:
        super().__init__(f"{name} 适配器尚未实现（计划在 {milestone} 落地）")
        self.name = name
        self.milestone = milestone


class UnavailableSCM:
    """M3 前 GitLab 读写通道的占位。"""

    def __init__(self, milestone: str = "M3") -> None:
        self._milestone = milestone

    def _boom(self) -> Any:
        raise AdapterUnavailable("GitLabClient(SCM)", self._milestone)

    async def default_branch(self, project: ProjectRef) -> str:
        self._boom()

    async def resolve_project(self, path: str) -> ProjectRef | None:
        self._boom()

    async def list_open_issues(
        self, project: ProjectRef, *, limit: int = 100
    ) -> tuple[IssueSummary, ...]:
        self._boom()

    async def list_closed_issues(
        self, project: ProjectRef, *, since: datetime, limit: int = 100
    ) -> tuple[IssueSummary, ...]:
        self._boom()

    async def list_recent_commits(
        self, project: ProjectRef, *, ref: str, since: datetime, limit: int = 50
    ) -> tuple[CommitInfo, ...]:
        self._boom()

    async def list_tags(self, project: ProjectRef, *, limit: int = 100) -> tuple[TagInfo, ...]:
        self._boom()

    async def compare_commits(
        self, project: ProjectRef, *, from_ref: str, to_ref: str, limit: int = 200
    ) -> tuple[CommitInfo, ...]:
        self._boom()

    async def get_release(self, project: ProjectRef, *, tag_name: str) -> ReleaseInfo | None:
        self._boom()

    async def get_member_access_level(self, project_id: int, user_id: int) -> int | None:
        self._boom()

    async def load_issue_context(self, task: Task) -> IssueContext:
        self._boom()

    async def load_merge_request_context(self, task: Task) -> MergeRequestContext:
        self._boom()

    async def list_labels(self, project_id: int) -> tuple[str, ...]:
        self._boom()

    async def create_label(
        self, project_id: int, *, name: str, color: str, description: str
    ) -> None:
        self._boom()

    async def set_labels(
        self, task: Task, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self._boom()

    async def upsert_comment(self, task: Task, body: str, *, marker: str) -> int:
        self._boom()

    async def find_comment(self, task: Task, *, marker: str) -> int | None:
        self._boom()

    async def create_draft_mr(
        self,
        task: Task,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestRef:
        self._boom()

    async def find_user_id(self, username: str) -> int | None:
        self._boom()

    async def assign_issue(self, task: Task, user_id: int) -> None:
        self._boom()

    async def link_issue(self, task: Task, *, target_issue_iid: int) -> None:
        self._boom()

    async def close_issue(self, task: Task) -> None:
        self._boom()

    async def create_release(
        self, project: ProjectRef, *, tag_name: str, name: str, description: str
    ) -> ReleaseInfo:
        self._boom()


class UnavailableVCS:
    """M2 前 git 操作的占位。"""

    def __init__(self, milestone: str = "M2") -> None:
        self._milestone = milestone

    def _boom(self) -> Any:
        raise AdapterUnavailable("GitCli(VCS)", self._milestone)

    async def prepare_workdir(self, task: Task, *, base_branch: str, branch: str | None) -> Workdir:
        self._boom()

    async def commit_all(self, task: Task, workdir: Workdir, *, message: str) -> str | None:
        self._boom()

    async def diff(self, task: Task, workdir: Workdir) -> DiffSummary:
        self._boom()

    async def worktree_dirty(self, task: Task, workdir: Workdir) -> bool:
        self._boom()

    async def push(self, task: Task, workdir: Workdir) -> None:
        self._boom()


class UnavailableAgent:
    """M2 前 agent 内核的占位。"""

    def __init__(self, milestone: str = "M2") -> None:
        self._milestone = milestone

    async def run(
        self,
        task: Task,
        *,
        workdir: Workdir,
        context_path: str,
        timeout_sec: float,
        model: str | None = None,
    ) -> AgentResult:
        raise AdapterUnavailable("CodexCli(AgentRunner)", self._milestone)


class UnavailableEnv:
    """真实准备阶段的占位（`UvEnvPreparer` 取代）。"""

    def __init__(self, milestone: str = "M2") -> None:
        self._milestone = milestone

    async def prepare(self, task: Task, workdir: Workdir) -> EnvReport:
        raise AdapterUnavailable("UvEnvPreparer(EnvPreparer)", self._milestone)


class UnavailableWorkspace:
    """M2 前工作目录管理的占位。"""

    def __init__(self, milestone: str = "M2") -> None:
        self._milestone = milestone

    def _boom(self) -> Any:
        raise AdapterUnavailable("LocalWorkspace(Workspace)", self._milestone)

    async def write_text(self, workdir: Workdir, relative_path: str, text: str) -> str:
        self._boom()

    async def read_file(self, workdir: Workdir, relative_path: str) -> str | None:
        self._boom()

    async def temp_dir(self, workdir: Workdir) -> str:
        self._boom()

    async def create_job_dir(self, task) -> Workdir:
        self._boom()

    async def remove(self, workdir: Workdir) -> None:
        # 清理失败不该掩盖真正的失败原因，这里静默
        return None
