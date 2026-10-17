"""测试用普通助手（非 fixture）：可控时钟、触发构造器、运行时把手。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from adapters.sqlite_store import SqliteStore
from core.models import ProjectRef, Trigger, TriggerActor, TriggerKind
from core.orchestrator import TaskOrchestrator
from core.worker import Worker
from gateway.config import Settings
from gateway.runtime import Runtime

PROJECT_PATH = "group/project-a"
PROJECT_ID = 42
ALICE_ID = 7
START = datetime(2026, 10, 17, 10, 31, 0, tzinfo=UTC)


class Clock:
    """手动推进的时钟：让租约 / 超时 / 时间文案都可断言。"""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


@dataclass
class Harness:
    settings: Settings
    clock: Clock
    store: SqliteStore
    scm: Any
    vcs: Any
    agent: Any
    workspace: Any
    runtime: Runtime
    orchestrator: TaskOrchestrator
    worker: Worker


def make_trigger(
    *,
    delivery_key: str = "delivery-1",
    issue_iid: int = 12,
    kind: TriggerKind = TriggerKind.LABEL,
    command: str | None = None,
    username: str = "alice",
    user_id: int | None = ALICE_ID,
    path: str = PROJECT_PATH,
    project_id: int = PROJECT_ID,
) -> Trigger:
    return Trigger(
        kind=kind,
        project=ProjectRef(project_id=project_id, path=path),
        issue_iid=issue_iid,
        actor=TriggerActor(user_id=user_id, username=username),
        delivery_key=delivery_key,
        command=command,
    )
