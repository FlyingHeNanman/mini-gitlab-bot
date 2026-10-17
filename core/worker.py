"""Worker：领取任务、心跳续租、看门狗回收过期租约、崩溃恢复。

对应的 v0 缺陷（PLAN §8）：v0 把任务只放在内存线程池里，重启即静默丢单。
这里任务状态**只以 SQLite 为准**，进程内的 asyncio 任务只是执行载体。

看门狗策略（`reap_expired`）：
* 租约过期且 `attempts < max_attempts` → 打回 `queued` 重跑；
* 租约过期且已用尽重试 → 置 `failed`，并回调 `on_task_failed` 去贴评论 / 切标签。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from core.logging_setup import bind_task
from core.models import ReapOutcome, Task

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Worker:
    def __init__(
        self,
        *,
        store,
        orchestrator,
        owner: str,
        concurrency: int = 2,
        lease_seconds: int = 60,
        heartbeat_interval_sec: int | None = None,
        poll_interval_sec: float = 1.0,
        clock: Callable[[], datetime] = _utcnow,
        on_task_failed: Callable[[Task], Awaitable[None]] | None = None,
        janitor=None,
        metrics=None,
    ) -> None:
        self._store = store
        self._orchestrator = orchestrator
        self._owner = owner
        self._concurrency = max(1, concurrency)
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_sec = heartbeat_interval_sec or max(5, lease_seconds // 3)
        self._poll_interval_sec = poll_interval_sec
        self._clock = clock
        self._on_task_failed = on_task_failed
        self._janitor = janitor
        self._metrics = metrics
        self._inflight: dict[str, asyncio.Task[None]] = {}

    @property
    def owner(self) -> str:
        return self._owner

    # ------------------------------------------------------------------ #
    # 单步操作（测试与运维用；run_forever 内部也复用它们）
    # ------------------------------------------------------------------ #

    async def recover(self) -> int:
        """启动时把所有非终态任务打回队列：这是"重启后任务恢复"的落点。"""
        recovered = self._store.requeue_inflight(now=self._clock())
        if recovered:
            logger.warning(
                "崩溃恢复：%d 个非终态任务已重新入队（%s）",
                len(recovered),
                ", ".join(task.id for task in recovered),
            )
        return len(recovered)

    async def heartbeat(self) -> int:
        """给本 worker 名下所有非终态任务续租。"""
        return self._store.renew_leases(
            owner=self._owner, now=self._clock(), lease_seconds=self._lease_seconds
        )

    async def reap(self) -> list[ReapOutcome]:
        """看门狗：处理租约过期的任务（重跑或判失败）。"""
        outcomes = self._store.reap_expired(now=self._clock())
        for outcome in outcomes:
            logger.warning(
                "看门狗处理任务 %s：%s",
                outcome.task.id,
                "已重新入队" if outcome.requeued else "已判失败",
            )
            if outcome.failed and self._on_task_failed is not None:
                try:
                    await self._on_task_failed(outcome.task)
                except Exception:  # noqa: BLE001 通知失败不能拖垮看门狗
                    logger.exception("看门狗通知任务 %s 失败", outcome.task.id)
        return outcomes

    async def step(self) -> bool:
        """领取并**同步跑完**一个任务；返回是否真的领到了任务。"""
        task = self._store.claim_next(
            owner=self._owner, now=self._clock(), lease_seconds=self._lease_seconds
        )
        if task is None:
            return False
        self._count_claim()
        with bind_task(task.id):
            await self._orchestrator.process(task)
        return True

    async def drain(self, *, max_steps: int = 100) -> int:
        """把当前队列跑空（测试里代替 run_forever）。返回处理的任务数。"""
        handled = 0
        while handled < max_steps and await self.step():
            handled += 1
        return handled

    # ------------------------------------------------------------------ #
    # 常驻循环
    # ------------------------------------------------------------------ #

    async def run_forever(self) -> None:
        await self.recover()
        maint = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._reap_loop(), name="watchdog"),
        ]
        if self._janitor is not None:
            maint.append(asyncio.create_task(self._janitor.run_forever(), name="janitor"))
        try:
            while True:
                self._fill()
                if self._inflight:
                    await asyncio.wait(
                        set(self._inflight.values()),
                        timeout=self._poll_interval_sec,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(self._poll_interval_sec)
                self._collect()
        except asyncio.CancelledError:
            raise
        finally:
            for job in maint:
                job.cancel()
            await asyncio.gather(*maint, return_exceptions=True)
            if self._inflight:
                await asyncio.gather(*self._inflight.values(), return_exceptions=True)

    def _fill(self) -> None:
        while len(self._inflight) < self._concurrency:
            task = self._store.claim_next(
                owner=self._owner, now=self._clock(), lease_seconds=self._lease_seconds
            )
            if task is None:
                return
            self._count_claim()
            self._inflight[task.id] = asyncio.create_task(
                self._process(task), name=f"task-{task.short_id}"
            )

    def _count_claim(self) -> None:
        if self._metrics is not None:
            self._metrics.inc("worker_claims_total")

    async def _process(self, task: Task) -> None:
        try:
            # 任务上下文：此后同一协程里的日志都会自动带 task_id
            with bind_task(task.id):
                await self._orchestrator.process(task)
        except Exception:  # noqa: BLE001 编排器内部已兜底，这里只防漏网
            logger.exception("任务 %s 处理时抛出未捕获异常", task.id)

    def _collect(self) -> None:
        for task_id, job in list(self._inflight.items()):
            if job.done():
                self._inflight.pop(task_id, None)
                if job.cancelled():
                    continue
                exc = job.exception()
                if exc is not None:
                    logger.error("任务 %s 的执行体异常：%r", task_id, exc)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_sec)
            try:
                await self.heartbeat()
            except Exception:  # noqa: BLE001
                logger.exception("心跳续租失败")

    @property
    def inflight(self) -> int:
        """当前并发中的任务数（给 /metrics 的 gauge 用）。"""
        return len(self._inflight)

    async def _reap_loop(self) -> None:
        interval = max(5.0, self._poll_interval_sec * 5)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap()
            except Exception:  # noqa: BLE001
                logger.exception("看门狗扫描失败")
