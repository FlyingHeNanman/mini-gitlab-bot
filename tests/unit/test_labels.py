"""标签体系契约：命名约束、互斥组、枚举校验、幂等创建所需的数据。"""

from __future__ import annotations

import pytest

from core import labels


def test_all_label_names_are_unique() -> None:
    names = [label.name for label in labels.LABELS]
    assert len(names) == len(set(names))
    assert len(labels.LABELS) == 32


def test_label_names_avoid_api_hostile_characters() -> None:
    # add_labels/remove_labels 是逗号拼接：逗号、引号、换行、emoji 会破坏请求
    for label in labels.LABELS:
        assert "," not in label.name
        assert '"' not in label.name
        assert "\n" not in label.name
        assert label.name == label.name.strip()
        assert 0 < len(label.name) <= 30


@pytest.mark.parametrize("label", labels.LABELS, ids=lambda item: item.name)
def test_group_matches_prefix_and_colors(label) -> None:
    assert labels.is_bot_label(label.name)
    assert label.color.startswith("#") and len(label.color) == 7
    assert label.description
    if label.group is not None:
        assert label.group in labels.SINGLE_VALUE_GROUPS
        assert labels.group_of(label.name) == label.group


@pytest.mark.parametrize("name", ["类型：缺陷", "严重度：一般", "状态：排查中"])
def test_lookup_group(name: str) -> None:
    assert labels.group_of(name) == name.split(labels.SEPARATOR)[0]


def test_non_bot_labels_have_no_group() -> None:
    assert labels.group_of("bug") is None
    assert labels.group_of("p1::urgent") is None


def test_component_labels_are_dynamic() -> None:
    assert labels.group_of(labels.component_label("gateway")) == "组件"
    assert labels.component_label("gateway") == "组件：gateway"
    assert not any(label.name == "组件：gateway" for label in labels.LABELS)


def test_exclusive_removals_only_same_group() -> None:
    current = ["状态：排查中", "状态：排队中", "证据：已复现", "bug"]
    assert labels.exclusive_removals(current, keep="状态：自动修复中") == (
        "状态：排查中",
        "状态：排队中",
    )
    # 多值组不互斥
    assert labels.exclusive_removals(current, keep="证据：有回归测试") == ()


def test_exclusive_removals_ignores_human_labels() -> None:
    assert labels.exclusive_removals(["bug", "p1"], keep="状态：已合并") == ()


def test_validate_triage_labels_accepts_known_values() -> None:
    assert labels.validate_triage_labels(type_value="缺陷", severity="一般") == ("缺陷", "一般")


@pytest.mark.parametrize(
    ("type_value", "severity"),
    [("bug", "一般"), ("缺陷", "S2"), ("严重度", "一般")],
)
def test_validate_triage_labels_rejects_unknown_values(type_value: str, severity: str) -> None:
    with pytest.raises(ValueError):
        labels.validate_triage_labels(type_value=type_value, severity=severity)


def test_missing_labels_is_idempotent() -> None:
    existing = {label.name for label in labels.LABELS}
    assert labels.missing_labels(existing) == ()
    partial = {"类型：缺陷", "严重度：一般"}
    missing = labels.missing_labels(partial)
    assert len(missing) == len(labels.LABELS) - len(partial)


def test_severity_is_fully_chinese() -> None:
    severity_names = [label.name for label in labels.labels_in_group("严重度")]
    assert severity_names == ["严重度：致命", "严重度：严重", "严重度：一般", "严重度：轻微"]
    assert not any(any(char.isdigit() for char in name) for name in severity_names)


def test_status_values_are_stable() -> None:
    status_names = [label.name for label in labels.labels_in_group("状态")]
    assert status_names == [
        "状态：排队中",
        "状态：排查中",
        "状态：待补充信息",
        "状态：待人工处理",
        "状态：自动修复中",
        "状态：待审批",
        "状态：已合并",
        "状态：已解决",
        "状态：已关闭重复",
        "状态：已关闭非缺陷",
        "状态：处理失败",
    ]
