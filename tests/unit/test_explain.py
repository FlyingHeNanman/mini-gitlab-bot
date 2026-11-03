"""合并解释：diff 截断、评论 marker、模板。"""

from __future__ import annotations

from datetime import UTC, datetime

from core.explain import (
    MAX_DIFF_LINES,
    clip_diff,
    explain_marker,
    render_explain_comment,
)
from core.models import IssueContext, MergeRequestContext, ProjectRef, Task, TriggerKind

START = datetime(2026, 11, 3, tzinfo=UTC)


def make_task() -> Task:
    return Task(
        id="task-explain",
        delivery_key="d",
        project=ProjectRef(project_id=1, path="group/project-a"),
        issue_iid=12,
        trigger_kind=TriggerKind.MERGE,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


def make_issue() -> IssueContext:
    return IssueContext(iid=12, title="空标签崩溃", description="…", author="alice", web_url="u")


def make_mr(diff: str = "--- app.py\n+ return []") -> MergeRequestContext:
    return MergeRequestContext(
        iid=3,
        title="fix",
        description="Closes #12",
        source_branch="fix",
        target_branch="main",
        merge_commit_sha="deadbeefcafe",
        web_url="https://gitlab.example.com/-/merge_requests/3",
        diff=diff,
        closes_issues=(12,),
    )


def test_comment_has_marker_and_signature() -> None:
    body = render_explain_comment(make_task(), make_issue(), make_mr(), "改了 app.py", now=START)
    assert body.splitlines()[0] == explain_marker(12)
    assert "改了 app.py" in body
    assert "未经二次复跑验证" in body
    assert "MR !3" in body


def test_diff_under_limit_is_not_truncated() -> None:
    clipped = clip_diff("--- a.py\n+ x")
    assert not clipped.truncated
    assert clipped.text.startswith("--- a.py")


def test_diff_over_limit_is_truncated() -> None:
    lines = "\n".join(f"+ line {i}" for i in range(MAX_DIFF_LINES + 50))
    clipped = clip_diff(f"--- big.py\n{lines}")
    assert clipped.truncated
    assert "diff 已截断" in clipped.text
    assert len(clipped.text.splitlines()) <= MAX_DIFF_LINES + 3
