"""`/tasks` 的鉴权与 HTTP 行为：默认关闭、token 校验、只读。"""

from __future__ import annotations

from datetime import timedelta

import httpx
from httpx import ASGITransport

from core.states import TaskState
from gateway.app import create_app
from tests.helpers import make_trigger

TOKEN = "admin-token-0123456789"


def make_app(runtime, settings, *, token: str = TOKEN):
    return create_app(settings=settings.model_copy(update={"admin_token": token}), runtime=runtime)


async def get(app, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """默认跟随 303 —— 首次带 ?token= 会被换成 Cookie 再重定向。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        return await client.get(path, headers=headers or {})


async def test_tasks_is_404_when_no_admin_token(make_runtime, settings) -> None:
    """默认关闭：不配 ADMIN_TOKEN 就完全不可见（v1 行为不变）。"""
    runtime, _ = make_runtime()
    app = create_app(settings=settings.model_copy(update={"admin_token": ""}), runtime=runtime)
    for path in ("/tasks", "/tasks.json", "/tasks/task-1"):
        assert (await get(app, path)).status_code == 404


async def test_tasks_requires_the_token(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = make_app(runtime, settings)
    assert (await get(app, "/tasks")).status_code == 401
    assert (await get(app, "/tasks", headers={"X-Admin-Token": "nope"})).status_code == 401
    assert (await get(app, "/tasks.json?token=nope")).status_code == 401
    assert (await get(app, "/tasks", headers={"X-Admin-Token": TOKEN})).status_code == 200


async def test_query_token_is_exchanged_for_a_cookie(make_runtime, settings) -> None:
    """`?token=` 只出现在登录那一次：303 到无 token 的 URL + 种 HttpOnly Cookie。"""
    runtime, _ = make_runtime()
    app = make_app(runtime, settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(f"/tasks?token={TOKEN}&state=failed")
        assert first.status_code == 303
        assert first.headers["location"] == "/tasks?state=failed"  # 不带 token
        cookie = first.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "mgb_admin=" in cookie

        # Cookie 已种下 → 后续请求不再需要 token
        second = await client.get("/tasks?state=failed")
        assert second.status_code == 200
        assert "text/html" in second.headers["content-type"]


async def test_tasks_accepts_token_via_query_with_redirects(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = make_app(runtime, settings)
    response = await get(app, f"/tasks?token={TOKEN}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_tasks_lists_real_tasks(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True)
    task = runtime.store.enqueue(make_trigger(), now=clock()).task
    assert task is not None
    runtime.store.transition(task.id, TaskState.PREPARING, now=clock())
    runtime.store.transition(task.id, TaskState.RUNNING, now=clock(), branch="ai-fix/12-x")

    app = make_app(runtime, settings)

    page = await get(app, f"/tasks?token={TOKEN}")
    assert page.status_code == 200
    assert task.short_id in page.text
    assert "group/project-a" in page.text
    assert "running" in page.text

    data = (await get(app, f"/tasks.json?token={TOKEN}")).json()
    assert [item["id"] for item in data["tasks"]] == [task.id]
    assert data["tasks"][0]["state"] == "running"
    assert data["tasks"][0]["branch"] == "ai-fix/12-x"


async def test_tasks_filter_by_state(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True)
    queued = runtime.store.enqueue(make_trigger(issue_iid=1, delivery_key="d1"), now=clock()).task
    done = runtime.store.enqueue(make_trigger(issue_iid=2, delivery_key="d2"), now=clock()).task
    assert queued is not None and done is not None
    for state in (
        TaskState.PREPARING,
        TaskState.RUNNING,
        TaskState.VALIDATING,
        TaskState.PUSHING,
        TaskState.OPENING_MR,
        TaskState.SUCCEEDED,
    ):
        runtime.store.transition(done.id, state, now=clock() + timedelta(seconds=1))

    app = make_app(runtime, settings)
    data = (await get(app, f"/tasks.json?token={TOKEN}&state=succeeded")).json()
    assert [item["id"] for item in data["tasks"]] == [done.id]
    all_tasks = (await get(app, f"/tasks.json?token={TOKEN}")).json()
    assert len(all_tasks["tasks"]) == 2


async def test_invalid_filters_return_400(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = make_app(runtime, settings)
    assert (await get(app, f"/tasks.json?token={TOKEN}&state=nope")).status_code == 400
    assert (await get(app, f"/tasks.json?token={TOKEN}&limit=abc")).status_code == 400


async def test_task_detail_page_and_404(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True)
    task = runtime.store.enqueue(make_trigger(), now=clock()).task
    assert task is not None
    app = make_app(runtime, settings)

    ok = await get(app, f"/tasks/{task.id}?token={TOKEN}")
    assert ok.status_code == 200
    assert task.short_id in ok.text
    assert "没有可读的日志文件" in ok.text

    missing = await get(app, f"/tasks/task-doesnotexist?token={TOKEN}")
    assert missing.status_code == 404


async def test_token_is_not_echoed_on_failure(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = make_app(runtime, settings)
    body = (await get(app, "/tasks", headers={"X-Admin-Token": "wrong"})).text
    assert TOKEN not in body
