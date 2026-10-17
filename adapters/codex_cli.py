"""`CodexCli`：真实 agent 内核（`codex exec`）。

对应 PLAN §7 加固基线 1/3/4/5 与 §6 的 14 条已验证事实。每一处都对应一条事实：

* 事实 #5（`-c` 优先级 30 > 项目 25）：`sandbox_mode` / `network_access` /
  `approval_policy` 全部用 `-c` 钉死。
* 事实 #4（项目配置默认 untrusted，但 deny-list 不含 `mcp_servers`）：启动前删掉
  workdir 里的 `.codex/`（基线 1），且永不写 `trust_level='trusted'`。
* 事实 #6（`turn.completed` 与有无改动无关；`Interrupted` 不发终止事件；`-o` 只在
  成功时写）：四条件判定 —— exit 0 ∧ 见到 `turn.completed` ∧ `-o` **新写入** ∧ diff
  非空（前三条在这里，第四条在编排器）。
* 事实 #3（`workspace-write` 读是全盘可读）：子进程环境用**显式白名单**构造，绝不继承
  网关环境 → agent 拿不到任何 GitLab 凭据。
* 事实 #8（`requires_openai_auth` 默认 false + `env_key`）：模型 key 只以环境变量名
  （`env_key`）出现在 `-c` 里，值只进子进程环境。

`-o` 的"**新写入**"语义：每次运行前先删掉目标文件，运行后存在即为新写入 —— 因为
失败时 codex 既不写也不删，不能靠"文件是否存在"判断（该路径可能上一次成功时留下的）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapters.proc import (
    AsyncProcessRunner,
    CompletedProcess,
    ProcessRunner,
    Scrubber,
    minimal_env,
)
from core import layout
from core.models import AgentResult, Task, Workdir

logger = logging.getLogger(__name__)

#: 传给 agent 的固定指令（详细上下文在 context.md 里，避免 prompt 过长）
TASK_PROMPT = (
    "你是仓库修复机器人。请先完整阅读 `{context}`（相对当前目录），"
    "按其中的「硬约束」「结构化报告」要求完成修复：改代码、按需跑测试、"
    "`git add -A` 并 `git commit`。**不要 push、不要新建 MR**。"
)

#: 模型 key 的环境变量名默认值（与 `env_key` 一致，见 PLAN §6 #8）
DEFAULT_API_KEY_ENV = "LLM_API_KEY"

#: 允许从网关环境透传给 codex 的变量名（其余一律不透传）
_PASSTHROUGH_ENV: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH")


@dataclass(slots=True)
class CodexSettings:
    """codex 调用的全部旋钮（由 `gateway.config.Settings` 映射而来）。"""

    bin: str = "codex"
    model_provider: str = ""
    model_name: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    #: 追加的 `-c key=value`（例如 `model_providers.internal.base_url=...`）
    config_overrides: tuple[str, ...] = ()
    timeout_sec: float = 1800.0
    expected_version: str = ""
    sandbox_mode: str = "workspace-write"


@dataclass
class CodexCli:
    settings: CodexSettings = field(default_factory=CodexSettings)
    log_dir: Path = Path("/data/logs")
    runner: ProcessRunner | None = None
    #: 环境变量来源：默认 `os.environ`，测试可注入（断言"凭据没有透传"）
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    scrubber: Scrubber | None = None

    def __post_init__(self) -> None:
        secrets = tuple(
            value for key, value in self.environ.items() if value and _looks_secret(key)
        )
        self._scrubber = self.scrubber or Scrubber(
            (*secrets, self.environ.get(self.settings.api_key_env, ""))
        )
        self._runner = self.runner or AsyncProcessRunner(scrubber=self._scrubber)
        self._version: str | None = None

    # ------------------------------------------------------------------ #
    # 端口实现
    # ------------------------------------------------------------------ #

    async def run(
        self, task: Task, *, workdir: Workdir, context_path: str, timeout_sec: float
    ) -> AgentResult:
        job_logs = Path(self.log_dir) / task.id
        job_logs.mkdir(parents=True, exist_ok=True)
        log_path = job_logs / layout.CODEX_LOG_FILENAME
        last_message_path = job_logs / layout.LAST_MESSAGE_FILENAME

        await self._harden_workdir(workdir)
        tmp_dir = await asyncio.to_thread(_mkdir, layout.temp_dir(workdir.path))
        # 清掉上次留下的 `-o` 文件 → 之后"存在"就等于"本次新写入"（PLAN §6 #6）
        if last_message_path.exists():
            await asyncio.to_thread(last_message_path.unlink)

        context_name = context_path.rsplit("/", 1)[-1]
        argv = await self._build_argv(
            workdir=workdir, context_name=context_name, last_message=str(last_message_path)
        )
        env = self._child_env(tmp_dir=tmp_dir, workdir=workdir)

        timeout = timeout_sec or self.settings.timeout_sec
        result = await self._runner.run(
            argv,
            cwd=workdir.path,
            env=env,
            timeout_sec=timeout,
            label=f"codex:{task.id}",
        )
        await asyncio.to_thread(log_path.write_text, result.stdout, encoding="utf-8")

        events = _parse_jsonl(result.stdout)
        completed = any(event.get("type") == "turn.completed" for event in events)
        last_message_written = last_message_path.exists()
        last_message = ""
        if last_message_written:
            last_message = await asyncio.to_thread(last_message_path.read_text, encoding="utf-8")

        report = await self._read_report(workdir)
        if last_message.strip() and not report.get("summary"):
            report["summary"] = _clip(last_message.strip(), 2000)
        report["agent_version"] = await self._agent_version()
        report["agent_exit_code"] = result.exit_code

        notes = _notes(events, result)
        return AgentResult(
            exit_code=result.exit_code,
            completed_event=completed,
            last_message_written=last_message_written,
            last_message=last_message,
            duration_sec=result.duration_sec,
            log_path=str(log_path),
            report=report,
            timed_out=result.timed_out,
            notes=notes,
            log_tail=result.tail(80),
            log_lines=result.line_count,
        )

    # ------------------------------------------------------------------ #
    # 命令构造 / 环境
    # ------------------------------------------------------------------ #

    async def _build_argv(
        self, *, workdir: Workdir, context_name: str, last_message: str
    ) -> list[str]:
        sandbox = self.settings.sandbox_mode or "workspace-write"
        argv = [
            self.settings.bin,
            "exec",
            "--json",
            "--sandbox",
            sandbox,
            "--cd",
            workdir.path,
            "-o",
            last_message,
            # ---- 安全关键项：全部用 -c 下发（优先级 30 > 项目配置 25）----
            "-c",
            f'sandbox_mode="{sandbox}"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            'approval_policy="never"',
        ]
        if self.settings.model_provider:
            argv += ["-c", f'model_provider="{self.settings.model_provider}"']
        if self.settings.model_name:
            argv += ["-c", f'model="{self.settings.model_name}"']
        for override in self.settings.config_overrides:
            if override.strip():
                argv += ["-c", override.strip()]
        argv.append(TASK_PROMPT.format(context=context_name))
        return argv

    def _child_env(self, *, tmp_dir: str, workdir: Workdir) -> dict[str, str]:
        """显式白名单 + 离线/沙箱相关变量。

        **绝不**整体继承 `os.environ`：网关进程里有 `GITLAB_TOKEN`、`WEBHOOK_SECRET`、
        `GIT_ASKPASS`，继承等于把凭据交给可能被 prompt 注入的 agent。
        """
        passthrough = {
            key: self.environ.get(key, "") for key in _PASSTHROUGH_ENV if self.environ.get(key)
        }
        api_key = self.environ.get(self.settings.api_key_env, "")
        extra = {
            "TMPDIR": tmp_dir,
            "UV_OFFLINE": "1",  # agent 阶段完全无网（PLAN §4）
            "UV_NO_SYNC": "1",
            "UV_CACHE_DIR": f"{workdir.path}/{layout.INTERNAL_DIRNAME}/uv-cache",
            "UV_PROJECT_ENVIRONMENT": f"{workdir.path}/.venv",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "CI": "1",
            # 模型凭据（若配置）：值只在这里出现，`-c` 里只有变量名
        }
        if api_key:
            extra[self.settings.api_key_env] = api_key
        return minimal_env(
            path=passthrough.get("PATH", "/usr/bin:/bin"),
            home=passthrough.get("HOME", str(Path(workdir.path).parent)),
            extra={**passthrough, **extra},
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _harden_workdir(self, workdir: Workdir) -> None:
        """基线 1：删掉仓库里的 `.codex/`（纵深防御，不依赖 trust 语义不变）。"""
        codex_dir = Path(workdir.path) / ".codex"
        if codex_dir.exists():
            logger.warning("删除 workdir 里的 .codex/：%s", codex_dir)
            await asyncio.to_thread(shutil.rmtree, codex_dir, True)

    async def _read_report(self, workdir: Workdir) -> dict[str, Any]:
        path = Path(workdir.report_path)
        if not path.exists():
            return {}
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("report.json 无法解析（%s），退化为使用最终消息", exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("report.json 不是 JSON 对象，忽略")
            return {}
        return {str(key): value for key, value in data.items()}

    async def _agent_version(self) -> str:
        if self._version is not None:
            return self._version
        try:
            result = await self._runner.run(
                [self.settings.bin, "--version"], timeout_sec=30, label="codex:version"
            )
            self._version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        except Exception as exc:  # noqa: BLE001 版本探测失败不影响任务
            logger.warning("获取 codex 版本失败：%s", exc)
            self._version = ""
        if (
            self._version
            and self.settings.expected_version
            and self.settings.expected_version not in self._version
        ):
            logger.warning(
                "codex 版本不符：期望含 %s，实际 %s（PLAN §7 基线 6 要求 pin 版本）",
                self.settings.expected_version,
                self._version,
            )
        return self._version


# --------------------------------------------------------------------------- #
# 纯函数：JSONL 解析 / 证据整理
# --------------------------------------------------------------------------- #


def _parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    """解析 `--json` 的 JSONL 输出；非 JSON 行（噪音）直接跳过。"""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def _notes(events: Sequence[Mapping[str, Any]], result: CompletedProcess) -> tuple[str, ...]:
    """把"为什么失败"的证据收集起来，进失败评论（便于人工定位，不用翻日志）。"""
    notes: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "turn.failed":
            error = event.get("error") or event.get("message") or ""
            notes.append(f"turn.failed：{_clip(str(error), 400)}")
        elif kind == "error":
            message = event.get("message") or event.get("error") or event
            notes.append(f"error 事件：{_clip(str(message), 400)}")
    if result.timed_out:
        notes.append("codex 超时，进程组已终止")
    if result.output_truncated:
        notes.append("输出过长已截断（只保留尾部）")
    if result.exit_code != 0 and not notes:
        notes.append(f"codex 退出码 {result.exit_code}（无 error 事件，疑似被中断）")
    return tuple(notes)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _mkdir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL"))
