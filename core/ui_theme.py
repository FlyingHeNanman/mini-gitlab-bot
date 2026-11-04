"""Dusty Canvas 的共用样式与文档壳（纯字符串，无 IO）。

唯一视觉依据是根目录 `DESIGN.md`：蒙灰蓝外框、浅灰应用容器、白卡片、22px 圆角、
衬线标题 + 无衬线正文、珊瑚红单焦点。这里集中 token 与组件样式，页面渲染器只拼结构，
避免 Dashboard / 登录 / 初始化三套 CSS 漂移。
"""

# ruff: noqa: E501

from __future__ import annotations

import html

#: 页面基本色与组件规格（对应 DESIGN.md §2 / §6）
_THEME_CSS = """
:root {
  --canvas: #B0B7CE;
  --shell: #F0F1F4;
  --surface: #FFFFFF;
  --surface-2: #EDEFF3;
  --ink: #1B2A4A;
  --ink-2: #46536A;
  --muted: #8A92A0;
  --line: #E3E6EC;
  --brand: #E24D33;
  --brand-soft: #FADFD9;
  --chart-blue: #7E95B8;
  --chip-red: #E25033;
  --chip-slate: #2E3D59;
  --chip-blue: #82A8D8;
  --failed: #C2503A;
  --failed-soft: #F9E5E0;
  --warning: #B9822A;
  --warning-soft: #FBF0DA;
  --rounded-shell: 28px;
  --rounded-card: 22px;
  --rounded-chip: 14px;
  --rounded-pill: 999px;
  --shadow-shell: 0 18px 50px rgba(30, 40, 70, .18);
  --shadow-card: 0 2px 8px rgba(30, 40, 70, .06);
  --shadow-overlay: 0 12px 32px rgba(30, 40, 70, .14);
  --font-display: "Source Serif 4", "Newsreader", Georgia, "Songti SC", serif;
  --font-body: "Inter", -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}

* { box-sizing: border-box; }

html, body { min-height: 100%; }

body {
  margin: 0;
  background: var(--canvas);
  color: var(--ink-2);
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

a { color: inherit; text-decoration: none; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible,
textarea:focus-visible, summary:focus-visible {
  outline: 2px solid var(--ink);
  outline-offset: 2px;
}

.shell {
  width: min(1280px, calc(100% - 48px));
  margin: 40px auto 64px;
  padding: 28px 32px 32px;
  background: var(--shell);
  border-radius: var(--rounded-shell);
  box-shadow: var(--shadow-shell);
}

.topbar {
  display: flex;
  align-items: center;
  gap: 28px;
  min-height: 76px;
  padding-bottom: 20px;
}

.brand { display: flex; align-items: center; gap: 12px; flex: none; }
.brand-mark {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: var(--rounded-chip);
  background: var(--brand);
  color: #fff;
  font-size: 20px;
  line-height: 1;
}
.brand-name {
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
  letter-spacing: .01em;
}
.brand-sub { color: var(--muted); font-size: 11px; }

.nav { display: flex; align-items: center; gap: 28px; margin: 0 0 0 8px; }
.nav a {
  position: relative;
  padding: 8px 0;
  color: var(--muted);
  font-size: 14px;
  font-weight: 500;
}
.nav a:hover { color: var(--ink-2); }
.nav a.active { color: var(--ink); }
.nav a.active::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 2px;
  background: var(--ink);
}

.top-actions { display: flex; align-items: center; gap: 12px; margin-left: auto; flex: none; }
.top-actions form { margin: 0; display: inline-flex; }

.health {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 0 14px;
  border-radius: var(--rounded-pill);
  background: var(--surface);
  color: var(--ink-2);
  font-size: 12px;
  white-space: nowrap;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ink); flex: none; }
.dot.warn { background: var(--warning); }
.dot.bad { background: var(--failed); }
.dot.ok { background: var(--chart-blue); }

.avatar {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--surface);
  color: var(--ink);
  font-size: 13px;
  font-weight: 600;
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 44px;
  padding: 10px 20px;
  border: 0;
  border-radius: var(--rounded-pill);
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.2;
  cursor: pointer;
  transition: transform .15s ease, background-color .15s ease, color .15s ease;
}
.btn:active { transform: scale(.96); }
.btn-primary { background: var(--ink); color: #fff; }
.btn-primary:hover { background: #24385f; }
.btn-secondary { background: var(--surface); color: var(--ink); box-shadow: var(--shadow-card); }
.btn-quiet { background: transparent; color: var(--ink-2); padding-left: 10px; padding-right: 10px; }
.btn-small { min-height: 40px; padding: 8px 16px; font-size: 12px; }
.btn-danger { background: var(--failed-soft); color: var(--failed); }

.hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 32px;
  padding: 28px 4px 30px;
}
.eyebrow {
  margin: 0 0 10px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: .12em;
  text-transform: uppercase;
}
.hero h1, .section-title, .auth h1 {
  margin: 0;
  color: var(--ink);
  font-family: var(--font-display);
  font-weight: 600;
  letter-spacing: -.015em;
}
.hero h1 { font-size: 40px; line-height: 1.2; }
.hero-lead { max-width: 620px; margin: 10px 0 0; color: var(--ink-2); font-size: 14px; }
.hero-meta { margin-top: 16px; color: var(--muted); font-size: 12px; }
.hero-meta span + span::before { content: " · "; }
.hero-actions { display: flex; align-items: center; gap: 10px; flex: none; }

.grid-main { display: grid; grid-template-columns: 7fr 5fr; gap: 24px; align-items: start; }
.grid-bottom { display: grid; grid-template-columns: .85fr 1.1fr 1.2fr; gap: 24px; margin-top: 24px; }

.card {
  background: var(--surface);
  border-radius: var(--rounded-card);
  box-shadow: var(--shadow-card);
  padding: 24px;
  min-width: 0;
}
.card-large { padding: 28px; min-height: 320px; }
.card-soft { background: var(--surface-2); box-shadow: none; }
.card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.card-title { margin: 0; color: var(--ink); font-family: var(--font-display); font-size: 26px; font-weight: 600; line-height: 1.3; }
.card-sub { margin: 6px 0 0; color: var(--ink-2); font-size: 13px; }
.card-label { color: var(--muted); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }

.big-number {
  margin: 14px 0 0;
  color: var(--ink);
  font-family: var(--font-display);
  font-size: 40px;
  font-weight: 700;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.big-number .unit { margin-left: 4px; color: var(--muted); font-family: var(--font-body); font-size: 12px; font-weight: 400; }
.focus-number { color: var(--brand); }

.trend { margin-top: 18px; }
.trend-bars { display: flex; align-items: flex-end; gap: 12px; height: 150px; padding-top: 22px; }
.trend-col { position: relative; display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; gap: 8px; }
.trend-bar { width: 2px; min-height: 6px; background: var(--chart-blue); border-radius: 2px; }
.trend-col.current .trend-bar { width: 3px; background: var(--ink); }
.trend-float {
  position: absolute;
  top: -4px;
  left: 50%;
  transform: translateX(-50%);
  padding: 4px 8px;
  border-radius: 10px;
  background: var(--ink);
  color: #fff;
  box-shadow: var(--shadow-overlay);
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}
.trend-dates { display: flex; gap: 12px; margin-top: 12px; }
.trend-date {
  display: grid;
  place-items: center;
  flex: 1;
  aspect-ratio: 1;
  max-width: 44px;
  margin: 0 auto;
  border-radius: 50%;
  background: var(--surface-2);
  color: var(--muted);
  font-size: 11px;
}
.trend-date.current { background: var(--ink); color: #fff; }
.trend-foot { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-top: 20px; }
.trend-note { color: var(--muted); font-size: 11px; line-height: 1.6; }

.list { margin: 14px 0 0; padding: 0; list-style: none; }
.list-row { display: flex; align-items: center; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--line); }
.list-row:last-child { border-bottom: 0; padding-bottom: 0; }
.list-row:first-child { padding-top: 0; }
.row-main { min-width: 0; flex: 1; }
.row-title { display: flex; align-items: center; gap: 8px; color: var(--ink); font-size: 14px; font-weight: 600; line-height: 1.4; }
.row-sub { margin-top: 3px; color: var(--muted); font-size: 11px; line-height: 1.5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-end { flex: none; color: var(--muted); font-size: 11px; text-align: right; }
.row-link:hover .row-title { text-decoration: underline; text-underline-offset: 2px; }

.icon-block {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: var(--rounded-chip);
  flex: none;
  color: #fff;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
}
.icon-slate { background: var(--chip-slate); }
.icon-blue { background: var(--chip-blue); }
.icon-soft { background: var(--surface-2); color: var(--ink); }

.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 10px;
  border-radius: var(--rounded-pill);
  background: var(--surface-2);
  color: var(--ink-2);
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}
.pill-paid { background: var(--ink); color: #fff; }
.pill-brand { background: var(--brand-soft); color: var(--brand); }
.pill-blue { background: #E0E7F2; color: #51708F; }
.pill-failed { background: var(--failed-soft); color: var(--failed); }
.pill-warn { background: var(--warning-soft); color: var(--warning); }
.pill-muted { background: var(--surface-2); color: var(--muted); }

.alert-list { display: grid; gap: 10px; margin-top: 14px; }
.alert {
  padding: 14px 16px;
  border-radius: 18px;
  background: var(--failed-soft);
}
.alert-title { color: var(--failed); font-size: 13px; font-weight: 600; }
.alert-body { margin-top: 3px; color: var(--ink-2); font-size: 12px; line-height: 1.5; }
.alert-meta { margin-top: 8px; color: var(--muted); font-size: 11px; }

.stat-strip { display: flex; gap: 0; margin-top: 18px; }
.stat { flex: 1; padding-right: 12px; }
.stat + .stat { padding-left: 12px; border-left: 1px solid var(--line); }
.stat-label { color: var(--muted); font-size: 11px; }
.stat-value { margin-top: 4px; color: var(--ink); font-family: var(--font-display); font-size: 28px; font-weight: 700; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stat-lines { display: flex; align-items: flex-end; gap: 3px; height: 34px; margin-top: 10px; }
.stat-lines span { width: 1px; height: 60%; background: var(--line); }
.stat-lines span:nth-child(3n) { height: 88%; background: var(--chart-blue); }
.stat-lines span:nth-child(7) { height: 100%; background: var(--brand); }

.empty { padding: 28px 8px; text-align: center; color: var(--muted); font-size: 13px; }
.empty-soft { padding: 18px; border-radius: 18px; background: var(--surface-2); color: var(--muted); font-size: 12px; }

.foot { display: flex; justify-content: space-between; gap: 20px; margin-top: 28px; padding: 20px 4px 0; color: var(--muted); font-size: 11px; }
.foot strong { color: var(--ink-2); font-weight: 600; }

.auth-shell { width: min(560px, calc(100% - 32px)); margin: 64px auto; padding: 32px; }
.auth-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; }
.auth h1 { font-size: 40px; }
.auth-sub { margin: 8px 0 24px; color: var(--ink-2); font-size: 13px; }
.auth-card { padding: 28px; }
.auth-foot { margin-top: 18px; color: var(--muted); font-size: 11px; text-align: center; }

.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.form-grid .full { grid-column: 1 / -1; }
.field { display: grid; gap: 6px; }
.field label { color: var(--ink-2); font-size: 12px; font-weight: 500; }
.field-help { color: var(--muted); font-size: 11px; line-height: 1.5; }
.input, .select, .textarea {
  width: 100%;
  min-height: 46px;
  padding: 11px 14px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13px;
}
.textarea { min-height: 96px; resize: vertical; }
.input::placeholder, .textarea::placeholder { color: var(--muted); }
.input:focus, .select:focus, .textarea:focus { border-color: var(--ink); outline: none; box-shadow: 0 0 0 3px rgba(27, 42, 74, .1); }
.check { display: flex; align-items: flex-start; gap: 10px; color: var(--ink-2); font-size: 12px; }
.check input { width: 18px; height: 18px; margin: 0; accent-color: var(--ink); }
.form-actions { display: flex; align-items: center; gap: 10px; margin-top: 24px; }

.notice { padding: 12px 16px; border-radius: 14px; font-size: 12px; line-height: 1.5; }
.notice-error { background: var(--failed-soft); color: var(--failed); }
.notice-ok { background: #E6EEF7; color: var(--ink); }
.notice-warn { background: var(--warning-soft); color: var(--warning); }

.setup-steps { display: flex; gap: 8px; margin: 4px 0 24px; }
.step { flex: 1; padding-top: 8px; border-top: 2px solid var(--line); color: var(--muted); font-size: 11px; }
.step.on { border-color: var(--brand); color: var(--ink); }

.section { margin-top: 24px; }
.section-title { font-size: 26px; }
.section-sub { margin: 6px 0 0; color: var(--ink-2); font-size: 13px; }
details.advanced { margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--line); }
details.advanced summary { cursor: pointer; color: var(--ink); font-family: var(--font-display); font-size: 20px; font-weight: 600; }
details.advanced[open] summary { margin-bottom: 18px; }

.code {
  padding: 16px;
  border-radius: 16px;
  background: var(--surface-2);
  color: var(--ink);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  overflow: auto;
  max-height: 420px;
}
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.muted { color: var(--muted); }
.nowrap { white-space: nowrap; }

.timeline { list-style: none; padding: 0; margin: 16px 0 0; }
.timeline li { position: relative; padding: 0 0 16px 22px; border-left: 1px solid var(--line); }
.timeline li:last-child { padding-bottom: 0; border-left: 0; }
.timeline li::before { content: ""; position: absolute; left: -4px; top: 2px; width: 7px; height: 7px; border-radius: 50%; background: var(--chart-blue); }
.timeline .when { color: var(--muted); font-size: 11px; }
.timeline .what { color: var(--ink); font-size: 13px; font-weight: 500; }
.timeline .detail { color: var(--muted); font-size: 11px; word-break: break-word; }

.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; margin-top: 12px; }
.detail-item { padding: 12px 0; border-bottom: 1px solid var(--line); min-width: 0; }
.detail-item .k { color: var(--muted); font-size: 11px; }
.detail-item .v { margin-top: 3px; color: var(--ink); font-size: 13px; word-break: break-word; }

@media (max-width: 1279px) {
  .shell { width: calc(100% - 32px); margin: 24px auto 40px; padding: 24px; }
  .grid-main { grid-template-columns: 1fr; }
  .grid-bottom { grid-template-columns: 1fr 1fr; }
  .grid-bottom .card:last-child { grid-column: 1 / -1; }
}

@media (max-width: 1023px) {
  .nav { gap: 16px; }
  .nav .optional { display: none; }
  .hero { align-items: flex-start; flex-direction: column; }
  .topbar { gap: 16px; }
}

@media (max-width: 767px) {
  body { font-size: 13px; }
  .shell { width: calc(100% - 20px); margin: 10px auto 24px; padding: 18px; border-radius: 24px; }
  .topbar { min-height: 64px; flex-wrap: wrap; gap: 12px; }
  .nav { order: 3; width: 100%; gap: 20px; margin: 0; overflow-x: auto; }
  .nav a { white-space: nowrap; padding-bottom: 6px; }
  .top-actions { gap: 8px; }
  .health { display: none; }
  .hero { padding: 20px 2px 22px; }
  .hero h1 { font-size: 32px; }
  .hero-actions { width: 100%; }
  .hero-actions .btn { flex: 1; }
  .grid-bottom { grid-template-columns: 1fr; }
  .grid-bottom .card:last-child { grid-column: auto; }
  .card, .card-large { padding: 20px; border-radius: 20px; }
  .form-grid { grid-template-columns: 1fr; }
  .form-grid .full { grid-column: auto; }
  .detail-grid { grid-template-columns: 1fr; }
  .foot { flex-direction: column; gap: 8px; }
  .auth-shell { margin: 24px auto; padding: 22px; }
  .auth h1 { font-size: 34px; }
  .auth-card { padding: 20px; }
}
"""


def document(title: str, body: str) -> str:
    """把页面 body 包成完整 HTML；标题转义，避免外部文本进入 `<title>`。"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_THEME_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


def brand(*, subtitle: str = "运维观测台") -> str:
    """左上的珊瑚红品牌块 + 字标。"""
    return f"""<a class="brand" href="/dashboard">
  <span class="brand-mark" aria-hidden="true">◆</span>
  <span><span class="brand-name">mini-gitlab-bot</span><br><span class="brand-sub">{html.escape(subtitle)}</span></span>
</a>"""
