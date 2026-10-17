"""文案渲染：marker、draft 前缀、Closes #N、固定失败措辞、degraded 报告。"""

from __future__ import annotations

from datetime import UTC, datetime

from core.models import IssueContext, MergeRequestRef, ProjectRef, Task, TriggerKind
from core.render import (
    MR_TITLE_PREFIX,
    extract_status_marker,
    mr_title,
    render_failed,
    render_mr_description,
    render_running,
    render_succeeded,
    status_marker,
)
from core.states import TaskState

START = datetime(2026, 10, 17, 10, 31, 0, tzinfo=UTC)

REPORT = {
    "summary": "把 add 里的减法改成加法",
    "fixes": ["修复 `parse_labels` 空标签返回 None（`gateway/config.py:42`）"],
    "others": ["补充类型注解（`core/models.py:10`）"],
    "tests": ["新增 `tests/unit/test_config.py::test_empty`"],
    "test_summary": "128 passed, 2 skipped",
    "test_duration": "4.2s",
    "not_run": ["ruff"],
    "risks": ["并发路径未覆盖"],
    "deps": ["tenacity>=9"],
    "agent_version": "codex 0.163.0",
}


def make_task(**overrides) -> Task:
    base: dict = {
        "id": "task-abc123",
        "delivery_key": "delivery-1",
        "project": ProjectRef(project_id=42, path="group/project-a"),
        "issue_iid": 12,
        "trigger_kind": TriggerKind.LABEL,
        "trigger_actor": "alice",
        "created_at": START,
        "updated_at": START,
    }
    base.update(overrides)
    return Task(**base)


def make_issue() -> IssueContext:
    return IssueContext(
        iid=12,
        title="parse_labels 在空标签时返回 None",
        description="复现步骤…",
        author="reporter",
        web_url="https://gitlab.example.com/group/project-a/-/issues/12",
    )


def test_status_marker_and_extraction_round_trip() -> None:
    body = f"{status_marker('task-abc123')}\n## 🤖 ai-fix · 运行中"
    assert extract_status_marker(body) == status_marker("task-abc123")
    assert extract_status_marker("没有任何标记") is None


def test_render_running_has_marker_and_stage() -> None:
    task = make_task(state=TaskState.PREPARING)
    body = render_running(task)
    assert body.splitlines()[0] == status_marker(task.id)
    assert "运行中" in body
    assert "准备环境" in body
    assert "请勿手动编辑" in body


def test_render_succeeded_links_mr_and_lists_risks() -> None:
    task = make_task(state=TaskState.SUCCEEDED, report=dict(REPORT), branch="ai-fix/12-fix")
    mr = MergeRequestRef(iid=45, url="https://gitlab.example.com/mr/45", title="Draft: fix")
    body = render_succeeded(task, mr=mr)
    assert body.splitlines()[0] == status_marker(task.id)
    assert "已完成" in body
    assert "!45" in body
    assert "Closes #12" in body
    assert "128 passed" in body
    assert "并发路径未覆盖" in body


def test_render_failed_contains_reason_evidence_and_retry_hint() -> None:
    task = make_task(
        state=TaskState.FAILED,
        failure_stage="running",
        failure_reason="codex 退出码 1，未产生 turn.completed 事件",
        failure_evidence="未通过：exit_code=1",
        branch="ai-fix/12-fix",
    )
    body = render_failed(task, log_tail="boom\ntraceback", log_lines=2)
    assert body.splitlines()[0] == status_marker(task.id)
    assert "失败" in body
    assert "codex 退出码 1" in body
    assert "exit_code=1" in body
    assert "如何重试" in body
    assert "/ai-fix retry" in body
    assert "traceback" in body


def test_render_failed_without_log_tail_still_valid() -> None:
    task = make_task(state=TaskState.FAILED, failure_stage="preparing", failure_reason="挂了")
    body = render_failed(task)
    assert "挂了" in body
    assert "<details>" not in body


def test_mr_title_has_draft_prefix_and_issue_iid() -> None:
    task = make_task()
    title = mr_title(task, issue_title="parse_labels 在空标签时返回 None")
    assert title.startswith(MR_TITLE_PREFIX)
    assert "issue-12" in title


def test_mr_title_truncates_long_titles() -> None:
    task = make_task()
    title = mr_title(task, issue_title="很长" * 200)
    assert len(title) <= len(MR_TITLE_PREFIX) + 20 + 100


def test_mr_description_structure() -> None:
    task = make_task(
        state=TaskState.SUCCEEDED,
        report=dict(REPORT),
        branch="ai-fix/12-fix",
        commit_sha="abc1234def5678",
    )
    body = render_mr_description(task, issue=make_issue())
    assert "Closes #12" in body
    for heading in (
        "## 概要",
        "## 改动",
        "### 修复",
        "### 测试",
        "## 验证",
        "## 风险与未决",
        "## Agent 元信息",
    ):
        assert heading in body, heading
    assert "tenacity>=9" in body
    assert "需要新增依赖" in body
    assert "codex 0.163.0" in body
    assert "自动生成" in body
    assert "**摘要**：把 add 里的减法改成加法" in body  # agent 的一句话摘要（M2）


def test_mr_description_degrades_when_report_is_empty() -> None:
    task = make_task(state=TaskState.SUCCEEDED, report={})
    body = render_mr_description(task, issue=make_issue())
    assert "Closes #12" in body
    assert "（未提供改动摘要）" in body
    assert "（无新增测试）" in body
    assert "✅ 未发现新增风险" in body
    assert "tenacity" not in body
