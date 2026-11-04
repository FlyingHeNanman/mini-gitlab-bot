"""周期扫描（TODO #1）：commit 引用解析、回帖渲染、补偿巡检与自动关闭。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from adapters.fakes import FakeSCM
from adapters.sqlite_store import SqliteStore
from core import labels
from core.models import CommitInfo, IssueSummary, ProjectRef, TriggerKind
from core.states import TaskKind
from core.sweep import (
    SweepConfig,
    Sweeper,
    commit_references,
    render_resolution_comment,
    resolution_marker,
)
from tests.helpers import PROJECT_ID, PROJECT_PATH, Clock, make_trigger

NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)


def make_sweeper(
    store: SqliteStore,
    scm: FakeSCM,
    *,
    clock: Clock,
    projects: tuple[str, ...] = (PROJECT_PATH,),
    **overrides,
) -> Sweeper:
    config = SweepConfig(**overrides)
    return Sweeper(store=store, scm=scm, projects=projects, settings=config, clock=clock)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("fix: issue\n\nCloses #12", (12,)),
        ("Fixes #3, closes #4", (3, 4)),
        ("resolved #7 and Resolve #8", (7, 8)),
        ("refs #9 but does not close", ()),
        ("Closes group/other#5", ()),
        ("revert: Closes #12", (12,)),
    ],
)
def test_commit_references(message: str, expected: tuple[int, ...]) -> None:
    assert commit_references(message) == expected


def test_resolution_comment_has_marker() -> None:
    issue = IssueSummary(iid=12, title="空标签返回 None")
    commit = CommitInfo(
        sha="abcdef123456",
        title="fix: 空标签返回 []",
        message="Closes #12",
        web_url="https://gitlab.example.com/group/project-a/-/commit/abcdef123456",
        created_at=NOW - timedelta(hours=1),
    )
    body = render_resolution_comment(issue, commit, now=NOW)
    assert resolution_marker(12) in body
    assert "`abcdef12`" in body
    assert "fix: 空标签返回 []" in body
    assert "状态：已解决" in body


def test_resolution_comment_for_already_closed_issue_does_not_claim_bot_closed_it() -> None:
    issue = IssueSummary(iid=12, title="空标签返回 None", state="closed")
    commit = CommitInfo(
        sha="abcdef123456",
        title="fix: 空标签返回 []",
        message="Fixes #12",
        created_at=NOW - timedelta(hours=1),
    )
    body = render_resolution_comment(issue, commit, now=NOW)
    assert resolution_marker(12) in body
    assert "GitLab 已关闭该 issue" in body
    assert "机器人已补上 `状态：已解决`" in body


# --------------------------------------------------------------------------- #
# 补偿巡检
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sweep_triage_skips_labeled_short_and_ignored(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(
        open_issues=[
            IssueSummary(iid=21, title="正常 issue", description="复现步骤：调用 parse_labels([])"),
            IssueSummary(
                iid=22,
                title="已有状态",
                description="复现步骤……",
                labels=(labels.STATUS_NEEDS_HUMAN,),
            ),
            IssueSummary(
                iid=23,
                title="人工跳过",
                description="复现步骤……",
                labels=(labels.SKIP_LABEL,),
            ),
            IssueSummary(iid=24, title="正文太短", description="就一句话"),
        ]
    )
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.triaged == (21,)
    assert report.quota_skipped == 0
    task = store.list_tasks()[0]
    assert task.kind is TaskKind.TRIAGE
    assert task.trigger_kind is TriggerKind.SWEEP
    assert task.issue_iid == 21
    assert task.payload["sweep"] is True
    store.close()


@pytest.mark.asyncio
async def test_sweep_triage_respects_hourly_quota(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(
        open_issues=[
            IssueSummary(iid=31, title="一", description="复现步骤：调用 parse_labels([])"),
            IssueSummary(iid=32, title="二", description="复现步骤：调用 parse_labels([])"),
        ]
    )
    sweeper = make_sweeper(store, scm, clock=clock, triage_quota_per_hour=1)
    report = await sweeper.run_once()
    assert report.triaged == (31,)
    assert report.quota_skipped == 1
    assert store.count() == 1
    store.close()


@pytest.mark.asyncio
async def test_sweep_triage_disabled_only_scans_resolution(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(open_issues=[IssueSummary(iid=41, title="x", description="复现步骤……")])
    sweeper = make_sweeper(store, scm, clock=clock, triage_enabled=False)
    report = await sweeper.run_once()
    assert report.triaged == ()
    assert store.count() == 0
    store.close()


# --------------------------------------------------------------------------- #
# commit 引用回收
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sweep_resolves_issue_referenced_by_commit(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    issue = IssueSummary(
        iid=12,
        title="parse_labels 空标签",
        description="复现步骤……",
        labels=("类型：缺陷", labels.STATUS_NEEDS_HUMAN),
        web_url="https://gitlab.example.com/group/project-a/-/issues/12",
    )
    commit = CommitInfo(
        sha="deadbeefcafe1234",
        title="fix(issue-12): 空标签返回 []",
        message="Closes #12",
        web_url="https://gitlab.example.com/group/project-a/-/commit/deadbeefcafe1234",
        created_at=NOW - timedelta(hours=2),
    )
    scm = FakeSCM(open_issues=[issue], recent_commits=[commit])
    sweeper = make_sweeper(store, scm, clock=clock)

    report = await sweeper.run_once()
    assert report.resolved == ((12, commit.sha),)
    assert 12 in scm.closed_issue_iids
    assert labels.STATUS_RESOLVED in scm.current_labels
    assert labels.STATUS_NEEDS_HUMAN not in scm.current_labels
    assert any(item["marker"] == resolution_marker(12) for item in scm.comments)

    # 再扫一遍：issue 已关闭，不应该重复回帖/关闭
    report2 = await sweeper.run_once()
    assert report2.resolved == ()
    assert len([c for c in scm.comments if c["marker"] == resolution_marker(12)]) == 1
    store.close()


@pytest.mark.asyncio
async def test_sweep_resolution_creates_missing_status_label(tmp_path) -> None:
    """新项目可能还没有任何机器人标签；周期扫描必须先建标签再 add_labels。"""
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    issue = IssueSummary(iid=17, title="缺标签", description="复现步骤……")
    commit = CommitInfo(
        sha="feedface1234",
        title="fix: 修复缺标签",
        message="Fixes #17",
        created_at=NOW - timedelta(hours=1),
    )
    # project_labels 默认为空：模拟"从未跑过 triage 的新项目"
    scm = FakeSCM(open_issues=[issue], recent_commits=[commit])
    sweeper = make_sweeper(store, scm, clock=clock)

    report = await sweeper.run_once()
    assert report.resolved == ((17, commit.sha),)
    assert labels.STATUS_RESOLVED in scm.project_labels
    assert any(item["name"] == labels.STATUS_RESOLVED for item in scm.created_labels)
    store.close()


@pytest.mark.asyncio
async def test_sweep_resolution_ignores_skip_label(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    commit = CommitInfo(
        sha="abc123456789",
        title="fix: x",
        message="Fixes #13 and #14",
        created_at=NOW,
    )
    scm = FakeSCM(
        open_issues=[
            IssueSummary(iid=13, title="跳过", description="x", labels=(labels.SKIP_LABEL,)),
            IssueSummary(iid=14, title="别的", description="x"),
        ],
        recent_commits=[commit],
    )
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.resolved == ((14, commit.sha),)
    assert 13 not in scm.closed_issue_iids
    store.close()


@pytest.mark.asyncio
async def test_sweep_resolution_handles_gitlab_autoclosed_issue_idempotently(tmp_path) -> None:
    """GitLab autoclose 后 issue 已 closed；机器人仍补回帖/标签，且不重复计数。"""
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    commit = CommitInfo(
        sha="feedface1234",
        title="fix: 自动关闭场景",
        message="Fixes #41",
        created_at=NOW - timedelta(hours=1),
    )
    closed = IssueSummary(
        iid=41,
        title="GitLab 已自动关闭",
        description="x",
        state="closed",
        updated_at=NOW - timedelta(hours=2),
    )
    scm = FakeSCM(closed_issue_summaries=[closed], recent_commits=[commit])
    sweeper = make_sweeper(store, scm, clock=clock)

    first = await sweeper.run_once()
    assert first.resolved == ((41, commit.sha),)
    assert 41 not in scm.closed_issue_iids  # 已经是 closed，不再调 close
    assert labels.STATUS_RESOLVED in scm.closed_issue_summaries[0].labels
    assert len(scm.comments) == 1
    assert "GitLab 已关闭该 issue" in scm.comments[0]["body"]

    second = await sweeper.run_once()
    assert second.resolved == ()
    assert len(scm.comments) == 1
    assert scm.comments[0]["revisions"] == 1
    store.close()


@pytest.mark.asyncio
async def test_sweep_ignores_old_closed_issue_outside_lookback(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    commit = CommitInfo(
        sha="abcdef123456",
        title="fix: old",
        message="Fixes #42",
        created_at=NOW - timedelta(days=30),
    )
    closed = IssueSummary(
        iid=42,
        title="很久以前关闭",
        description="x",
        state="closed",
        updated_at=NOW - timedelta(days=30),
    )
    scm = FakeSCM(closed_issue_summaries=[closed], recent_commits=[commit])
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.resolved == ()
    assert scm.comments == []
    store.close()


@pytest.mark.asyncio
async def test_closed_issue_scan_failure_does_not_block_open_resolution(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    commit = CommitInfo(
        sha="abc123456789",
        title="fix: x",
        message="Fixes #43",
        created_at=NOW,
    )
    scm = FakeSCM(
        open_issues=[IssueSummary(iid=43, title="open", description="x")],
        recent_commits=[commit],
        fail_on=("list_closed_issues",),
    )
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.resolved == ((43, commit.sha),)
    assert any("list_closed_issues" in error for error in report.errors)
    store.close()


@pytest.mark.asyncio
async def test_sweep_handles_gitlab_errors_without_raising(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(fail_on=("list_open_issues",))
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.errors
    store.close()


@pytest.mark.asyncio
async def test_sweep_uses_project_whitelist(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(project_refs={PROJECT_PATH: ProjectRef(project_id=PROJECT_ID, path=PROJECT_PATH)})
    sweeper = make_sweeper(store, scm, clock=clock)
    await sweeper.run_once()
    assert scm.project_lookups == [PROJECT_PATH]
    store.close()


@pytest.mark.asyncio
async def test_sweep_resolution_uses_newest_commit(tmp_path) -> None:
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    older = CommitInfo(
        sha="aaaa1111",
        title="older",
        message="Closes #15",
        created_at=NOW - timedelta(days=2),
    )
    newer = CommitInfo(
        sha="bbbb2222",
        title="newer",
        message="Closes #15",
        created_at=NOW - timedelta(hours=2),
    )
    scm = FakeSCM(
        open_issues=[IssueSummary(iid=15, title="x", description="x")],
        recent_commits=[newer, older],
    )
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.resolved == ((15, newer.sha),)
    store.close()


@pytest.mark.asyncio
async def test_sweep_enqueues_triage_only_once_per_second(tmp_path) -> None:
    """同一秒内重跑：delivery_key 相同 → 只建一个任务（保护手工重跑/时钟抖动）。"""
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(
        open_issues=[IssueSummary(iid=51, title="x", description="复现步骤：调用 parse_labels([])")]
    )
    sweeper = make_sweeper(store, scm, clock=clock)
    first = await sweeper.run_once()
    assert first.triaged == (51,)
    second = await sweeper.run_once()
    assert second.triaged == ()
    assert store.count() == 1
    store.close()


def test_sweeper_task_trigger_is_auditable(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    store.enqueue(
        make_trigger(delivery_key="manual", kind=TriggerKind.SWEEP, issue_iid=61),
        now=NOW,
    )
    task = store.list_tasks()[0]
    assert task.trigger_kind is TriggerKind.SWEEP
    assert task.kind is TaskKind.TRIAGE
    store.close()


@pytest.mark.asyncio
async def test_resolved_issue_is_not_also_triaged(tmp_path) -> None:
    """commit 已引用修复时，不先补建 triage 再关闭（避免白白跑一次模型）。"""
    clock = Clock(NOW)
    store = SqliteStore(tmp_path / "tasks.db")
    commit = CommitInfo(
        sha="abc123456789",
        title="fix: 已修复",
        message="Closes #16",
        created_at=NOW,
    )
    scm = FakeSCM(
        open_issues=[
            IssueSummary(
                iid=16,
                title="要修的",
                description="复现步骤：调用 parse_labels([]) 得到 None",
            )
        ],
        recent_commits=[commit],
    )
    sweeper = make_sweeper(store, scm, clock=clock)
    report = await sweeper.run_once()
    assert report.resolved == ((16, commit.sha),)
    assert report.triaged == ()
    assert store.count() == 0
    store.close()
