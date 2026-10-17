"""结构化日志：一行一个 JSON，带任务上下文。

为什么不用第三方库：需要的只是"把 record 变成 JSON + 注入 task_id"，`logging` 自带
`Formatter` 就够了；多一个依赖反而要在内网镜像里多解释一遍来源。

`task_id` 用 `contextvars` 传递：worker 在处理某个任务期间 `bind_task(task.id)`，
此后同一协程里发出的所有日志（含编排器、适配器）都会自动带上它 —— 不需要给每个
函数加参数，也不会在并发任务之间串味（contextvars 是协程级隔离的）。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

_TASK_ID: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="")

#: 除标准字段外，额外带进 JSON 的 `extra=` 键（有就带上，没有就忽略）
_EXTRA_KEYS = (
    "event",
    "task_id",
    "project_id",
    "issue_iid",
    "state",
    "stage",
    "duration_sec",
    "attempts",
    "reason",
)


def current_task_id() -> str:
    return _TASK_ID.get()


@contextmanager
def bind_task(task_id: str) -> Iterator[None]:
    """在这段代码里发出的日志都带上 `task_id`（协程级隔离，不串任务）。"""
    token = _TASK_ID.set(task_id or "")
    try:
        yield
    finally:
        _TASK_ID.reset(token)


class TaskContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "task_id", ""):
            record.task_id = _TASK_ID.get()
        return True


class JsonFormatter(logging.Formatter):
    """`{ts, level, logger, msg, ...}`，时间用 UTC ISO8601（容器里比本地时间好对账）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRA_KEYS:
            value = getattr(record, key, None)
            if value not in (None, ""):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "json", *, stream: Any = None) -> None:
    """配置根 logger。重复调用是幂等的（不会叠出重复行）。"""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(TaskContextFilter())
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(task_id)s] %(message)s")
        )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


#: uvicorn 会给自己的 logger 装 handler 且 `propagate=False`。这里**摘掉它的 handler**
#: 并接回根 logger —— 否则同一行既输出纯文本（uvicorn）又输出 JSON（我们），日志翻倍。
#: 注意调用时机：必须在 uvicorn 应用完它自己的 logging 配置**之后**（lifespan 里再调一次）。
def attach_uvicorn_loggers() -> None:
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger_ = logging.getLogger(name)
        logger_.handlers.clear()
        logger_.propagate = True
