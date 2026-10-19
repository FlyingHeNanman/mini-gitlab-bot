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
    """runtime 必须带着目标 token 构建（token 在 build_runtime 时解析）。"""
    return create_app(settings=settings, runtime=runtime)


async def get(app, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """默认跟随 303 —— 首次带 ?token= 会被换成 Cookie 再重定向。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        return await client.get(path, headers=headers or {})


async def test_tasks_is_404_when_the_page_is_disabled(make_runtime, settings) -> None:
    """显式关掉（或 token 持久化失败）时完全不可见。"""
    runtime, _ = make_runtime(tasks_page_enabled=False)
    assert runtime.admin_token == ""
    app = create_app(settings=settings, runtime=runtime)
    for path in ("/tasks", "/tasks.json", "/tasks/task-1"):
        assert (await get(app, path)).status_code == 404


async def test_tasks_requires_the_token(make_runtime, settings) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    assert (await get(app, "/tasks")).status_code == 401
    assert (await get(app, "/tasks", headers={"X-Admin-Token": "nope"})).status_code == 401
    assert (await get(app, "/tasks.json?token=nope")).status_code == 401
    assert (await get(app, "/tasks", headers={"X-Admin-Token": TOKEN})).status_code == 200


async def test_query_token_is_exchanged_for_a_cookie(make_runtime, settings) -> None:
    """`?token=` 只出现在登录那一次：303 到无 token 的 URL + 种 HttpOnly Cookie。"""
    runtime, _ = make_runtime(admin_token=TOKEN)
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
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    response = await get(app, f"/tasks?token={TOKEN}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_tasks_lists_real_tasks(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
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
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
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
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    assert (await get(app, f"/tasks.json?token={TOKEN}&state=nope")).status_code == 400
    assert (await get(app, f"/tasks.json?token={TOKEN}&limit=abc")).status_code == 400


async def test_task_detail_page_and_404(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True, admin_token=TOKEN)
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
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    body = (await get(app, "/tasks", headers={"X-Admin-Token": "wrong"})).text
    assert TOKEN not in body


async def test_token_is_generated_and_persisted_when_unset(make_runtime) -> None:
    """默认开启：没配 ADMIN_TOKEN 就自动生成，并落到 DB 同目录的 0600 文件里。"""
    runtime, _ = make_runtime()
    token = runtime.admin_token
    assert len(token) >= 16
    token_file = runtime.settings.db_path.parent / "admin_token"
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert (token_file.stat().st_mode & 0o777) == 0o600

    # 同一个 DB 目录再起一个 runtime → 复用同一个 token（重启后 URL 不变）
    again, _ = make_runtime()
    assert again.admin_token == token


async def test_explicit_admin_token_wins(make_runtime) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)
    assert runtime.admin_token == TOKEN


async def test_stale_cookie_does_not_lock_out_a_new_token(make_runtime, settings) -> None:
    """浏览器里可能残留旧 token 的 cookie：带新 token 的链接必须仍能进（并覆盖 cookie）。"""
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("mgb_admin", "old-token-from-a-previous-run")
        # 旧 cookie 单独用 → 401（不会因为带了 cookie 就放行）
        assert (await client.get("/tasks", follow_redirects=False)).status_code == 401
        # 但带新 token 的链接必须能进：303 + 覆盖 cookie
        fresh = await client.get(f"/tasks?token={TOKEN}", follow_redirects=False)
        assert fresh.status_code == 303
        assert fresh.headers["set-cookie"].startswith(f"mgb_admin={TOKEN}")
        # 覆盖后正常访问
        assert (await client.get("/tasks")).status_code == 200


async def test_any_credential_source_is_enough(make_runtime, settings) -> None:
    runtime, _ = make_runtime(admin_token=TOKEN)
    app = make_app(runtime, settings)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 错误 header + 正确 cookie → 放行（或关系，不是与关系）
        client.cookies.set("mgb_admin", TOKEN)
        response = await client.get("/tasks", headers={"X-Admin-Token": "wrong"})
        assert response.status_code == 200
