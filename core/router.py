"""按 `task.kind` 把任务分派给对应编排器（fix / triage / explain）。

Worker 只认识一个 `process` / `finalize_failure` 入口，路由放在这里，避免 Worker
知道每种任务的细节。找不到对应编排器时**明确报错**（任务会按失败处理），绝不静默丢任务。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from core.models import Task
from core.states import TaskKind

logger = logging.getLogger(__name__)


class KindRouter:
    def __init__(self, orchestrators: Mapping[TaskKind, Any]) -> None:
        self._orchestrators = dict(orchestrators)

    def supports(self, kind: TaskKind) -> bool:
        return kind in self._orchestrators

    def kinds(self) -> tuple[TaskKind, ...]:
        return tuple(self._orchestrators)

    async def process(self, task: Task) -> Task:
        orchestrator = self._orchestrators.get(task.kind)
        if orchestrator is None:
            raise RuntimeError(f"没有为任务类型 {task.kind} 装配编排器")
        return await orchestrator.process(task)

    async def finalize_failure(self, task: Task, **kwargs: Any) -> Task:
        orchestrator = self._orchestrators.get(task.kind)
        if orchestrator is None:
            raise RuntimeError(f"没有为任务类型 {task.kind} 装配编排器")
        return await orchestrator.finalize_failure(task, **kwargs)
