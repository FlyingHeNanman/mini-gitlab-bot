"""Notifier 端口：把状态评论投递到 issue 上。

实现必须按首行 marker **原地编辑**同一条评论（`SCM.upsert_comment`），
所以"单条状态评论"的语义由 core 的渲染器 + SCM 的 upsert 共同保证。

投递失败**不改变任务结论**（编排器按 best-effort 处理并记事件），
但必须留痕：失败不静默（PLAN 成功标准 3）。
"""

from __future__ import annotations

from typing import Protocol

from core.models import Task


class Notifier(Protocol):
    async def publish(self, task: Task, body: str) -> int | None:
        """投递/更新状态评论，返回 comment id（拿不到就返回 None）。"""
        ...
