"""合并后「解释响应」：不 clone、不跑测试，只根据 diff 与 issue 生成说明。

marker 按 issue 号（`<!-- mini-gitlab-bot:explain #8 -->`），同一 issue 的多次合并
原地更新同一条评论。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from core import layout
from core.models import IssueContext, MergeRequestContext, Task
from core.render import BOT_SIGNATURE

EXPLAIN_MARKER_PREFIX = "mini-gitlab-bot:explain"
MAX_DIFF_FILES = 50
MAX_DIFF_LINES = 2000
MAX_DESCRIPTION_CHARS = 4000


def explain_marker(issue_iid: int) -> str:
    return f"<!-- {EXPLAIN_MARKER_PREFIX} #{issue_iid} -->"


@dataclass(frozen=True, slots=True)
class ClippedDiff:
    text: str
    files: int
    lines: int
    truncated: bool


def clip_diff(diff: str) -> ClippedDiff:
    """按文件数 / 行数截断 diff；超限时在末尾注明。"""
    files = diff.count("\n--- ") + (1 if diff.startswith("--- ") else 0)
    lines = diff.count("\n") + (1 if diff and not diff.endswith("\n") else 0)
    if files <= MAX_DIFF_FILES and lines <= MAX_DIFF_LINES:
        return ClippedDiff(diff, files, lines, False)

    kept: list[str] = []
    kept_lines = 0
    for block in diff.split("\n--- ")[:MAX_DIFF_FILES]:
        if kept_lines >= MAX_DIFF_LINES:
            break
        block = block if not kept else "--- " + block
        block_lines = block.count("\n") + 1
        if kept_lines + block_lines > MAX_DIFF_LINES:
            block = "\n".join(block.splitlines()[: max(0, MAX_DIFF_LINES - kept_lines)])
            block_lines = block.count("\n") + 1
        kept.append(block)
        kept_lines += block_lines
    text = "\n".join(kept)
    return ClippedDiff(
        text + "\n\n…（diff 已截断：仅解释主要改动）",
        files,
        lines,
        True,
    )


def _ymd_hm(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if moment else "—"


def render_explain_context(
    task: Task,
    issue: IssueContext,
    mr: MergeRequestContext,
    *,
    triage_payload: dict | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    diff = clip_diff(mr.diff)
    triage_block = ""
    if triage_payload:
        verdict = triage_payload.get("verdict")
        confidence = triage_payload.get("confidence")
        component = triage_payload.get("component")
        files = ", ".join(triage_payload.get("files") or []) or "（无）"
        triage_block = (
            "\n## 巡检结论（供解释参考）\n\n"
            f"- 结论：`{verdict}`（置信度 {confidence}）\n"
            f"- 组件：`{component}`\n"
            f"- 命中文件：{files}\n"
        )

    return (
        "\n".join(
            [
                f"# 任务：解释 MR !{mr.iid} 如何解决 issue #{issue.iid}",
                "",
                "你是代码合并后的解释者。**只读 API 提供的 MR diff 与 issue**，不要 clone、",
                "不要运行测试、不要修改任何文件。",
                "",
                "## MR",
                "",
                f"- 标题：{mr.title}",
                f"- 分支：`{mr.source_branch}` → `{mr.target_branch}`",
                f"- 合并提交：`{mr.merge_commit_sha}`",
                f"- 链接：{mr.web_url}",
                "",
                "## Issue",
                "",
                f"- 标题：{issue.title}",
                f"- 正文：{(issue.description or '（空）')[:MAX_DESCRIPTION_CHARS]}",
                triage_block,
                "## Diff（已预取）",
                "",
                "```diff",
                diff.text,
                "```",
                "",
                "## 输出要求",
                "",
                "把解释写进**最终消息**（markdown，不要整段包在代码块里）：",
                "",
                "1. 改了哪些文件 / 函数；",
                "2. 为什么这样改（根因 → 方案，用大白话）；",
                "3. 与 issue 描述对应：「你报的现象 X 由 Y 引起，现在 Z」；",
                "4. 若给了巡检结论，说明原复现路径现在走什么分支；",
                "5. 明确提醒：本说明由模型根据 diff 生成，未经二次复跑验证。",
                "",
                "不要输出行动建议以外的内容，不要编造 diff 里不存在的改动。",
                "",
                f"<sub>工作目录：{layout.internal_dir('.')}；任务 {task.id}；生成于 "
                f"{_ymd_hm(now)} UTC</sub>",
            ]
        ).rstrip()
        + "\n"
    )


def render_explain_comment(
    task: Task,
    issue: IssueContext,
    mr: MergeRequestContext,
    explanation: str,
    *,
    truncated: bool = False,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    body = [
        explain_marker(issue.iid),
        "## 📘 合并说明（自动生成）",
        "",
        explanation.strip() or "（模型没有给出解释）",
    ]
    if truncated:
        body += ["", "> 注：diff 较大，本说明只覆盖主要改动。"]
    body += [
        "",
        "### 关联",
        "",
        f"- MR !{mr.iid} · 合并提交 `{(mr.merge_commit_sha or '')[:12]}`",
        f"- {mr.web_url}",
        "",
        "<sub>本说明由模型根据 diff 生成，未经二次复跑验证。</sub>",
        BOT_SIGNATURE,
    ]
    return "\n".join(body)


def render_explain_failure(
    task: Task,
    issue: IssueContext,
    *,
    reason: str,
    now: datetime | None = None,
) -> str:
    return "\n".join(
        [
            explain_marker(issue.iid),
            "## ⚠️ 合并说明生成失败",
            "",
            f"- 原因：{reason}",
            f"- 任务：`{task.id}` · {_ymd_hm(now or datetime.now(UTC))} UTC",
            "",
            BOT_SIGNATURE,
        ]
    )
