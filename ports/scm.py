"""SCM 端口：网关独占的 **GitLab 读写**通道（PLAN §3.2、§5）。

agent 永远拿不到这个端口 —— 它零凭据（PLAN §7 红线 2）。

约束提醒：`create_draft_mr` 只能开 **draft**（GitLab 靠标题前缀表达，API 无 `draft` 参数），
`upsert_comment` 必须按 marker **原地编辑**同一条评论，绝不能每次新开一条。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from core.models import IssueContext, MergeRequestContext, MergeRequestRef, ProjectRef, Task


class SCM(Protocol):
    # ---- 读 ----

    async def default_branch(self, project: ProjectRef) -> str:
        """目标分支：v1 直接取项目的 `default_branch`（PLAN §12 #5，零配置）。"""
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
        """关闭 issue（高置信重复 / 非缺陷结论用）。"""
        ...
