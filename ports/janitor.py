"""`Janitor` 端口：保留策略（PLAN §9 M5）。

Worker 只依赖这个协议，不知道清理发生在文件系统还是别处 —— 测试里可以注入
`FakeJanitor` 验证"清理会被周期性触发"，而不必真的造 90 天前的数据。
"""

from __future__ import annotations

from typing import Protocol

from core.models import JanitorReport


class Janitor(Protocol):
    async def run(self) -> JanitorReport:
        """执行一次清理，返回删了多少东西。"""
        ...

    async def run_forever(self) -> None:
        """周期性清理（由 worker 以 asyncio 任务启动）。"""
        ...
