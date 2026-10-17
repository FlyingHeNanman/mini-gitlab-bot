"""子进程执行层：**唯一**允许 spawn 外部命令的地方。

集中在这里解决四件事（每一件都有对应测试）：

1. **绝不把凭据放进 argv**（PLAN §7 红线 5）：token 只经环境变量传给子进程
   （git 走 `GIT_ASKPASS`，见 `adapters/git_cli.py`）。
2. **超时/取消必须杀掉整个进程组**：codex 会 spawn 子进程，只 kill 父进程会留下孤儿。
   因此用 `start_new_session=True` + `os.killpg`。
3. **日志脱敏**：子进程输出在落盘/贴评论之前先过 `Scrubber`，避免 token 万一被回显。
4. **可注入**：`ProcessRunner` 是协议，测试用假 runner 断言 argv/env，不需要真二进制。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

#: 常见的凭据形态，兜底脱敏（即使没人告诉我们要脱敏什么）。
#: 顺序很重要：**先**按形态抹掉整段值，**再**抹已知密钥 —— 反过来的话
#: `--token=<密钥>` 会先变成 `--token=***`，再被形态规则连选项名一起吃掉。
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"glpat-[A-Za-z0-9_\-]{8,}"),
    # 头部/键值形式：从键一直抹到行尾（`Authorization: Bearer x` 整段消失）
    re.compile(r"(?i)\b(?:authorization|private[-_]?token|api[-_]?key|token)\b\s*[:=][^\n]*"),
    re.compile(r"https://[^/\s:@]+:[^/\s@]+@"),  # https://user:pass@host
)


class Scrubber:
    """把已知密钥与常见 token 形态从文本里抹掉。

    这是纵深防御：正确的设计下 token 根本不会出现在输出里（不进 argv），
    但 git/codex 有可能回显 URL 或环境变量，所以落盘前一律再抹一遍。
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        unique = {secret for secret in secrets if secret and len(secret) >= 6}
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub("***", text)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        return tuple(self(item) for item in argv)

    def env(self, env: Mapping[str, str]) -> dict[str, str]:
        """只用于日志：值一律脱敏，密钥名保留。"""
        return {key: self(value) for key, value in env.items()}


@dataclass(frozen=True, slots=True)
class CompletedProcess:
    """一次子进程执行的结果（输出已脱敏、已截断到 `max_output_chars`）。"""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    timed_out: bool = False
    #: 原始输出是否被截断（只保留尾部，见 `tail_keep`）
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def tail(self, lines: int) -> str:
        """末尾若干行（stdout+stderr 合并），用于失败评论。"""
        return "\n".join(self._merged().splitlines()[-lines:])

    @property
    def line_count(self) -> int:
        return len(self._merged().splitlines())

    def _merged(self) -> str:
        return "\n".join(part.strip() for part in (self.stdout, self.stderr) if part.strip())


class ProcessRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        label: str = "",
        max_output_chars: int = 200_000,
    ) -> CompletedProcess: ...


@dataclass
class AsyncProcessRunner:
    """真实实现（`asyncio.create_subprocess_exec`）。

    `env=None` 表示**不继承**父进程环境（用最小环境，见 `base_env`）—— 这是刻意的：
    网关进程里有 `GITLAB_TOKEN`、`WEBHOOK_SECRET`，绝不能顺手传给 agent 子进程。
    """

    scrubber: Scrubber = field(default_factory=Scrubber)
    calls: list[dict[str, object]] = field(default_factory=list)

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        label: str = "",
        max_output_chars: int = 200_000,
    ) -> CompletedProcess:
        argv = [str(item) for item in argv]
        self.calls.append(
            {
                "argv": self.scrubber.argv(argv),
                "cwd": cwd,
                "env": self.scrubber.env(env or {}),
                "label": label,
            }
        )
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # 独立进程组 → 超时可整组杀掉
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except TimeoutError:
            timed_out = True
            await self._kill_group(process)
            stdout_b, stderr_b = await self._drain(process)
        except asyncio.CancelledError:
            # 上游 wait_for 取消我们（编排器的硬超时）：也必须整组清掉，否则留下孤儿 codex
            await self._kill_group(process)
            raise
        duration = time.monotonic() - started

        stdout, stdout_cut = _decode_tail(stdout_b, max_output_chars)
        stderr, stderr_cut = _decode_tail(stderr_b, max_output_chars)
        exit_code = process.returncode if process.returncode is not None else -1
        if timed_out:
            # 被 SIGKILL 的退出码（-9）没有诊断价值，统一成 -1：调用方只需看 timed_out
            exit_code = -1
        result = CompletedProcess(
            argv=tuple(self.scrubber.argv(argv)),
            exit_code=exit_code,
            stdout=self.scrubber(stdout),
            stderr=self.scrubber(stderr),
            duration_sec=duration,
            timed_out=timed_out,
            output_truncated=stdout_cut or stderr_cut,
        )
        if timed_out:
            logger.warning(
                "子进程超时（%.0fs）已终止进程组：%s %s", duration, label, result.argv[:3]
            )
        return result

    @staticmethod
    async def _kill_group(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # 已经退出
            pass
        except OSError:  # pragma: no cover - 平台差异
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:  # pragma: no cover - 极端情况
            logger.error("子进程 %s 在 SIGKILL 后仍未退出", process.pid)

    @staticmethod
    async def _drain(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        try:
            return await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError:  # pragma: no cover
            return b"", b""


def _decode_tail(raw: bytes, limit: int) -> tuple[str, bool]:
    """解码并保留**尾部** `limit` 个字符（失败日志的价值在尾部）。"""
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def minimal_env(*, path: str, home: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """构造最小子进程环境：显式列出，绝不整体继承网关环境。"""
    env: dict[str, str] = {
        "PATH": path,
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    if extra:
        env.update({key: value for key, value in extra.items() if value})
    return env
