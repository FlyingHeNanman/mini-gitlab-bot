"""VCS 端口：本地 git 操作（clone / 分支 / 提交 / diff / push）。

本端口**不负责**环境准备（`uv sync` 等，属 M2 的准备阶段）也不负责 GitLab API。

硬要求：
* `push` 的凭据**不得出现在 argv / `ps` 可见处**，也不得打印到日志（PLAN §7 红线 5）。
  v0 的 `_authed_url`（token 内嵌 URL 且进 argv）是反面教材，M3 必须换掉。
* `commit_all`：agent 应自己 `git commit`，这里是**兜底**；返回分支相对基线是否有新提交
  —— 有则返回 HEAD sha，没有则返回 `None`（编排器据此判"零改动"）。
"""

from __future__ import annotations

from typing import Protocol

from core.models import DiffSummary, Task, Workdir


class VCS(Protocol):
    async def prepare_workdir(self, task: Task, *, base_branch: str, branch: str) -> Workdir:
        """为本次 job 分配独立目录、clone 仓库、切出工作分支。"""
        ...

    async def commit_all(self, task: Task, workdir: Workdir, *, message: str) -> str | None:
        """提交工作区里的全部改动（agent 漏提交时兜底）。

        返回 `HEAD` sha；若分支相对 `base_branch` 没有任何新提交，返回 `None`。
        """
        ...

    async def diff(self, task: Task, workdir: Workdir) -> DiffSummary:
        """工作分支相对基线分支的改动摘要（仅文件名与增删行数）。"""
        ...

    async def push(self, task: Task, workdir: Workdir) -> None: ...
