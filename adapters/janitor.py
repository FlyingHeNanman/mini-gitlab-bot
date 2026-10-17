"""保留策略（PLAN §9 M5）：磁盘与队列都不会无限增长。

三类对象，三种清理口径：

| 对象 | 位置 | 保留期 | 为什么 |
| --- | --- | --- | --- |
| codex / uv 日志 | `<LOG_DIR>/<task_id>/` | 14 天 | 复盘失败要能翻到当时的 JSONL 与 uv 输出 |
| 终态任务 | SQLite `tasks` + `task_events` | 90 天 | 够做完季度审计，又不会让库无限长 |
| 孤儿 job 目录 | `<WORK_DIR>/task-*` | 1 天 | 进程被 kill 时来不及清；留着会占满卷 |

孤儿目录的判定很保守：**名字必须是 `task-` 开头**、**不在任何非终态任务的 workdir 里**、
且 **mtime 超过保留期**。宁可不删，也不删错（删错等于删掉正在跑的任务）。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.metrics import Metrics
from core.models import JanitorReport
from core.states import ACTIVE_STATES

logger = logging.getLogger(__name__)

TASK_DIR_PREFIX = "task-"


@dataclass(slots=True)
class RetentionSettings:
    log_days: int = 14
    task_days: int = 90
    orphan_job_days: int = 1
    #: 清理间隔（秒）；默认每小时
    interval_sec: float = 3600.0


class Janitor:
    """`ports.janitor.Janitor` 的真实实现：文件系统 + SQLite。"""

    def __init__(
        self,
        *,
        store,
        work_dir: Path | str,
        log_dir: Path | str,
        settings: RetentionSettings | None = None,
        clock=None,
        metrics: Metrics | None = None,
    ) -> None:
        self._store = store
        self._work_dir = Path(work_dir)
        self._log_dir = Path(log_dir)
        self._settings = settings or RetentionSettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._metrics = metrics

    async def run(self) -> JanitorReport:
        now = self._clock()
        removed_logs = await asyncio.to_thread(
            self._clean_task_dirs, self._log_dir, now - timedelta(days=self._settings.log_days)
        )
        purged = await asyncio.to_thread(self._purge_tasks, now)
        removed_jobs = await asyncio.to_thread(self._clean_orphan_jobs, now)

        report = JanitorReport(
            removed_log_dirs=removed_logs, removed_job_dirs=removed_jobs, purged_tasks=purged
        )
        if report.total:
            logger.info(
                "保留策略清理：日志目录 %d、孤儿 job 目录 %d、终态任务 %d",
                report.removed_log_dirs,
                report.removed_job_dirs,
                report.purged_tasks,
            )
        if self._metrics is not None:
            self._metrics.inc("janitor_runs_total")
            self._metrics.inc("janitor_removed_total", report.removed_log_dirs, kind="log_dir")
            self._metrics.inc("janitor_removed_total", report.removed_job_dirs, kind="job_dir")
            self._metrics.inc("janitor_removed_total", report.purged_tasks, kind="task")
        return report

    async def run_forever(self) -> None:
        interval = max(60.0, self._settings.interval_sec)
        while True:
            try:
                await self.run()
            except Exception:  # noqa: BLE001 清理失败不能拖垮 worker
                logger.exception("保留策略清理失败")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #

    def _clean_task_dirs(self, root: Path, cutoff: datetime) -> int:
        if not root.is_dir():
            return 0
        removed = 0
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.startswith(TASK_DIR_PREFIX):
                continue
            if _mtime(entry) >= cutoff:
                continue
            if _rmtree(entry):
                removed += 1
        return removed

    def _clean_orphan_jobs(self, now: datetime) -> int:
        if not self._work_dir.is_dir():
            return 0
        cutoff = now - timedelta(days=self._settings.orphan_job_days)
        active = self._active_workdirs()
        removed = 0
        for entry in self._work_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith(TASK_DIR_PREFIX):
                continue
            if str(entry) in active or _mtime(entry) >= cutoff:
                continue
            if _rmtree(entry):
                removed += 1
        return removed

    def _active_workdirs(self) -> set[str]:
        active: set[str] = set()
        for state in ACTIVE_STATES:
            for task in self._store.list_tasks(state=state, limit=1000):
                if task.workdir:
                    active.add(task.workdir)
        return active

    def _purge_tasks(self, now: datetime) -> int:
        cutoff = now - timedelta(days=self._settings.task_days)
        return self._store.purge_terminal(before=cutoff)


def _mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:  # pragma: no cover - 竞态：刚被删掉
        return datetime.now(UTC)


def _rmtree(path: Path) -> bool:
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except OSError as exc:  # pragma: no cover - 权限/竞态
        logger.warning("删除 %s 失败：%s", path, exc)
        return False
