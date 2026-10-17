"""极小的指标注册表 + Prometheus 文本格式渲染。

**不引入 prometheus_client**：我们只需要几个计数器/仪表盘，而它会把镜像和依赖面变大。
文本格式本身只有十几行，抓取端（Prometheus / VictoriaMetrics）只认格式，不认库。

约定：所有指标以 `mini_gitlab_bot_` 开头；**绝不带项目名、issue 标题、任务 id 这类
高基数/敏感标签**（标签只用状态、阶段这种有限枚举）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

PREFIX = "mini_gitlab_bot_"

#: 指标名 → (类型, 帮助文本)
_DEFINITIONS: dict[str, tuple[str, str]] = {
    "tasks_total": ("counter", "任务终态计数（按最终状态）"),
    "task_duration_seconds": ("counter", "任务耗时累计（秒）与次数（按最终状态）"),
    "task_failures_total": ("counter", "失败计数（按失败阶段）"),
    "worker_claims_total": ("counter", "worker 领取任务的次数"),
    "janitor_runs_total": ("counter", "保留策略清理执行次数"),
    "janitor_removed_total": ("counter", "保留策略删除的对象数（按类型）"),
    "queue_depth": ("gauge", "队列深度（按队列类别）"),
    "worker_inflight": ("gauge", "当前并发执行中的任务数"),
}


def _labels_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _render_labels(key: tuple[tuple[str, str], ...]) -> str:
    if not key:
        return ""
    inner = ",".join(f'{name}="{value}"' for name, value in key)
    return f"{{{inner}}}"


@dataclass
class Metrics:
    """线程安全的计数器/仪表盘。刻意只保留"累加"和"覆盖"两种操作。"""

    _counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(default_factory=dict)
    _gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:  # noqa: D102 见类文档
        key = (name, _labels_key({k: v for k, v in labels.items() if v != ""}))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: str) -> None:  # noqa: A003
        key = (name, _labels_key({k: v for k, v in labels.items() if v != ""}))
        with self._lock:
            self._gauges[key] = float(value)

    # ---- 便捷方法：调用点读起来更像业务语义 ----

    def task_finished(self, state: str, duration_sec: float) -> None:
        self.inc("tasks_total", state=state)
        self.inc("task_duration_seconds", duration_sec, state=state)
        self.inc("task_duration_seconds_count", state=state)

    def task_failed(self, stage: str) -> None:
        self.inc("task_failures_total", stage=stage)

    def value(self, name: str, **labels: str) -> float:
        key = (name, _labels_key({k: v for k, v in labels.items() if v != ""}))
        with self._lock:
            return self._counters.get(key, self._gauges.get(key, 0.0))

    def render(self) -> str:
        """Prometheus 文本格式（text/plain; version=0.0.4）。"""
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)

        lines: list[str] = []
        emitted: set[str] = set()
        for (name, labels), value in sorted(counters.items()):
            full = f"{PREFIX}{name}"
            if name not in emitted:
                emitted.add(name)
                help_text = _DEFINITIONS.get(name, ("counter", name))[1]
                lines += [f"# HELP {full} {help_text}", f"# TYPE {full} counter"]
            lines.append(f"{full}{_render_labels(labels)} {_format(value)}")
        for (name, labels), value in sorted(gauges.items()):
            full = f"{PREFIX}{name}"
            if name not in emitted:
                emitted.add(name)
                help_text = _DEFINITIONS.get(name, ("gauge", name))[1]
                lines += [f"# HELP {full} {help_text}", f"# TYPE {full} gauge"]
            lines.append(f"{full}{_render_labels(labels)} {_format(value)}")
        return "\n".join(lines) + ("\n" if lines else "")


def _format(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")
