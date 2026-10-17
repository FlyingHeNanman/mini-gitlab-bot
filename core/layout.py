"""job 目录里的内部文件布局 —— 唯一事实来源。

**为什么单独一个模块**：这些路径同时被 workspace（写 context.md）、agent 适配器
（读 report.json、`-o` 落盘）、VCS 适配器（把目录写进 `.git/info/exclude`）使用，
写错一处就会出现"内部文件被 `git add -A` 提交进 MR"这类事故。

布局约定：

```
<workdir>/                        ← git 工作区（agent 可写）
  .mini-gitlab-bot/               ← 全部内部文件，必须被 git 忽略
    context.md                    ← 网关预取的任务上下文（agent 的唯一入口）
    report.json                   ← agent 结束时写的结构化报告
    tmp/                          ← 本 job 独立 TMPDIR（PLAN §7 基线 4）

<log_dir>/<task_id>/              ← **不随 job 目录清理**（PLAN §9 M5：codex 日志留 14 天）
  codex.jsonl                     ← `codex exec --json` 的 stdout 落盘
  last-message.md                 ← `-o/--output-last-message` 的落盘
  uv-sync.log                     ← 准备阶段（uv 两阶段）的日志
```

`report.json` 与 `context.md` 必须在 workdir 内（agent 的沙箱只能写 workdir），
而日志必须在 workdir 外（终态会 `rmtree` 整个 job 目录，但日志要留 14 天），
所以这两类文件的路径来源不同，不能混用。
"""

from __future__ import annotations

#: workdir 内的内部目录名（务必以 `/` 结尾地忽略整个目录）
INTERNAL_DIRNAME = ".mini-gitlab-bot"

CONTEXT_FILENAME = "context.md"
REPORT_FILENAME = "report.json"
TMP_DIRNAME = "tmp"

#: 日志目录内的文件名
CODEX_LOG_FILENAME = "codex.jsonl"
LAST_MESSAGE_FILENAME = "last-message.md"
UV_LOG_FILENAME = "uv-sync.log"

#: `.git/info/exclude` 里阻止任务本地产物进提交的规则。
#: 这些路径是**这个 job 自己**产生的（网关写 context.md、`uv sync` 建 `.venv`、
#: agent 跑 pytest 生成 `__pycache__`/`.pytest_cache`），绝不能进 MR。
GIT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    f"{INTERNAL_DIRNAME}/",
    ".venv/",
    "__pycache__/",
    "*.py[cod]",
    ".uv-cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".coverage",
    "htmlcov/",
)


def internal_dir(workdir_path: str) -> str:
    return f"{workdir_path.rstrip('/')}/{INTERNAL_DIRNAME}"


#: 相对 workdir 的路径（`Workspace.write_text` 用这种相对形式）
RELATIVE_CONTEXT_PATH = f"{INTERNAL_DIRNAME}/{CONTEXT_FILENAME}"
RELATIVE_REPORT_PATH = f"{INTERNAL_DIRNAME}/{REPORT_FILENAME}"


def context_path(workdir_path: str) -> str:
    return f"{internal_dir(workdir_path)}/{CONTEXT_FILENAME}"


def report_path(workdir_path: str) -> str:
    return f"{internal_dir(workdir_path)}/{REPORT_FILENAME}"


def temp_dir(workdir_path: str) -> str:
    return f"{internal_dir(workdir_path)}/{TMP_DIRNAME}"


def matches_git_exclude(path: str) -> bool:
    """路径是否是"任务本地产物"（应当被 git 忽略）。

    用做 `git add` 之后的**后置校验**：万一 `.git/info/exclude` 没生效，
    宁可让任务明确失败，也不能把 `.venv/` 或 `__pycache__/` 提交进 MR。
    """
    from fnmatch import fnmatch

    cleaned = path.strip().lstrip("/")
    for pattern in GIT_EXCLUDE_PATTERNS:
        if pattern.endswith("/"):
            if cleaned.startswith(pattern) or cleaned == pattern.rstrip("/"):
                return True
        elif fnmatch(cleaned, pattern) or fnmatch(cleaned.rsplit("/", 1)[-1], pattern):
            return True
    return False
