"""`GitLabSCM` 的契约测试：真 HTTP 打到本地桩 GitLab（`tests/stub_gitlab.py`）。

这里断言的重点不是"我们的分支逻辑"，而是**请求形状**：路径、查询参数、请求头、
以及"哪些操作绝不发生"（拼 token 进 URL、重复开 MR、每次新开评论、合并且删分支）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.gitlab_client import GitLabSCM, GitLabSettings, ScmError
from core.models import ProjectRef, Task, TriggerKind
from tests.helpers import START
from tests.stub_gitlab import DEFAULT_TOKEN, StubGitLab

PROJECT_ID = 42
ALICE_ID = 7
ISSUE = 12


@pytest.fixture
def stub():
    with StubGitLab() as server:
        server.state.members[ALICE_ID] = 30
        server.state.add_issue(ISSUE, labels=["ai-fix"])
        yield server


@pytest.fixture
def scm(stub: StubGitLab) -> GitLabSCM:
    return GitLabSCM(
        settings=GitLabSettings(url=stub.url, token=DEFAULT_TOKEN, timeout_sec=10, comment_limit=20)
    )


def make_task(issue_iid: int = ISSUE) -> Task:
    return Task(
        id="task-scm-1",
        delivery_key="delivery-1",
        project=ProjectRef(project_id=PROJECT_ID, path="group/project-a"),
        issue_iid=issue_iid,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


async def test_default_branch_comes_from_the_project(stub: StubGitLab, scm: GitLabSCM) -> None:
    assert await scm.default_branch(ProjectRef(project_id=PROJECT_ID, path="group/project-a")) == (
        "main"
    )
    assert stub.state.request_paths() == [f"GET /api/v4/projects/{PROJECT_ID}"]


async def test_member_access_level_uses_members_all(stub: StubGitLab, scm: GitLabSCM) -> None:
    """`members/all` 才包含从 group 继承的成员；写成 `members` 会漏人。"""
    assert await scm.get_member_access_level(PROJECT_ID, ALICE_ID) == 30
    assert any(
        f"/projects/{PROJECT_ID}/members/all/{ALICE_ID}" in path
        for path in stub.state.request_paths()
    )


async def test_non_member_returns_none_instead_of_raising(stub: StubGitLab, scm: GitLabSCM) -> None:
    assert await scm.get_member_access_level(PROJECT_ID, 999) is None


async def test_issue_context_filters_system_notes_and_orders_by_time(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    stub.state.add_note(ISSUE, "我也遇到了这个问题", author="bob")
    stub.state.add_note(ISSUE, "added ai-fix label", system=True)
    stub.state.add_note(ISSUE, "试了 3.12 也一样", author="carol")

    ctx = await scm.load_issue_context(make_task())

    assert ctx.iid == ISSUE
    assert ctx.title.startswith("parse_labels")
    assert ctx.author == "reporter"
    assert ctx.labels == ("ai-fix",)
    assert ctx.state == "opened"
    assert ctx.web_url.endswith(f"/issues/{ISSUE}")
    # 系统 note 被丢掉，人类评论按时间正序
    assert ctx.comments == ("我也遇到了这个问题", "试了 3.12 也一样")


async def test_issue_context_keeps_only_the_newest_n_comments(stub: StubGitLab) -> None:
    scm = GitLabSCM(settings=GitLabSettings(url=stub.url, token=DEFAULT_TOKEN, comment_limit=3))
    for index in range(6):
        stub.state.add_note(ISSUE, f"第 {index} 条评论")
    ctx = await scm.load_issue_context(make_task())
    assert len(ctx.comments) == 3
    assert ctx.comments == ("第 3 条评论", "第 4 条评论", "第 5 条评论")  # 最新三条，正序


async def test_issue_context_paginates_to_find_recent_comments(stub: StubGitLab) -> None:
    """评论超过一页时仍要拿到最新几条（`iterator=True` + 倒序翻页）。"""
    scm = GitLabSCM(settings=GitLabSettings(url=stub.url, token=DEFAULT_TOKEN, comment_limit=2))
    for index in range(250):
        stub.state.add_note(ISSUE, f"comment-{index}")
    ctx = await scm.load_issue_context(make_task())
    assert ctx.comments == ("comment-248", "comment-249")


# --------------------------------------------------------------------------- #
# 标签
# --------------------------------------------------------------------------- #


async def test_set_labels_sends_add_and_remove(stub: StubGitLab, scm: GitLabSCM) -> None:
    await scm.set_labels(make_task(), add=("ai-fix:running",), remove=("ai-fix",))

    update = [item for item in stub.state.requests if item["method"] == "PUT"]
    assert len(update) == 1
    assert update[0]["body"]["add_labels"] == "ai-fix:running"
    assert update[0]["body"]["remove_labels"] == "ai-fix"
    assert stub.state.issues[ISSUE]["labels"] == ["ai-fix:running"]


async def test_set_labels_with_nothing_to_do_makes_no_request(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    await scm.set_labels(make_task())
    assert stub.state.requests == []


async def test_label_update_does_not_clobber_concurrent_edits(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    """用 add_labels/remove_labels，而不是"读出全部标签再整体写回"。"""
    stub.state.issues[ISSUE]["labels"] = ["ai-fix", "human-tag"]
    await scm.set_labels(make_task(), add=("ai-fix:done",), remove=("ai-fix",))
    assert stub.state.issues[ISSUE]["labels"] == ["human-tag", "ai-fix:done"]


# --------------------------------------------------------------------------- #
# 状态评论：原地编辑
# --------------------------------------------------------------------------- #


async def test_upsert_comment_creates_then_edits_the_same_note(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    marker = "<!-- mini-gitlab-bot:status task-scm-1 -->"
    first = await scm.upsert_comment(make_task(), f"{marker}\n运行中", marker=marker)
    second = await scm.upsert_comment(make_task(), f"{marker}\n已完成", marker=marker)

    assert first == second
    notes = stub.state.notes[ISSUE]
    assert len(notes) == 1  # 只有一条被反复编辑
    assert notes[0]["body"].endswith("已完成")
    methods = [item["method"] for item in stub.state.requests]
    assert methods.count("POST") == 1
    assert methods.count("PUT") == 1


async def test_upsert_comment_ignores_system_notes_with_the_same_text(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    marker = "<!-- mini-gitlab-bot:status task-scm-1 -->"
    stub.state.add_note(ISSUE, f"{marker}", system=True)
    await scm.upsert_comment(make_task(), f"{marker}\n新建", marker=marker)
    human_notes = [note for note in stub.state.notes[ISSUE] if not note["system"]]
    assert len(human_notes) == 1


async def test_upsert_comment_finds_the_note_on_a_later_page(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    marker = "<!-- mini-gitlab-bot:status task-scm-1 -->"
    await scm.upsert_comment(make_task(), f"{marker}\n第一版", marker=marker)
    for index in range(150):  # 把状态评论挤到第二页之后
        stub.state.add_note(ISSUE, f"后来的人说 {index}")
    await scm.upsert_comment(make_task(), f"{marker}\n第二版", marker=marker)

    with_marker = [note for note in stub.state.notes[ISSUE] if marker in note["body"]]
    assert len(with_marker) == 1
    assert with_marker[0]["body"].endswith("第二版")


# --------------------------------------------------------------------------- #
# draft MR
# --------------------------------------------------------------------------- #


async def test_create_draft_mr_uses_title_prefix_and_keeps_branch(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    mr = await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-fix-labels",
        target_branch="main",
        title="Draft: fix(issue-12): parse_labels",
        description="Closes #12\n\n说明",
    )

    assert mr.iid == 1
    assert mr.draft is True
    assert mr.url.endswith("/merge_requests/1")
    created = stub.state.merge_requests[0]
    assert created["source_branch"] == "ai-fix/12-fix-labels"
    assert created["target_branch"] == "main"
    assert created["title"].startswith("Draft:")
    post = [item for item in stub.state.requests if item["method"] == "POST"][0]
    assert post["body"]["remove_source_branch"] is False  # 合并后保留分支（审计证据）


async def test_create_draft_mr_is_idempotent_on_retry(stub: StubGitLab, scm: GitLabSCM) -> None:
    """重试 / 崩溃恢复后不能开出第二条 MR，只把描述刷新。"""
    first = await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-fix-labels",
        target_branch="main",
        title="Draft: fix(issue-12)",
        description="旧描述",
    )
    second = await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-fix-labels",
        target_branch="main",
        title="Draft: fix(issue-12)",
        description="新描述",
    )

    assert first.iid == second.iid
    assert len(stub.state.merge_requests) == 1
    assert stub.state.merge_requests[0]["description"] == "新描述"


async def test_create_draft_mr_reuses_a_concurrently_created_one(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    """另一个 worker 抢先建了 MR（POST 409）→ 退化成取回并更新，而不是报错。"""
    stub.state.merge_requests.append(
        {
            "id": 5001,
            "iid": 1,
            "project_id": PROJECT_ID,
            "title": "Draft: fix(issue-12)",
            "description": "别人建的",
            "source_branch": "ai-fix/12-fix-labels",
            "target_branch": "main",
            "state": "opened",
            "web_url": "https://gitlab.example.com/group/project-a/-/merge_requests/1",
        }
    )
    mr = await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-fix-labels",
        target_branch="main",
        title="Draft: fix(issue-12)",
        description="我这边准备的描述",
    )
    assert mr.iid == 1
    assert len(stub.state.merge_requests) == 1


# --------------------------------------------------------------------------- #
# 错误映射与凭据
# --------------------------------------------------------------------------- #


async def test_404_is_reported_as_not_found(stub: StubGitLab, scm: GitLabSCM) -> None:
    with pytest.raises(ScmError) as excinfo:
        await scm.load_issue_context(make_task(issue_iid=999))
    assert excinfo.value.not_found is True
    assert "404" in str(excinfo.value)


async def test_http_500_becomes_scm_error_with_status(stub: StubGitLab) -> None:
    """关掉重试才能稳定看到 5xx：默认会由 python-gitlab 自动重试（这是我们要的行为）。"""
    scm = GitLabSCM(
        settings=GitLabSettings(
            url=stub.url, token=DEFAULT_TOKEN, timeout_sec=10, retry_transient=False
        )
    )
    stub.state.failures.append(("GET", rf"/api/v4/projects/{PROJECT_ID}", 500))
    with pytest.raises(ScmError) as excinfo:
        await scm.default_branch(ProjectRef(project_id=PROJECT_ID, path="group/project-a"))
    assert "500" in str(excinfo.value)


async def test_transient_errors_are_retried_by_default(stub: StubGitLab, scm: GitLabSCM) -> None:
    """一次 500 不该让任务失败：默认重试后应当成功。"""
    stub.state.failures.append(("GET", rf"/api/v4/projects/{PROJECT_ID}", 500))
    branch = await scm.default_branch(ProjectRef(project_id=PROJECT_ID, path="group/project-a"))
    assert branch == "main"


async def test_bad_token_is_reported_without_echoing_it(stub: StubGitLab) -> None:
    bad = "glpat-wrong-token-abc123456"
    scm = GitLabSCM(settings=GitLabSettings(url=stub.url, token=bad, timeout_sec=10))
    with pytest.raises(ScmError) as excinfo:
        await scm.default_branch(ProjectRef(project_id=PROJECT_ID, path="group/project-a"))
    message = str(excinfo.value)
    assert "401" in message
    assert bad not in message


async def test_token_travels_only_in_the_header(stub: StubGitLab, scm: GitLabSCM) -> None:
    await scm.default_branch(ProjectRef(project_id=PROJECT_ID, path="group/project-a"))
    await scm.set_labels(make_task(), add=("ai-fix:running",))
    await scm.upsert_comment(make_task(), "hi", marker="m")
    await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-x",
        target_branch="main",
        title="Draft: x",
        description="y",
    )

    for item in stub.state.requests:
        assert DEFAULT_TOKEN not in item["path"]
        assert item["headers"]["private-token"] == DEFAULT_TOKEN  # 只在这里
        assert "oauth2:" not in item["path"]
        assert "glpat-" not in item["path"]


# --------------------------------------------------------------------------- #
# 周期扫描 / Release 新增的端点（TODO #1 / TODO #2）
# --------------------------------------------------------------------------- #


async def test_resolve_project_by_path(stub: StubGitLab, scm: GitLabSCM) -> None:
    project = await scm.resolve_project("group/project-a")
    assert project == ProjectRef(project_id=PROJECT_ID, path="group/project-a")
    # path_with_namespace 里的 / 必须编码，否则会被当成两个路径段
    assert any("group%2Fproject-a" in item["path"] for item in stub.state.requests)


async def test_list_open_issues_asks_for_opened_only(stub: StubGitLab, scm: GitLabSCM) -> None:
    stub.state.add_issue(20, title="关闭的不算", state="closed")
    issues = await scm.list_open_issues(
        ProjectRef(project_id=PROJECT_ID, path="group/project-a"), limit=50
    )
    assert [item.iid for item in issues] == [ISSUE]
    request = next(item for item in stub.state.requests if "/issues?" in item["path"])
    assert request["query"]["state"] == ["opened"]
    assert request["query"]["per_page"] == ["50"]


async def test_list_recent_commits_passes_since_and_ref(stub: StubGitLab, scm: GitLabSCM) -> None:
    stub.state.add_commit("aaa111", title="feat: x", created_at="2026-10-20T00:00:00.000Z")
    stub.state.add_commit("bbb222", title="old", created_at="2026-10-01T00:00:00.000Z")
    commits = await scm.list_recent_commits(
        ProjectRef(project_id=PROJECT_ID, path="group/project-a"),
        ref="main",
        since=datetime(2026, 10, 10, tzinfo=UTC),
        limit=10,
    )
    assert [item.sha for item in commits] == ["aaa111"]
    request = next(item for item in stub.state.requests if "/repository/commits?" in item["path"])
    assert request["query"]["ref_name"] == ["main"]
    assert request["query"]["since"][0].startswith("2026-10-10")


async def test_list_tags_sorts_by_commit_time(stub: StubGitLab, scm: GitLabSCM) -> None:
    stub.state.add_tag("v1.0.0", "aaa111", created_at="2026-10-01T00:00:00.000Z")
    stub.state.add_tag("v1.1.0", "bbb222", created_at="2026-10-20T00:00:00.000Z")
    tags = await scm.list_tags(ProjectRef(project_id=PROJECT_ID, path="group/project-a"))
    assert [item.name for item in tags] == ["v1.1.0", "v1.0.0"]
    # 真 GitLab 的 tag 对象没有顶层 web_url；必须从 commit.web_url 推出 tag 链接，
    # 否则 build_compare_url 拿不到 base，Release 正文会缺 compare 链接。
    assert tags[0].web_url.endswith("/-/tags/v1.1.0")
    assert tags[1].web_url.endswith("/-/tags/v1.0.0")


async def test_compare_commits_returns_commits_between_refs(
    stub: StubGitLab, scm: GitLabSCM
) -> None:
    stub.state.add_commit("aaa111", title="a", created_at="2026-10-01T00:00:00.000Z")
    stub.state.add_commit("bbb222", title="b", created_at="2026-10-02T00:00:00.000Z")
    stub.state.add_commit("ccc333", title="c", created_at="2026-10-03T00:00:00.000Z")
    stub.state.add_tag("v1.0.0", "aaa111")
    stub.state.add_tag("v1.1.0", "ccc333")
    commits = await scm.compare_commits(
        ProjectRef(project_id=PROJECT_ID, path="group/project-a"),
        from_ref="v1.0.0",
        to_ref="v1.1.0",
    )
    assert [item.sha for item in commits] == ["aaa111", "bbb222", "ccc333"]


async def test_release_get_and_create(stub: StubGitLab, scm: GitLabSCM) -> None:
    project = ProjectRef(project_id=PROJECT_ID, path="group/project-a")
    assert await scm.get_release(project, tag_name="v1.0.0") is None
    stub.state.add_tag("v1.0.0", "aaa111")
    release = await scm.create_release(
        project, tag_name="v1.0.0", name="v1.0.0", description="## 变更\n- x"
    )
    assert release.tag_name == "v1.0.0"
    assert release.web_url.endswith("/releases/v1.0.0")
    assert (await scm.get_release(project, tag_name="v1.0.0")).tag_name == "v1.0.0"
    create_request = next(
        item
        for item in stub.state.requests
        if item["method"] == "POST" and item["path"].endswith("/releases")
    )
    assert create_request["body"] == {
        "tag_name": "v1.0.0",
        "name": "v1.0.0",
        "description": "## 变更\n- x",
    }
