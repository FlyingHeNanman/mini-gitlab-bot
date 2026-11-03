"""HTTP 层：secret 校验、幂等、权限闸门，以及**绝不 5xx**这条铁律。"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from adapters.fakes import FakeAgent, FakeSCM, default_agent_result
from core.labels import TRIAGE_LABEL, TRIGGER_LABEL
from core.models import TriggerKind
from core.states import TaskKind, TaskState
from gateway.app import create_app
from tests.payloads import (
    headers,
    issue_payload,
    merge_payload,
    note_payload,
    tag_push_payload,
)

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
        app, note_payload(note="/fix retry"), headers(event="Note Hook", delivery="d-note")
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
        (
            issue_payload(previous=(TRIGGER_LABEL,), current=()),
            headers(),
            "bot_label_not_added",
        ),
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


async def test_open_issue_ignored_when_triage_disabled(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    disabled = settings.model_copy(update={"triage_enabled": False})
    app = create_app(settings=disabled, runtime=runtime)
    response = await post(app, issue_payload(action="open", change_labels=False), headers())
    assert response.json() == {"status": "ignored", "reason": "triage_disabled"}
    assert runtime.store.count() == 0


async def test_open_issue_queues_triage_without_permission_gate(make_runtime, settings) -> None:
    """新 issue 的自动巡检对所有人生效：非成员也能触发（人工命令才过闸门）。"""
    runtime, _ = make_runtime(scm=FakeSCM(access_levels={}))
    enabled = settings.model_copy(update={"triage_enabled": True})
    app = create_app(settings=enabled, runtime=runtime)
    response = await post(
        app,
        issue_payload(action="open", change_labels=False, current=()),
        headers(delivery="triage-1"),
    )
    body = response.json()
    assert body["status"] == "queued"
    task = runtime.store.get(body["task_id"])
    assert task is not None
    assert task.kind is TaskKind.TRIAGE
    assert task.trigger_kind is TriggerKind.ISSUE_OPEN
    assert task.subject_key == "issue:12"
    assert runtime.store.count() == 1


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


async def test_open_issue_full_triage_flow(make_runtime, settings) -> None:
    """webhook → 入队 → worker → 标签/评论，整条链路在假件上跑通。"""
    report = {
        "verdict": "confirmed",
        "confidence": 0.8,
        "summary": "确认可以复现",
        "repro": {"result": "reproduced", "evidence": "AssertionError"},
        "location": {"files": ["gateway/config.py:42"]},
        "labels": {"type": "缺陷", "severity": "一般"},
        "recommendation": "assign",
    }
    runtime, scm = make_runtime(
        agent=FakeAgent(result=default_agent_result(report=report)),
        with_worker=True,
    )
    enabled = settings.model_copy(update={"triage_enabled": True})
    app = create_app(settings=enabled, runtime=runtime)
    response = await post(
        app,
        issue_payload(action="open", change_labels=False, current=()),
        headers(delivery="triage-e2e"),
    )
    body = response.json()
    assert body["status"] == "queued"

    assert await runtime.worker.drain() == 1
    task = runtime.store.get(body["task_id"])
    assert task.state is TaskState.SUCCEEDED
    assert "类型：缺陷" in scm.current_labels
    assert "组件：gateway" in scm.current_labels
    assert any("自动巡检结论" in c["body"] for c in scm.comments)


async def test_triage_label_queues_triage_task(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(
        app,
        issue_payload(previous=(), current=(TRIAGE_LABEL,)),
        headers(delivery="tri-label-1"),
    )
    body = response.json()
    assert body["status"] == "queued"
    task = runtime.store.get(body["task_id"])
    assert task.kind is TaskKind.TRIAGE
    assert task.command == "triage"


async def test_triage_label_respects_permission_gate(make_runtime, settings) -> None:
    runtime, _ = make_runtime(scm=FakeSCM(access_levels={7: 20}))
    app = create_app(settings=settings, runtime=runtime)
    response = await post(
        app,
        issue_payload(previous=(), current=(TRIAGE_LABEL,)),
        headers(delivery="tri-label-2"),
    )
    assert response.json()["reason"] == "permission_denied:insufficient_access_level(20<30)"
    assert runtime.store.count() == 0


async def test_comment_triage_command_is_queued(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(
        app, note_payload(note="/triage"), headers(event="Note Hook", delivery="d-triage")
    )
    assert response.json()["status"] == "queued"
    task = runtime.store.list_tasks()[0]
    assert task.kind is TaskKind.TRIAGE
    assert task.command == "triage"


async def test_merge_event_queues_explain(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(app, merge_payload(), headers(event="Merge Request Hook", delivery="m-1"))
    body = response.json()
    assert body["status"] == "queued"
    task = runtime.store.get(body["task_id"])
    assert task.kind is TaskKind.EXPLAIN
    assert task.mr_iid == 3
    assert task.issue_iid == 12
    assert task.subject_key == "mr:3:deadbeefcafe"


async def test_triage_quota_limits_per_project(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    limited = settings.model_copy(update={"triage_quota_per_hour": 1})
    app = create_app(settings=limited, runtime=runtime)
    first = await post(
        app,
        issue_payload(action="open", change_labels=False, iid=12, current=()),
        headers(delivery="q-1"),
    )
    assert first.json()["status"] == "queued"
    second = await post(
        app,
        issue_payload(action="open", change_labels=False, iid=13, current=()),
        headers(delivery="q-2"),
    )
    assert second.json() == {"status": "ignored", "reason": "triage_quota_exceeded"}
    assert runtime.store.count() == 1


async def test_tag_push_queues_release(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(settings=settings, runtime=runtime)
    response = await post(
        app,
        tag_push_payload(tag="v1.0.0", after="abc123"),
        headers(event="Tag Push Hook", delivery="tag-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    task = runtime.store.get(body["task_id"])
    assert task.kind is TaskKind.RELEASE
    assert task.trigger_kind is TriggerKind.TAG_PUSH
    assert task.issue_iid == 0
    assert task.subject_key == "tag:v1.0.0"
    assert task.payload["tag_name"] == "v1.0.0"


async def test_tag_push_ignored_when_release_disabled(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    disabled = settings.model_copy(update={"release_enabled": False})
    app = create_app(settings=disabled, runtime=runtime)
    response = await post(
        app,
        tag_push_payload(),
        headers(event="Tag Push Hook", delivery="tag-2"),
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "release_disabled"}
    assert runtime.store.count() == 0
