"""Workspace 端口：job 工作目录里的文件与隔离。

目录本身由 `VCS.prepare_workdir` 创建（它要 clone 进去），本端口负责：

* 写 `context.md`（agent 唯一的上下文入口）；
* 提供**每 job 独立的 `TMPDIR`** —— `workspace-write` 默认还能写 `/tmp`，
  并发任务可能经 `/tmp` 交换文件，所以必须隔离（PLAN §7 基线 4）；
* 终态清理整个目录（PLAN §12 #7：job 目录终态即清，否则磁盘必爆）。
"""

from __future__ import annotations

from typing import Protocol

from core.models import Workdir


class Workspace(Protocol):
    async def write_text(self, workdir: Workdir, relative_path: str, text: str) -> str:
        """写入工作目录内的文件，返回绝对路径。"""
        ...

    async def temp_dir(self, workdir: Workdir) -> str:
        """返回（并确保存在）本 job 专属的临时目录。"""
        ...

    async def remove(self, workdir: Workdir) -> None:
        """删除整个 job 目录（终态清理）。"""
        ...
