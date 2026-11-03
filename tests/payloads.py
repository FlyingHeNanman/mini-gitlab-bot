"""GitLab webhook 载荷构造器（形状照抄真实 payload 的关键字段）。"""

from __future__ import annotations

from typing import Any

from core.labels import TRIGGER_LABEL

PROJECT_PATH = "group/project-a"
PROJECT_ID = 42


def _label_objects(titles: tuple[str, ...]) -> list[dict[str, Any]]:
    """`changes.labels.*` 的元素是**对象**，不是字符串（PLAN §6 #10）。"""
    return [{"id": index + 1, "title": title, "name": title} for index, title in enumerate(titles)]


def issue_payload(
    *,
    action: str = "update",
    iid: int = 12,
    state: str = "opened",
    previous: tuple[str, ...] = (),
    current: tuple[str, ...] = (TRIGGER_LABEL,),
    change_labels: bool = True,
    user_id: int | None = 7,
    username: str = "alice",
    project_id: int = PROJECT_ID,
    path: str = PROJECT_PATH,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "object_kind": "issue",
        "event_type": "issue",
        "user": {"id": user_id, "username": username},
        "project": {"id": project_id, "path_with_namespace": path},
        "object_attributes": {
            "iid": iid,
            "action": action,
            "state": state,
            "title": "parse_labels 在空标签时返回 None",
            "description": "复现步骤…",
            "url": f"https://gitlab.example.com/{path}/-/issues/{iid}",
            "labels": _label_objects(current),
        },
    }
    if change_labels:
        payload["changes"] = {
            "labels": {"previous": _label_objects(previous), "current": _label_objects(current)}
        }
    return payload


def note_payload(
    *,
    note: str = "/fix",
    system: bool = False,
    noteable_type: str = "Issue",
    iid: int = 13,
    user_id: int | None = 7,
    username: str = "alice",
    project_id: int = PROJECT_ID,
    path: str = PROJECT_PATH,
) -> dict[str, Any]:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": user_id, "username": username},
        "project": {"id": project_id, "path_with_namespace": path},
        "object_attributes": {
            "note": note,
            "system": system,
            "noteable_type": noteable_type,
            "noteable_iid": iid,
            "url": f"https://gitlab.example.com/{path}/-/issues/{iid}#note_1",
        },
        "issue": {"iid": iid},
    }


def merge_payload(
    *,
    action: str = "merge",
    state: str = "merged",
    iid: int = 3,
    title: str = "Draft: fix(issue-12): parse_labels 空标签",
    description: str = "Closes #12",
    merge_commit_sha: str = "deadbeefcafe",
    target_branch: str = "main",
    user_id: int | None = 7,
    username: str = "alice",
    project_id: int = PROJECT_ID,
    path: str = PROJECT_PATH,
) -> dict[str, Any]:
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {"id": user_id, "username": username},
        "project": {"id": project_id, "path_with_namespace": path},
        "object_attributes": {
            "iid": iid,
            "action": action,
            "state": state,
            "title": title,
            "description": description,
            "merge_commit_sha": merge_commit_sha,
            "target_branch": target_branch,
            "source_branch": "ai-fix/12-parse-labels",
            "url": f"https://gitlab.example.com/{path}/-/merge_requests/{iid}",
        },
    }


def tag_push_payload(
    *,
    tag: str = "v1.0.0",
    ref: str | None = None,
    before: str = "0" * 40,
    after: str = "deadbeefcafe1234",
    user_id: int | None = 7,
    username: str = "alice",
    project_id: int = PROJECT_ID,
    path: str = PROJECT_PATH,
) -> dict[str, Any]:
    return {
        "object_kind": "tag_push",
        "event_type": "tag_push",
        "user_id": user_id,
        "user_name": username,
        "ref": ref or f"refs/tags/{tag}",
        "before": before,
        "after": after,
        "checkout_sha": after,
        "project": {"id": project_id, "path_with_namespace": path},
        "commits": [],
        "total_commits_count": 0,
    }


def headers(
    *,
    token: str | None = "s3cr3t",
    event: str = "Issue Hook",
    delivery: str | None = "delivery-1",
) -> dict[str, str]:
    result: dict[str, str] = {"X-Gitlab-Event": event}
    if token is not None:
        result["X-Gitlab-Token"] = token
    if delivery is not None:
        result["Idempotency-Key"] = delivery
    return result
