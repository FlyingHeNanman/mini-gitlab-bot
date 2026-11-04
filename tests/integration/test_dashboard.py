"""`/dashboard` 的 HTTP 行为：Basic Auth、数据展示、只读与任务详情。"""

from __future__ import annotations

import base64
from datetime import timedelta

import httpx
from httpx import ASGITransport

from gateway.app import create_app
from tests.helpers import make_trigger

PASSWORD = "s3cr3t-pass"
TOKEN = "admin-token-0123456789"


def basic_auth(username: str = "admin", password: str = PASSWORD) -> dict[str, str]:
    raw = f"{username}:{password}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


async def get(app, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


def make_app(runtime, settings, **overrides):
    dashboard_settings = settings.model_copy(
        update={"dashboard_username": "admin", "dashboard_password": PASSWORD, **overrides}
    )
    return create_app(settings=dashboard_settings, runtime=runtime)


async def test_dashboard_is_404_without_password(make_runtime, settings) -> None:
    runtime, _ = make_runtime(tasks_page_enabled=False)
    for overrides in (
        {"dashboard_password": ""},
        {"dashboard_enabled": False},
    ):
        app = make_app(runtime, settings, **overrides)
        assert (await get(app, "/dashboard")).status_code == 404
        assert (await get(app, "/dashboard/task/task-x")).status_code == 404


async def test_dashboard_requires_basic_auth(make_runtime, settings) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    denied = await get(app, "/dashboard")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Basic ")
    assert "unauthorized" in denied.text

    assert (await get(app, "/dashboard", headers=basic_auth(password="wrong"))).status_code == 401
    assert (await get(app, "/dashboard", headers=basic_auth(username="nope"))).status_code == 401
    assert (
        await get(app, "/dashboard", headers={"Authorization": "Basic not-base64"})
    ).status_code == 401
    assert (await get(app, "/dashboard", headers=basic_auth())).status_code == 200


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
    page = await get(app, "/dashboard", headers=basic_auth())
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


async def test_dashboard_task_detail_is_basic_auth_protected(make_runtime, settings, clock) -> None:
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

    assert (await get(app, f"/dashboard/task/{task.id}")).status_code == 401
    detail = await get(app, f"/dashboard/task/{task.id}", headers=basic_auth())
    assert detail.status_code == 200
    assert "任务 " + task.short_id in detail.text
    assert "diff 超限" in detail.text
    assert "a.py +2000" in detail.text
    assert "事件时间线" in detail.text
    assert "返回 Dashboard" in detail.text
    assert TOKEN not in detail.text

    missing = await get(app, "/dashboard/task/task-doesnotexist", headers=basic_auth())
    assert missing.status_code == 404


async def test_dashboard_is_read_only_and_ignores_admin_token(
    make_runtime, settings, clock
) -> None:
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
    task = runtime.store.enqueue(make_trigger(issue_iid=4), now=clock()).task
    assert task is not None
    app = make_app(runtime, settings)

    # 只读：没有可写的 POST 入口
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/dashboard/task/{task.id}", headers=basic_auth())
    assert response.status_code == 405

    # 任务页的 token 不是 Dashboard 的凭据；两种入口互不混用
    assert (await get(app, "/dashboard", headers={"X-Admin-Token": TOKEN})).status_code == 401


async def test_dashboard_store_failure_returns_503_without_details(
    make_runtime, settings, monkeypatch
) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)

    def boom(**_: object) -> None:
        raise RuntimeError("db down at /secret/path")

    monkeypatch.setattr(runtime.store, "dashboard_stats", boom)
    app = make_app(runtime, settings)
    response = await get(app, "/dashboard", headers=basic_auth())
    assert response.status_code == 503
    assert "db down" not in response.text
    assert "/secret/path" not in response.text
