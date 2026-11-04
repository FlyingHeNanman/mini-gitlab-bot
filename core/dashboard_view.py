"""Dashboard 只读页面的渲染（纯函数，不碰 HTTP / 数据库）。

数据由 `gateway` 采集，这里只负责把 `DashboardData` 变成 HTML：

* 服务端渲染，**零 JS、零构建**；刷新用普通链接，不用 meta refresh；
* 所有来自 GitLab/SQLite 的文本一律 `html.escape`（项目路径、失败原因、事件 detail）；
* 只读：不提供 retry / cancel / 手动触发，这些是第二期操作台的内容。
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from core.models import DashboardStats, ProjectSummary, Task, TaskEvent
from core.states import TaskKind, TaskState, stage_label

KIND_LABELS: dict[TaskKind, str] = {
    TaskKind.FIX: "修复",
    TaskKind.TRIAGE: "巡检",
    TaskKind.EXPLAIN: "解释",
    TaskKind.RELEASE: "Release",
}

STATE_CLASSES: dict[TaskState, str] = {
    TaskState.QUEUED: "queued",
    TaskState.PREPARING: "active",
    TaskState.RUNNING: "active",
    TaskState.VALIDATING: "active",
    TaskState.PUSHING: "active",
    TaskState.OPENING_MR: "active",
    TaskState.FINALIZING: "active",
    TaskState.SUCCEEDED: "ok",
    TaskState.FAILED: "bad",
}

METRICS = (
    ("sweep_runs_total", "Sweep 轮次"),
    ("sweep_triaged_total", "Sweep 补建巡检"),
    ("sweep_resolved_total", "Sweep 回收 issue"),
    ("sweep_errors_total", "Sweep 错误"),
    ("release_total", "Release 处理"),
    ("release_skipped_total", "Release 跳过"),
    ("release_failures_total", "Release 失败"),
)


@dataclass(frozen=True, slots=True)
class DashboardData:
    """一次手动刷新时的只读快照。"""

    now: datetime
    version: str
    worker_enabled: bool
    stalled: bool
    stats: DashboardStats
    projects: Sequence[ProjectSummary]
    recent: Sequence[Task]
    failures: Sequence[Task]
    metrics: Mapping[str, float]
    gitlab_url: str


def render_dashboard(data: DashboardData) -> str:
    """总览页：健康、队列、24h 统计、失败中心、最近任务、项目、运维指标。"""
    health_class, health_title, health_note = _health(data)
    stats = data.stats
    kpis = _kpi_cards(stats)
    recent = _recent_tasks(data.recent, now=data.now, gitlab_url=data.gitlab_url)
    failures = _failure_list(data.failures, now=data.now, gitlab_url=data.gitlab_url)
    projects = _project_table(data.projects, gitlab_url=data.gitlab_url)
    metrics = _metric_cards(data.metrics)
    body = f"""
<header class="top">
  <div class="brand">
    <span class="logo">◈</span>
    <div>
      <h1>mini-gitlab-bot <span class="slash">/</span> Dashboard</h1>
      <p class="sub">只读观测台 · 当前实例 · 手动刷新</p>
    </div>
  </div>
  <div class="actions">
    <span class="chip">v{_e(data.version)}</span>
    <span class="chip">只读</span>
    <a class="btn" href="/dashboard">↻ 刷新数据</a>
  </div>
</header>

<section class="hero {health_class}">
  <div class="hero-main">
    <span class="dot"></span>
    <div>
      <h2>{html.escape(health_title)}</h2>
      <p>{html.escape(health_note)}</p>
    </div>
  </div>
  <div class="hero-time">
    上次刷新<br><b>{_fmt_time(data.now)} UTC</b>
  </div>
</section>

<section>
  <div class="section-head"><h2>运行概况</h2><span>本次进程 · 全量 SQLite</span></div>
  <div class="kpis">{kpis}</div>
</section>

<div class="columns">
  <section class="panel wide">
    <div class="section-head"><h2>最近任务</h2><a href="#tasks">共 {stats.total} 条</a></div>
    {recent}
  </section>
  <section class="panel">
    <div class="section-head"><h2>需要关注</h2><span>最近失败</span></div>
    {failures}
  </section>
</div>

<section id="projects">
  <div class="section-head"><h2>项目</h2><span>按最后活动时间排序 · 最多 50 个</span></div>
  {projects}
</section>

<section id="ops">
  <div class="section-head"><h2>运维指标</h2><span>进程启动以来累计，重启归零</span></div>
  <div class="metrics">{metrics}</div>
</section>

<a id="tasks"></a>
<footer class="foot">
  <span>只读视图，不提供重试 / 取消 / 手动触发</span>
  <span>所有时间 UTC</span>
</footer>
"""
    return _page("Dashboard · mini-gitlab-bot", body)


def render_dashboard_task(
    task: Task,
    *,
    events: Sequence[TaskEvent],
    log_tail: str = "",
    log_lines: int = 0,
    now: datetime,
    gitlab_url: str,
) -> str:
    """任务详情：状态、失败现场、事件时间线与日志尾部。"""
    issue_url = _issue_url(gitlab_url, task.project.path, task.issue_iid)
    project_url = _project_url(gitlab_url, task.project.path)
    kind = KIND_LABELS.get(task.kind, str(task.kind))

    def row(name: str, value: str) -> str:
        return f"<tr><th>{html.escape(name)}</th><td>{value}</td></tr>"

    links = []
    if task.mr_url and task.mr_iid:
        links.append(f'<a href="{html.escape(task.mr_url)}">MR !{task.mr_iid}</a>')
    if task.branch:
        links.append(f'<span class="mono">{html.escape(task.branch)}</span>')
    if task.commit_sha:
        links.append(f"<code>{html.escape(task.commit_sha[:12])}</code>")
    detail = "".join(
        [
            row("任务", f'<span class="mono">{html.escape(task.id)}</span>'),
            row(
                "项目",
                f'<a href="{html.escape(project_url)}">{html.escape(task.project.path)}</a>',
            ),
            row("issue", f'<a href="{html.escape(issue_url)}">#{task.issue_iid}</a>'),
            row("类型", html.escape(kind)),
            row("状态", _badge(task)),
            row(
                "触发",
                f"{html.escape(str(task.trigger_kind))} by @{html.escape(task.trigger_actor)}",
            ),
            row("尝试", f"{task.attempts}/{task.max_attempts}"),
            row("创建", f"{_fmt_time(task.created_at)} UTC"),
            row("结束", f"{_fmt_time(task.finished_at)} UTC" if task.finished_at else "—"),
            row("耗时", _fmt_duration(task.elapsed_sec(now))),
            row("产物", " ".join(links) or "—"),
            row("失败阶段", html.escape(task.failure_stage or "—")),
            row("失败原因", html.escape(task.failure_reason or "—")),
        ]
    )
    evidence = (
        f'<section class="panel"><div class="section-head"><h2>失败证据</h2></div>'
        f'<pre class="mono">{html.escape(task.failure_evidence or "—")}</pre></section>'
        if task.failure_evidence
        else ""
    )
    timeline = _timeline(events)
    tail = (
        f'<section class="panel"><div class="section-head"><h2>日志尾部</h2>'
        f"<span>{log_lines} 行</span></div>"
        f'<pre class="mono">{html.escape(log_tail)}</pre></section>'
        if log_tail
        else '<p class="muted">没有可读的日志文件。</p>'
    )
    body = f"""
<header class="top">
  <div class="brand">
    <span class="logo">◈</span>
    <div>
      <h1>任务 <span class="mono">{html.escape(task.short_id)}</span></h1>
      <p class="sub">{html.escape(kind)} · {html.escape(task.project.path)} · #{task.issue_iid}</p>
    </div>
  </div>
  <div class="actions">
    <a class="btn" href="/dashboard">← 返回 Dashboard</a>
  </div>
</header>

<section class="hero {_state_class(task.state)}">
  <div class="hero-main">
    <span class="dot"></span>
    <div><h2>{html.escape(stage_label(task.state, task.kind))}</h2>
    <p>{html.escape(task.failure_reason or "任务状态已持久化；本页为只读快照。")}</p></div>
  </div>
</section>

<section class="panel"><div class="section-head"><h2>基本信息</h2></div>
<table>{detail}</table></section>

{evidence}
<section class="panel">
  <div class="section-head"><h2>事件时间线</h2><span>{len(events)} 条</span></div>
{timeline}</section>

{tail}

<footer class="foot"><span>只读视图 · 所有时间 UTC</span></footer>
"""
    return _page(f"任务 {task.short_id} · mini-gitlab-bot", body)


# --------------------------------------------------------------------------- #
# 片段
# --------------------------------------------------------------------------- #


def _health(data: DashboardData) -> tuple[str, str, str]:
    stats = data.stats
    if not data.worker_enabled:
        return "bad", "Worker 未启用", "任务会留在队列里，需要启用 WORKER_ENABLED 才能执行。"
    if data.stalled:
        return "bad", "有任务租约过期", "存在超过阈值未续租的任务，请检查 worker 与日志。"
    if stats.failed_since:
        return "warn", "有失败需要关注", f"过去 24h 有 {stats.failed_since} 个任务失败。"
    if stats.queued:
        return "warn", "队列有积压", f"当前有 {stats.queued} 个任务等待 worker。"
    if stats.active:
        return "ok", "正在处理", f"当前有 {stats.active} 个任务在流水线中。"
    return "ok", "运行正常", "队列为空，没有需要处理的积压。"


def _kpi_cards(stats: DashboardStats) -> str:
    items = (
        ("总任务", stats.total, "全部历史"),
        ("排队", stats.queued, "等待 worker"),
        ("执行中", stats.active - stats.queued, "preparing → opening_mr"),
        ("已完成", stats.succeeded, "succeeded"),
        ("失败", stats.failed, "failed"),
        (
            "24h 新增",
            stats.created_since,
            f"成功 {stats.succeeded_since} / 失败 {stats.failed_since}",
        ),
    )
    return "".join(
        f'<div class="kpi"><span class="kpi-label">{html.escape(label)}</span>'
        f'<b class="kpi-value">{value}</b><span class="kpi-note">{html.escape(note)}</span></div>'
        for label, value, note in items
    )


def _recent_tasks(tasks: Sequence[Task], *, now: datetime, gitlab_url: str) -> str:
    if not tasks:
        return '<div class="empty">还没有任务。webhook 或周期扫描入队后会显示在这里。</div>'
    rows = []
    for task in tasks:
        issue_url = _issue_url(gitlab_url, task.project.path, task.issue_iid)
        kind = html.escape(KIND_LABELS.get(task.kind, str(task.kind)))
        rows.append(
            "<tr>"
            f'<td class="num">{_fmt_time(task.created_at)}</td>'
            f'<td><a class="mono" href="/dashboard/task/{html.escape(task.id)}">'
            f"{html.escape(task.short_id)}</a></td>"
            f'<td><span class="kind">{kind}</span></td>'
            f'<td><a href="{html.escape(issue_url)}">{html.escape(task.project.path)}</a>'
            f'<span class="muted"> #{task.issue_iid}</span></td>'
            f"<td>{_badge(task)}</td>"
            f'<td class="num">{html.escape(_fmt_duration(task.elapsed_sec(now)))}</td>'
            f'<td class="reason">{html.escape(_clip(task.failure_reason) or "—")}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>创建 (UTC)</th><th>任务</th><th>类型</th>"
        "<th>项目 / issue</th><th>状态</th><th>耗时</th><th>失败原因</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _failure_list(tasks: Sequence[Task], *, now: datetime, gitlab_url: str) -> str:
    if not tasks:
        return '<div class="empty ok-empty">✓ 暂无失败任务</div>'
    items = []
    for task in tasks:
        issue_url = _issue_url(gitlab_url, task.project.path, task.issue_iid)
        items.append(
            f'<li><a href="/dashboard/task/{html.escape(task.id)}" class="mono">'
            f"{html.escape(task.short_id)}</a> "
            f'<span class="kind">{html.escape(KIND_LABELS.get(task.kind, str(task.kind)))}</span>'
            f'<br><span class="muted">{html.escape(task.project.path)} '
            f'<a href="{html.escape(issue_url)}">#{task.issue_iid}</a> · '
            f"{html.escape(task.failure_stage or 'unknown')} · "
            f"{_fmt_time(task.updated_at)} UTC</span>"
            f'<p class="reason">{html.escape(_clip(task.failure_reason, 140) or "—")}</p></li>'
        )
    return f'<ul class="failure-list">{"".join(items)}</ul>'


def _project_table(projects: Sequence[ProjectSummary], *, gitlab_url: str) -> str:
    if not projects:
        return '<div class="empty">还没有项目数据。任务入队后按项目聚合。</div>'
    rows = []
    for item in projects:
        project_url = _project_url(gitlab_url, item.project.path)
        rows.append(
            "<tr>"
            f'<td><a href="{html.escape(project_url)}">{html.escape(item.project.path)}</a></td>'
            f'<td class="num">{item.total}</td>'
            f'<td class="num">{item.active}</td>'
            f'<td class="num ok-text">{item.succeeded}</td>'
            f'<td class="num bad-text">{item.failed}</td>'
            f'<td class="num">{_fmt_time(item.last_activity)}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>项目</th><th>总任务</th><th>进行中</th>"
        "<th>成功</th><th>失败</th><th>最后活动 (UTC)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _metric_cards(metrics: Mapping[str, float]) -> str:
    cards = []
    for name, label in METRICS:
        value = metrics.get(name, 0.0)
        cards.append(
            f'<div class="metric"><span>{html.escape(label)}</span>'
            f"<b>{_fmt_number(value)}</b></div>"
        )
    return "".join(cards)


def _timeline(events: Sequence[TaskEvent]) -> str:
    if not events:
        return '<div class="empty">没有事件记录。</div>'
    items = []
    for event in events:
        detail = json.dumps(event.detail, ensure_ascii=False, default=str, sort_keys=True)
        items.append(
            f'<li><span class="time">{_fmt_time(event.at)} UTC</span>'
            f'<span class="event-kind">{html.escape(event.kind)}</span>'
            f'<span class="event-detail">{html.escape(_clip(detail, 300))}</span></li>'
        )
    return f'<ul class="timeline">{"".join(items)}</ul>'


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #


def _badge(task: Task) -> str:
    return (
        f'<span class="badge {_state_class(task.state)}">'
        f"{html.escape(stage_label(task.state, task.kind))}</span>"
    )


def _state_class(state: TaskState) -> str:
    return STATE_CLASSES.get(state, "active")


def _fmt_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%m-%d %H:%M:%S") if value else "—"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _clip(text: str | None, limit: int = 90) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _e(value: object) -> str:
    return html.escape(str(value))


def _project_url(gitlab_url: str, path: str) -> str:
    base = gitlab_url.rstrip("/")
    return f"{base}/{quote(path, safe='/')}"


def _issue_url(gitlab_url: str, path: str, iid: int) -> str:
    return f"{_project_url(gitlab_url, path)}/-/issues/{iid}"


# --------------------------------------------------------------------------- #
# 页面骨架
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: light dark; --bg:#f6f8fa; --panel:#fff; --text:#1f2328; --muted:#59636e;
        --line:#d0d7de; --soft:#eef1f4; --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e;
        --blue:#0969da; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --panel:#161b22; --text:#e6edf3; --muted:#8b949e; --line:#30363d;
          --soft:#21262d; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --blue:#58a6ff; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; font:14px/1.55 -apple-system,"Segoe UI","Noto Sans CJK SC",
       sans-serif; background:var(--bg); color:var(--text); }
h1,h2 { margin:0; }
h1 { font-size:19px; font-weight:650; letter-spacing:-.01em; }
h2 { font-size:15px; font-weight:650; }
a { color:var(--blue); text-decoration:none; }
a:hover { text-decoration:underline; }
.top { display:flex; align-items:flex-start; justify-content:space-between; gap:16px;
       flex-wrap:wrap; padding-bottom:16px; border-bottom:1px solid var(--line); }
.brand { display:flex; align-items:center; gap:12px; min-width:0; }
.logo { display:grid; place-items:center; width:34px; height:34px; border-radius:9px;
        background:linear-gradient(135deg,#0969da,#1a7f37); color:#fff; font-size:17px; flex:none; }
.slash { color:var(--muted); font-weight:400; }
.sub { margin:3px 0 0; color:var(--muted); font-size:12.5px; }
.actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.chip { font-size:12px; padding:2px 9px; border-radius:999px; background:var(--soft);
        color:var(--muted); white-space:nowrap; }
.btn { font-size:12.5px; padding:5px 11px; border:1px solid var(--line); border-radius:7px;
       background:var(--panel); color:var(--text); white-space:nowrap; }
.btn:hover { background:var(--soft); text-decoration:none; }
.hero { display:flex; align-items:center; justify-content:space-between; gap:16px;
        background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--blue);
        border-radius:10px; padding:14px 16px; margin:18px 0 22px; }
.hero.ok { border-left-color:var(--ok); }
.hero.warn { border-left-color:var(--warn); }
.hero.bad { border-left-color:var(--bad); }
.hero-main { display:flex; align-items:center; gap:12px; }
.hero .dot { width:10px; height:10px; border-radius:50%; background:var(--blue);
             box-shadow:0 0 0 5px color-mix(in srgb,var(--blue) 12%,transparent); flex:none; }
.hero.ok .dot { background:var(--ok);
                 box-shadow:0 0 0 5px color-mix(in srgb,var(--ok) 12%,transparent); }
.hero.warn .dot { background:var(--warn);
                   box-shadow:0 0 0 5px color-mix(in srgb,var(--warn) 12%,transparent); }
.hero.bad .dot { background:var(--bad);
                  box-shadow:0 0 0 5px color-mix(in srgb,var(--bad) 12%,transparent); }
.hero p { margin:2px 0 0; color:var(--muted); font-size:12.5px; }
.hero-time { text-align:right; color:var(--muted); font-size:12px; white-space:nowrap; }
.hero-time b { color:var(--text); font-weight:600; }
section { margin:24px 0; }
.section-head { display:flex; align-items:baseline; justify-content:space-between; gap:12px;
                margin-bottom:9px; }
.section-head span, .section-head a:not(.btn) { color:var(--muted); font-size:12px; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; }
.kpi { background:var(--panel); border:1px solid var(--line); border-radius:10px;
       padding:12px 14px; }
.kpi-label { color:var(--muted); font-size:12px; }
.kpi-value { display:block; font-size:25px; line-height:1.25; font-weight:650;
             letter-spacing:-.02em; }
.kpi-note { color:var(--muted); font-size:11.5px; }
.columns { display:grid; grid-template-columns:minmax(0,1.65fr) minmax(280px,.85fr); gap:16px;
           align-items:start; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
.panel.wide { min-width:0; overflow:hidden; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th { text-align:left; color:var(--muted); font-weight:600; border-bottom:1px solid var(--line);
     padding:7px 8px; white-space:nowrap; }
td { border-bottom:1px solid var(--line); padding:8px; vertical-align:top; }
tr:last-child td { border-bottom:0; }
.num { white-space:nowrap; color:var(--muted); font-variant-numeric:tabular-nums; }
.mono, code { font-family:ui-monospace,SFMono-Regular,"SF Mono",Consolas,monospace;
              font-size:12px; }
.muted { color:var(--muted); }
.reason { color:var(--muted); max-width:260px; overflow-wrap:anywhere; }
.kind { display:inline-block; font-size:11px; padding:1px 6px; border-radius:5px;
        background:var(--soft); color:var(--muted); white-space:nowrap; }
.badge { display:inline-block; font-size:11.5px; padding:2px 8px; border-radius:999px;
         background:var(--soft); color:var(--muted); white-space:nowrap; }
.badge.ok { background:color-mix(in srgb,var(--ok) 14%,transparent); color:var(--ok); }
.badge.bad { background:color-mix(in srgb,var(--bad) 14%,transparent); color:var(--bad); }
.badge.warn { background:color-mix(in srgb,var(--warn) 14%,transparent); color:var(--warn); }
.badge.active { background:color-mix(in srgb,var(--blue) 14%,transparent); color:var(--blue); }
.badge.queued { background:var(--soft); color:var(--muted); }
.ok-text { color:var(--ok); } .bad-text { color:var(--bad); }
.failure-list { list-style:none; margin:0; padding:0; }
.failure-list li { padding:9px 0; border-bottom:1px solid var(--line); }
.failure-list li:last-child { border-bottom:0; padding-bottom:0; }
.failure-list p { margin:3px 0 0; font-size:12px; }
.empty { padding:22px 12px; text-align:center; color:var(--muted); font-size:13px;
         border:1px dashed var(--line); border-radius:9px; }
.ok-empty { color:var(--ok); border-color:color-mix(in srgb,var(--ok) 35%,var(--line)); }
.metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
.metric { display:flex; align-items:center; justify-content:space-between; gap:10px;
          background:var(--panel); border:1px solid var(--line); border-radius:9px;
          padding:10px 12px; }
.metric span { color:var(--muted); font-size:12px; }
.metric b { font-size:18px; }
.timeline { list-style:none; margin:0; padding:0; }
.timeline li { display:grid; grid-template-columns:135px 110px minmax(0,1fr); gap:10px;
               padding:7px 0; border-bottom:1px solid var(--line); font-size:12px; }
.timeline li:last-child { border-bottom:0; }
.timeline .time { color:var(--muted); white-space:nowrap; }
.timeline .event-kind { font-weight:600; }
.timeline .event-detail { color:var(--muted); overflow-wrap:anywhere; }
pre { margin:0; padding:12px; background:var(--bg); border:1px solid var(--line); border-radius:8px;
      overflow:auto; max-height:460px; white-space:pre-wrap; overflow-wrap:anywhere; }
table th:first-child, table td:first-child { padding-left:0; }
table th:last-child, table td:last-child { padding-right:0; }
.foot { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-top:30px;
        padding-top:12px; border-top:1px solid var(--line); color:var(--muted); font-size:11.5px; }
@media (max-width: 900px) {
  .columns { grid-template-columns:1fr; }
  .recent-table { overflow-x:auto; }
}
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )
