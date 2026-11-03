"""旧标签迁移：纯计划函数 + 幂等语义（不碰真 GitLab）。"""

from __future__ import annotations

from core import labels
from core.label_sync import FALLBACK_COMPONENT_LABEL
from scripts.migrate_labels import migration_plan


def test_plan_detects_legacy_labels() -> None:
    existing = [
        "ai-fix",
        "ai-fix:running",
        "ai-fix:done",
        "ai-fix:failed",
        "类型：缺陷",
        "无关标签",
    ]
    _, to_delete = migration_plan(existing)
    assert to_delete == list(labels.LEGACY_FIX_LABELS)


def test_plan_creates_all_bot_labels_and_fallback() -> None:
    to_create, _ = migration_plan([])
    created = {item.name for item in to_create}
    assert created == {item.name for item in labels.LABELS} | {FALLBACK_COMPONENT_LABEL.name}


def test_plan_is_idempotent_when_labels_already_exist() -> None:
    existing = [item.name for item in labels.LABELS] + [FALLBACK_COMPONENT_LABEL.name]
    to_create, to_delete = migration_plan(existing)
    assert to_create == []
    assert to_delete == []


def test_plan_keeps_unrelated_labels() -> None:
    _, to_delete = migration_plan(["bug", "ai-fix"])
    assert to_delete == ["ai-fix"]
