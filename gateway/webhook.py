"""GitLab webhook 解析与校验（纯函数，便于单测）。

已核实的事实（PLAN §6）：

* #10 **标签变更没有独立事件** —— 必须认 issue hook `action=="update"` +
  `changes.labels.{previous,current}`，且元素是**对象**、要取 `title`。
  v0 只认 `open`/`reopen`，所以"打标签"这个主要用法永远不触发。
* 标签变更还会附带一条 **system note**，必须忽略，否则同一次打标签会触发两次；
* #9 webhook 连续失败 4 次临时禁用、40 次永久禁用 —— 所以调用方**绝不返回 5xx**。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.models import ProjectRef, Trigger, TriggerActor, TriggerKind

#: 投递幂等键候选请求头（按优先级）
DELIVERY_KEY_HEADERS: tuple[str, ...] = (
    "idempotency-key",
    "x-gitlab-event-uuid",
    "webhook-id",
    "x-request-id",
)

#: 只处理仍然打开的 issue（重新打开的 issue，state 同样是 "opened"）
ISSUE_OPEN_STATES = ("opened",)


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """解析结果。`trigger is None` 表示按设计忽略，`reason` 用于日志与响应体。"""

    reason: str
    delivery_key: str
    event_type: str = ""
    trigger: Trigger | None = None

    @property
    def is_trigger(self) -> bool:
        return self.trigger is not None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """大小写无关地取请求头（兼容 starlette Headers 与普通 dict）。"""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, item in headers.items():
        if key.lower() == lowered:
            return item
    return None


def verify_token(provided: str | None, expected: str) -> bool:
    """校验 `X-Gitlab-Token`。

    `expected` 为空视为**配置缺失** → 一律拒绝（fail closed），
    而不是"没配就放行"。
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def delivery_key(*, headers: Mapping[str, str], raw_body: bytes) -> str:
    for name in DELIVERY_KEY_HEADERS:
        value = _header(headers, name)
        if value:
            return f"{name}:{value}"
    return "sha256:" + hashlib.sha256(raw_body).hexdigest()


def _label_titles(items: Any) -> set[str]:
    """`changes.labels.*` 的元素是对象；兼容字符串写法。"""
    titles: set[str] = set()
    for item in items or ():
        if isinstance(item, str):
            titles.add(item)
        elif isinstance(item, Mapping):
            title = item.get("title") or item.get("name")
            if title:
                titles.add(str(title))
    return titles


def _ignored(reason: str, key: str, event_type: str) -> ParsedEvent:
    return ParsedEvent(reason=reason, delivery_key=key, event_type=event_type)


def _whitelisted(path: str, settings) -> bool:
    return bool(path) and path in set(settings.repo_whitelist)


def _common_checks(
    payload: Mapping[str, Any], settings, key: str, event_type: str
) -> tuple[ParsedEvent | None, dict[str, Any]]:
    """项目白名单 + 机器人自触发检查，返回 (忽略结果, 上下文)。"""
    project = payload.get("project") or {}
    path = str(project.get("path_with_namespace") or "")
    if not _whitelisted(path, settings):
        return _ignored("project_not_whitelisted", key, event_type), {}
    project_id = project.get("id")
    if not isinstance(project_id, int):
        return _ignored("missing_project_id", key, event_type), {}

    actor = payload.get("user") or {}
    username = str(actor.get("username") or "")
    if settings.bot_username and username == settings.bot_username:
        return _ignored("self_triggered", key, event_type), {}

    return None, {
        "project": ProjectRef(project_id=project_id, path=path),
        "actor": TriggerActor(
            user_id=actor.get("id") if isinstance(actor.get("id"), int) else None,
            username=username,
        ),
    }


def parse_webhook(
    *, headers: Mapping[str, str], payload: Mapping[str, Any], settings, raw_body: bytes = b""
) -> ParsedEvent:
    key = delivery_key(headers=headers, raw_body=raw_body)
    event_type = _header(headers, "x-gitlab-event") or ""
    object_kind = payload.get("object_kind")

    if object_kind == "issue":
        return _parse_issue(payload, settings, key, event_type)
    if object_kind == "note":
        return _parse_note(payload, settings, key, event_type)
    return _ignored("unsupported_event", key, event_type)


def _parse_issue(payload: Mapping[str, Any], settings, key: str, event_type: str) -> ParsedEvent:
    attrs = payload.get("object_attributes") or {}
    if attrs.get("action") != "update":
        return _ignored("not_an_update_action", key, event_type)

    label_change = (payload.get("changes") or {}).get("labels")
    if not label_change:
        return _ignored("no_label_change", key, event_type)

    current = _label_titles(label_change.get("current"))
    previous = _label_titles(label_change.get("previous"))
    label = settings.ai_fix_label
    if label not in (current - previous):
        return _ignored("ai_fix_label_not_added", key, event_type)

    state = attrs.get("state")
    if state is not None and state not in ISSUE_OPEN_STATES:
        return _ignored(f"issue_state_{state}", key, event_type)

    ignored, context = _common_checks(payload, settings, key, event_type)
    if ignored is not None:
        return ignored

    issue_iid = attrs.get("iid")
    if not isinstance(issue_iid, int):
        return _ignored("missing_issue_iid", key, event_type)

    return ParsedEvent(
        reason="label_added",
        delivery_key=key,
        event_type=event_type,
        trigger=Trigger(
            kind=TriggerKind.LABEL,
            project=context["project"],
            issue_iid=issue_iid,
            actor=context["actor"],
            delivery_key=key,
            issue_labels=tuple(sorted(current)),
            source=str(attrs.get("url") or ""),
        ),
    )


def parse_command(text: str, label: str) -> str | None:
    """把评论正文解析成命令：`/ai-fix` → `run`，`/ai-fix retry` → `retry`。"""
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    token = f"/{label}"
    if not first_line.lower().startswith(token.lower()):
        return None
    rest = first_line[len(token) :].strip().lower()
    if not rest:
        return "run"
    if rest in ("retry", "rerun"):
        return "retry"
    return None


def _parse_note(payload: Mapping[str, Any], settings, key: str, event_type: str) -> ParsedEvent:
    attrs = payload.get("object_attributes") or {}
    if attrs.get("system"):
        return _ignored("system_note", key, event_type)
    if attrs.get("noteable_type") != "Issue":
        return _ignored("note_not_on_issue", key, event_type)

    command = parse_command(str(attrs.get("note") or ""), settings.ai_fix_label)
    if command is None:
        return _ignored("not_a_command", key, event_type)

    ignored, context = _common_checks(payload, settings, key, event_type)
    if ignored is not None:
        return ignored

    issue = payload.get("issue") or {}
    issue_iid = issue.get("iid") or attrs.get("noteable_iid")
    if not isinstance(issue_iid, int):
        return _ignored("missing_issue_iid", key, event_type)

    return ParsedEvent(
        reason=f"command_{command}",
        delivery_key=key,
        event_type=event_type,
        trigger=Trigger(
            kind=TriggerKind.COMMENT,
            project=context["project"],
            issue_iid=issue_iid,
            actor=context["actor"],
            delivery_key=key,
            command=command,
            source=str(attrs.get("url") or ""),
        ),
    )
