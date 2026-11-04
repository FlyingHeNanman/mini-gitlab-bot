"""`/dashboard` 的 HTTP 行为：登录页 + session、数据展示、只读与任务详情。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from httpx import ASGITransport

from gateway.app import create_app
from tests.helpers import make_trigger

PASSWORD = "s3cr3t-pass"
TOKEN = "admin-token-0123456789"


def make_app(runtime, settings, **overrides):
    dashboard_settings = settings.model_copy(
        update={
            "dashboard_username": "admin",
            "dashboard_password": PASSWORD,
            "dashboard_enabled": True,
            **overrides,
        }
    )
    return create_app(settings=dashboard_settings, runtime=runtime)


async def get(app, path: str, *, cookies: httpx.Cookies | None = None) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(path, follow_redirects=False)


async def login(app, *, password: str = PASSWORD, username: str = "admin") -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/login",
            data={"username": username, "password": password, "next": "/dashboard"},
            follow_redirects=False,
        )


@asynccontextmanager
async def logged_in_client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "next": "/dashboard"},
        )
        assert response.status_code == 303
        yield client


async def test_dashboard_without_password_fails_closed_or_goes_to_setup(
    make_runtime, settings
) -> None:
    runtime, _ = make_runtime(tasks_page_enabled=False)
    app = make_app(runtime, settings, dashboard_password="")
    response = await get(app, "/dashboard")
    assert response.status_code in {303, 404}
    if response.status_code == 303:
        assert response.headers["location"] == "/setup"
    assert (await get(app, "/dashboard/task/task-x")).status_code in {303, 404}


async def test_dashboard_uses_login_page_and_session(make_runtime, settings) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)

    redirected = await get(app, "/dashboard")
    assert redirected.status_code == 303
    assert "/login" in redirected.headers["location"]

    login_page = await get(app, "/login")
    assert login_page.status_code == 200
    assert "登录观测台" in login_page.text
    assert "Basic" not in login_page.text

    denied = await login(app, password="wrong")
    assert denied.status_code == 401
    assert "用户名或密码不正确" in denied.text

    response = await login(app)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    cookie = response.cookies.get("mgb_session")
    assert cookie

    page = await get(app, "/dashboard", cookies=httpx.Cookies({"mgb_session": cookie}))
    assert page.status_code == 200


async def test_dashboard_shows_real_task_data_without_admin_token(
    make_runtime, settings, clock
) -> None:
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
    queued = runtime.store.enqueue(make_trigger(issue_iid=1, delivery_key="q"), now=clock()).task
    failed = runtime.store.enqueue(make_trigger(issue_iid=2, delivery_key="f"), now=clock()).task
    assert queued is not None and failed is not None
    runtime.store.fail(failed.id, stage="running", reason="模型返回空结果", now=clock())
    runtime.metrics.inc("sweep_runs_total")
    runtime.metrics.inc("sweep_errors_total")

    app = make_app(runtime, settings)
    async with logged_in_client(app) as client:
        page = await client.get("/dashboard")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "运行概况" in page.text
        assert "group/project-a" in page.text
        assert "模型返回空结果" in page.text
        assert queued.short_id in page.text
        assert failed.short_id in page.text
        assert "Sweep 轮次" in page.text
        assert "刷新数据" in page.text
        assert 'http-equiv="refresh"' not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert TOKEN not in page.text


async def test_dashboard_task_detail_is_login_protected(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
    task = runtime.store.enqueue(make_trigger(issue_iid=9), now=clock()).task
    assert task is not None
    runtime.store.fail(
        task.id,
        stage="validating",
        reason="diff 超限",
        evidence="a.py +2000",
        now=clock() + timedelta(seconds=1),
    )
    app = make_app(runtime, settings)

    assert (await get(app, f"/dashboard/task/{task.id}")).status_code == 303
    async with logged_in_client(app) as client:
        detail = await client.get(f"/dashboard/task/{task.id}")
        assert detail.status_code == 200
        assert "任务 " + task.short_id in detail.text
        assert "diff 超限" in detail.text
        assert "a.py +2000" in detail.text
        assert "事件时间线" in detail.text
        assert "返回 Dashboard" in detail.text
        assert TOKEN not in detail.text

        missing = await client.get("/dashboard/task/task-doesnotexist")
        assert missing.status_code == 404


async def test_dashboard_is_read_only_and_ignores_admin_token(
    make_runtime, settings, clock
) -> None:
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
    task = runtime.store.enqueue(make_trigger(issue_iid=4), now=clock()).task
    assert task is not None
    app = make_app(runtime, settings)

    async with logged_in_client(app) as client:
        # 只读：没有可写的 POST 入口
        response = await client.post(f"/dashboard/task/{task.id}")
        assert response.status_code == 405

    # 任务页的 token 不是 Dashboard 的凭据；两种入口互不混用
    assert (
        await get(app, "/dashboard", cookies=httpx.Cookies({"X-Admin-Token": TOKEN}))
    ).status_code == 303


async def test_dashboard_store_failure_returns_503_without_details(
    make_runtime, settings, monkeypatch
) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)

    def boom(**_: object) -> None:
        raise RuntimeError("db down at /secret/path")

    monkeypatch.setattr(runtime.store, "dashboard_stats", boom)
    app = make_app(runtime, settings)
    async with logged_in_client(app) as client:
        response = await client.get("/dashboard")
        assert response.status_code == 503
        assert "db down" not in response.text
        assert "/secret/path" not in response.text
