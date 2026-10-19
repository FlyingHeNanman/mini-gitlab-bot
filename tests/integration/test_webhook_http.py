"""HTTP 层：secret 校验、幂等、权限闸门，以及**绝不 5xx**这条铁律。"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from adapters.fakes import FakeSCM
from gateway.app import create_app
from tests.payloads import headers, issue_payload, note_payload

WEBHOOK = "/webhook/gitlab"


async def post(app, payload, hdrs, *, raw: bytes | None = None) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if raw is not None:
            return await client.post(WEBHOOK, content=raw, headers=hdrs)
        return await client.post(WEBHOOK, json=payload, headers=hdrs)


async def get(app, path: str) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --------------------------------------------------------------------------- #
# secret
# --------------------------------------------------------------------------- #


async def test_healthz_is_information_free(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    response = await get(create_app(settings=settings, runtime=runtime), "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_wrong_token_returns_401(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, issue_payload(), headers(token="wrong"))
    assert response.status_code == 401
    assert response.json() == {"status": "unauthorized"}
    assert runtime.store.count() == 0


async def test_unset_secret_fails_closed(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    insecure = settings.model_copy(update={"webhook_secret": ""})
    app = create_app(settings=insecure, runtime=runtime)
    response = await post(app, issue_payload(), headers(token=""))
    assert response.status_code == 401
    assert runtime.store.count() == 0


# --------------------------------------------------------------------------- #
# 正常入队与幂等
# --------------------------------------------------------------------------- #


async def test_label_event_is_queued(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, issue_payload(), headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"].startswith("task-")
    assert runtime.store.count() == 1


async def test_comment_command_is_queued(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(
        app, note_payload(note="/ai-fix retry"), headers(event="Note Hook", delivery="d-note")
    )
    assert response.json()["status"] == "queued"
    task = runtime.store.list_tasks()[0]
    assert task.command == "retry"


async def test_duplicate_delivery_is_answered_but_not_requeued(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    first = await post(app, issue_payload(), headers(delivery="same-key"))
    second = await post(app, issue_payload(), headers(delivery="same-key"))
    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "duplicate"
    assert second.json()["reason"] == "duplicate_delivery"
    assert runtime.store.count() == 1


# --------------------------------------------------------------------------- #
# 忽略与权限
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "hdrs", "reason"),
    [
        (issue_payload(path="other/repo"), headers(), "project_not_whitelisted"),
        (issue_payload(previous=("ai-fix",), current=()), headers(), "ai_fix_label_not_added"),
        (issue_payload(action="open", change_labels=False), headers(), "not_an_update_action"),
        (
            note_payload(note="added ~ai-fix label", system=True),
            headers(event="Note Hook", delivery="sys-1"),
            "system_note",
        ),
        ({"object_kind": "push"}, headers(), "unsupported_event"),
    ],
)
async def test_ignored_events_still_return_200(
    make_runtime, settings, payload, hdrs, reason: str
) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, payload, hdrs)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": reason}
    assert runtime.store.count() == 0


async def test_reporter_is_denied_by_permission_gate(make_runtime, settings) -> None:
    runtime, _ = make_runtime(scm=FakeSCM(access_levels={7: 20}))
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, issue_payload(), headers())
    assert response.status_code == 200
    assert response.json()["reason"] == "permission_denied:insufficient_access_level(20<30)"
    assert runtime.store.count() == 0


async def test_non_member_is_denied_by_permission_gate(make_runtime, settings) -> None:
    runtime, _ = make_runtime(scm=FakeSCM(access_levels={}))
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, issue_payload(), headers())
    assert response.json()["reason"] == "permission_denied:not_a_project_member"
    assert runtime.store.count() == 0


async def test_bot_own_event_is_ignored(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, issue_payload(username="mini-gitlab-bot"), headers())
    assert response.json() == {"status": "ignored", "reason": "self_triggered"}


# --------------------------------------------------------------------------- #
# 绝不 5xx
# --------------------------------------------------------------------------- #


async def test_adapter_exception_still_returns_200(make_runtime, settings) -> None:
    runtime, _ = make_runtime(scm=FakeSCM(fail_on=["get_member_access_level"]))
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, issue_payload(), headers())
    assert response.status_code == 200
    assert response.json()["reason"] == "permission_denied:permission_check_failed"


async def test_store_exception_still_returns_200(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)

    class ExplodingStore:
        def enqueue(self, *args, **kwargs):
            raise RuntimeError("磁盘炸了")

        def close(self) -> None:
            return None

    runtime.store = ExplodingStore()
    response = await post(app, issue_payload(), headers())
    assert response.status_code == 200
    assert response.json() == {"status": "error"}


async def test_malformed_json_still_returns_200(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, None, headers(), raw=b"{not json")
    assert response.status_code == 200
    assert response.json() == {"status": "error", "reason": "invalid_json"}


async def test_no_route_exposes_task_internals_without_a_token(make_runtime, settings) -> None:
    """任务信息默认受 token 保护：匿名访问 /tasks 一律 401，且不泄露内容。"""
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await get(app, "/tasks")
    assert response.status_code == 401
    assert "group/project-a" not in response.text
    assert (await get(app, "/")).status_code == 404
