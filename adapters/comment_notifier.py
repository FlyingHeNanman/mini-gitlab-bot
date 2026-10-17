"""把状态评论投递到 SCM（GitLab issue 评论），并按 marker **原地编辑**。

core 的渲染器已经把 marker 放在正文首行，这里只负责取出来传给 `upsert_comment`。
"""

from __future__ import annotations

from core.models import Task
from core.render import extract_status_marker, status_marker


class CommentNotifier:
    """`ports.notifier.Notifier` 的默认实现（依赖任意 SCM 实现，含假 SCM）。"""

    def __init__(self, scm) -> None:
        self._scm = scm

    async def publish(self, task: Task, body: str) -> int | None:
        marker = extract_status_marker(body) or status_marker(task.id)
        return await self._scm.upsert_comment(task, body, marker=marker)
