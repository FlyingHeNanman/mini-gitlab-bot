"""`LocalWorkspace`：真实文件系统实现。

两条安全/健壮性要求：

1. **清理前必须确认目录在 jobs 根之下** —— `workdir` 是从 SQLite 读回来的字符串，
   一旦被写坏（或未来被 webhook 数据污染），`shutil.rmtree` 就是"删库"操作。
   这里用 `resolve()` + `is_relative_to` 卡死。
2. **每个 job 独立 TMPDIR**（PLAN §7 基线 4）：`workspace-write` 默认还能写 `/tmp`，
   并发任务可以经 `/tmp` 交换文件，所以必须把 `TMPDIR` 指向 job 目录内。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from core import layout
from core.models import Workdir

logger = logging.getLogger(__name__)


class WorkspaceViolation(RuntimeError):
    """试图操作 jobs 根目录之外的路径。"""


class LocalWorkspace:
    def __init__(self, jobs_root: Path | str) -> None:
        self._root = Path(jobs_root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ #

    async def write_text(self, workdir: Workdir, relative_path: str, text: str) -> str:
        target = self._resolve_in_job(workdir.path, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再 replace，agent 永远不会读到半个文件
        tmp = target.with_name(f".{target.name}.tmp")
        await asyncio.to_thread(tmp.write_text, text, encoding="utf-8")
        await asyncio.to_thread(os.replace, tmp, target)
        return str(target)

    async def read_text(self, path: str) -> str:
        """读回 job 目录内的文件（如 agent 写的 `report.json`）。"""
        return await asyncio.to_thread(Path(path).read_text, encoding="utf-8")

    async def temp_dir(self, workdir: Workdir) -> str:
        path = Path(layout.temp_dir(workdir.path))
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(os.chmod, path, 0o700)
        return str(path)

    async def remove(self, workdir: Workdir) -> None:
        target = self._resolve(workdir.path)
        if target == self._root:
            raise WorkspaceViolation(f"拒绝删除 jobs 根目录本身：{target}")
        await asyncio.to_thread(shutil.rmtree, target, True)

    # ------------------------------------------------------------------ #

    def _resolve(self, *parts: str) -> Path:
        """相对 **jobs 根** 做包含检查（`remove` 用）。"""
        candidate = Path(*parts).expanduser()
        resolved = candidate.resolve()
        if resolved != self._root and not resolved.is_relative_to(self._root):
            raise WorkspaceViolation(f"路径 {resolved} 不在 jobs 根 {self._root} 之下，拒绝操作")
        return resolved

    def _resolve_in_job(self, job_dir: str, relative_path: str) -> Path:
        """相对 **job 目录** 做包含检查（写入用）。

        只卡 jobs 根是不够的：`../其他任务/context.md` 依然在根之下，却能跨任务写文件。
        另外这里顺便挡掉符号链接逃逸（`resolve()` 会跟随链接）。
        """
        job = Path(job_dir).expanduser().resolve()
        if not job.is_relative_to(self._root):
            raise WorkspaceViolation(f"job 目录 {job} 不在 jobs 根 {self._root} 之下，拒绝操作")
        resolved = (job / relative_path).expanduser().resolve()
        if not resolved.is_relative_to(job):
            raise WorkspaceViolation(f"路径 {resolved} 不在 job 目录 {job} 之下，拒绝写入")
        return resolved
