"""triage 的 `context.md` 渲染：巡检任务的唯一输入。

与 fix 的 context 不同，这里要求 agent **只读**（不 commit / 不 push），并在
`.mini-gitlab-bot/report.json` 里写结构化结论。所有网关能确定的事实（标签枚举、
组件映射、是否关闭 issue）都不让模型决定。
"""

from __future__ import annotations

from datetime import UTC, datetime

from core import layout
from core.models import IssueContext, Task

MAX_BODY_CHARS = 8000
MAX_COMMENT_CHARS = 3000
MAX_COMMENTS = 10


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n…（已截断，原文 {len(text)} 字符）"


def render_triage_context(
    task: Task,
    issue: IssueContext,
    *,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    labels = "、".join(f"`{item}`" for item in issue.labels) or "（无）"

    parts: list[str] = [
        f"# 任务：排查 GitLab issue #{issue.iid}",
        "",
        "你是仓库维护机器人的巡检阶段。你的产出是**判断 + 证据 + 定位**，不是修复。",
        "",
        "## 元信息",
        "",
        f"- 项目：`{task.project.path}`（project_id={task.project.project_id}）",
        f"- 标题：{issue.title}",
        f"- 作者：@{issue.author}",
        f"- 状态：`{issue.state}`",
        f"- 标签：{labels}",
        f"- 链接：{issue.web_url}",
        f"- 生成时间：{now.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        "## Issue 正文",
        "",
        _clip(issue.description.strip() or "（正文为空）", MAX_BODY_CHARS),
    ]

    if issue.comments:
        parts += ["", f"## 评论（最近 {min(len(issue.comments), MAX_COMMENTS)} 条）", ""]
        for index, comment in enumerate(issue.comments[-MAX_COMMENTS:], start=1):
            parts += [f"### 评论 {index}", "", _clip(comment.strip(), MAX_COMMENT_CHARS), ""]

    parts += [
        "## 你要做的事（按顺序）",
        "",
        "1. **复现**：按 issue 描述在仓库里尝试复现。用离线命令："
        "`UV_OFFLINE=1 uv run --no-sync pytest <相关测试> -q`（依赖已由网关装好，"
        "你**没有网络**）。真跑了就记录命令与结果；环境不具备（要数据库/外部服务）"
        "就如实写 `not_run`，不要编造。",
        "2. **定位**：用 `grep` / `git log -S` / `git blame` 找到最可能的文件与引入提交。"
        "只列**确有必要**的文件（最多 10 个）。",
        "3. **复现固化**（可选）：如果改动很小就能写出失败的回归测试，把测试 patch 写进"
        "结论的 `repro.regression_test_patch`；写不出来就留空。",
        "4. **分类**：判断 `type`（类型）与 `severity`（严重度），只能从下面的枚举里选。",
        "5. **相似 issue**：你**无法访问 GitLab**，只能依据 issue 正文里提到的 `#编号` "
        "以及仓库内的历史线索。没有把握就把 `similar` 留空数组，不要瞎猜。",
        "",
        "## 硬约束（必须遵守）",
        "",
        "1. 你**没有网络**：不要 `uv add` / `pip install` / `uv sync` / `git fetch` / `curl`。",
        "2. **只读**：不要 `git commit`、不要 `git push`、不要改仓库里被跟踪的文件。"
        "需要临时脚本或测试文件就写到 "
        f"`{layout.internal_dir('.')}/{layout.TMP_DIRNAME}/` 下面（已在 git 忽略里）。",
        "3. 运行测试产生的 `__pycache__` / `.pytest_cache` 不会进提交，可以忽略。",
        "4. 不要修改 `.gitlab-ci.yml`、`.env`、密钥文件等受保护路径。",
        "5. 结论是给 issue 作者看的：摘要用中文、像人话，不要贴大段日志。",
        "",
        "## 结构化结论（必需）",
        "",
        f"结束前把 JSON 写进 `{layout.REPORT_FILENAME}`（相对本目录，即 "
        f"`{layout.internal_dir('.')}/{layout.REPORT_FILENAME}`）。字段如下：",
        "",
        "```json",
        "{",
        '  "verdict": "confirmed | not_reproducible | need_info | not_a_bug | duplicate",',
        '  "confidence": 0.0 到 1.0,',
        '  "summary": "一句话结论（中文）",',
        '  "repro": {',
        '    "command": "实际执行的命令",',
        '    "result": "reproduced | failed | not_run",',
        '    "evidence": "关键输出摘要（≤ 10 行）",',
        '    "regression_test_patch": "可选：回归测试 diff"',
        "  },",
        '  "location": {"files": ["路径/文件.py:行号"]},',
        '  "similar": [{"iid": 12, "relation": "duplicate | related", "reason": "…"}],',
        '  "labels": {"type": "缺陷", "severity": "一般", "component": "gateway"},',
        '  "size": {"files": 2, "lines": 40, "modules": 1, "risk": "low", "confidence": 0.8},',
        '  "recommendation": "self_fix | assign | ask_reporter | close_duplicate | no_action"',
        "}",
        "```",
        "",
        "枚举约束（写错会被网关丢弃，整个结论判为无效）：",
        "",
        "- `type`：`缺陷` / `新功能` / `优化` / `文档` / `疑问` / `安全`",
        "- `severity`：`致命` / `严重` / `一般` / `轻微`",
        "- `verdict`：`confirmed`（成立）/ `not_reproducible`（没复现）/ "
        "`need_info`（信息不足）/ `not_a_bug`（不是缺陷）/ `duplicate`（重复）",
        "- `component`：写仓库里的顶层目录名（如 `gateway`）；拿不准写 `其他`",
        "",
        "这个文件只写结论，**不要**提交它（网关已把该目录加入 git 忽略）。",
    ]
    return "\n".join(parts).rstrip() + "\n"
