"""`UvEnvPreparer`：准备阶段（网关联网）的 uv 两阶段落地（PLAN §4）。

流程：

```
1. 探测项目声明的 Python 版本（.python-version / pyproject.toml 的 requires-python）
2. 判断镜像预置的 3.13 是否满足要求；不满足才 `uv python install <版本>`
3. uv sync [--frozen] [--python <版本>]   ← 依赖装进 workdir/.venv，源指向内网 kkRepo
4. UV_OFFLINE=1 uv run --no-sync python -c ''   ← 证明 agent 阶段的离线路径可用
```

第 4 步是刻意的：agent 阶段零网络，环境问题必须在**还有网**的时候暴露，
而不是等 agent 跑到一半报一堆 import error。

**缓存位置**：`UV_CACHE_DIR` 必须在容器卷内（`~/.cache/uv` 在 codex 沙箱里不可写，
见 PLAN §4 细节 1），这里统一放在 `<cache_dir>`（默认 `<work_dir>/.uv-cache`，跨任务复用）。

**镜像只预置 3.13**（PLAN §10 前提 4）：只有 3.13 不满足 `requires-python` 时才联网补装，
补装失败即任务失败并写明原因。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from adapters.proc import AsyncProcessRunner, CompletedProcess, ProcessRunner, Scrubber
from core import layout
from core.models import EnvReport, Task, Workdir

logger = logging.getLogger(__name__)

REQUIRES_PYTHON_RE = re.compile(r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

#: 镜像里预置的 CPython 版本（PLAN §10 前提 4）
PRESET_PYTHON = "3.13"

#: 准备阶段允许从网关环境透传的变量（网关有网，这里不做零凭据白名单，但也不全量继承）
_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    }
)


@dataclass(slots=True)
class UvSettings:
    bin: str = "uv"
    cache_dir: str = ""
    default_index: str = ""
    python_install_timeout_sec: float = 900.0
    sync_timeout_sec: float = 1800.0


@dataclass
class UvEnvPreparer:
    settings: UvSettings = field(default_factory=UvSettings)
    log_dir: Path = Path("/data/logs")
    runner: ProcessRunner | None = None
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def __post_init__(self) -> None:
        self._runner = self.runner or AsyncProcessRunner(scrubber=Scrubber())

    async def prepare(self, task: Task, workdir: Workdir) -> EnvReport:
        job_logs = Path(self.log_dir) / task.id
        await asyncio.to_thread(job_logs.mkdir, parents=True, exist_ok=True)
        log_path = job_logs / layout.UV_LOG_FILENAME
        sections: list[str] = []
        steps: list[str] = []
        duration = 0.0

        requires = await asyncio.to_thread(_detect_requires_python, Path(workdir.path))
        install, use = _python_plan(requires)
        if requires:
            steps.append(f"requires-python: {requires}")

        if install:
            result = await self._run(
                [self.settings.bin, "python", "install", install],
                cwd=workdir.path,
                timeout=self.settings.python_install_timeout_sec,
                label="uv:python-install",
            )
            sections.append(_section(f"{self.settings.bin} python install {install}", result))
            duration += result.duration_sec
            if not result.ok:
                return await self._fail(
                    log_path,
                    sections,
                    steps,
                    duration,
                    reason=(
                        f"准备环境失败：uv python install {install} 退出码 {result.exit_code}"
                        f"（镜像只预置 {PRESET_PYTHON}；项目要求 `{requires}`）"
                    ),
                    result=result,
                )
            steps.append(f"installed python {install}")

        locked = await asyncio.to_thread((Path(workdir.path) / "uv.lock").exists)
        sync_argv = [self.settings.bin, "sync", "--no-progress"]
        if locked:
            # 有 lock 就按 lock 装：agent 阶段无网，装不出 lock 之外的解
            sync_argv.append("--frozen")
        if use and use != PRESET_PYTHON:
            sync_argv += ["--python", use]
        result = await self._run(
            sync_argv, cwd=workdir.path, timeout=self.settings.sync_timeout_sec, label="uv:sync"
        )
        sections.append(_section(" ".join(sync_argv), result))
        duration += result.duration_sec
        if not result.ok:
            return await self._fail(
                log_path,
                sections,
                steps,
                duration,
                reason=f"准备环境失败：uv sync 退出码 {result.exit_code}",
                result=result,
            )
        steps.append("uv sync" + (" --frozen" if locked else ""))

        if not await asyncio.to_thread(_venv_exists, workdir):
            return await self._fail(
                log_path,
                sections,
                steps,
                duration,
                reason="准备环境失败：uv sync 结束但 workdir/.venv 不存在",
                result=result,
            )

        if locked:
            # 离线自检：证明 agent 阶段的 `UV_OFFLINE=1 uv run --no-sync` 真的能用
            check = await self._run(
                [self.settings.bin, "run", "--no-sync", "python", "-c", ""],
                cwd=workdir.path,
                timeout=300.0,
                label="uv:offline-check",
                extra_env={"UV_OFFLINE": "1"},
            )
            sections.append(
                _section(f"UV_OFFLINE=1 {self.settings.bin} run --no-sync python -c ''", check)
            )
            duration += check.duration_sec
            if not check.ok:
                return await self._fail(
                    log_path,
                    sections,
                    steps,
                    duration,
                    reason="准备环境失败：离线自检未通过（UV_OFFLINE=1 uv run --no-sync 跑不起来）",
                    result=check,
                )
            steps.append("offline check ok")
        else:
            steps.append("skipped offline check（无 uv.lock）")

        await asyncio.to_thread(log_path.write_text, "\n".join(sections), encoding="utf-8")
        return EnvReport(
            ok=True,
            python_version=use or PRESET_PYTHON,
            steps=tuple(steps),
            duration_sec=duration,
            log_path=str(log_path),
        )

    # ------------------------------------------------------------------ #

    async def _fail(
        self,
        log_path: Path,
        sections: list[str],
        steps: list[str],
        duration: float,
        *,
        reason: str,
        result: CompletedProcess,
    ) -> EnvReport:
        await asyncio.to_thread(log_path.write_text, "\n".join(sections), encoding="utf-8")
        logger.error("准备阶段失败：%s", reason)
        return EnvReport(
            ok=False,
            steps=tuple(steps),
            duration_sec=duration,
            failure_reason=reason,
            log_tail=result.tail(60),
            log_lines=result.line_count,
            log_path=str(log_path),
        )

    async def _run(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        label: str,
        extra_env: Mapping[str, str] | None = None,
    ) -> CompletedProcess:
        return await self._runner.run(
            argv, cwd=cwd, env=self._env(extra_env), timeout_sec=timeout, label=label
        )

    def _env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {key: value for key, value in self.environ.items() if key in _ENV_ALLOWLIST}
        env.setdefault("PATH", "/usr/bin:/bin")
        if self.settings.cache_dir:
            env["UV_CACHE_DIR"] = self.settings.cache_dir
        if self.settings.default_index:
            env["UV_DEFAULT_INDEX"] = self.settings.default_index
        env["UV_NO_PROGRESS"] = "1"
        if extra:
            env.update({key: value for key, value in extra.items() if value})
        return env


# --------------------------------------------------------------------------- #
# 纯函数：Python 版本规划（可单测，不需要真 uv）
# --------------------------------------------------------------------------- #


def _detect_requires_python(workdir: Path) -> str:
    """`.python-version` 优先于 `pyproject.toml` 的 `requires-python`。"""
    version_file = workdir / ".python-version"
    if version_file.exists():
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    pyproject = workdir / "pyproject.toml"
    if pyproject.exists():
        match = REQUIRES_PYTHON_RE.search(pyproject.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1).strip()
    return ""


def _python_plan(requires: str) -> tuple[str, str]:
    """返回 `(要补装的版本, 实际使用的版本)`；无需补装时第一项为空串。

    * 没有声明 → 用预置的 3.13；
    * 3.13 满足声明 → 用 3.13（**不联网**，这是绝大多数情况）；
    * 否则取声明的下界版本补装并使用它。
    """
    if not requires:
        return "", PRESET_PYTHON
    if _satisfies(PRESET_PYTHON, requires):
        return "", PRESET_PYTHON
    versions = re.findall(r"\d+\.\d+(?:\.\d+)?", requires)
    if not versions:
        return "", PRESET_PYTHON
    target = versions[0]
    return target, target


def _satisfies(version: str, specifier: str) -> bool:
    """判断版本是否满足 PEP 440 风格的逗号分隔约束（够用即可，不求完整实现）。"""
    current = _version_tuple(version)
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(===|==|!=|>=|<=|~=|>|<)\s*(\d+(?:\.\d+)*)(\.\*)?$", clause)
        if not match:
            continue  # 认不出的约束：不据此否决预置版本
        operator, wanted_raw, wildcard = match.groups()
        wanted = _version_tuple(wanted_raw)
        if operator in ("==", "==="):
            matched = _prefix_equal(current, wanted) if wildcard else current == wanted
        elif operator == "!=":
            matched = not (_prefix_equal(current, wanted) if wildcard else current == wanted)
            if not matched:
                return False
            continue
        elif operator == ">=":
            matched = current >= wanted
        elif operator == "<=":
            matched = current <= wanted
        elif operator == ">":
            matched = current > wanted
        elif operator == "<":
            matched = current < wanted
        else:  # ~=X.Y → >=X.Y, ==X.*（允许的位数是 len-1 段前缀）
            matched = current >= wanted and _prefix_equal(current, wanted[:-1])
        if not matched:
            return False
    return True


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text)
    return tuple(int(part) for part in parts) or (0,)


def _prefix_equal(current: tuple[int, ...], wanted: tuple[int, ...]) -> bool:
    return current[: len(wanted)] == wanted


def _venv_python(workdir: Workdir) -> str:
    return f"{workdir.path}/.venv/bin/python"


def _venv_exists(workdir: Workdir) -> bool:
    return Path(_venv_python(workdir)).exists()


def _section(title: str, result: CompletedProcess) -> str:
    header = f"$ {title}   [exit={result.exit_code}{' timeout' if result.timed_out else ''}]"
    body = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
    return f"{header}\n{body}".strip()
