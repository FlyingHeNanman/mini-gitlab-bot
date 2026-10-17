"""`GitCli`：真实 git 实现（clone / 分支 / 提交 / diff / push）。

**凭据处理（PLAN §7 红线 5）**：token 绝不进 argv、绝不进 URL、绝不进日志。做法是
`GIT_ASKPASS` 指向一个 0700 的脚本，token 只存在于**子进程环境变量**里：

```
git clone https://gitlab.example.com/group/proj.git   ← argv 里没有任何凭据
   env: GIT_ASKPASS=<jobs 根>/.git-askpass
        MINI_GITLAB_BOT_GIT_TOKEN=<token>
```

v0 的 `_authed_url()`（`https://oauth2:<token>@host/...` 直接进 argv）是反面教材：
argv 会出现在 `ps`、进程审计、以及任何 `CalledProcessError` 的回显里。

**内部文件绝不进提交**：clone 完成后立刻把 `core.layout.GIT_EXCLUDE_PATTERNS` 写进
`.git/info/exclude`（本地忽略，不改动仓库里的 `.gitignore`），并在 `git add` 时再加一层
`:(exclude)` 显式排除 —— 否则 `uv sync` 产生的 `.venv/`、agent 跑测试产生的
`__pycache__/`、以及我们自己的 `.mini-gitlab-bot/` 都会被提交进 MR。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from adapters.proc import AsyncProcessRunner, CompletedProcess, ProcessRunner, Scrubber
from core import layout
from core.models import DiffSummary, Task, Workdir

logger = logging.getLogger(__name__)

ASKPASS_FILENAME = ".git-askpass"
_ASKPASS_SCRIPT = """#!/bin/sh
# 由 mini-gitlab-bot 生成：git 的凭据只经环境变量传入，绝不进 argv / URL。
case "$1" in
  *sername*) printf '%s' "${MINI_GITLAB_BOT_GIT_USER:-oauth2}" ;;
  *) printf '%s' "$MINI_GITLAB_BOT_GIT_TOKEN" ;;
esac
"""


class JobArtifactLeak(RuntimeError):
    """任务本地产物被暂存了 —— 说明 `.git/info/exclude` 没生效。

    这是"宁可失败也不要把 `.venv/` 提交进 MR"的兜底：走到这里说明忽略规则失效，
    继续提交会把虚拟环境整个塞进 MR。
    """

    def __init__(self, paths: Sequence[str]) -> None:
        super().__init__("任务本地产物被暂存（忽略规则失效）：" + "、".join(paths))
        self.paths = tuple(paths)


class GitError(RuntimeError):
    """git 命令失败（消息里已脱敏，可安全贴进 issue 评论）。"""

    def __init__(self, stage: str, result: CompletedProcess) -> None:
        tail = result.tail(20) or result.stderr.strip() or "（无输出）"
        super().__init__(f"git {stage} 失败（退出码 {result.exit_code}）：{tail[-800:]}")
        self.stage = stage
        self.result = result


class GitCli:
    def __init__(
        self,
        *,
        jobs_root: Path | str,
        remote_base: str,
        token: str = "",
        username: str = "oauth2",
        author_name: str = "mini-gitlab-bot",
        author_email: str = "mini-gitlab-bot@localhost",
        clone_depth: int = 0,
        timeout_sec: float = 600.0,
        runner: ProcessRunner | None = None,
        git_bin: str = "git",
    ) -> None:
        self._root = Path(jobs_root).expanduser()
        self._remote_base = remote_base.rstrip("/")
        self._token = token
        self._username = username
        self._author_name = author_name
        self._author_email = author_email
        self._clone_depth = clone_depth
        self._timeout = timeout_sec
        self._git = git_bin
        self._runner = runner or AsyncProcessRunner(scrubber=Scrubber((token,)))
        self._askpass: Path | None = None

    # ------------------------------------------------------------------ #
    # 端口实现
    # ------------------------------------------------------------------ #

    async def prepare_workdir(self, task: Task, *, base_branch: str, branch: str) -> Workdir:
        self._root.mkdir(parents=True, exist_ok=True)
        job = self._root / task.id
        if job.exists():  # 重试时残留的旧目录：先清掉，避免 clone 撞车
            await asyncio.to_thread(shutil.rmtree, job, True)

        argv = [self._git, "clone", "--quiet", "--branch", base_branch, "--single-branch"]
        if self._clone_depth > 0:
            argv += ["--depth", str(self._clone_depth)]
        argv += [self.remote_url(task.project.path), str(job)]
        await self._git_run(argv, stage="clone", cwd=str(self._root))

        await self._git_run(
            self._git_argv(job, "checkout", "--quiet", "-b", branch), stage="checkout"
        )
        await self._git_run(
            self._git_argv(job, "config", "user.name", self._author_name), stage="config"
        )
        await self._git_run(
            self._git_argv(job, "config", "user.email", self._author_email), stage="config"
        )
        await self._write_git_exclude(job)
        return Workdir(path=str(job), branch=branch, base_branch=base_branch)

    async def commit_all(self, task: Task, workdir: Workdir, *, message: str) -> str | None:
        """兜底提交，返回 HEAD；分支相对基线没有任何新提交时返回 None。

        与 v0 的 `_has_commits`（对 fresh clone 恒为真 → 重试逻辑完全失效）不同，这里
        比较的是 `base...HEAD` 的提交数，而不是"有没有提交"。
        """
        # 注意：不能用 `git add -A -- . ':(exclude).venv'` —— 显式排除路径会让 git
        # 以"你正在添加被忽略的路径"为由直接退出 1（实测）。忽略交给 clone 时写好的
        # `.git/info/exclude`，并在 add 之后做后置校验（见 `_assert_no_job_artifacts`）。
        if await self._is_dirty(workdir):
            await self._git_run(self._git_argv(workdir.path, "add", "-A", "--", "."), stage="add")
            await self._assert_no_job_artifacts(workdir)
            await self._git_run(
                self._git_argv(workdir.path, "commit", "--quiet", "--no-verify", "-m", message),
                stage="commit",
            )

        if await self._head_commit_count(workdir) == 0:
            return None
        result = await self._git_run(
            self._git_argv(workdir.path, "rev-parse", "HEAD"), stage="rev-parse"
        )
        return result.stdout.strip()

    async def diff(self, task: Task, workdir: Workdir) -> DiffSummary:
        result = await self._git_run(
            self._git_argv(
                workdir.path,
                "diff",
                "--numstat",
                "--no-renames",
                "--no-color",
                f"{workdir.base_branch}...HEAD",
            ),
            stage="diff",
        )
        return _parse_numstat(result.stdout)

    async def push(self, task: Task, workdir: Workdir) -> None:
        await self._git_run(
            self._git_argv(
                workdir.path, "push", "--quiet", "origin", f"HEAD:refs/heads/{workdir.branch}"
            ),
            stage="push",
        )

    # ------------------------------------------------------------------ #
    # 供测试/诊断使用
    # ------------------------------------------------------------------ #

    def remote_url(self, project_path: str) -> str:
        return f"{self._remote_base}/{project_path}.git"

    def git_env(self) -> dict[str, str]:
        """git 子进程环境：最小集合 + 凭据（**只在这里出现**）。"""
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",  # 缺凭据时立刻失败，不要挂着等输入
        }
        if self._token:
            env["GIT_ASKPASS"] = str(self._ensure_askpass())
            env["MINI_GITLAB_BOT_GIT_TOKEN"] = self._token
            env["MINI_GITLAB_BOT_GIT_USER"] = self._username
        return env

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _git_argv(self, job: Path | str, *args: str) -> list[str]:
        # core.quotepath=false：让非 ASCII 路径以原样输出，否则 diff 解析会拿到 C 转义串
        return [self._git, "-c", "core.quotepath=false", "-C", str(job), *args]

    async def _assert_no_job_artifacts(self, workdir: Workdir) -> None:
        """后置校验：任务本地产物一旦被暂存，就明确失败（而不是提交进 MR）。"""
        result = await self._git_run(
            self._git_argv(workdir.path, "diff", "--cached", "--name-only"), stage="diff-cached"
        )
        offenders = [
            path
            for path in result.stdout.splitlines()
            if path.strip() and layout.matches_git_exclude(path)
        ]
        if offenders:
            raise JobArtifactLeak(offenders)

    async def _git_run(
        self, argv: Sequence[str], *, stage: str, cwd: str | None = None
    ) -> CompletedProcess:
        result = await self._runner.run(
            argv, cwd=cwd, env=self.git_env(), timeout_sec=self._timeout, label=f"git:{stage}"
        )
        if not result.ok:
            raise GitError(stage, result)
        return result

    async def _head_commit_count(self, workdir: Workdir) -> int:
        """HEAD 上有、基线分支上没有的提交数（`base..HEAD`）。"""
        result = await self._git_run(
            self._git_argv(workdir.path, "rev-list", "--count", f"{workdir.base_branch}..HEAD"),
            stage="rev-list",
        )
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:  # pragma: no cover - git 不会这样输出
            return 0

    async def _is_dirty(self, workdir: Workdir) -> bool:
        result = await self._git_run(
            self._git_argv(workdir.path, "status", "--porcelain"), stage="status"
        )
        return bool(result.stdout.strip())

    async def _write_git_exclude(self, job: Path) -> None:
        await asyncio.to_thread(_append_git_exclude, job)

    def _ensure_askpass(self) -> Path:
        if self._askpass is None:
            self._root.mkdir(parents=True, exist_ok=True)
            path = self._root / ASKPASS_FILENAME
            path.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
            os.chmod(path, 0o700)
            self._askpass = path
        return self._askpass


def _parse_numstat(stdout: str) -> DiffSummary:
    """解析 `git diff --numstat`：`<增>\t<删>\t<路径>`（二进制文件是 `-`）。"""
    files: list[str] = []
    insertions = 0
    deletions = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:  # pragma: no cover - 防御性
            continue
        added, removed, path = parts[0], parts[1], "\t".join(parts[2:])
        files.append(path.strip())
        insertions += _as_int(added)
        deletions += _as_int(removed)
    return DiffSummary(files=tuple(files), insertions=insertions, deletions=deletions)


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:  # 二进制文件
        return 0


def _append_git_exclude(job: Path) -> None:
    """把任务本地产物写进 `.git/info/exclude`（本地忽略，不动仓库的 `.gitignore`）。"""
    exclude = job / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    body = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if body and not body.endswith("\n"):
        body += "\n"
    body += "# mini-gitlab-bot：任务本地产物，绝不进提交\n"
    body += "".join(f"{pattern}\n" for pattern in layout.GIT_EXCLUDE_PATTERNS)
    exclude.write_text(body, encoding="utf-8")
