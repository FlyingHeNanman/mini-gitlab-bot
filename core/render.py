"""文案渲染：MR 描述 + 状态评论（单条原地编辑）。

措辞定案见 `docs/templates.md`（uv release-notes 风格：分类式小节 + 风险与未决分区）。
**改这里的文案时请同步那份文档**，否则两处会漂移。

所有状态评论首行都是 `<!-- mini-gitlab-bot:status <task_id> -->`，
网关靠它找到"自己那条评论"做原地编辑（PLAN §3.1 ⑩）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.models import IssueContext, MergeRequestRef, Task
from core.states import NORMAL_ORDER, STAGE_LABELS, TaskState

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

STATUS_MARKER_PREFIX = "mini-gitlab-bot:status"
BOT_SIGNATURE = "<sub>机器人原地更新本条评论，请勿手动编辑。</sub>"

#: 状态评论里展示的阶段（不含 succeeded —— 它是结果，不是阶段）
PIPELINE_STAGES: tuple[TaskState, ...] = tuple(
    s for s in NORMAL_ORDER if s is not TaskState.SUCCEEDED
)

MR_TITLE_PREFIX = "Draft: "
MAX_TITLE_CHARS = 100


def status_marker(task_id: str) -> str:
    return f"<!-- {STATUS_MARKER_PREFIX} {task_id} -->"


def extract_status_marker(body: str) -> str | None:
    """从评论正文里取出 marker 行（Notifier 靠它做原地编辑）。"""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"<!-- {STATUS_MARKER_PREFIX}") and stripped.endswith("-->"):
            return stripped
    return None


def mr_title(task: Task, *, issue_title: str) -> str:
    """GitLab 的 draft 只能靠**标题前缀**表达（API 无 draft 参数，`WIP:` 已废弃）。"""
    title = issue_title.strip() or f"issue #{task.issue_iid}"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return f"{MR_TITLE_PREFIX}fix(issue-{task.issue_iid}): {title}"


def stage_label(state: TaskState | str | None) -> str:
    if isinstance(state, str):
        try:
            state = TaskState(state)
        except ValueError:
            return state
    return STAGE_LABELS.get(state, str(state))


def _hm(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%H:%M:%S") if moment else "—"


def _ymd_hm(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if moment else "—"


def _bullets(items: Sequence[str], *, empty: str) -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def _stage_table(task: Task) -> str:
    """阶段表。每阶段耗时需要单独的时间戳表，留到 M3；这里只给"最后更新时刻"。"""
    try:
        current_index = PIPELINE_STAGES.index(TaskState(task.state))
    except ValueError:
        current_index = len(PIPELINE_STAGES)

    rows = ["| 阶段 | 状态 | 时间 |", "| --- | --- | --- |"]
    for index, stage in enumerate(PIPELINE_STAGES):
        if index < current_index:
            rows.append(f"| {STAGE_LABELS[stage]} | ✅ | — |")
        elif index == current_index:
            rows.append(f"| {STAGE_LABELS[stage]} | 🔄 进行中 | {_hm(task.updated_at)} |")
        else:
            rows.append(f"| {STAGE_LABELS[stage]} | ⏳ | — |")
    return "\n".join(rows)


def _meta_line(task: Task, *, now: datetime) -> str:
    bits = [f"触发人 @{task.trigger_actor}", f"任务 `{task.id}`"]
    if task.branch:
        bits.append(f"分支 `{task.branch}`")
    if task.commit_sha:
        bits.append(f"提交 `{task.commit_sha[:12]}`")
    bits.append(f"耗时 {task.elapsed_sec(now):.0f}s")
    return " · ".join(bits)


# --------------------------------------------------------------------------- #
# 状态评论
# --------------------------------------------------------------------------- #


def render_running(task: Task, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return "\n".join(
        [
            status_marker(task.id),
            f"## 🤖 ai-fix · 运行中（{stage_label(task.state)}）",
            "",
            _stage_table(task),
            "",
            _meta_line(task, now=now),
            BOT_SIGNATURE,
        ]
    )


def render_succeeded(task: Task, *, mr: MergeRequestRef, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    fixes = task.report_list("fixes")
    risks = task.report_list("risks")
    return "\n".join(
        [
            status_marker(task.id),
            "## ✅ ai-fix · 已完成",
            "",
            f"→ draft MR !{mr.iid}（Closes #{task.issue_iid}）：{mr.title}",
            f"→ {mr.url}",
            "",
            "**改动**",
            _bullets(fixes, empty="（agent 未给出改动摘要）"),
            "",
            f"**验证**：{task.report_text('test_summary', '（无测试结果）')}",
            "",
            "**风险与未决**",
            _bullets(risks, empty="✅ 未发现新增风险"),
            "",
            _meta_line(task, now=now),
            BOT_SIGNATURE,
        ]
    )


def render_failed(
    task: Task,
    *,
    log_tail: str = "",
    log_lines: int = 0,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    lines = [
        status_marker(task.id),
        "## ❌ ai-fix · 失败",
        "",
        f"**阶段**：{stage_label(task.failure_stage)}",
        f"**原因**：{task.failure_reason or '（未记录）'}",
        f"**判定依据**：{task.failure_evidence or '（未记录）'}",
        f"**分支**：`{task.branch or '未创建'}`（{_push_state(task)}）",
        f"**工作目录**：`{task.workdir or '未创建'}`",
    ]
    if log_tail:
        lines += [
            "",
            f"<details><summary>日志尾部（{log_lines} 行，已截断）</summary>",
            "",
            "````",
            log_tail.rstrip(),
            "````",
            "",
            "</details>",
        ]
    lines += [
        "",
        "**如何重试**",
        "- 重新打一次 `ai-fix` 标签，或评论 `/ai-fix retry`",
        f"- 若需人工接管：{_takeover_hint(task)}",
        "",
        _meta_line(task, now=now),
        BOT_SIGNATURE,
    ]
    return "\n".join(lines)


def _push_state(task: Task) -> str:
    if task.commit_sha and task.mr_iid:
        return "已推送并已开 MR"
    if task.commit_sha:
        return "已提交，未必已推送"
    return "未推送"


def _takeover_hint(task: Task) -> str:
    if task.branch:
        return (
            f"分支 `{task.branch}` 可能已存在，人工可直接在其上继续；"
            f"任务 `{task.id}` 已终结，重试会新建任务。"
        )
    return f"任务 `{task.id}` 已终结，重试会新建任务并重新准备环境。"


# --------------------------------------------------------------------------- #
# MR 描述
# --------------------------------------------------------------------------- #


def render_mr_description(
    task: Task,
    *,
    issue: IssueContext,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    fixes = task.report_list("fixes")
    others = task.report_list("others")
    tests = task.report_list("tests")
    risks = task.report_list("risks")
    deps = task.report_list("deps")

    risk_lines = [f"⚠️ {item}" for item in risks]
    risk_lines += [
        f"⚠️ 需要新增依赖：`{dep}`（沙箱无网络，未安装、未写入 `pyproject.toml`）→ **请人工判断**"
        for dep in deps
    ]
    test_command = task.report_text("test_command", "UV_OFFLINE=1 uv run --no-sync pytest -q")
    test_summary = task.report_text("test_summary", "（未运行）")
    test_duration = task.report_text("test_duration")
    duration_suffix = f"，耗时 {test_duration}" if test_duration else ""
    # agent 自己写的一句话摘要（report.json 的 summary）：人工审批第一眼要看的东西
    summary = task.report_text("summary")
    summary_block = [f"**摘要**：{summary}", ""] if summary else []

    return "\n".join(
        [
            "## 概要",
            f"Closes #{task.issue_iid}",
            "",
            f"由 `mini-gitlab-bot` 自动生成（draft，需人工审批）。"
            f"触发：@{task.trigger_actor} 于 {_ymd_hm(task.created_at)} 打上 `ai-fix` 标签。",
            f"任务：`{task.id}`",
            *summary_block,
            "",
            "## 改动",
            "### 修复",
            _bullets(fixes, empty="（未提供改动摘要）"),
            "### 重构 / 其他",
            _bullets(others, empty="（无）"),
            "### 测试",
            _bullets(tests, empty="（无新增测试）"),
            "",
            "## 验证",
            f"- 命令：`{test_command}`",
            f"- 结果：`{test_summary}`{duration_suffix}",
            f"- 未运行：{'、'.join(task.report_list('not_run')) or '（无）'}",
            "",
            "## 风险与未决",
            _bullets(risk_lines, empty="✅ 未发现新增风险"),
            "",
            "## Agent 元信息",
            "| 项 | 值 |",
            "| --- | --- |",
            f"| 内核 | {task.report_text('agent_version', 'codex（版本未上报）')} |",
            f"| 任务 | `{task.id}` |",
            f"| 分支 | `{task.branch or '—'}` |",
            f"| 提交 | `{(task.commit_sha or '—')[:12]}` |",
            f"| 耗时 | {task.elapsed_sec(now):.0f}s |",
            f"| issue | #{issue.iid} {issue.web_url} |",
            "",
            "<sub>自动生成 · 合并前请人工审查</sub>",
        ]
    )
