# ruff: noqa: E501
"""Dashboard 只读页面的渲染（纯函数，不碰 HTTP / 数据库）。

视觉规范以仓库根目录的 `DESIGN.md` 为准：云图柔板 / Dusty Canvas——

* 蒙灰蓝外框 + 浅灰圆角应用容器 + 白色软阴影卡片；
* 衬线标题配无衬线正文，珊瑚红只保留一个焦点；
* 列表行优先于高密度表格，失败用浅红、警告用浅黄。

数据由 `gateway` 采集，这里只负责把 `DashboardData` 变成 HTML。所有来自
GitLab / SQLite 的文本一律 `html.escape`；只读，不提供重试 / 取消 / 手动触发。
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from core.models import DailyCount, DashboardStats, ProjectSummary, Task, TaskEvent
from core.states import TaskKind, TaskState
from core.ui_theme import brand, document

KIND_LABELS: dict[TaskKind, str] = {
    TaskKind.FIX: "修复",
    TaskKind.TRIAGE: "巡检",
    TaskKind.EXPLAIN: "解释",
    TaskKind.RELEASE: "Release",
}

STATE_LABELS: dict[TaskState, str] = {
    TaskState.QUEUED: "排队",
    TaskState.PREPARING: "准备",
    TaskState.RUNNING: "执行中",
    TaskState.VALIDATING: "验证",
    TaskState.PUSHING: "推送",
    TaskState.OPENING_MR: "开 MR",
    TaskState.FINALIZING: "收尾",
    TaskState.SUCCEEDED: "已完成",
    TaskState.FAILED: "失败",
}

STATE_PILLS: dict[TaskState, str] = {
    TaskState.QUEUED: "pill-muted",
    TaskState.PREPARING: "pill-blue",
    TaskState.RUNNING: "pill-blue",
    TaskState.VALIDATING: "pill-blue",
    TaskState.PUSHING: "pill-blue",
    TaskState.OPENING_MR: "pill-blue",
    TaskState.FINALIZING: "pill-blue",
    TaskState.SUCCEEDED: "pill-muted",
    TaskState.FAILED: "pill-failed",
}

METRICS = (
    ("sweep_runs_total", "Sweep 轮次"),
    ("sweep_triaged_total", "补建巡检"),
    ("sweep_resolved_total", "回收 issue"),
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
    daily: Sequence[DailyCount] = ()
    username: str = "admin"


def render_dashboard(data: DashboardData) -> str:
    """总览页：健康、7 日趋势、失败中心、最近任务、项目与运维指标。"""
    health_class, health_title, health_note = _health(data)
    body = f"""
<div class="shell">
  {_topbar(data, health_class, health_title, active="overview")}
  <header class="hero">
    <div>
      <p class="eyebrow">只读观测台 · 当前实例</p>
      <h1>机器人今天怎么样</h1>
      <p class="hero-lead">{html.escape(health_note)}</p>
      <p class="hero-meta">
        <span>最近刷新 {_fmt_datetime(data.now)} UTC</span>
        <span>版本 v{html.escape(data.version)}</span>
      </p>
    </div>
    <div class="hero-actions">
      <a class="btn btn-primary" href="/dashboard">刷新数据 <span aria-hidden="true">›</span></a>
      <a class="btn btn-secondary" href="/settings">配置</a>
    </div>
  </header>

  <div class="grid-main">
    {_trend_card(data)}
    {_failure_card(data)}
  </div>

  <div class="grid-bottom">
    {_stats_card(data)}
    {_recent_card(data)}
    {_projects_card(data)}
  </div>

  <section class="card section" id="ops">
    <div class="card-head">
      <div>
        <p class="card-label">进程指标</p>
        <h2 class="card-title">这一程，机器人都做了什么</h2>
        <p class="card-sub">进程启动以来累计，重启归零；长期趋势请抓取 Prometheus /metrics。</p>
      </div>
    </div>
    {_metrics(data.metrics)}
  </section>

  <footer class="foot">
    <span><strong>mini-gitlab-bot</strong> · 只读观测台 · 当前实例 SQLite</span>
    <span>不提供重试 / 取消 / 手动触发 · 所有时间 UTC</span>
  </footer>
</div>
"""
    return document("运维观测台 · mini-gitlab-bot", body)


def render_dashboard_recent(data: DashboardData) -> str:
    """最近任务独立页：导航栏“最近任务”的落点，展示最多 50 条。"""
    health_class, health_title, health_note = _health(data)
    body = f"""
<div class="shell">
  {_topbar(data, health_class, health_title, active="recent")}
  <header class="hero">
    <div>
      <p class="eyebrow">只读观测台 · 任务流</p>
      <h1>最近任务</h1>
      <p class="hero-lead">按最后更新时间倒序，最多展示 50 条；点任意一条看失败现场与日志。</p>
      <p class="hero-meta">
        <span>最近刷新 {_fmt_datetime(data.now)} UTC</span>
        <span>{len(data.recent)} 条记录</span>
      </p>
    </div>
    <div class="hero-actions">
      <a class="btn btn-primary" href="/dashboard/recent">刷新数据 <span aria-hidden="true">›</span></a>
      <a class="btn btn-secondary" href="/dashboard">返回总览</a>
    </div>
  </header>

  <section class="card section">
    <div class="card-head">
      <div>
        <p class="card-label">Task stream</p>
        <h2 class="card-title">队列里正在发生什么</h2>
        <p class="card-sub">{html.escape(health_note)}</p>
      </div>
      <span class="pill pill-muted">只读</span>
    </div>
    <div class="list">{_recent_rows(data.recent, data.now, 50)}</div>
  </section>

  <footer class="foot">
    <span><strong>mini-gitlab-bot</strong> · 最近任务 · 当前实例 SQLite</span>
    <span>只读，不在这里重试 / 取消 · 所有时间 UTC</span>
  </footer>
</div>
"""
    return document("最近任务 · mini-gitlab-bot", body)


def render_dashboard_projects(data: DashboardData) -> str:
    """项目独立页：导航栏“项目”的落点，展示按最后活动排序的 50 个项目。"""
    health_class, health_title, health_note = _health(data)
    body = f"""
<div class="shell">
  {_topbar(data, health_class, health_title, active="projects")}
  <header class="hero">
    <div>
      <p class="eyebrow">只读观测台 · 项目</p>
      <h1>项目节奏</h1>
      <p class="hero-lead">这是本实例见过的项目，按最近活动倒序；每行只汇总任务结果，不读 GitLab 实时状态。</p>
      <p class="hero-meta">
        <span>{len(data.projects)} 个项目</span>
        <span>最近刷新 {_fmt_datetime(data.now)} UTC</span>
      </p>
    </div>
    <div class="hero-actions">
      <a class="btn btn-primary" href="/dashboard/projects">刷新数据 <span aria-hidden="true">›</span></a>
      <a class="btn btn-secondary" href="/dashboard">返回总览</a>
    </div>
  </header>

  <section class="card section">
    <div class="card-head">
      <div>
        <p class="card-label">Repositories</p>
        <h2 class="card-title">每个仓库的节奏</h2>
        <p class="card-sub">{html.escape(health_note)}</p>
      </div>
      <span class="pill pill-muted">{len(data.projects)} 个</span>
    </div>
    <div class="list">{_project_rows(data.projects, data.gitlab_url, data.now, 50)}</div>
  </section>

  <footer class="foot">
    <span><strong>mini-gitlab-bot</strong> · 项目 · 当前实例 SQLite</span>
    <span>只读汇总 · 项目链接指向 GitLab · 所有时间 UTC</span>
  </footer>
</div>
"""
    return document("项目 · mini-gitlab-bot", body)


def render_dashboard_task(
    task: Task,
    *,
    events: Sequence[TaskEvent],
    log_tail: str = "",
    log_lines: int = 0,
    now: datetime,
    gitlab_url: str,
) -> str:
    """任务详情：任务档案、失败现场、事件时间线与日志尾部。"""
    issue_url = _issue_url(gitlab_url, task.project.path, task.issue_iid)
    project_url = _project_url(gitlab_url, task.project.path)
    kind = KIND_LABELS.get(task.kind, str(task.kind))
    state = STATE_LABELS.get(task.state, str(task.state))
    pill = STATE_PILLS.get(task.state, "pill-muted")
    links = []
    if task.mr_url and task.mr_iid:
        links.append(f'<a class="muted" href="{html.escape(task.mr_url)}">MR !{task.mr_iid}</a>')
    if task.branch:
        links.append(f'<span class="mono">{html.escape(task.branch)}</span>')
    if task.commit_sha:
        links.append(f'<span class="mono">{html.escape(task.commit_sha[:12])}</span>')

    def item(name: str, value: str) -> str:
        return f'<div class="detail-item"><div class="k">{html.escape(name)}</div><div class="v">{value}</div></div>'

    fields = "".join(
        [
            item("任务", f'<span class="mono">{html.escape(task.id)}</span>'),
            item(
                "项目",
                f'<a href="{html.escape(project_url)}">{html.escape(task.project.path)}</a>',
            ),
            item("issue", f'<a href="{html.escape(issue_url)}">#{task.issue_iid}</a>'),
            item("类型", html.escape(kind)),
            item("状态", _state_pill(task.state)),
            item(
                "触发",
                f"{html.escape(str(task.trigger_kind))} · @{html.escape(task.trigger_actor)}",
            ),
            item("尝试", f"{task.attempts} / {task.max_attempts}"),
            item("创建", f"{_fmt_datetime(task.created_at)} UTC"),
            item("结束", f"{_fmt_datetime(task.finished_at)} UTC" if task.finished_at else "—"),
            item("耗时", _fmt_duration(task.elapsed_sec(now))),
            item("产物", " ".join(links) or "—"),
            item("失败阶段", html.escape(task.failure_stage or "—")),
        ]
    )
    evidence = (
        f"""
  <section class="card section">
    <p class="card-label">失败证据</p>
    <h2 class="card-title">现场</h2>
    <pre class="code">{html.escape(task.failure_evidence or "—")}</pre>
  </section>"""
        if task.failure_evidence
        else ""
    )
    tail = (
        f"""
  <section class="card section">
    <p class="card-label">日志尾部</p>
    <h2 class="card-title">最后 {log_lines} 行</h2>
    <pre class="code">{html.escape(log_tail)}</pre>
  </section>"""
        if log_tail
        else ""
    )
    timeline = (
        "".join(
            f"""<li>
          <div class="when">{_fmt_datetime(event.at)} UTC</div>
          <div class="what">{html.escape(event.kind)}</div>
          <div class="detail">{html.escape(_event_detail(event.detail))}</div>
        </li>"""
            for event in events
        )
        or '<p class="empty-soft">还没有事件记录。</p>'
    )
    body = f"""
<div class="shell">
  {_topbar(None, "warn", state, active="recent")}
  <header class="hero">
    <div>
      <p class="eyebrow">任务档案</p>
      <h1>任务 {html.escape(task.short_id)}</h1>
      <p class="hero-lead">{html.escape(task.project.path)} · issue #{task.issue_iid} · {html.escape(kind)}</p>
    </div>
    <div class="hero-actions">
      <a class="btn btn-primary" href="/dashboard/recent">返回最近任务</a>
    </div>
  </header>

  <div class="grid-main">
    <section class="card card-large">
      <div class="card-head">
        <div>
          <p class="card-label">状态</p>
          <h2 class="card-title">{html.escape(state)}</h2>
          <p class="card-sub">{html.escape(task.failure_reason or "任务正在按状态机推进。")}</p>
        </div>
        <span class="pill {pill}">{html.escape(state)}</span>
      </div>
      <div class="detail-grid">{fields}</div>
    </section>
    <section class="card">
      <p class="card-label">最近事件</p>
      <h2 class="card-title">事件时间线</h2>
      <ul class="timeline">{timeline}</ul>
    </section>
  </div>
  {evidence}
  {tail}
  <footer class="foot">
    <span><strong>mini-gitlab-bot</strong> · 只读任务档案</span>
    <span>所有时间 UTC · 只读，不可在此重试</span>
  </footer>
</div>
"""
    return document(f"任务 {task.short_id} · mini-gitlab-bot", body)


def _topbar(
    data: DashboardData | None, health_class: str, health_title: str, *, active: str = "overview"
) -> str:
    username = data.username if data else "operator"
    initial = (username[:1] or "M").upper()

    def item(label: str, href: str, key: str) -> str:
        cls = ' class="active"' if active == key else ""
        return f'<a{cls} href="{href}">{label}</a>'

    return f"""<div class="topbar">
  {brand()}
  <nav class="nav" aria-label="主导航">
    {item("总览", "/dashboard", "overview")}
    {item("最近任务", "/dashboard/recent", "recent")}
    {item("项目", "/dashboard/projects", "projects")}
    {item("配置", "/settings", "settings")}
  </nav>
  <div class="top-actions">
    <span class="health"><span class="dot {health_class}"></span>{html.escape(health_title)}</span>
    <span class="avatar" title="{html.escape(username)}">{html.escape(initial)}</span>
    <form method="post" action="/logout">
      <button class="btn btn-quiet btn-small" type="submit">退出</button>
    </form>
  </div>
</div>"""


def _trend_card(data: DashboardData) -> str:
    days = _trend_days(data.daily, data.now)
    max_total = max([point[1].total for point in days] + [1])
    today = days[-1][1].total if days else 0
    bars = []
    dates = []
    for index, (day, count) in enumerate(days):
        current = index == len(days) - 1
        height = max(6, round(100 * count.total / max_total))
        bars.append(
            f"""<div class="trend-col{" current" if current else ""}">
              {f'<span class="trend-float">{count.total}</span>' if current else ""}
              <span class="trend-bar" style="height:{height}%"></span>
            </div>"""
        )
        dates.append(
            f'<span class="trend-date{" current" if current else ""}">{day.strftime("%m/%d")}</span>'
        )
    rate = 0
    if data.stats.created_since:
        rate = round(100 * data.stats.succeeded_since / data.stats.created_since)
    bar_html = "".join(bars)
    date_html = "".join(dates)
    return f"""<section class="card card-large">
  <div class="card-head">
    <div>
      <p class="card-label">7 日任务趋势</p>
      <h2 class="card-title">这周跑了多少活</h2>
      <p class="card-sub">按创建时间统计 UTC 自然日；只读快照，刷新页面重新读取。</p>
    </div>
    <span class="pill pill-muted">近 7 天</span>
  </div>
  <p class="big-number"><span class="focus-number">{today}</span><span class="unit">今日任务</span></p>
  <div class="trend">
    <div class="trend-bars">{bar_html}</div>
    <div class="trend-dates">{date_html}</div>
    <div class="trend-foot">
      <div>
        <p class="big-number" style="font-size:28px;margin:0">{rate}%<span class="unit">24h 成功率</span></p>
        <p class="trend-note">成功率 = 24h 内成功任务 / 24h 内新建任务。<br>不是完成率，失败与在飞任务都计入分母。</p>
      </div>
      <span class="pill pill-blue">近 24h {data.stats.created_since} 个</span>
    </div>
  </div>
</section>"""


def _failure_card(data: DashboardData) -> str:
    if not data.failures:
        content = '<div class="empty">暂无失败任务。队列是干净的。</div>'
    else:
        rows = "".join(
            f"""<a class="list-row row-link" href="/dashboard/task/{html.escape(task.id)}">
              <span class="icon-block icon-soft">{html.escape(_kind_mark(task.kind))}</span>
              <span class="row-main">
                <span class="row-title">{html.escape(task.project.path)} <span class="pill pill-failed">失败</span></span>
                <span class="row-sub">{html.escape(_clip(task.failure_reason or task.failure_stage or "未记录失败原因", 72))}</span>
              </span>
              <span class="row-end">{html.escape(_fmt_ago(task.updated_at, data.now))}</span>
            </a>"""
            for task in data.failures
        )
        content = f'<div class="alert-list">{rows}</div>'
    return f"""<section class="card" id="failures">
  <div class="card-head">
    <div>
      <p class="card-label">需要关注</p>
      <h2 class="card-title">失败的现场，留在这里</h2>
      <p class="card-sub">最近 {len(data.failures)} 条失败任务；只展示原因，不在这里执行重试。</p>
    </div>
    <span class="pill pill-failed">{len(data.failures)} 条</span>
  </div>
  {content}
</section>"""


def _stats_card(data: DashboardData) -> str:
    stats = data.stats
    return f"""<section class="card">
  <p class="card-label">运行概况</p>
  <h2 class="card-title">队列与结果</h2>
  <div class="stat-strip">
    <div class="stat"><div class="stat-label">执行中</div><div class="stat-value">{stats.active}</div><div class="stat-lines"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div></div>
    <div class="stat"><div class="stat-label">排队</div><div class="stat-value">{stats.queued}</div><div class="stat-lines"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div></div>
    <div class="stat"><div class="stat-label">24h 失败</div><div class="stat-value">{stats.failed_since}</div><div class="stat-lines"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div></div>
  </div>
  <p class="card-sub" style="margin-top:16px">历史累计 {stats.total} 个任务 · 成功 {stats.succeeded} · 失败 {stats.failed}</p>
</section>"""


def _recent_rows(tasks: Sequence[Task], now: datetime, limit: int) -> str:
    if not tasks:
        return '<p class="empty">还没有任务。</p>'
    return "".join(
        f"""<a class="list-row row-link" href="/dashboard/task/{html.escape(task.id)}">
          <span class="icon-block icon-slate">{html.escape(_kind_mark(task.kind))}</span>
          <span class="row-main">
            <span class="row-title">{html.escape(task.project.path)} <span class="pill {STATE_PILLS.get(task.state, "pill-muted")}">{html.escape(STATE_LABELS.get(task.state, str(task.state)))}</span></span>
            <span class="row-sub">#{task.issue_iid} · {html.escape(KIND_LABELS.get(task.kind, str(task.kind)))} · {html.escape(task.short_id)}</span>
          </span>
          <span class="row-end">{html.escape(_fmt_ago(task.updated_at, now))}</span>
        </a>"""
        for task in tasks[:limit]
    )


def _recent_card(data: DashboardData) -> str:
    return f"""<section class="card" id="recent">
  <div class="card-head">
    <div><p class="card-label">最近任务</p><h2 class="card-title">队列里正在发生什么</h2></div>
    <a class="pill pill-muted" href="/dashboard/recent">查看全部</a>
  </div>
  <div class="list">{_recent_rows(data.recent, data.now, 8)}</div>
</section>"""


def _project_rows(
    projects: Sequence[ProjectSummary], gitlab_url: str, now: datetime, limit: int
) -> str:
    if not projects:
        return '<p class="empty">还没有项目数据。</p>'
    return "".join(
        f"""<a class="list-row row-link" href="{html.escape(_project_url(gitlab_url, project.project.path))}">
          <span class="icon-block {"icon-blue" if index % 2 else "icon-slate"}">{html.escape(project.project.path[:1].upper())}</span>
          <span class="row-main">
            <span class="row-title">{html.escape(project.project.path)}</span>
            <span class="row-sub">{project.total} 个任务 · 成功 {project.succeeded} · 失败 {project.failed}</span>
          </span>
          <span class="row-end">{project.active} 在飞<br>{html.escape(_fmt_ago(project.last_activity, now))}</span>
        </a>"""
        for index, project in enumerate(projects[:limit])
    )


def _projects_card(data: DashboardData) -> str:
    return f"""<section class="card" id="projects">
  <div class="card-head">
    <div><p class="card-label">项目</p><h2 class="card-title">每个仓库的节奏</h2></div>
    <a class="pill pill-muted" href="/dashboard/projects">全部 {len(data.projects)} 个</a>
  </div>
  <div class="list">{_project_rows(data.projects, data.gitlab_url, data.now, 6)}</div>
</section>"""


def _metrics(metrics: Mapping[str, float]) -> str:
    items = "".join(
        f"""<div class="stat"><div class="stat-label">{label}</div><div class="stat-value">{_fmt_number(metrics.get(name, 0))}</div></div>"""
        for name, label in METRICS
    )
    return f'<div class="stat-strip" style="flex-wrap:wrap;gap:16px 0">{items}</div>'


def _health(data: DashboardData) -> tuple[str, str, str]:
    if not data.worker_enabled:
        return "bad", "Worker 未启用", "Worker 未启用，新任务只会留在队列里。"
    if data.stalled:
        return "bad", "有任务租约过期", "存在超过阈值未续租的任务，请检查 worker 与日志。"
    if data.stats.failed_since:
        return "warn", "有失败需要关注", f"过去 24h 有 {data.stats.failed_since} 个任务失败。"
    return "ok", "运行正常", "队列、worker 与最近任务都没有异常信号。"


def _trend_days(
    daily: Sequence[DailyCount], now: datetime, *, days: int = 7
) -> list[tuple[datetime, DailyCount]]:
    today = now.astimezone(UTC).date()
    by_day = {point.day: point for point in daily}
    result: list[tuple[datetime, DailyCount]] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        result.append(
            (
                datetime.combine(day, datetime.min.time(), UTC),
                by_day.get(key, DailyCount(key, 0, 0)),
            )
        )
    return result


def _kind_mark(kind: TaskKind) -> str:
    return {
        TaskKind.FIX: "修",
        TaskKind.TRIAGE: "巡",
        TaskKind.EXPLAIN: "释",
        TaskKind.RELEASE: "R",
    }.get(kind, "?")


def _state_pill(state: TaskState) -> str:
    return f'<span class="pill {STATE_PILLS.get(state, "pill-muted")}">{html.escape(STATE_LABELS.get(state, str(state)))}</span>'


def _event_detail(detail: Mapping[str, object]) -> str:
    text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 140 else text[:137] + "…"


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ago(value: datetime, now: datetime) -> str:
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _fmt_number(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 1 else f"{value:g}"


def _clip(text: str | None, limit: int = 90) -> str:
    if not text:
        return ""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _project_url(gitlab_url: str, path: str) -> str:
    return f"{gitlab_url.rstrip('/')}/{quote(path, safe='/')}"


def _issue_url(gitlab_url: str, path: str, iid: int) -> str:
    return f"{_project_url(gitlab_url, path)}/-/issues/{iid}"
