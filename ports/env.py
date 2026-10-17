"""EnvPreparer 端口：两阶段模型的前半段（PLAN §4）。

**为什么单独成端口**：agent 阶段是零网络的，所以"装依赖"只能由网关在**有网**的时候
先做完。把这一步显式建模，才能保证：

* 准备阶段的失败有明确的失败原因（而不是等到 agent 报一堆 import error）；
* 假实现能让整条链路在没有网络、没有 uv 的 CI 里跑通。

真实适配器（`adapters/uv_env.py`）负责 `uv python install` + `uv sync`，
并把 `UV_CACHE_DIR` 放在容器卷内（`~/.cache/uv` 在沙箱里不可写，见 PLAN §4 细节 1）。
"""

from __future__ import annotations

from typing import Protocol

from core.models import EnvReport, Task, Workdir


class EnvPreparer(Protocol):
    async def prepare(self, task: Task, workdir: Workdir) -> EnvReport:
        """在 workdir 里装好依赖（此时网关有网络）。

        返回 `EnvReport(ok=False, ...)` 表示准备失败 —— 编排器会直接判任务失败并写明原因。
        抛异常同样会被编排器兜底成失败；但优先用 `ok=False` + 明确原因，别靠异常。
        """
        ...
