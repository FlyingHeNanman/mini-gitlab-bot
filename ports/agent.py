"""AgentRunner 端口：唤醒 agent 内核（M2 起是 `codex exec`）。

调用方（编排器）只认 `AgentResult`，成功判定是**四者与运算**
（PLAN §7 基线 5）：`exit 0` 且见到 `turn.completed` 且 `-o` 文件被新写入 且 diff 非空。
前三条由适配器如实体现在 `AgentResult` 里，第四条由编排器自查仓库。

真实适配器的硬要求（M2）：`-c` 钉死 `sandbox_mode` / `network_access` / `approval_policy`、
启动前删除 workdir 内的 `.codex/`、`--json` 落盘、每个 job 独立 `TMPDIR`。
"""

from __future__ import annotations

from typing import Protocol

from core.models import AgentResult, Task, Workdir


class AgentRunner(Protocol):
    async def run(
        self,
        task: Task,
        *,
        workdir: Workdir,
        context_path: str,
        timeout_sec: float,
    ) -> AgentResult: ...
