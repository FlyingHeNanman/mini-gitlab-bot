"""自动修裁决：模型只给估计，网关按确定性规则放行/拒绝。"""

from __future__ import annotations

from typing import Any

import pytest

from core import labels
from core.autofix import AutofixConfig, adjudicate
from core.triage import parse_triage_result

CONFIG = AutofixConfig(
    enabled=True,
    max_files=3,
    max_lines=150,
    min_confidence=0.7,
    min_access_level=0,
    forbidden_paths=(".gitlab-ci.yml", "*.pem", ".gitlab/"),
)

BASE: dict[str, Any] = {
    "verdict": "confirmed",
    "confidence": 0.9,
    "summary": "空标签返回 None",
    "repro": {"result": "reproduced", "evidence": "1 failed"},
    "location": {"files": ["app.py:4"]},
    "labels": {"type": "缺陷", "severity": "一般"},
    "size": {"files": 1, "lines": 4, "risk": "low"},
    "recommendation": "self_fix",
}


def make_result(**overrides: Any):
    report = {**BASE, **overrides}
    return parse_triage_result(report)


def test_small_bug_is_eligible() -> None:
    decision = adjudicate(make_result(), CONFIG)
    assert decision.allowed
    assert decision.reason == "eligible"
    assert decision.risk_labels == ()


@pytest.mark.parametrize(
    ("overrides", "reason", "risk"),
    [
        ({"confidence": 0.5}, "low_confidence", None),
        ({"verdict": "not_reproducible"}, "verdict_not_reproducible", None),
        ({"recommendation": "assign"}, "recommendation_assign", None),
        ({"location": {"files": []}}, "no_location", None),
        (
            {"size": {"files": 4, "lines": 10, "risk": "low"}},
            "too_many_files",
            labels.RISK_LARGE_DIFF,
        ),
        (
            {"size": {"files": 1, "lines": 151, "risk": "low"}},
            "too_many_lines",
            labels.RISK_LARGE_DIFF,
        ),
        (
            {"size": {"files": 1, "lines": 4, "risk": "high"}},
            "risk_not_low",
            labels.RISK_NEEDS_HUMAN,
        ),
        ({"deps": ["httpx>=0.27"]}, "needs_dependencies", labels.RISK_NEEDS_DEP),
    ],
)
def test_rejections(overrides: dict[str, Any], reason: str, risk: str | None) -> None:
    decision = adjudicate(make_result(**overrides), CONFIG)
    assert not decision.allowed
    assert decision.reason == reason
    if risk:
        assert risk in decision.risk_labels


def test_cross_component_is_rejected() -> None:
    decision = adjudicate(make_result(location={"files": ["gateway/a.py", "core/b.py"]}), CONFIG)
    assert not decision.allowed
    assert decision.reason == "cross_component"
    assert labels.RISK_CROSS_MODULE in decision.risk_labels


def test_forbidden_path_is_rejected() -> None:
    decision = adjudicate(make_result(location={"files": [".gitlab-ci.yml"]}), CONFIG)
    assert not decision.allowed
    assert decision.reason.startswith("forbidden_path:")


def test_disabled_switch_rejects() -> None:
    decision = adjudicate(make_result(), AutofixConfig(enabled=False))
    assert not decision.allowed
    assert decision.reason == "autofix_disabled"


def test_access_level_threshold() -> None:
    config = AutofixConfig(enabled=True, min_access_level=30)
    assert not adjudicate(make_result(), config, access_level=20).allowed
    assert not adjudicate(make_result(), config, access_level=None).allowed
    assert adjudicate(make_result(), config, access_level=30).allowed
