"""Store 端口：任务持久化 + 队列 + 租约 + 崩溃恢复。

实现必须保证（这些是 M1 完成判据）：

* `enqueue` 幂等：同一 `delivery_key` 重复投递只产生一个任务；
  同一 issue 已有**非终态**任务时不重复建单（`active_task_exists`）；
* 状态流转一律经过 `core.states` 的合法边校验；
* `requeue_inflight` / `reap_expired` 覆盖"重启后任务恢复"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from core.models import EnqueueOutcome, ReapOutcome, Task, TaskEvent, Trigger
from core.states import TaskState


class Store(Protocol):
    # ---- 入队与读取 ----

    def enqueue(
        self, trigger: Trigger, *, now: datetime, max_attempts: int = 3
    ) -> EnqueueOutcome: ...

    def get(self, task_id: str) -> Task | None: ...

    def list_tasks(self, *, state: TaskState | None = None, limit: int = 50) -> list[Task]: ...

    def count(self, *, state: TaskState | None = None) -> int: ...

    # ---- 队列与租约 ----

    def claim_next(self, *, owner: str, now: datetime, lease_seconds: int) -> Task | None:
        """原子领取一个 `queued` 任务并置为 `preparing`，`attempts += 1`。"""
        ...

    def renew_leases(self, *, owner: str, now: datetime, lease_seconds: int) -> int:
        """续租该 owner 名下所有非终态任务，返回受影响行数。"""
        ...

    # ---- 状态流转 ----

    def transition(self, task_id: str, to: TaskState, *, now: datetime, **fields: Any) -> Task:
        """按合法边流转并写入字段白名单内的字段；非法流转抛 `InvalidTransition`。"""
        ...

    def fail(
        self,
        task_id: str,
        *,
        stage: str,
        reason: str,
        evidence: str = "",
        log_path: str = "",
        now: datetime,
    ) -> Task: ...

    def requeue(self, task_id: str, *, reason: str, now: datetime) -> Task:
        """恢复专用边：非终态 → `queued`。"""
        ...

    def requeue_inflight(self, *, now: datetime) -> list[Task]:
        """进程启动时把所有非终态任务打回队列。"""
        ...

    def reap_expired(self, *, now: datetime) -> list[ReapOutcome]:
        """看门狗：租约过期的任务重跑或判失败。"""
        ...

    # ---- 审计 ----

    def append_event(
        self, task_id: str, kind: str, detail: dict[str, Any], *, now: datetime
    ) -> None: ...

    def events(self, task_id: str) -> list[TaskEvent]: ...

    def close(self) -> None: ...
