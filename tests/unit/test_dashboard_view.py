"""Dashboard 渲染：手动刷新、只读、转义、空态。"""

from __future__ import annotations

from datetime import UTC, datetime

from core.dashboard_view import DashboardData, render_dashboard, render_dashboard_task
from core.models import DashboardStats, ProjectRef, ProjectSummary, Task, TaskEvent
from core.states import TaskKind, TaskState

NOW = datetime(2026, 11, 4, 3, 0, 0, tzinfo=UTC)


def make_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "id": "task-abc",
        "delivery_key": "d-1",
        "project": ProjectRef(project_id=42, path="group/project-a"),
        "issue_iid": 12,
        "trigger_kind": "label",
        "trigger_actor": "alice",
        "created_at": NOW,
        "updated_at": NOW,
        "kind": TaskKind.TRIAGE,
        "state": TaskState.FAILED,
        "failure_stage": "running",
        "failure_reason": "模型返回空结果",
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def make_data(**overrides: object) -> DashboardData:
    values: dict[str, object] = {
        "now": NOW,
        "version": "0.2.1",
        "worker_enabled": True,
        "stalled": False,
        "stats": DashboardStats(3, 1, 1, 1, 1, 2, 1, 1),
        "projects": (
            ProjectSummary(ProjectRef(42, "group/project-a"), 2, 1, 0, 1, NOW),
            ProjectSummary(ProjectRef(7, "group/project-b"), 1, 0, 1, 0, NOW),
        ),
        "recent": (make_task(),),
        "failures": (make_task(),),
        "metrics": {"sweep_runs_total": 4, "sweep_errors_total": 1},
        "gitlab_url": "https://gitlab.example.com",
    }
    values.update(overrides)
    return DashboardData(**values)  # type: ignore[arg-type]


def test_dashboard_is_read_only_and_manually_refreshed() -> None:
    body = render_dashboard(make_data())
    assert "刷新数据" in body
    assert 'http-equiv="refresh"' not in body
    assert "只读" in body
    assert "不提供重试 / 取消 / 手动触发" in body


def test_dashboard_escapes_external_text() -> None:
    malicious = make_task(
        project=ProjectRef(42, "<script>alert(1)</script>"),
        failure_reason='<img src=x onerror="alert(2)">',
    )
    body = render_dashboard(
        make_data(
            projects=(
                ProjectSummary(ProjectRef(42, "<script>alert(1)</script>"), 1, 0, 0, 1, NOW),
            ),
            recent=(malicious,),
            failures=(malicious,),
        )
    )
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert '<img src=x onerror="alert(2)">' not in body
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in body


def test_dashboard_empty_state() -> None:
    body = render_dashboard(
        make_data(
            stats=DashboardStats(0, 0, 0, 0, 0, 0, 0, 0),
            projects=(),
            recent=(),
            failures=(),
            metrics={},
        )
    )
    assert "还没有任务" in body
    assert "暂无失败任务" in body
    assert "还没有项目数据" in body


def test_dashboard_task_detail_renders_events_and_issue_link() -> None:
    task = make_task(failure_evidence="traceback")
    events = [
        TaskEvent(at=NOW, kind="queued", detail={"by": "alice"}),
        TaskEvent(at=NOW, kind="failed", detail={"reason": "empty"}),
    ]
    body = render_dashboard_task(
        task,
        events=events,
        log_tail="codex log tail",
        log_lines=3,
        now=NOW,
        gitlab_url="https://gitlab.example.com",
    )
    assert "任务 abc" in body
    assert "https://gitlab.example.com/group/project-a/-/issues/12" in body
    assert "traceback" in body
    assert "queued" in body and "failed" in body
    assert "codex log tail" in body
    assert "返回 Dashboard" in body
