"""webhook 解析：标签状态机、评论命令、system note、幂等键、secret 校验。"""

from __future__ import annotations

import pytest

from core.models import TriggerKind
from gateway.webhook import delivery_key, parse_command, parse_webhook, verify_token
from tests.payloads import headers, issue_payload, note_payload


def _parse(payload, *, hdrs=None, settings=None, raw=b"{}"):
    return parse_webhook(
        headers=hdrs or headers(), payload=payload, settings=settings, raw_body=raw
    )


# --------------------------------------------------------------------------- #
# secret
# --------------------------------------------------------------------------- #


def test_verify_token_accepts_matching_secret() -> None:
    assert verify_token("s3cr3t", "s3cr3t") is True


@pytest.mark.parametrize(
    ("provided", "expected"),
    [(None, "s3cr3t"), ("wrong", "s3cr3t"), ("s3cr3t", ""), (None, ""), ("", "s3cr3t")],
)
def test_verify_token_fails_closed(provided: str | None, expected: str) -> None:
    assert verify_token(provided, expected) is False


# --------------------------------------------------------------------------- #
# 标签触发（主要用法）
# --------------------------------------------------------------------------- #


def test_label_added_triggers(settings) -> None:
    event = _parse(issue_payload(previous=(), current=("ai-fix",)), settings=settings)
    assert event.trigger is not None
    assert event.reason == "label_added"
    trigger = event.trigger
    assert trigger.kind is TriggerKind.LABEL
    assert trigger.issue_iid == 12
    assert trigger.actor.username == "alice"
    assert trigger.actor.user_id == 7
    assert trigger.project.project_id == 42
    assert trigger.project.path == "group/project-a"
    assert "ai-fix" in trigger.issue_labels


def test_label_added_alongside_other_labels_triggers(settings) -> None:
    event = _parse(issue_payload(previous=("bug",), current=("bug", "ai-fix")), settings=settings)
    assert event.trigger is not None


def test_label_removed_does_not_trigger(settings) -> None:
    event = _parse(issue_payload(previous=("ai-fix",), current=()), settings=settings)
    assert event.trigger is None
    assert event.reason == "ai_fix_label_not_added"


def test_open_action_without_label_change_is_ignored(settings) -> None:
    event = _parse(
        issue_payload(action="open", change_labels=False, current=("ai-fix",)), settings=settings
    )
    assert event.trigger is None
    assert event.reason == "not_an_update_action"


def test_closed_issue_is_ignored(settings) -> None:
    event = _parse(issue_payload(state="closed"), settings=settings)
    assert event.trigger is None
    assert event.reason == "issue_state_closed"


def test_non_whitelisted_project_is_ignored(settings) -> None:
    event = _parse(issue_payload(path="other/secret-repo"), settings=settings)
    assert event.trigger is None
    assert event.reason == "project_not_whitelisted"


def test_bot_own_label_event_is_ignored(settings) -> None:
    event = _parse(issue_payload(username="mini-gitlab-bot"), settings=settings)
    assert event.trigger is None
    assert event.reason == "self_triggered"


# --------------------------------------------------------------------------- #
# 评论命令
# --------------------------------------------------------------------------- #


def test_comment_command_triggers(settings) -> None:
    event = _parse(note_payload(note="/ai-fix"), hdrs=headers(event="Note Hook"), settings=settings)
    assert event.trigger is not None
    assert event.trigger.kind is TriggerKind.COMMENT
    assert event.trigger.command == "run"
    assert event.trigger.issue_iid == 13


def test_comment_retry_command(settings) -> None:
    event = _parse(
        note_payload(note="/ai-fix retry"), hdrs=headers(event="Note Hook"), settings=settings
    )
    assert event.trigger is not None
    assert event.trigger.command == "retry"


def test_system_note_from_label_change_is_ignored(settings) -> None:
    # 打标签会附带一条 system note；不忽略就会重复触发（PLAN §6 #10）
    event = _parse(
        note_payload(note="added ~ai-fix label", system=True),
        hdrs=headers(event="Note Hook"),
        settings=settings,
    )
    assert event.trigger is None
    assert event.reason == "system_note"


def test_note_on_merge_request_is_ignored(settings) -> None:
    event = _parse(
        note_payload(note="/ai-fix", noteable_type="MergeRequest"),
        hdrs=headers(event="Note Hook"),
        settings=settings,
    )
    assert event.trigger is None
    assert event.reason == "note_not_on_issue"


def test_bot_own_comment_is_ignored(settings) -> None:
    event = _parse(
        note_payload(note="/ai-fix", username="mini-gitlab-bot"),
        hdrs=headers(event="Note Hook"),
        settings=settings,
    )
    assert event.trigger is None
    assert event.reason == "self_triggered"


@pytest.mark.parametrize(
    "note",
    ["/ai-fixx", "please run /ai-fix", "ai-fix", "/ai-fix please now", "/other"],
)
def test_non_command_notes_are_ignored(note: str, settings) -> None:
    event = _parse(note_payload(note=note), hdrs=headers(event="Note Hook"), settings=settings)
    assert event.trigger is None
    assert event.reason == "not_a_command"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ai-fix", "run"),
        ("/ai-fix retry", "retry"),
        ("/AI-FIX Rerun", "retry"),
        ("/ai-fix x", None),
    ],
)
def test_parse_command(text: str, expected: str | None) -> None:
    assert parse_command(text, "ai-fix") == expected


# --------------------------------------------------------------------------- #
# 幂等键与其他事件
# --------------------------------------------------------------------------- #


def test_delivery_key_prefers_idempotency_header(settings) -> None:
    event = _parse(issue_payload(), hdrs=headers(delivery="abc-1"), settings=settings)
    assert event.delivery_key == "idempotency-key:abc-1"


def test_delivery_key_falls_back_to_body_hash(settings) -> None:
    first = _parse(issue_payload(), hdrs=headers(delivery=None), settings=settings, raw=b"same")
    second = _parse(issue_payload(), hdrs=headers(delivery=None), settings=settings, raw=b"same")
    third = _parse(issue_payload(), hdrs=headers(delivery=None), settings=settings, raw=b"other")
    assert first.delivery_key == second.delivery_key
    assert first.delivery_key != third.delivery_key
    assert first.delivery_key.startswith("sha256:")


def test_header_lookup_is_case_insensitive() -> None:
    key = delivery_key(headers={"IDEMPOTENCY-KEY": "x-9"}, raw_body=b"")
    assert key == "idempotency-key:x-9"


def test_unsupported_event_is_ignored(settings) -> None:
    event = _parse({"object_kind": "push"}, settings=settings)
    assert event.trigger is None
    assert event.reason == "unsupported_event"
