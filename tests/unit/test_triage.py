"""triage 契约：JSON 解析、组件推导、标签互斥、评论渲染。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core import labels
from core.models import IssueContext, ProjectRef, Task, TriggerKind
from core.triage import (
    TriageResult,
    TriageResultError,
    TriageVerdict,
    common_component,
    label_plan,
    parse_triage_result,
    render_triage_comment,
    render_triage_failure,
    triage_marker,
)

START = datetime(2026, 10, 17, 10, 31, 0, tzinfo=UTC)


def make_task(**overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": "task-triage",
        "delivery_key": "delivery-1",
        "project": ProjectRef(project_id=42, path="group/project-a"),
        "issue_iid": 12,
        "trigger_kind": TriggerKind.ISSUE_OPEN,
        "trigger_actor": "reporter",
        "created_at": START,
        "updated_at": START,
    }
    base.update(overrides)
    return Task(**base)


def make_issue() -> IssueContext:
    return IssueContext(
        iid=12,
        title="gateway 在空标签时崩溃",
        description="复现步骤…",
        author="reporter",
        web_url="https://gitlab.example.com/group/project-a/-/issues/12",
        labels=("ai-fix", "类型：新功能", "严重度：严重"),
    )


def make_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "verdict": "confirmed",
        "confidence": 0.82,
        "summary": "崩溃确实存在：空标签被当成 None 迭代",
        "repro": {
            "command": "UV_OFFLINE=1 uv run --no-sync pytest tests/unit/test_config.py -q",
            "result": "reproduced",
            "evidence": "AttributeError: 'NoneType' object is not iterable",
        },
        "location": {"files": ["gateway/config.py:42", "gateway/config.py"]},
        "similar": [{"iid": 7, "relation": "duplicate", "reason": "同一个空标签问题"}],
        "labels": {"type": "缺陷", "severity": "一般", "component": "gateway"},
        "recommendation": "self_fix",
    }
    report.update(overrides)
    return report


def test_parse_valid_report() -> None:
    result = parse_triage_result(make_report())
    assert result.verdict is TriageVerdict.CONFIRMED
    assert result.confidence == 0.82
    assert result.type_value == "缺陷"
    assert result.severity == "一般"
    assert result.files == ("gateway/config.py",)  # 去重 + 去掉行号
    assert result.repro_result == "reproduced"
    assert result.similar == ((7, "duplicate", "同一个空标签问题"),)
    assert result.recommendation == "self_fix"
    assert result.component == "gateway"
    assert result.status_label == labels.STATUS_NEEDS_HUMAN
    assert result.evidence_builders == (labels.EVIDENCE_REPRODUCED,)


@pytest.mark.parametrize("verdict", ["", "fixed", "CONFIRMED", None])
def test_parse_rejects_bad_verdict(verdict: Any) -> None:
    with pytest.raises(TriageResultError, match="verdict"):
        parse_triage_result(make_report(verdict=verdict))


@pytest.mark.parametrize("confidence", ["abc", None, -0.1, 1.1])
def test_parse_rejects_bad_confidence(confidence: Any) -> None:
    with pytest.raises(TriageResultError, match="confidence"):
        parse_triage_result(make_report(confidence=confidence))


def test_parse_rejects_missing_type_or_severity() -> None:
    with pytest.raises(TriageResultError, match="类型"):
        parse_triage_result(make_report(labels={"type": "", "severity": "一般"}))
    with pytest.raises(TriageResultError, match="严重度"):
        parse_triage_result(make_report(labels={"type": "缺陷", "severity": "低"}))


def test_parse_rejects_empty_summary() -> None:
    with pytest.raises(TriageResultError, match="summary"):
        parse_triage_result(make_report(summary="   "))


def test_parse_tolerates_unknown_enums_that_have_fallbacks() -> None:
    result = parse_triage_result(make_report(repro={"result": "flaky"}, recommendation="merge_now"))
    assert result.repro_result == "not_run"
    assert result.recommendation == "assign"


def test_component_is_common_top_level_dir() -> None:
    assert common_component(["gateway/a.py", "gateway/b.py"]) == "gateway"
    assert common_component(["gateway/a.py", "core/b.py"]) == "其他"
    assert common_component([]) == "其他"
    assert common_component(["README.md"]) == "其他"


def test_label_plan_adds_and_removes_exclusive_groups() -> None:
    issue = make_issue()
    result = parse_triage_result(make_report())
    add, remove = label_plan(issue.labels, result)
    assert "类型：缺陷" in add
    assert "严重度：一般" in add
    assert "组件：gateway" in add
    assert labels.STATUS_NEEDS_HUMAN in add
    assert labels.EVIDENCE_REPRODUCED in add
    assert "类型：新功能" in remove
    assert "严重度：严重" in remove
    # 非单值组（ai-fix、证据）不动
    assert "ai-fix" not in remove
    assert labels.EVIDENCE_REPRODUCED not in remove


@pytest.mark.parametrize(
    ("verdict", "repro_result", "status", "evidence"),
    [
        ("confirmed", "reproduced", labels.STATUS_NEEDS_HUMAN, labels.EVIDENCE_REPRODUCED),
        ("confirmed", "failed", labels.STATUS_NEEDS_HUMAN, labels.EVIDENCE_NOT_REPRODUCED),
        ("confirmed", "not_run", labels.STATUS_NEEDS_HUMAN, None),
        ("need_info", "not_run", labels.STATUS_NEEDS_INFO, labels.EVIDENCE_NO_ENV),
        ("need_info", "reproduced", labels.STATUS_NEEDS_INFO, labels.EVIDENCE_REPRODUCED),
    ],
)
def test_status_and_evidence_mapping(
    verdict: str, repro_result: str, status: str, evidence: str | None
) -> None:
    result = parse_triage_result(make_report(verdict=verdict, repro={"result": repro_result}))
    assert result.status_label == status
    if evidence is None:
        assert result.evidence_builders == ()
    else:
        assert result.evidence_builders == (evidence,)


def test_triage_comment_has_marker_and_verdict() -> None:
    result = parse_triage_result(make_report())
    body = render_triage_comment(make_task(), make_issue(), result, now=START)
    assert body.splitlines()[0] == triage_marker(12)
    assert "自动巡检结论：确认成立" in body
    assert "崩溃确实存在" in body
    assert "`组件：gateway`" in body
    assert "`gateway/config.py`" in body
    assert "#7（duplicate）" in body
    assert "未经人工确认" in body


def test_duplicate_comment_says_疑似重复() -> None:
    result = TriageResult(
        verdict=TriageVerdict.DUPLICATE,
        confidence=0.9,
        summary="与 #7 重复",
        type_value="缺陷",
        severity="一般",
        similar=((7, "duplicate", "同因"),),
    )
    body = render_triage_comment(make_task(), make_issue(), result, now=START)
    assert "自动巡检结论：疑似重复" in body
    assert "#7（duplicate）" in body


def test_failure_comment_has_stage_reason_and_retry_hint() -> None:
    body = render_triage_failure(
        make_task(),
        make_issue(),
        stage="running",
        reason="巡检结论格式不合法：verdict 非法",
        log_tail="Traceback…",
        now=START,
    )
    assert body.splitlines()[0] == triage_marker(12)
    assert "自动巡检失败" in body
    assert "巡检结论格式不合法" in body
    assert "重新打开 issue" in body
    assert "Traceback" in body
