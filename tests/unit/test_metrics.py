"""指标注册表：Prometheus 文本格式的边界（标签、HELP 只出现一次、浮点格式）。"""

from __future__ import annotations

from core.metrics import Metrics


def test_counter_and_gauge_render_prometheus_text() -> None:
    metrics = Metrics()
    metrics.inc("tasks_total", state="succeeded")
    metrics.inc("tasks_total", state="succeeded")
    metrics.inc("tasks_total", state="failed")
    metrics.set("queue_depth", 3, state="queued")

    text = metrics.render()
    assert "# TYPE mini_gitlab_bot_tasks_total counter" in text
    assert 'mini_gitlab_bot_tasks_total{state="succeeded"} 2' in text
    assert 'mini_gitlab_bot_tasks_total{state="failed"} 1' in text
    assert "# TYPE mini_gitlab_bot_queue_depth gauge" in text
    assert 'mini_gitlab_bot_queue_depth{state="queued"} 3' in text
    # HELP 每个指标只出现一次
    assert text.count("# HELP mini_gitlab_bot_tasks_total") == 1


def test_gauge_set_overwrites() -> None:
    metrics = Metrics()
    metrics.set("worker_inflight", 2)
    metrics.set("worker_inflight", 0)
    assert metrics.value("worker_inflight") == 0


def test_empty_labels_render_without_braces() -> None:
    metrics = Metrics()
    metrics.inc("janitor_runs_total")
    assert "mini_gitlab_bot_janitor_runs_total 1" in metrics.render()


def test_task_finished_records_duration_and_count() -> None:
    metrics = Metrics()
    metrics.task_finished("succeeded", 12.5)
    metrics.task_finished("succeeded", 7.5)
    assert metrics.value("task_duration_seconds", state="succeeded") == 20.0
    assert metrics.value("task_duration_seconds_count", state="succeeded") == 2
    assert metrics.value("tasks_total", state="succeeded") == 2


def test_task_failed_labels_the_stage() -> None:
    metrics = Metrics()
    metrics.task_failed("validating")
    assert metrics.value("task_failures_total", stage="validating") == 1


def test_float_formatting_is_prometheus_friendly() -> None:
    metrics = Metrics()
    metrics.inc("task_duration_seconds", 1.5, state="failed")
    text = metrics.render()
    assert "1.5" in text
    assert "1.500000" not in text


def test_render_is_deterministic() -> None:
    metrics = Metrics()
    metrics.inc("tasks_total", state="failed")
    metrics.inc("tasks_total", state="succeeded")
    first = metrics.render()
    second = metrics.render()
    assert first == second
    assert first.index('state="failed"') < first.index('state="succeeded"')
