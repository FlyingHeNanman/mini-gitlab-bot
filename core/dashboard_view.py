"""Dashboard 只读页面的渲染（纯函数，不碰 HTTP / 数据库）。

视觉规范以仓库根目录的 `DESIGN.md` 为准：Apple 式产品画廊语言——

* 深黑 global nav + parchment 磨砂 sub-nav；
* 白色 / parchment / 近黑 tile 交替，靠表面色变化分段，不靠边框或阴影；
* Action Blue 是唯一的交互强调色，正文 17px，display 600 + 负字距；
* 全圆角 pill 主 CTA，按压 `scale(.95)`；没有装饰渐变，没有卡片阴影。

数据由 `gateway` 采集，这里只负责把 `DashboardData` 变成 HTML。所有来自
GitLab / SQLite 的文本一律 `html.escape`；只读，不提供重试 / 取消 / 手动触发。
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
    body = f"""
{_global_nav(data.version)}
{_sub_nav()}

<main>
  <section class="hero">
    <div class="wrap hero-inner">
      <p class="hero-kicker">
        <span class="status-dot {health_class}"></span>{html.escape(health_title)}
      </p>
      <h1>运维观测台</h1>
      <p class="hero-lead">{html.escape(health_note)}</p>
      <div class="hero-actions">
        <a class="button-primary" href="#failures">查看需要关注</a>
        <a class="button-secondary" href="#recent">查看最近任务</a>
      </div>
      <div class="hero-meta">
        <span>上次刷新 {_fmt_time(data.now)} UTC</span>
        <span>只读 · 当前实例 SQLite</span>
      </div>
    </div>
  </section>

  <section class="section section-parchment" id="overview">
    <div class="wrap">
      <div class="section-heading">
        <p class="eyebrow">运行概况</p>
        <h2>机器人现在怎么样</h2>
        <p class="section-caption">
          全部历史与最近 24 小时；数据来自当前 SQLite，刷新页面即可重新读取。
        </p>
      </div>
      <div class="kpi-grid">{_kpi_cards(stats)}</div>
    </div>
  </section>

  <section class="section section-dark" id="recent">
    <div class="wrap">
      <div class="section-heading on-dark">
        <p class="eyebrow">最近任务</p>
        <h2>队列里正在发生什么</h2>
        <p class="section-caption">
          最近 {len(data.recent)} 条；点击任务编号查看失败现场、事件时间线与日志尾部。
        </p>
      </div>
      {_recent_tasks(data.recent, now=data.now, gitlab_url=data.gitlab_url)}
    </div>
  </section>

  <section class="section section-canvas" id="failures">
    <div class="wrap">
      <div class="section-heading">
        <p class="eyebrow">需要关注</p>
        <h2>失败的现场，留在这里</h2>
        <p class="section-caption">
          最近 {len(data.failures)} 条失败任务；只展示原因，不在这里执行重试。
        </p>
      </div>
      {_failure_list(data.failures, now=data.now, gitlab_url=data.gitlab_url)}
    </div>
  </section>

  <section class="section section-parchment" id="projects">
    <div class="wrap">
      <div class="section-heading">
        <p class="eyebrow">项目</p>
        <h2>每个仓库的节奏</h2>
        <p class="section-caption">
          按最后活动时间排序，最多展示 50 个项目；这是单实例 SQLite 内的多项目视图。
        </p>
      </div>
      <div class="utility-card table-card">
        {_project_table(data.projects, gitlab_url=data.gitlab_url)}
      </div>
    </div>
  </section>

  <section class="section section-dark-2" id="ops">
    <div class="wrap">
      <div class="section-heading on-dark">
        <p class="eyebrow">运维指标</p>
        <h2>这一程，机器人都做了什么</h2>
        <p class="section-caption">
          进程启动以来累计，重启归零；长期趋势请抓取 Prometheus /metrics。
        </p>
      </div>
      <div class="metrics-row">{_metric_cards(data.metrics)}</div>
    </div>
  </section>
</main>

<footer class="footer">
  <div class="wrap footer-inner">
    <div>
      <p class="footer-brand">mini-gitlab-bot</p>
      <p class="footer-copy">只读观测台 · 当前实例 SQLite</p>
    </div>
    <p class="footer-legal">不提供重试 / 取消 / 手动触发 · 所有时间 UTC</p>
  </div>
</footer>
"""
    return _page("运维观测台 · mini-gitlab-bot", body)


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
        links.append(
            f'<a class="text-link" href="{html.escape(task.mr_url)}">MR !{task.mr_iid}</a>'
        )
    if task.branch:
        links.append(f'<span class="mono">{html.escape(task.branch)}</span>')
    if task.commit_sha:
        links.append(f"<code>{html.escape(task.commit_sha[:12])}</code>")
    detail = "".join(
        [
            row("任务", f'<span class="mono">{html.escape(task.id)}</span>'),
            row(
                "项目",
                f'<a class="text-link" href="{html.escape(project_url)}">'
                f"{html.escape(task.project.path)}</a>",
            ),
            row(
                "issue",
                f'<a class="text-link" href="{html.escape(issue_url)}">#{task.issue_iid}</a>',
            ),
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
        f"""
  <section class="section section-canvas">
    <div class="wrap">
      <div class="section-heading"><p class="eyebrow">失败证据</p><h2>现场</h2></div>
      <div class="utility-card">
        <pre class="code-block">{html.escape(task.failure_evidence or "—")}</pre>
      </div>
    </div>
  </section>"""
        if task.failure_evidence
        else ""
    )
    tail = (
        f"""
  <section class="section section-dark">
    <div class="wrap">
      <div class="section-heading on-dark">
        <p class="eyebrow">日志尾部</p>
        <h2>最后 {log_lines} 行</h2>
        <p class="section-caption">路径来自数据库；读取失败时这里不会显示内容。</p>
      </div>
      <pre class="code-block code-block-dark">{html.escape(log_tail)}</pre>
    </div>
  </section>"""
        if log_tail
        else """
  <section class="section section-canvas">
    <div class="wrap"><div class="empty">没有可读的日志文件。</div></div>
  </section>"""
    )
    body = f"""
{_global_nav()}
<div class="sub-nav">
  <div class="sub-inner">
    <div class="sub-crumb">
      <span>任务</span><span>/</span><strong>{html.escape(task.short_id)}</strong>
    </div>
    <div class="sub-actions">
      <a class="button-secondary button-small" href="/dashboard">返回 Dashboard</a>
    </div>
  </div>
</div>

<main>
  <section class="hero hero-detail">
    <div class="wrap hero-inner hero-inner-left">
      <p class="hero-kicker">
        {html.escape(kind)} · {html.escape(task.project.path)} · #{task.issue_iid}
      </p>
      <h1>任务 <span class="mono">{html.escape(task.short_id)}</span></h1>
      <p class="hero-lead">
        {html.escape(stage_label(task.state, task.kind))}。
        {html.escape(task.failure_reason or "任务状态已持久化；本页为只读快照。")}
      </p>
      <div class="hero-actions"><a class="button-primary" href="/dashboard">返回 Dashboard</a></div>
    </div>
  </section>

  <section class="section section-parchment">
    <div class="wrap">
      <div class="section-heading"><p class="eyebrow">基本信息</p><h2>任务档案</h2></div>
      <div class="utility-card table-card"><table class="info-table">{detail}</table></div>
    </div>
  </section>

  {evidence}

  <section class="section section-canvas">
    <div class="wrap">
      <div class="section-heading">
        <p class="eyebrow">事件时间线</p>
        <h2>从入队到收尾</h2>
        <p class="section-caption">{len(events)} 条事件，按发生顺序排列。</p>
      </div>
      <div class="utility-card">{_timeline(events)}</div>
    </div>
  </section>

  {tail}
</main>

<footer class="footer">
  <div class="wrap footer-inner">
    <div><p class="footer-brand">mini-gitlab-bot</p><p class="footer-copy">任务详情 · 只读</p></div>
    <p class="footer-legal">所有时间 UTC · 不提供重试 / 取消 / 手动触发</p>
  </div>
</footer>
"""
    return _page(f"任务 {task.short_id} · mini-gitlab-bot", body)


# --------------------------------------------------------------------------- #
# 导航与片段
# --------------------------------------------------------------------------- #


def _global_nav(version: str = "") -> str:
    v = f'<span class="nav-version">v{_e(version)}</span>' if version else ""
    return f"""
<nav class="global-nav">
  <div class="nav-inner">
    <a class="nav-brand" href="/dashboard"><span class="brand-glyph">◈</span> mini-gitlab-bot</a>
    <div class="nav-links">
      <a href="#overview">概况</a>
      <a href="#recent">任务</a>
      <a href="#failures">失败</a>
      <a href="#projects">项目</a>
      <a href="#ops">指标</a>
    </div>
    <div class="nav-meta"><span>只读模式</span>{v}</div>
  </div>
</nav>
"""


def _sub_nav() -> str:
    return """
<div class="sub-nav">
  <div class="sub-inner">
    <div class="sub-crumb"><span>运维</span><span>/</span><strong>Dashboard</strong></div>
    <div class="sub-actions">
      <span class="sub-note">手动刷新</span>
      <a class="button-primary button-small" href="/dashboard">刷新数据</a>
    </div>
  </div>
</div>
"""


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
        f'<article class="utility-card kpi-card">'
        f'<p class="kpi-label">{html.escape(label)}</p>'
        f'<p class="kpi-value">{value}</p>'
        f'<p class="kpi-note">{html.escape(note)}</p></article>'
        for label, value, note in items
    )


def _recent_tasks(tasks: Sequence[Task], *, now: datetime, gitlab_url: str) -> str:
    if not tasks:
        return (
            '<div class="empty empty-dark">还没有任务。webhook 或周期扫描入队后会显示在这里。</div>'
        )
    rows = []
    for task in tasks:
        issue_url = _issue_url(gitlab_url, task.project.path, task.issue_iid)
        kind = html.escape(KIND_LABELS.get(task.kind, str(task.kind)))
        rows.append(
            "<tr>"
            f'<td class="num">{_fmt_time(task.created_at)}</td>'
            f'<td><a class="text-link mono" href="/dashboard/task/{html.escape(task.id)}">'
            f"{html.escape(task.short_id)}</a></td>"
            f'<td><span class="kind">{kind}</span></td>'
            f'<td><a class="text-link" href="{html.escape(issue_url)}">'
            f"{html.escape(task.project.path)}</a>"
            f'<span class="muted"> #{task.issue_iid}</span></td>'
            f"<td>{_badge(task)}</td>"
            f'<td class="num">{html.escape(_fmt_duration(task.elapsed_sec(now)))}</td>'
            f'<td class="reason">{html.escape(_clip(task.failure_reason) or "—")}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="dark-table"><thead><tr>'
        "<th>创建 (UTC)</th><th>任务</th><th>类型</th><th>项目 / issue</th>"
        "<th>状态</th><th>耗时</th><th>失败原因</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _failure_list(tasks: Sequence[Task], *, now: datetime, gitlab_url: str) -> str:
    if not tasks:
        return '<div class="empty">✓ 暂无失败任务</div>'
    cards = []
    for task in tasks:
        issue_url = _issue_url(gitlab_url, task.project.path, task.issue_iid)
        cards.append(
            f"""
<article class="utility-card failure-card">
  <div class="failure-top">
    <span class="flag">失败</span>
    <span class="caption">
      {html.escape(task.failure_stage or "unknown")} · {_fmt_time(task.updated_at)} UTC
    </span>
  </div>
  <a class="failure-title mono" href="/dashboard/task/{html.escape(task.id)}">
    {html.escape(task.short_id)}
  </a>
  <p class="failure-project">
    {html.escape(task.project.path)}
    <a class="text-link" href="{html.escape(issue_url)}">#{task.issue_iid}</a>
  </p>
  <p class="failure-reason">{html.escape(_clip(task.failure_reason, 160) or "—")}</p>
  <p class="failure-foot">
    {html.escape(KIND_LABELS.get(task.kind, str(task.kind)))} · 耗时
    {html.escape(_fmt_duration(task.elapsed_sec(now)))}
  </p>
</article>"""
        )
    return f'<div class="failure-grid">{"".join(cards)}</div>'


def _project_table(projects: Sequence[ProjectSummary], *, gitlab_url: str) -> str:
    if not projects:
        return '<div class="empty">还没有项目数据。任务入队后按项目聚合。</div>'
    rows = []
    for item in projects:
        project_url = _project_url(gitlab_url, item.project.path)
        rows.append(
            "<tr>"
            f'<td><a class="text-link" href="{html.escape(project_url)}">'
            f"{html.escape(item.project.path)}</a></td>"
            f'<td class="num">{item.total}</td>'
            f'<td class="num">{item.active}</td>'
            f'<td class="num">{item.succeeded}</td>'
            f'<td class="num">{item.failed}</td>'
            f'<td class="num">{_fmt_time(item.last_activity)}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="light-table"><thead><tr>'
        "<th>项目</th><th>总任务</th><th>进行中</th><th>成功</th><th>失败</th>"
        f"<th>最后活动 (UTC)</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _metric_cards(metrics: Mapping[str, float]) -> str:
    cards = []
    for name, label in METRICS:
        value = metrics.get(name, 0.0)
        cards.append(
            f'<div class="metric"><p class="metric-label">{html.escape(label)}</p>'
            f'<p class="metric-value">{_fmt_number(value)}</p></div>'
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
/* DESIGN.md token map: colors / typography / rounded / spacing */
:root {
  --color-primary: #0066cc;
  --color-primary-focus: #0071e3;
  --color-primary-on-dark: #2997ff;
  --color-ink: #1d1d1f;
  --color-body: #1d1d1f;
  --color-body-on-dark: #ffffff;
  --color-body-muted: #cccccc;
  --color-ink-muted-80: #333333;
  --color-ink-muted-48: #7a7a7a;
  --color-divider-soft: #f0f0f0;
  --color-hairline: #e0e0e0;
  --color-canvas: #ffffff;
  --color-parchment: #f5f5f7;
  --color-pearl: #fafafc;
  --color-tile-1: #272729;
  --color-tile-2: #2a2a2c;
  --color-tile-3: #252527;
  --color-black: #000000;
  --color-on-primary: #ffffff;
  --font-display: "SF Pro Display", system-ui, -apple-system, BlinkMacSystemFont,
    "Inter", sans-serif;
  --font-text: "SF Pro Text", system-ui, -apple-system, BlinkMacSystemFont,
    "Inter", sans-serif;
  --rounded-sm: 8px;
  --rounded-lg: 18px;
  --rounded-pill: 9999px;
  --space-xxs: 4px;
  --space-xs: 8px;
  --space-sm: 12px;
  --space-md: 17px;
  --space-lg: 24px;
  --space-xl: 32px;
  --space-xxl: 48px;
  --space-section: 80px;
  --content-max: 1440px;
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--color-parchment);
  color: var(--color-ink);
  font-family: var(--font-text);
  font-size: 17px;
  font-weight: 400;
  line-height: 1.47;
  letter-spacing: -0.374px;
  -webkit-font-smoothing: antialiased;
}
h1, h2, p { margin: 0; }
a { color: var(--color-primary); text-decoration: none; }
a:focus-visible, button:focus-visible, .button-primary:focus-visible,
.button-secondary:focus-visible {
  outline: 2px solid var(--color-primary-focus);
  outline-offset: 2px;
}
.wrap {
  width: min(var(--content-max), 100%);
  margin: 0 auto;
  padding: 0 var(--space-xxl);
}

/* global-nav: DESIGN.md component.global-nav */
.global-nav {
  height: 44px;
  background: var(--color-black);
  color: var(--color-body-on-dark);
  font-family: var(--font-text);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: -0.12px;
}
.nav-inner {
  width: min(var(--content-max), 100%);
  height: 44px;
  margin: 0 auto;
  padding: 0 var(--space-xxl);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-lg);
}
.nav-brand {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  color: var(--color-body-on-dark);
  font-size: 21px;
  font-weight: 600;
  line-height: 1.19;
  letter-spacing: 0.231px;
  white-space: nowrap;
}
.brand-glyph { color: var(--color-body-on-dark); font-size: 18px; }
.nav-links { display: flex; align-items: center; gap: 20px; }
.nav-links a { color: var(--color-body-on-dark); opacity: .78; font-size: 12px; }
.nav-meta {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  color: var(--color-body-muted);
  white-space: nowrap;
}
.nav-version { color: var(--color-body-muted); font-size: 12px; }

/* sub-nav-frosted: parchment 80% + backdrop blur */
.sub-nav {
  position: sticky;
  top: 0;
  z-index: 5;
  height: 52px;
  background: rgba(245, 245, 247, .8);
  border-bottom: 1px solid rgba(0, 0, 0, .08);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
}
.sub-inner {
  width: min(var(--content-max), 100%);
  height: 52px;
  margin: 0 auto;
  padding: 0 var(--space-xxl);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-lg);
}
.sub-crumb {
  display: flex;
  align-items: baseline;
  gap: var(--space-xs);
  color: var(--color-ink-muted-80);
  font-family: var(--font-text);
  font-size: 21px;
  font-weight: 600;
  line-height: 1.19;
  letter-spacing: 0.231px;
}
.sub-crumb span { color: var(--color-ink-muted-48); }
.sub-actions { display: flex; align-items: center; gap: var(--space-sm); }
.sub-note { color: var(--color-ink-muted-48); font-size: 12px; }

/* buttons: full-pill primary / ghost secondary; active scale only */
.button-primary,
.button-secondary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 11px 22px;
  border-radius: var(--rounded-pill);
  font-family: var(--font-text);
  font-size: 17px;
  font-weight: 400;
  line-height: 1.47;
  letter-spacing: -0.374px;
  white-space: nowrap;
  transition: transform .15s ease;
}
.button-primary { background: var(--color-primary); color: var(--color-on-primary); }
.button-secondary {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-primary);
}
.button-primary:active, .button-secondary:active { transform: scale(.95); }
.button-small {
  min-height: 44px;
  padding: 10px 18px;
  font-size: 14px;
  line-height: 1.29;
  letter-spacing: -0.224px;
}

/* hero: light full-bleed, centered product-tile rhythm */
.hero {
  padding: var(--space-section) 0;
  background: var(--color-canvas);
  text-align: center;
}
.hero-inner { max-width: 980px; }
.hero-inner-left { text-align: left; }
.hero-kicker {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  margin-bottom: var(--space-md);
  color: var(--color-ink-muted-48);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-ink);
  flex: none;
}
.status-dot.warn { background: var(--color-ink-muted-48); }
.status-dot.bad { background: var(--color-black); }
.hero h1 {
  font-family: var(--font-display);
  font-size: 56px;
  font-weight: 600;
  line-height: 1.07;
  letter-spacing: -0.28px;
}
.hero-lead {
  max-width: 720px;
  margin: var(--space-md) auto var(--space-xl);
  color: var(--color-body);
  font-family: var(--font-text);
  font-size: 24px;
  font-weight: 300;
  line-height: 1.5;
  letter-spacing: 0;
}
.hero-actions { display: flex; justify-content: center; gap: var(--space-sm); flex-wrap: wrap; }
.hero-detail .hero-actions { justify-content: flex-start; }
.hero-meta {
  display: flex;
  justify-content: center;
  gap: var(--space-lg);
  flex-wrap: wrap;
  margin-top: var(--space-xl);
  color: var(--color-ink-muted-48);
  font-size: 12px;
  line-height: 1.3;
  letter-spacing: -0.12px;
}
.hero-detail h1 { font-size: 40px; line-height: 1.1; letter-spacing: 0; }
.hero-detail .hero-lead { margin-left: 0; margin-right: 0; }

/* full-bleed section pulse: canvas / parchment / tile-1 / tile-2 */
.section { padding: var(--space-section) 0; }
.section-canvas { background: var(--color-canvas); }
.section-parchment { background: var(--color-parchment); }
.section-dark { background: var(--color-tile-1); color: var(--color-body-on-dark); }
.section-dark-2 { background: var(--color-tile-2); color: var(--color-body-on-dark); }
.section-heading { max-width: 780px; margin-bottom: var(--space-xl); }
.eyebrow {
  margin-bottom: var(--space-xs);
  color: var(--color-ink-muted-48);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.29;
  letter-spacing: -0.224px;
}
.section-heading h2 {
  font-family: var(--font-text);
  font-size: 34px;
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: -0.374px;
}
.section-caption {
  margin-top: var(--space-xs);
  color: var(--color-ink-muted-48);
  font-size: 17px;
  line-height: 1.47;
  letter-spacing: -0.374px;
}
.on-dark h2 { color: var(--color-body-on-dark); }
.on-dark .eyebrow, .on-dark .section-caption { color: var(--color-body-muted); }

/* utility cards: 18px radius, hairline, no shadow */
.utility-card {
  background: var(--color-canvas);
  border: 1px solid var(--color-hairline);
  border-radius: var(--rounded-lg);
  padding: var(--space-lg);
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
}
.kpi-card { min-height: 156px; }
.kpi-label {
  color: var(--color-ink-muted-48);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.29;
  letter-spacing: -0.224px;
}
.kpi-value {
  margin-top: var(--space-xs);
  color: var(--color-ink);
  font-family: var(--font-display);
  font-size: 40px;
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: 0;
}
.kpi-note {
  margin-top: var(--space-xs);
  color: var(--color-ink-muted-48);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
}

/* dark data table: white text, sky links */
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
.dark-table { min-width: 860px; }
.dark-table th {
  padding: 0 var(--space-sm) var(--space-sm);
  color: var(--color-body-muted);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.29;
  letter-spacing: -0.224px;
  text-align: left;
  border-bottom: 1px solid rgba(255, 255, 255, .16);
  white-space: nowrap;
}
.dark-table td {
  padding: var(--space-md) var(--space-sm);
  color: var(--color-body-on-dark);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
  border-bottom: 1px solid rgba(255, 255, 255, .12);
  vertical-align: top;
}
.dark-table tr:last-child td { border-bottom: 0; }
.dark-table .text-link { color: var(--color-primary-on-dark); }
.dark-table .muted, .dark-table .num { color: var(--color-body-muted); }
.dark-table th:first-child, .dark-table td:first-child { padding-left: 0; }
.dark-table th:last-child, .dark-table td:last-child { padding-right: 0; }
.light-table th {
  padding: 0 var(--space-sm) var(--space-sm);
  color: var(--color-ink-muted-48);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.29;
  letter-spacing: -0.224px;
  text-align: left;
  border-bottom: 1px solid var(--color-hairline);
  white-space: nowrap;
}
.light-table td {
  padding: var(--space-md) var(--space-sm);
  color: var(--color-ink);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
  border-bottom: 1px solid var(--color-divider-soft);
  vertical-align: top;
}
.light-table tr:last-child td { border-bottom: 0; }
.light-table th:first-child, .light-table td:first-child { padding-left: 0; }
.light-table th:last-child, .light-table td:last-child { padding-right: 0; }
.table-card { padding: var(--space-lg); }
.num { white-space: nowrap; color: var(--color-ink-muted-48); font-variant-numeric: tabular-nums; }
.reason { max-width: 260px; color: var(--color-ink-muted-48); overflow-wrap: anywhere; }
.mono, code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
  font-size: 13px;
}
.muted { color: var(--color-ink-muted-48); }
.kind {
  display: inline-block;
  color: var(--color-ink-muted-48);
  font-size: 12px;
  line-height: 1.3;
  letter-spacing: -0.12px;
  white-space: nowrap;
}
.text-link { color: var(--color-primary); }
.badge {
  display: inline-block;
  padding: 4px 10px;
  border-radius: var(--rounded-sm);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: -0.12px;
  white-space: nowrap;
}
.badge.ok { background: var(--color-divider-soft); color: var(--color-ink); }
.badge.bad { background: var(--color-ink); color: var(--color-body-on-dark); }
.badge.active { background: var(--color-parchment); color: var(--color-ink); }
.badge.queued { background: var(--color-parchment); color: var(--color-ink-muted-48); }
.dark-table .badge.ok { background: rgba(255, 255, 255, .14); color: var(--color-body-on-dark); }
.dark-table .badge.bad { background: var(--color-body-on-dark); color: var(--color-ink); }
.dark-table .badge.active {
  background: rgba(41, 151, 255, .16);
  color: var(--color-primary-on-dark);
}
.dark-table .badge.queued { background: rgba(255, 255, 255, .1); color: var(--color-body-muted); }

/* failure cards: utility card, black flag, no red accent */
.failure-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.failure-card { display: flex; flex-direction: column; min-height: 210px; }
.failure-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-sm);
}
.flag {
  display: inline-block;
  padding: 4px 10px;
  border-radius: var(--rounded-sm);
  background: var(--color-ink);
  color: var(--color-body-on-dark);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: -0.12px;
}
.caption {
  color: var(--color-ink-muted-48);
  font-size: 12px;
  line-height: 1.3;
  letter-spacing: -0.12px;
}
.failure-title {
  display: inline-block;
  margin-top: var(--space-md);
  color: var(--color-ink);
  font-size: 21px;
  font-weight: 600;
  line-height: 1.19;
  letter-spacing: 0.231px;
}
.failure-project {
  margin-top: var(--space-xxs);
  color: var(--color-ink-muted-80);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
}
.failure-reason {
  margin-top: var(--space-sm);
  color: var(--color-ink-muted-80);
  font-size: 17px;
  line-height: 1.47;
  letter-spacing: -0.374px;
  overflow-wrap: anywhere;
}
.failure-foot {
  margin-top: auto;
  padding-top: var(--space-lg);
  color: var(--color-ink-muted-48);
  font-size: 12px;
  line-height: 1.3;
  letter-spacing: -0.12px;
}
.empty {
  padding: var(--space-xxl) var(--space-lg);
  border: 1px solid var(--color-hairline);
  border-radius: var(--rounded-lg);
  background: var(--color-canvas);
  color: var(--color-ink-muted-48);
  font-size: 17px;
  line-height: 1.47;
  letter-spacing: -0.374px;
  text-align: center;
}
.empty-dark {
  border-color: rgba(255, 255, 255, .2);
  background: transparent;
  color: var(--color-body-muted);
}

/* metrics: numbers on dark tile, no cards */
.metrics-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-xl);
}
.metric { border-top: 1px solid rgba(255, 255, 255, .18); padding-top: var(--space-md); }
.metric-label {
  color: var(--color-body-muted);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
}
.metric-value {
  margin-top: var(--space-xs);
  color: var(--color-body-on-dark);
  font-family: var(--font-display);
  font-size: 40px;
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: 0;
}

/* detail: info table, timeline, logs */
.info-table th {
  width: 150px;
  padding: var(--space-sm) var(--space-md);
  color: var(--color-ink-muted-48);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.29;
  letter-spacing: -0.224px;
  text-align: left;
  border-bottom: 1px solid var(--color-divider-soft);
  vertical-align: top;
}
.info-table td {
  padding: var(--space-sm) var(--space-md);
  color: var(--color-ink);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
  border-bottom: 1px solid var(--color-divider-soft);
  overflow-wrap: anywhere;
}
.info-table tr:last-child th, .info-table tr:last-child td { border-bottom: 0; }
.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li {
  display: grid;
  grid-template-columns: 140px 110px minmax(0, 1fr);
  gap: var(--space-sm);
  padding: var(--space-sm) 0;
  border-bottom: 1px solid var(--color-divider-soft);
  font-size: 14px;
  line-height: 1.43;
  letter-spacing: -0.224px;
}
.timeline li:last-child { border-bottom: 0; }
.timeline .time { color: var(--color-ink-muted-48); white-space: nowrap; }
.timeline .event-kind { font-weight: 600; }
.timeline .event-detail { color: var(--color-ink-muted-48); overflow-wrap: anywhere; }
.code-block {
  margin: 0;
  padding: var(--space-lg);
  border: 1px solid var(--color-hairline);
  border-radius: var(--rounded-sm);
  background: var(--color-pearl);
  color: var(--color-ink);
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
  overflow: auto;
  max-height: 460px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.code-block-dark {
  background: var(--color-tile-3);
  border-color: rgba(255, 255, 255, .16);
  color: var(--color-body-on-dark);
}

/* footer: parchment, fine print */
.footer { padding: 64px 0; background: var(--color-parchment); }
.footer-inner {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-lg);
  flex-wrap: wrap;
}
.footer-brand {
  color: var(--color-ink);
  font-size: 21px;
  font-weight: 600;
  line-height: 1.19;
  letter-spacing: 0.231px;
}
.footer-copy {
  margin-top: var(--space-xxs);
  color: var(--color-ink-muted-48);
  font-size: 12px;
  line-height: 1.3;
  letter-spacing: -0.12px;
}
.footer-legal {
  color: var(--color-ink-muted-48);
  font-size: 12px;
  line-height: 1.3;
  letter-spacing: -0.12px;
}

/* breakpoints: 1068 / 833 / 640 / 419 */
@media (max-width: 1068px) {
  .wrap, .nav-inner, .sub-inner { padding-left: var(--space-xl); padding-right: var(--space-xl); }
  .hero h1 { font-size: 40px; }
  .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 833px) {
  .nav-links { display: none; }
  .sub-note { display: none; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .failure-grid { grid-template-columns: 1fr; }
  .metrics-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .section { padding: 64px 0; }
  .hero { padding: 64px 0; }
}
@media (max-width: 640px) {
  .wrap, .nav-inner, .sub-inner { padding-left: var(--space-lg); padding-right: var(--space-lg); }
  .hero h1 { font-size: 34px; }
  .hero-lead { font-size: 21px; line-height: 1.19; font-weight: 600; letter-spacing: 0.231px; }
  .section { padding: var(--space-xxl) 0; }
  .hero { padding: var(--space-xxl) 0; }
  .kpi-grid { grid-template-columns: 1fr; }
  .metrics-row { grid-template-columns: 1fr 1fr; gap: var(--space-lg); }
  .footer { padding: var(--space-xxl) 0; }
  .timeline li { grid-template-columns: 1fr; gap: var(--space-xxs); }
  .dark-table { min-width: 720px; }
}
@media (max-width: 419px) {
  .hero h1 { font-size: 28px; }
  .sub-crumb { font-size: 17px; }
  .hero-actions .button-primary, .hero-actions .button-secondary { width: 100%; }
  .metrics-row { grid-template-columns: 1fr; }
}
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )
