"""首次初始化、配置页与登录后的配置写入。"""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from gateway.app import create_app
from gateway.config import Settings
from gateway.config_store import is_initialized, read_config

FORM = {
    "gitlab_url": "https://gitlab.example.com",
    "gitlab_token": "glpat-0123456789",
    "gitlab_readonly_token": "glpat-readonly-0123456789",
    "webhook_secret": "w" * 32,
    "bot_username": "mini-gitlab-bot",
    "repo_whitelist": "group/a,group/b",
    "model_base_url": "http://llm.internal/v1",
    "model_name": "glm-5",
    "llm_api_key": "sk-secret-value",
    "dashboard_username": "admin",
    "dashboard_password": "preview-pass-2026",
    "agent_concurrency": "2",
}


async def request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        return await client.request(method, path, **kwargs)


def fresh_app(settings, runtime, tmp_path):
    fresh = settings.model_copy(
        update={
            "config_file": tmp_path / "setup.json",
            "dashboard_password": "",
            "gitlab_token": "",
            "model_base_url": "",
            "webhook_secret": "",
            "bot_username": "",
            "repo_whitelist": [],
        }
    )
    return fresh, create_app(settings=fresh, runtime=runtime)


async def test_setup_is_open_only_before_initialization(make_runtime, settings, tmp_path) -> None:
    runtime, _ = make_runtime()
    fresh, app = fresh_app(settings, runtime, tmp_path)

    page = await request(app, "GET", "/setup")
    assert page.status_code == 200
    assert "初始化你的机器人" in page.text
    assert "基础变量" in page.text
    assert "高级变量" in page.text
    assert "w" * 32 not in page.text  # 每次 GET 生成新的 webhook secret，旧值不回显

    posted = await request(app, "POST", "/setup", data=FORM)
    assert posted.status_code == 303
    assert posted.headers["location"] == "/setup/done"
    assert is_initialized(fresh.config_file)
    data = read_config(fresh.config_file)
    assert data["repo_whitelist"] == ["group/a", "group/b"]
    assert data["gitlab_token"] == FORM["gitlab_token"]
    assert (fresh.config_file.stat().st_mode & 0o777) == 0o600

    assert (await request(app, "GET", "/setup")).status_code == 303
    done = await request(app, "GET", "/setup/done")
    assert done.status_code == 200
    assert "配置已保存" in done.text


async def test_setup_rejects_cross_origin_and_never_echoes_secrets(
    make_runtime, settings, tmp_path
) -> None:
    runtime, _ = make_runtime()
    fresh, app = fresh_app(settings, runtime, tmp_path)
    cross = await request(
        app,
        "POST",
        "/setup",
        data=FORM,
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403

    bad = await request(
        app,
        "POST",
        "/setup",
        data={"gitlab_url": "https://gitlab.example.com", "gitlab_token": "glpat-secret-value"},
    )
    assert bad.status_code == 400
    assert "glpat-secret-value" not in bad.text
    assert "sk-secret-value" not in bad.text


async def test_settings_requires_login_and_saves_with_csrf_origin(
    make_runtime, settings, tmp_path
) -> None:
    runtime, _ = make_runtime()
    fresh, app = fresh_app(settings, runtime, tmp_path)
    await request(app, "POST", "/setup", data=FORM)

    configured = Settings(**read_config(fresh.config_file), config_file=fresh.config_file)
    configured_app = create_app(settings=configured, runtime=runtime)
    assert (await request(configured_app, "GET", "/settings")).status_code == 303

    login = await request(
        configured_app,
        "POST",
        "/login",
        data={"username": "admin", "password": FORM["dashboard_password"], "next": "/settings"},
    )
    assert login.status_code == 303
    cookie = login.cookies.get("mgb_session")
    assert cookie

    headers = {"Cookie": f"mgb_session={cookie}"}
    page = await request(configured_app, "GET", "/settings", headers=headers)
    assert page.status_code == 200
    assert "高级变量" in page.text
    assert "group/a, group/b" in page.text
    assert FORM["gitlab_token"] not in page.text
    assert "已配置，留空保持不变" in page.text

    cross = await request(
        configured_app,
        "POST",
        "/settings",
        data={"gitlab_url": "https://gitlab.example.com"},
        headers={**headers, "Origin": "https://evil.example"},
    )
    assert cross.status_code == 403

    saved = await request(
        configured_app,
        "POST",
        "/settings",
        data={
            **FORM,
            "gitlab_token": "",
            "dashboard_password": "",
            "sweep_enabled": "0",
        },
        headers=headers,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings?saved=1"
    data = read_config(fresh.config_file)
    assert data["gitlab_token"] == FORM["gitlab_token"]
    assert data["sweep_enabled"] is False
