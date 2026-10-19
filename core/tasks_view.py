"""`/tasks` 只读页面的渲染（纯函数，不碰 HTTP / 数据库）。

为什么放在 `core/`：这是**纯字符串拼装**，可以单测（尤其是 HTML 转义 —— issue 标题、
项目路径都来自外部输入，直接拼进 HTML 就是 XSS）。

设计取舍：
* 服务端渲染，**零 JS、零构建**：一个内部状态页不值得引入前端工程链。
* 自动刷新用 `<meta http-equiv="refresh">`，不用 fetch。
* 只读：不提供 retry / cancel（那属于"操作"，得单独设计权限与审计）。
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime

from core.models import Task
from core.states import TaskState

#: 列表页每行的原因截断长度（完整内容在详情页）
REASON_CLIP = 80


@dataclass(frozen=True, slots=True)
class TaskFilters:
    state: str = ""
    limit: int = 50


# --------------------------------------------------------------------------- #
# 数据视图（JSON 与 HTML 共用，保证两种出口内容一致）
# --------------------------------------------------------------------------- #


def task_to_dict(task: Task) -> dict[str, object]:
    """一条任务的对外表示。**不含** report 正文与 workdir（可能含敏感路径）。"""
    duration = _duration_sec(task)
    return {
        "id": task.id,
        "short_id": task.short_id,
        "project": task.project.path,
        "project_id": task.project.project_id,
        "issue_iid": task.issue_iid,
        "state": task.state.value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "trigger_kind": str(task.trigger_kind),
        "trigger_actor": task.trigger_actor,
        "branch": task.branch,
        "base_branch": task.base_branch,
        "commit_sha": task.commit_sha,
        "mr_iid": task.mr_iid,
        "mr_url": task.mr_url,
        "failure_stage": task.failure_stage,
        "failure_reason": task.failure_reason,
        "log_path": task.log_path,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "finished_at": _iso(task.finished_at),
        "duration_sec": duration,
    }


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _duration_sec(task: Task) -> float | None:
    end = task.finished_at or (datetime.now(UTC) if task.is_terminal else None)
    if end is None:
        return None
    return max(0.0, (end - task.created_at).total_seconds())


def _fmt_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%m-%d %H:%M:%S") if value else "—"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _clip(text: str | None, limit: int = REASON_CLIP) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font: 14px/1.5 -apple-system, "Segoe UI", "Noto Sans CJK SC",
       sans-serif; background: #fafafa; color: #1f2328; }
h1, h2 { margin: 0; }
h2 { font-size: 15px; margin: 24px 0 8px; }
.top { display: flex; align-items: center; justify-content: space-between; gap: 16px;
       flex-wrap: wrap; padding-bottom: 14px; border-bottom: 1px solid #d0d7de; }
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #1a7f37; flex: none;
       box-shadow: 0 0 0 4px rgba(26, 127, 55, 0.12); }
.brand h1 { font-size: 17px; font-weight: 650; letter-spacing: -0.01em; white-space: nowrap; }
.brand .slash { color: #8b949e; }
.chip { font-size: 12.5px; padding: 2px 10px; border-radius: 999px; background: #eef1f4;
        color: #59636e; white-space: nowrap; }
.meta { font-size: 12.5px; color: #59636e; display: flex; gap: 14px; flex-wrap: wrap; }
.toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 16px 0 12px; }
.toolbar .spacer { flex: 1; }
.stats { display: inline-flex; gap: 12px; align-items: center; }
.stats .ok { color: #1a7f37; }
.stats .bad { color: #cf222e; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
form { margin: 16px 0; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
input, select, button { padding: 6px 10px; border: 1px solid #d0d7de; border-radius: 6px;
                        font: inherit; background: #ffffff; color: #1f2328; }
button { cursor: pointer; background: #f6f8fa; }
table { width: 100%; border-collapse: collapse; border: 1px solid #d0d7de; border-radius: 8px;
        overflow: hidden; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #d0d7de;
         vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; white-space: nowrap; }
tr:nth-child(even) td { background: #f6f8fa; }
tr:last-child td { border-bottom: none; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
pre { margin: 0; padding: 12px; border-radius: 8px; overflow: auto; max-height: 480px;
      background: #f6f8fa; border: 1px solid #d0d7de; }
.muted { color: #59636e; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px;
         font-weight: 600; }
.badge-succeeded { background: #dafbe1; color: #1a7f37; }
.badge-failed { background: #ffebe9; color: #cf222e; }
.badge-active { background: #fff8c5; color: #9a6700; }
.empty { padding: 32px; text-align: center; color: #59636e; }

/* 深色模式必须放在所有基础规则**之后**：同优先级下靠后者胜出。
   之前把它放在前面，button/th 的基础背景色又把深色覆盖回去，
   结果就是"白底 + 浅色字"看不见（真实 bug）。 */
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  a { color: #7aa2f7; }
  .top { border-bottom-color: #30363d; }
  .chip { background: #21262d; color: #8b949e; }
  .stats .ok { color: #7ee787; }
  .stats .bad { color: #ffa198; }
  .meta { color: #8b949e; }
  .brand .slash { color: #6e7681; }
  table { border-color: #30363d; }
  th, td { border-bottom-color: #30363d; }
  th { background: #161b22; }
  tr:nth-child(even) td { background: #11161d; }
  input, select, button { background: #161b22; color: #e6edf3; border-color: #30363d; }
  button { background: #21262d; }
  button:hover { background: #30363d; }
  pre { background: #161b22; border-color: #30363d; color: #e6edf3; }
  .muted { color: #8b949e; }
  .empty { color: #8b949e; }
  .badge-succeeded { background: #12261e; color: #7ee787; }
  .badge-failed { background: #3d1214; color: #ffa198; }
  .badge-active { background: #2b2411; color: #e3b341; }
}
"""


def _badge(task: Task) -> str:
    if task.state is TaskState.SUCCEEDED:
        css = "badge-succeeded"
    elif task.state is TaskState.FAILED:
        css = "badge-failed"
    else:
        css = "badge-active"
    return f'<span class="badge {css}">{html.escape(task.state.value)}</span>'


def _header(*, section: str, right: str = "") -> str:
    return (
        '<div class="top"><div class="brand">'
        '<span class="dot"></span>'
        "<h1>mini-gitlab-bot</h1>"
        '<span class="slash">/</span>'
        f'<span class="chip">{section}</span>'
        f"</div>{f'<div class="meta">{right}</div>' if right else ''}</div>"
    )


def _page(title: str, body: str, *, refresh_sec: int | None = None) -> str:
    refresh = f'<meta http-equiv="refresh" content="{refresh_sec}">' if refresh_sec else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"{refresh}<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_tasks_page(
    tasks: list[Task],
    *,
    filters: TaskFilters,
    token: str,
    refresh_sec: int | None = 10,
    now: datetime | None = None,
) -> str:
    """任务列表页。`token` 会拼进过滤表单（否则刷新后 401）。"""
    now = now or datetime.now(UTC)
    options = ['<option value="">全部状态</option>']
    for state in TaskState:
        selected = " selected" if state.value == filters.state else ""
        options.append(
            f'<option value="{html.escape(state.value)}"{selected}>'
            f"{html.escape(state.value)}</option>"
        )
    ok_count = sum(1 for task in tasks if task.state is TaskState.SUCCEEDED)
    bad_count = sum(1 for task in tasks if task.state is TaskState.FAILED)
    running = len(tasks) - ok_count - bad_count
    stats = (
        f"<span><b>{len(tasks)}</b> 条</span>"
        f'<span class="ok">✓ <b>{ok_count}</b></span>'
        f'<span class="bad">✕ <b>{bad_count}</b></span>'
        + (f"<span>▶ <b>{running}</b></span>" if running else "")
    )
    form = (
        '<form class="toolbar" method="get" action="/tasks">'
        f'<input type="hidden" name="token" value="{html.escape(token)}">'
        f'<select name="state" aria-label="状态">{"".join(options)}</select>'
        f'<input type="number" name="limit" min="1" max="500" value="{filters.limit}"'
        ' aria-label="条数" style="width: 90px">'
        '<button type="submit">过滤</button>'
        '<span class="spacer"></span>'
        f'<span class="meta"><span class="stats">{stats}</span>'
        f"<span>{now.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC"
        f"{f' · {refresh_sec}s 刷新' if refresh_sec else ''}</span></span>"
        "</form>"
    )

    if tasks:
        rows = []
        for task in tasks:
            detail = f"/tasks/{html.escape(task.id)}?token={html.escape(token)}"
            mr = (
                f'<a href="{html.escape(task.mr_url)}">!{task.mr_iid}</a>'
                if task.mr_url and task.mr_iid
                else "—"
            )
            branch = f'<span class="mono">{html.escape(task.branch)}</span>' if task.branch else "—"
            rows.append(
                "<tr>"
                f'<td class="num">{html.escape(_fmt_time(task.created_at))}</td>'
                f'<td><a href="{detail}" class="mono">{html.escape(task.short_id)}</a></td>'
                f"<td>{html.escape(task.project.path)}<br>"
                f'<span class="muted">#{task.issue_iid}</span></td>'
                f"<td>{_badge(task)}</td>"
                f'<td class="num">{html.escape(task.failure_stage or "—")}</td>'
                f'<td title="{html.escape(task.failure_reason or "")}">'
                f"{html.escape(_clip(task.failure_reason) or '—')}</td>"
                f'<td class="num">{html.escape(_fmt_duration(_duration_sec(task)))}</td>'
                f"<td>{mr}<br>{branch}</td>"
                "</tr>"
            )
        table = (
            "<table><thead><tr>"
            "<th>时间(UTC)</th><th>任务</th><th>项目 / issue</th><th>状态</th>"
            "<th>失败阶段</th><th>原因</th><th>耗时</th><th>产物</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        table = '<div class="empty">没有符合条件的任务</div>'

    body = _header(section="任务") + form + table
    return _page("任务 · mini-gitlab-bot", body, refresh_sec=refresh_sec)


def render_task_detail(task: Task, *, token: str, log_tail: str = "", log_lines: int = 0) -> str:
    """详情页：完整失败原因/证据 + codex 日志尾部。"""

    def row(name: str, value: str) -> str:
        return f"<tr><th>{html.escape(name)}</th><td>{value}</td></tr>"

    links = ""
    if task.mr_url:
        links += f'<a href="{html.escape(task.mr_url)}">MR !{task.mr_iid}</a> '
    if task.branch:
        links += f'<span class="mono">{html.escape(task.branch)}</span>'
    detail = "".join(
        [
            row("任务", f'<span class="mono">{html.escape(task.id)}</span>'),
            row("项目", html.escape(task.project.path)),
            row("issue", f"#{task.issue_iid}"),
            row("状态", _badge(task)),
            row(
                "触发",
                f"{html.escape(str(task.trigger_kind))} by @{html.escape(task.trigger_actor)}",
            ),
            row("尝试", f"{task.attempts}/{task.max_attempts}"),
            row("创建", html.escape(_fmt_time(task.created_at))),
            row("结束", html.escape(_fmt_time(task.finished_at))),
            row("耗时", html.escape(_fmt_duration(_duration_sec(task)))),
            row("产物", links or "—"),
            row("失败阶段", html.escape(task.failure_stage or "—")),
            row("失败原因", html.escape(task.failure_reason or "—")),
            row(
                "失败证据",
                f'<pre class="mono">{html.escape(task.failure_evidence or "—")}</pre>',
            ),
            row("日志路径", f'<span class="mono">{html.escape(task.log_path or "—")}</span>'),
        ]
    )
    tail = (
        f'<h2>日志尾部（{log_lines} 行）</h2><pre class="mono">{html.escape(log_tail)}</pre>'
        if log_tail
        else '<p class="muted">没有可读的日志文件。</p>'
    )
    body = (
        _header(section=f"任务 {html.escape(task.short_id)}", right=_badge(task))
        + f'<div class="toolbar">'
        f'<a class="chip" href="/tasks?token={html.escape(token)}">← 返回列表</a>'
        f"</div>"
        f"<table>{detail}</table>{tail}"
    )
    return _page(f"任务 {task.short_id} · mini-gitlab-bot", body)
