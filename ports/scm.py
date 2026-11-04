"""SCM 端口：网关独占的 **GitLab 读写**通道（PLAN §3.2、§5）。

agent 永远拿不到这个端口 —— 它零凭据（PLAN §7 红线 2）。

约束提醒：`create_draft_mr` 只能开 **draft**（GitLab 靠标题前缀表达，API 无 `draft` 参数），
`upsert_comment` 必须按 marker **原地编辑**同一条评论，绝不能每次新开一条。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from core.models import (
    CommitInfo,
    IssueContext,
    IssueSummary,
    MergeRequestContext,
    MergeRequestRef,
    ProjectRef,
    ReleaseInfo,
    TagInfo,
    Task,
)


class SCM(Protocol):
    # ---- 读 ----

    async def resolve_project(self, path: str) -> ProjectRef | None:
        """按 `path_with_namespace` 解析项目（周期扫描从白名单路径出发）。"""
        ...

    async def default_branch(self, project: ProjectRef) -> str:
        """目标分支：v1 直接取项目的 `default_branch`（PLAN §12 #5，零配置）。"""
        ...

    async def list_open_issues(
        self, project: ProjectRef, *, limit: int = 100
    ) -> tuple[IssueSummary, ...]:
        """项目里所有 open issue 的摘要（周期扫描 / 补偿巡检用）。"""
        ...

    async def list_closed_issues(
        self, project: ProjectRef, *, since: datetime, limit: int = 100
    ) -> tuple[IssueSummary, ...]:
        """`updated_at >= since` 的 closed issue（GitLab autoclose 后补回帖用）。"""
        ...

    async def list_recent_commits(
        self, project: ProjectRef, *, ref: str, since: datetime, limit: int = 50
    ) -> tuple[CommitInfo, ...]:
        """某分支/引用上 `since` 之后的提交（新的在前）。"""
        ...

    async def list_tags(self, project: ProjectRef, *, limit: int = 100) -> tuple[TagInfo, ...]:
        """项目 tag 列表（按 tag 指向的提交时间倒序）。"""
        ...

    async def compare_commits(
        self, project: ProjectRef, *, from_ref: str, to_ref: str, limit: int = 200
    ) -> tuple[CommitInfo, ...]:
        """两个 ref 之间的提交（release notes 输入）。"""
        ...

    async def get_release(self, project: ProjectRef, *, tag_name: str) -> ReleaseInfo | None:
        """按 tag 查已有 Release；不存在返回 None（发布幂等用）。"""
        ...

    async def get_member_access_level(self, project_id: int, user_id: int) -> int | None:
        """成员权限等级；**非成员返回 None**（调用方按拒绝处理）。

        真实实现用 `GET /projects/:id/members/all/:user_id`（`members/all` 才含
        继承自 group 的成员），404 视为 None。
        """
        ...

    async def load_issue_context(self, task: Task) -> IssueContext: ...

    async def load_merge_request_context(self, task: Task) -> MergeRequestContext:
        """explain 输入：MR 元信息 + diff + closes_issues（不 clone）。"""
        ...

    async def list_labels(self, project_id: int) -> tuple[str, ...]:
        """项目里已有的标签名（`ensure_labels` 幂等创建用）。"""
        ...

    async def create_label(
        self, project_id: int, *, name: str, color: str, description: str
    ) -> None:
        """创建一个标签；已存在时实现应当**静默成功**（幂等）。"""
        ...

    async def find_user_id(self, username: str) -> int | None:
        """按 username 查 GitLab 用户；不存在返回 None（指派用）。"""
        ...

    # ---- 写 ----

    async def set_labels(
        self, task: Task, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None: ...

    async def upsert_comment(self, task: Task, body: str, *, marker: str) -> int:
        """按 marker 原地编辑状态评论；不存在则新建。返回 comment id。"""
        ...

    async def find_comment(self, task: Task, *, marker: str) -> int | None:
        """按 marker 查已有评论；closed issue 的幂等判断用，不存在返回 None。"""
        ...

    async def create_draft_mr(
        self,
        task: Task,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestRef: ...

    async def assign_issue(self, task: Task, user_id: int) -> None:
        """把 issue 指派给某个 GitLab 用户（CODEOWNERS 解析用）。"""
        ...

    async def link_issue(self, task: Task, *, target_issue_iid: int) -> None:
        """在 issue 与目标 issue 之间建立 `relates_to` 关联（重复判定用）。"""
        ...

    async def close_issue(self, task: Task) -> None:
        """关闭 issue（高置信重复 / 非缺陷结论 / commit 引用已解决）。"""
        ...

    async def create_release(
        self, project: ProjectRef, *, tag_name: str, name: str, description: str
    ) -> ReleaseInfo:
        """新建 GitLab Release；tag 已存在 Release 时实现应抛错由调用方兜底。"""
        ...
