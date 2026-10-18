"""`context.md` 预取：agent 唯一的任务上下文入口。

agent 零网络、零凭据（PLAN §7 红线 2），所以它需要知道的一切 —— issue 正文、评论、
离线测试命令、禁改路径 —— 都必须在唤醒它之前落到 `workdir/context.md` 里。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from core import layout
from core.mcp_spec import TOOLS_HINT
from core.models import IssueContext, Task
from core.policy import DiffPolicy

MAX_BODY_CHARS = 8000
MAX_COMMENT_CHARS = 4000


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n…（已截断，原文 {len(text)} 字符）"


def render_context(
    task: Task,
    issue: IssueContext,
    *,
    policy: DiffPolicy,
    mcp_tools: Sequence[str] = (),
    extra_sections: Sequence[str] = (),
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    labels = "、".join(f"`{label}`" for label in issue.labels) or "（无）"
    forbidden = "、".join(f"`{p}`" for p in policy.forbidden_paths)

    parts: list[str] = [
        f"# 任务：修复 GitLab issue #{issue.iid}",
        "",
        "## 元信息",
        "",
        f"- 项目：`{task.project.path}`（project_id={task.project.project_id}）",
        f"- issue 标题：{issue.title}",
        f"- issue 作者：@{issue.author}",
        f"- issue 状态：`{issue.state}`",
        f"- 标签：{labels}",
        f"- 链接：{issue.web_url}",
        f"- 任务 id：`{task.id}`｜触发方式：{task.trigger_kind} by @{task.trigger_actor}",
        f"- 生成时间：{now.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        "## Issue 正文",
        "",
        _clip(issue.description.strip() or "（正文为空）", MAX_BODY_CHARS),
    ]

    if issue.comments:
        parts += ["", f"## 评论（{len(issue.comments)} 条）", ""]
        for index, comment in enumerate(issue.comments, start=1):
            parts += [f"### 评论 {index}", "", _clip(comment.strip(), MAX_COMMENT_CHARS), ""]

    parts += [
        "## 硬约束（必须遵守）",
        "",
        "1. 你**没有网络**（连内网也不通）：不要 `uv add`、`pip install`、`uv sync`、`git fetch`、"
        "`curl`。跑测试请用：`UV_OFFLINE=1 uv run --no-sync pytest -q`",
        "2. **不要** `git push`、**不要**新建 MR —— 收尾（校验 → push → 开 draft MR）由网关完成。"
        "你只负责改代码并 `git commit`。",
        "3. 只改必要文件，保持项目既有风格。**禁止改动**：" + forbidden,
        "4. 改完文件即可。**不要依赖自己 `git commit`**：沙箱里 `.git` 是只读的，"
        f"提交失败属于正常现象 —— 网关会兜底提交（信息以 `fix(issue-{issue.iid}): ` 开头）。",
        "5. 如果确实需要新增依赖：**不要**改 `pyproject.toml`，在最终报告里写明"
        "「需要新增依赖 X 及理由」，由网关写进 MR 描述交人工判断。",
        "6. 最终消息里请给出：修了什么（带 `文件:行号`）、跑了什么测试、结果数字、"
        "风险与未决（没覆盖的场景、你无法确认的假设）。",
        "",
        "## 结构化报告（必需）",
        "",
        f"结束前把报告写进 `{layout.REPORT_FILENAME}`（相对本目录，即"
        f"`{layout.internal_dir('.')}/{layout.REPORT_FILENAME}`），JSON 对象，字段：",
        "",
        "```json",
        "{",
        '  "summary": "一句话结论",',
        '  "fixes": ["修了什么（带 `文件:行号`）"],',
        '  "tests": ["新增/修改了哪些测试"],',
        '  "test_command": "实际运行的测试命令",',
        '  "test_summary": "测试结果数字（如 12 passed）",',
        '  "not_run": ["没跑的检查"],',
        '  "risks": ["风险与未决"],',
        '  "deps": ["需要新增的依赖（没有就留空数组）"]',
        "}",
        "```",
        "",
        "这个文件只写报告，**不要**提交它（网关已把该目录加入 git 忽略）；网关读不到时"
        "会退化成使用你的最终消息，MR 描述会因此变粗糙。",
    ]

    if mcp_tools:
        # 只读 MCP 是"按需取上下文"的唯一通道（agent 无网络），必须告诉它有哪些工具
        parts += [
            "",
            "## 只读上下文工具（MCP）",
            "",
            TOOLS_HINT.format(
                count=len(mcp_tools), tools=" ".join(f"`{name}`" for name in mcp_tools)
            ),
        ]

    if extra_sections:
        parts += ["", "## 补充上下文", ""]
        parts += [section.strip() for section in extra_sections if section.strip()]

    return "\n".join(parts).rstrip() + "\n"
