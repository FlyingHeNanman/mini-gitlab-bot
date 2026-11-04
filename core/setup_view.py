# ruff: noqa: E501
"""首次初始化 / 基础与高级配置页（纯渲染）。"""

from __future__ import annotations

import html
from collections.abc import Mapping

from core.ui_theme import brand, document

BASIC_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("gitlab_url", "GitLab 地址", "text", "例如 https://gitlab.example.com"),
    (
        "gitlab_token",
        "GitLab 写 token",
        "password",
        "需要 api 权限；只保存在本机 /data/config.json",
    ),
    ("gitlab_readonly_token", "GitLab 只读 token", "password", "可选；read_api 权限，用于迷你 MCP"),
    ("webhook_secret", "Webhook Secret", "text", "GitLab Webhook 里填同一个值；至少 16 位"),
    ("bot_username", "机器人用户名", "text", "GitLab 上的机器人账号，用于忽略自己产生的事件"),
    ("repo_whitelist", "项目白名单", "text", "逗号分隔，例如 group/a,group/b"),
    (
        "model_base_url",
        "模型端点",
        "text",
        "OpenAI Responses 兼容端点，例如 http://llm-gateway:8000/v1",
    ),
    ("model_name", "模型名", "text", "默认模型；triage/explain/fix 可在高级里分别指定"),
    ("llm_api_key", "模型 API Key", "password", "只写入配置文件，不回显、不进日志"),
    ("dashboard_username", "Dashboard 用户名", "text", "登录页使用的管理员用户名"),
    ("dashboard_password", "Dashboard 密码", "password", "至少 8 位；留空不会保存"),
    ("agent_concurrency", "并发 agent 数", "number", "同时执行的任务数；默认 2"),
)

ADVANCED_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("min_access_level", "最小权限等级", "number", "Developer=30；低于 30 有权限提升风险"),
    ("triage_quota_per_hour", "每项目每小时巡检上限", "number", "0 = 不限"),
    ("autofix_max_files", "自动修最大文件数", "number", "小改动自动修的 diff 上限"),
    ("autofix_max_lines", "自动修最大行数", "number", "小改动自动修的 diff 上限"),
    ("diff_max_files", "任务 diff 最大文件数", "number", "push 前的硬闸门"),
    ("diff_max_lines", "任务 diff 最大行数", "number", "push 前的硬闸门"),
    ("agent_timeout_sec", "Agent 超时（秒）", "number", "超时后任务失败并保留日志"),
    ("gitlab_timeout_sec", "GitLab API 超时（秒）", "number", "单次请求超时"),
    ("issue_comment_limit", "context.md 评论条数", "number", "只取最新 N 条人类评论"),
    ("sweep_interval_sec", "Sweep 间隔（秒）", "number", "补偿巡检 + commit 回收"),
    ("sweep_commit_lookback_days", "Sweep 提交回溯天数", "number", "只扫最近 N 天提交"),
    ("release_lookback_days", "Release 回溯天数", "number", "首个 tag 最多回溯多少天"),
    ("log_retention_days", "日志保留天数", "number", "codex / uv 日志"),
    ("task_retention_days", "任务保留天数", "number", "终态任务与事件"),
    ("model_triage", "巡检模型", "text", "留空回落到 MODEL_NAME"),
    ("model_explain", "解释模型", "text", "留空回落到 MODEL_NAME"),
    ("model_fix", "修复模型", "text", "留空回落到 MODEL_NAME"),
    ("uv_default_index", "uv 默认 index", "text", "内网镜像；留空使用 uv 默认"),
    ("codex_expected_version", "Codex 版本 pin", "text", "留空只告警，不阻断"),
    ("forbidden_paths", "禁改路径", "text", "逗号分隔；push 前命中即拦下"),
)

BOOLEAN_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("triage_enabled", "新 issue 自动巡检", "关闭后只能靠手工触发"),
    ("autofix_enabled", "小改动自动派生修复", "巡检结论满足规则时"),
    ("sweep_enabled", "周期扫描", "补偿巡检 + commit 引用回收"),
    ("release_enabled", "自动 Release", "Tag Push 后补 Release 说明"),
    ("mcp_enabled", "只读迷你 MCP", "需要只读 token；没有时自动失效"),
    ("metrics_enabled", "Prometheus /metrics", "只暴露有限枚举标签"),
)

ALL_LABELS: dict[str, str] = {
    **{key: label for key, label, _kind, _help in BASIC_FIELDS},
    **{key: label for key, label, _kind, _help in ADVANCED_FIELDS},
    **{key: label for key, label, _help in BOOLEAN_FIELDS},
}


def render_setup(
    *,
    values: Mapping[str, object],
    errors: Mapping[str, str] | None = None,
    secret_state: Mapping[str, bool] | None = None,
) -> str:
    """首次初始化页：一个表单，按 GitLab / 模型 / 管理账号分成三张卡。"""
    body = _config_body(
        values=values,
        errors=errors or {},
        secret_state=secret_state or {},
        action="/setup",
        title="初始化你的机器人",
        subtitle="第一次部署只需要填这一页。保存后会写入 /data/config.json，重启容器后生效。",
        submit="保存并完成初始化",
        setup=True,
    )
    return document("初始化 · mini-gitlab-bot", body)


def render_settings(
    *,
    username: str,
    values: Mapping[str, object],
    errors: Mapping[str, str] | None = None,
    secret_state: Mapping[str, bool] | None = None,
    notice: str = "",
    restart: bool = False,
) -> str:
    """登录后的基础 / 高级配置页。"""
    body = _config_body(
        values=values,
        errors=errors or {},
        secret_state=secret_state or {},
        action="/settings",
        title="配置",
        subtitle="基础变量首次初始化后仍可修改；高级变量有默认值，按需调整。",
        submit="保存配置",
        setup=False,
        username=username,
        notice=notice,
        restart=restart,
    )
    return document("配置 · mini-gitlab-bot", body)


def render_setup_done() -> str:
    """初始化保存完成：重启后配置才进入运行时。"""
    body = f"""
<div class="shell auth-shell" style="width:min(720px,calc(100% - 32px))">
  {brand(subtitle="初始化完成")}
  <div class="auth">
    <p class="eyebrow">Setup complete</p>
    <h1>配置已保存</h1>
    <p class="auth-sub">下一步重启容器，让 /data/config.json 进入运行时。重启后打开 /login 登录。</p>
    <div class="card">
      <div class="setup-steps"><span class="step">1 填写配置</span><span class="step">2 保存</span><span class="step on">3 重启并登录</span></div>
      <form method="post" action="/setup/restart">
        <button class="btn btn-primary" type="submit">重启服务 <span aria-hidden="true">›</span></button>
      </form>
      <p class="field-help" style="margin-top:16px">容器使用 <span class="mono">restart: unless-stopped</span> 或 <span class="mono">--restart unless-stopped</span> 时会自动拉起；否则请手动执行 <span class="mono">docker compose restart</span>。</p>
    </div>
  </div>
</div>
"""
    return document("初始化完成 · mini-gitlab-bot", body)


def _config_body(
    *,
    values: Mapping[str, object],
    errors: Mapping[str, str],
    secret_state: Mapping[str, bool],
    action: str,
    title: str,
    subtitle: str,
    submit: str,
    setup: bool,
    username: str = "",
    notice: str = "",
    restart: bool = False,
) -> str:
    top = (
        f"""<div class="topbar">
  {brand(subtitle="配置")}
  <nav class="nav"><a href="/dashboard">总览</a><a href="/dashboard/recent">最近任务</a><a href="/dashboard/projects">项目</a><a class="active" href="/settings">配置</a></nav>
  <div class="top-actions"><span class="avatar">{html.escape((username[:1] or "A").upper())}</span><form method="post" action="/logout"><button class="btn btn-quiet btn-small" type="submit">退出</button></form></div>
</div>"""
        if not setup
        else ""
    )
    error_html = ""
    if errors.get("_form"):
        error_html = f'<p class="notice notice-error">{html.escape(errors["_form"])}</p>'
    notice_html = f'<p class="notice notice-ok">{html.escape(notice)}</p>' if notice else ""
    advanced_open = " open" if any(key in errors for key, *_ in ADVANCED_FIELDS) else ""
    basic_fields = "".join(
        _field(key, label, kind, help_text, values, errors, secret_state, setup)
        for key, label, kind, help_text in BASIC_FIELDS
    )
    advanced_fields = "".join(
        _field(key, label, kind, help_text, values, errors, secret_state, setup)
        for key, label, kind, help_text in ADVANCED_FIELDS
    )
    booleans = "".join(
        _boolean(key, label, help_text, values) for key, label, help_text in BOOLEAN_FIELDS
    )
    restart_html = ""
    if restart:
        restart_html = """
      <div class="section">
        <p class="card-label">生效</p>
        <h2 class="section-title">保存后需要重启</h2>
        <p class="section-sub">配置只在进程启动时读取；重启容器后新配置才会生效。</p>
        <form method="post" action="/settings/restart" style="margin-top:16px">
          <button class="btn btn-secondary" type="submit">立即重启服务</button>
        </form>
      </div>"""
    body = f"""
<div class="shell">
  {top}
  <header class="hero">
    <div>
      <p class="eyebrow">{"首次部署" if setup else "实例配置"}</p>
      <h1>{html.escape(title)}</h1>
      <p class="hero-lead">{html.escape(subtitle)}</p>
    </div>
    <div class="hero-actions">
      <a class="btn btn-secondary" href="{"/login" if setup else "/dashboard"}">取消</a>
      <button class="btn btn-primary" type="submit" form="config-form">{html.escape(submit)} <span aria-hidden="true">›</span></button>
    </div>
  </header>
  {notice_html}
  {error_html}
  <form method="post" action="{html.escape(action)}" id="config-form">
    <section class="card section">
      <div class="setup-steps"><span class="step on">1 GitLab</span><span class="step on">2 模型</span><span class="step on">3 管理账号</span></div>
      <p class="card-label">基础变量</p>
      <h2 class="section-title">连接 GitLab 与模型</h2>
      <p class="section-sub">这些是机器人能跑起来的最小集合；其余变量在下方高级区有默认值。</p>
      <div class="form-grid" style="margin-top:20px">{basic_fields}</div>
    </section>
    <details class="advanced"{advanced_open}>
      <summary>高级变量（可选）</summary>
      <section class="card">
        <p class="card-label">Advanced</p>
        <h2 class="section-title">行为、限额与保留策略</h2>
        <p class="section-sub">不确定就先保持默认；保存后随时可以回来改。</p>
        <div class="form-grid" style="margin-top:20px">{advanced_fields}</div>
        <div class="form-grid" style="margin-top:18px">{booleans}</div>
      </section>
    </details>
    <div class="form-actions">
      <button class="btn btn-primary" type="submit">{html.escape(submit)} <span aria-hidden="true">›</span></button>
      <span class="field-help">凭据只写入本机 /data/config.json（0600），不会回显到页面。</span>
    </div>
  </form>
  {restart_html}
  <footer class="foot"><span><strong>mini-gitlab-bot</strong> · {"初始化" if setup else "配置"}</span><span>保存后重启生效</span></footer>
</div>
"""
    if setup:
        body = body.replace(
            '<a class="btn btn-secondary" href="/login">取消</a>',
            '<a class="btn btn-secondary" href="/healthz">服务状态</a>',
        )
    return body


def _field(
    key: str,
    label: str,
    kind: str,
    help_text: str,
    values: Mapping[str, object],
    errors: Mapping[str, str],
    secret_state: Mapping[str, bool],
    setup: bool,
) -> str:
    value = values.get(key, "")
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    else:
        text = "" if value is None else str(value)
    is_secret = key in {
        "gitlab_token",
        "gitlab_readonly_token",
        "webhook_secret",
        "llm_api_key",
        "dashboard_password",
    }
    input_type = (
        "text" if key == "webhook_secret" and setup else ("password" if is_secret else kind)
    )
    placeholder = "已配置，留空保持不变" if is_secret and secret_state.get(key) else ""
    attrs = f' type="{input_type}"'
    if input_type == "number":
        attrs += ' min="0" step="1"'
    error = errors.get(key)
    error_html = (
        f'<span class="field-help" style="color:var(--failed)">{html.escape(error)}</span>'
        if error
        else ""
    )
    full = " full" if key in {"repo_whitelist", "forbidden_paths"} else ""
    return f"""<div class="field{full}">
  <label for="{key}">{html.escape(label)}</label>
  <input class="input" id="{key}" name="{key}" value="{html.escape(text)}"{attrs} placeholder="{html.escape(placeholder)}">
  <span class="field-help">{html.escape(help_text)}</span>
  {error_html}
</div>"""


def _boolean(key: str, label: str, help_text: str, values: Mapping[str, object]) -> str:
    checked = " checked" if values.get(key) in {True, "true", "True", "on", "1"} else ""
    return f"""<div class="field full"><label class="check"><input type="hidden" name="{key}" value="0"><input type="checkbox" name="{key}" value="1"{checked}><span><strong>{html.escape(label)}</strong><br><span class="field-help">{html.escape(help_text)}</span></span></label></div>"""
