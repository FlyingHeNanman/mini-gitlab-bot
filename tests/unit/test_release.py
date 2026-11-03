"""Release 说明生成与 Tag Push 编排器（TODO #2）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from adapters.fakes import FakeSCM
from adapters.sqlite_store import SqliteStore
from core.models import CommitInfo, ReleaseInfo, TagInfo, TriggerKind
from core.release import (
    build_compare_url,
    classify_commit,
    find_previous_tag,
    render_release_notes,
)
from core.release_orchestrator import ReleaseConfig, ReleaseOrchestrator
from core.states import TaskKind, TaskState
from tests.helpers import PROJECT_PATH, Clock, make_trigger

NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)


def make_commit(sha: str, title: str, *, message: str | None = None, hours: int = 1) -> CommitInfo:
    return CommitInfo(
        sha=sha,
        title=title,
        message=message or title,
        web_url=f"https://gitlab.example.com/{PROJECT_PATH}/-/commit/{sha}",
        created_at=NOW - timedelta(hours=hours),
    )


def make_tag(name: str, sha: str, *, hours: int = 0) -> TagInfo:
    return TagInfo(
        name=name,
        commit_sha=sha,
        web_url=f"https://gitlab.example.com/{PROJECT_PATH}/-/tags/{name}",
        committed_at=NOW - timedelta(hours=hours),
    )


def make_orchestrator(store: SqliteStore, scm: FakeSCM) -> ReleaseOrchestrator:
    return ReleaseOrchestrator(store=store, scm=scm, clock=Clock(NOW), config=ReleaseConfig())


def enqueue_tag(store: SqliteStore, tag: str, sha: str = "bbb222") -> None:
    store.enqueue(
        make_trigger(
            delivery_key=f"tag:{tag}",
            kind=TriggerKind.TAG_PUSH,
            issue_iid=0,
            tag_name=tag,
            subject_sha=sha,
        ),
        now=NOW,
        payload={"tag_name": tag},
    )


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_classify_commit_understands_conventional_prefixes() -> None:
    assert classify_commit(make_commit("a", "feat(api): 增加接口")) == ("feat", False)
    assert classify_commit(make_commit("b", "fix!: 修正破坏性行为")) == ("fix", True)
    assert classify_commit(make_commit("c", "不规范的提交")) == ("other", False)
    assert classify_commit(make_commit("d", "chore: 升级依赖")) == ("chore", False)


def test_breaking_change_footer_is_detected() -> None:
    commit = make_commit(
        "e", "feat: 新接口", message="feat: 新接口\\n\\nBREAKING CHANGE: 返回值变了"
    )
    assert classify_commit(commit) == ("feat", True)


def test_find_previous_tag_uses_adjacent_tag() -> None:
    current = make_tag("v1.2.0", "ccc", hours=0)
    tags = [current, make_tag("v1.1.0", "bbb", hours=10), make_tag("v1.0.0", "aaa", hours=20)]
    assert find_previous_tag(tags, current) == tags[1]
    assert find_previous_tag([current], current) is None


def test_find_previous_tag_falls_back_to_commit_time() -> None:
    current = make_tag("v1.2.0", "ccc", hours=0)
    tags = [make_tag("v1.1.0", "bbb", hours=10), make_tag("v1.0.0", "aaa", hours=20)]
    assert find_previous_tag(tags, current) == tags[0]


def test_build_compare_url_uses_tag_web_url() -> None:
    current = make_tag("v1.2.0", "ccc")
    previous = make_tag("v1.1.0", "bbb")
    url = build_compare_url(current, previous)
    assert url == (f"https://gitlab.example.com/{PROJECT_PATH}/-/compare/v1.1.0...v1.2.0")
    assert build_compare_url(TagInfo(name="v1.2.0", commit_sha="x"), previous) == ""


def test_render_release_notes_groups_by_section() -> None:
    current = make_tag("v1.2.0", "ccc")
    previous = make_tag("v1.1.0", "bbb", hours=10)
    notes = render_release_notes(
        tag=current,
        previous=previous,
        commits=[
            make_commit("aaa", "fix: 修复空标签"),
            make_commit("bbb", "feat: 新功能"),
            make_commit("ccc", "docs: 补文档"),
            make_commit("ddd", "随便写写"),
        ],
        now=NOW,
    )
    assert "## v1.2.0 发布说明" in notes
    assert "对比：[`v1.1.0` → `v1.2.0`]" in notes
    assert notes.index("### 新功能") < notes.index("### 修复") < notes.index("### 文档")
    assert "### 其他变更" in notes


def test_render_release_notes_marks_breaking_changes() -> None:
    notes = render_release_notes(
        tag=make_tag("v2.0.0", "x"),
        previous=None,
        commits=[make_commit("abcd1234", "feat!: 重写接口")],
        now=NOW,
    )
    assert "**破坏性变更** 重写接口" in notes
    assert "`abcd1234`" in notes


def test_render_release_notes_without_commits() -> None:
    notes = render_release_notes(tag=make_tag("v1.0.0", "x"), previous=None, commits=[], now=NOW)
    assert "首个 Release" in notes
    assert "没有可列出的提交变更" in notes


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #


async def _run(store: SqliteStore, scm: FakeSCM) -> object:
    orchestrator = make_orchestrator(store, scm)
    task = store.claim_next(owner="test", now=NOW, lease_seconds=60, kinds=(TaskKind.RELEASE,))
    assert task is not None
    return await orchestrator.process(task)


async def test_orchestrator_creates_release_from_compare(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    commits = [
        make_commit("aaa111", "feat: 新功能"),
        make_commit("bbb222", "fix: 修复问题"),
    ]
    scm = FakeSCM(
        tags=[make_tag("v1.2.0", "bbb"), make_tag("v1.1.0", "aaa", hours=10)],
        commits_between={("v1.1.0", "v1.2.0"): commits},
    )
    enqueue_tag(store, "v1.2.0")
    task = await _run(store, scm)

    assert task.state is TaskState.SUCCEEDED
    assert task.report["previous_tag"] == "v1.1.0"
    assert task.report["commit_count"] == 2
    assert len(scm.release_creations) == 1
    created = scm.release_creations[0]
    assert created.tag_name == "v1.2.0"
    assert "### 新功能" in created.description
    assert "### 修复" in created.description
    store.close()


async def test_orchestrator_skips_existing_release(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    existing = ReleaseInfo(
        tag_name="v1.2.0",
        name="v1.2.0",
        web_url=f"https://gitlab.example.com/{PROJECT_PATH}/-/releases/v1.2.0",
        description="人工写的说明",
    )
    scm = FakeSCM(
        tags=[make_tag("v1.2.0", "bbb")],
        commits_between={("v1.1.0", "v1.2.0"): (make_commit("aaa", "fix: x"),)},
        existing_releases={"v1.2.0": existing},
    )
    enqueue_tag(store, "v1.2.0")
    task = await _run(store, scm)

    assert task.state is TaskState.SUCCEEDED
    assert task.report["skipped"] == "existing"
    assert task.report["release_url"] == existing.web_url
    assert scm.release_creations == []
    store.close()


async def test_orchestrator_first_release_uses_recent_commits(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(
        tags=[make_tag("v1.0.0", "ccc")],
        recent_commits=[make_commit("aaa", "feat: 首个功能")],
    )
    enqueue_tag(store, "v1.0.0")
    task = await _run(store, scm)

    assert task.state is TaskState.SUCCEEDED
    assert task.report["previous_tag"] is None
    assert len(scm.release_creations) == 1
    assert "首个 Release" in scm.release_creations[0].description
    store.close()


async def test_orchestrator_filters_merge_commits(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(
        tags=[make_tag("v1.2.0", "bbb"), make_tag("v1.1.0", "aaa", hours=10)],
        commits_between={
            ("v1.1.0", "v1.2.0"): [
                make_commit("m1", "Merge branch 'feature'"),
                make_commit("f1", "feat: 真功能"),
            ]
        },
    )
    enqueue_tag(store, "v1.2.0")
    task = await _run(store, scm)

    assert task.report["commit_count"] == 1
    assert "真功能" in scm.release_creations[0].description
    store.close()


async def test_orchestrator_without_tag_name_fails(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM()
    store.enqueue(
        make_trigger(delivery_key="d-no-tag", kind=TriggerKind.TAG_PUSH, issue_iid=0),
        now=NOW,
    )
    task = await _run(store, scm)
    assert task.state is TaskState.FAILED
    assert "tag_name" in (task.failure_reason or "")
    store.close()


async def test_orchestrator_failure_is_recorded_not_raised(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tasks.db")
    scm = FakeSCM(
        tags=[make_tag("v1.2.0", "bbb")],
        fail_on=("create_release",),
    )
    enqueue_tag(store, "v1.2.0")
    task = await _run(store, scm)
    assert task.state is TaskState.FAILED
    assert task.failure_stage == TaskState.FINALIZING.value
    store.close()
