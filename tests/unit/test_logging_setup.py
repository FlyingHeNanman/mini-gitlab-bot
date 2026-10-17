"""结构化日志：一行一个 JSON、task_id 自动注入、重复配置不叠行。"""

from __future__ import annotations

import io
import json
import logging

from core.logging_setup import (
    JsonFormatter,
    TaskContextFilter,
    bind_task,
    configure_logging,
    current_task_id,
)


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="core.orchestrator",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="任务 %s 开始",
        args=("t1",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_line_with_expected_fields() -> None:
    line = JsonFormatter().format(_record(task_id="task-abc"))
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "core.orchestrator"
    assert payload["msg"] == "任务 t1 开始"
    assert payload["task_id"] == "task-abc"
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]


def test_task_id_comes_from_contextvar() -> None:
    assert current_task_id() == ""
    with bind_task("task-xyz"):
        assert current_task_id() == "task-xyz"
        record = _record()
        TaskContextFilter().filter(record)
        assert record.task_id == "task-xyz"
    assert current_task_id() == ""


def test_explicit_task_id_wins_over_context() -> None:
    with bind_task("task-from-context"):
        record = _record(task_id="task-explicit")
        TaskContextFilter().filter(record)
        assert record.task_id == "task-explicit"


def test_nested_bind_restores_outer_value() -> None:
    with bind_task("outer"):
        with bind_task("inner"):
            assert current_task_id() == "inner"
        assert current_task_id() == "outer"


def test_configure_logging_is_idempotent_and_json() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    configure_logging("INFO", "json", stream=stream)
    root = logging.getLogger()
    assert len(root.handlers) == 1  # 不会叠出重复行
    with bind_task("task-log"):
        logging.getLogger("core.test").info("hello")
    payload = json.loads(stream.getvalue().strip())
    assert payload["msg"] == "hello"
    assert payload["task_id"] == "task-log"


def test_text_format_still_works_locally() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", "text", stream=stream)
    with bind_task("task-txt"):
        logging.getLogger("core.test").debug("你好")
    assert "你好" in stream.getvalue()
    assert "task-txt" in stream.getvalue()
    assert logging.getLogger().level == logging.DEBUG
