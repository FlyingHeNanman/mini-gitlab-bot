# ruff: noqa: E501

"""登录页渲染（纯函数）。"""

from __future__ import annotations

import html

from core.ui_theme import brand, document


def render_login(
    *,
    next_path: str = "/dashboard",
    error: str = "",
    notice: str = "",
    username: str = "admin",
) -> str:
    """登录页：项目内第一个需要用户主动输入凭据的界面。"""
    notice_html = ""
    if error:
        notice_html = f'<p class="notice notice-error">{html.escape(error)}</p>'
    elif notice:
        notice_html = f'<p class="notice notice-ok">{html.escape(notice)}</p>'
    body = f"""
<div class="shell auth-shell">
  {brand(subtitle="只读运维观测台")}
  <div class="auth">
    <p class="eyebrow">Dashboard</p>
    <h1>登录观测台</h1>
    <p class="auth-sub">用初始化时设置的管理员账号登录；凭据只用于本实例。</p>
    {notice_html}
    <div class="card auth-card" style="margin-top:20px">
      <form method="post" action="/login">
        <input type="hidden" name="next" value="{html.escape(next_path)}">
        <div class="form-grid">
          <div class="field full">
            <label for="username">用户名</label>
            <input class="input" id="username" name="username" type="text" value="{html.escape(username)}" autocomplete="username" required>
          </div>
          <div class="field full">
            <label for="password">密码</label>
            <input class="input" id="password" name="password" type="password" autocomplete="current-password" required autofocus>
          </div>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" type="submit">登录 <span aria-hidden="true">›</span></button>
          <a class="btn btn-quiet" href="/healthz">服务状态</a>
        </div>
      </form>
    </div>
    <p class="auth-foot">session 只保存签名后的用户名与过期时间 · 12 小时后自动失效</p>
  </div>
</div>
"""
    return document("登录 · mini-gitlab-bot", body)
