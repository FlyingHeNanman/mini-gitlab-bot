"""M3 收尾链路：真 `GitLabSCM`（打本地桩 GitLab）+ 假 VCS/agent 跑完整闭环。

这条链路就是 PLAN §9 里 M3 的判据在仓库内的等价物：真实 GitLab API 契约
（`members/all`、`add_labels/remove_labels`、notes 原地编辑、`source_branch` 查重、
draft 标题前缀）全部经真 HTTP 验证；唯一没验的是"真 GitLab 实例 + 真 codex"，
那必须在有内网模型端点的机器上做（见 PLAN §8.3）。
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from adapters.fakes import FakeAgent, FakeEnvPreparer, FakeVCS, FakeWorkspace
from adapters.gitlab_client import GitLabSCM, GitLabSettings
from adapters.sqlite_store import SqliteStore
from core.models import AgentResult
from core.states import TaskState
from gateway.app import create_app
from gateway.runtime import build_runtime
from tests.helpers import ALICE_ID, Clock, make_trigger
from tests.payloads import headers, issue_payload
from tests.stub_gitlab import DEFAULT_TOKEN, StubGitLab

PROJECT_ID = 42
ISSUE = 12
WEBHOOK = "/webhook/gitlab"


@pytest.fixture
def stub():
    with StubGitLab() as server:
        server.state.members[ALICE_ID] = 30
        server.state.add_issue(ISSUE, labels=["ai-fix"])
        yield server


@pytest.fixture
def make(settings, clock: Clock, tmp_path, stub: StubGitLab):
    """用真 SCM + 假 VCS/agent 装配运行时（每种组合一个独立 SQLite）。"""

    created: list[SqliteStore] = []

    def _make(*, agent: FakeAgent | None = None, retry_transient: bool = True):
        scm = GitLabSCM(
            settings=GitLabSettings(
                url=stub.url,
                token=DEFAULT_TOKEN,
                timeout_sec=10,
                retry_transient=retry_transient,
            )
        )
        settings_for_run = settings.model_copy(
            update={"gitlab_url": stub.url, "gitlab_token": DEFAULT_TOKEN}
        )
        store = SqliteStore(tmp_path / f"tasks-{len(created)}.db")
        runtime = build_runtime(
            settings_for_run,
            store=store,
            scm=scm,
            vcs=FakeVCS(),
            agent=agent or FakeAgent(),
            workspace=FakeWorkspace(),
            env=FakeEnvPreparer(),
            with_worker=True,
            clock=clock,
        )
        created.append(store)
        return runtime

    yield _make
    for store in created:
        store.close()


async def run_pipeline(runtime, clock):
    outcome = runtime.store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    assert runtime.worker is not None
    assert await runtime.worker.step() is True
    task = runtime.store.get(outcome.task.id)
    assert task is not None
    return task


# --------------------------------------------------------------------------- #
# 成功闭环
# --------------------------------------------------------------------------- #


async def test_success_creates_draft_mr_and_edits_one_comment(make, clock, stub) -> None:
    runtime = make()
    task = await run_pipeline(runtime, clock)

    assert task.state is TaskState.SUCCEEDED, task.failure_reason
    # --- MR ---
    assert len(stub.state.merge_requests) == 1
    mr = stub.state.merge_requests[0]
    assert mr["title"].startswith("Draft: ")
    assert f"closes #{ISSUE}".lower() in mr["description"].lower()
    assert mr["source_branch"] == task.branch
    assert mr["target_branch"] == "main"
    assert task.mr_iid == mr["iid"]
    # --- 状态评论：单条原地编辑（先"运行中"后"已完成"）---
    notes = stub.state.notes[ISSUE]
    assert len(notes) == 1
    body = notes[0]["body"]
    assert "ai-fix" in body and "已完成" in body
    # --- 标签流转：ai-fix → ai-fix:running → ai-fix:done ---
    assert stub.state.issues[ISSUE]["labels"] == ["ai-fix:done"]


async def test_success_sends_labels_via_add_remove(make, clock, stub) -> None:
    runtime = make()
    await run_pipeline(runtime, clock)
    updates = [
        item["body"]
        for item in stub.state.requests
        if item["method"] == "PUT" and item["body"].get("add_labels")
    ]
    assert updates[0]["add_labels"] == "ai-fix:running"
    assert updates[0]["remove_labels"] == "ai-fix"
    assert updates[-1]["add_labels"] == "ai-fix:done"
    assert "ai-fix:running" in updates[-1]["remove_labels"]


async def test_success_never_deletes_the_source_branch(make, clock, stub) -> None:
    runtime = make()
    await run_pipeline(runtime, clock)
    create = [
        item
        for item in stub.state.requests
        if item["method"] == "POST" and item["path"].endswith("/merge_requests")
    ][0]
    assert create["body"]["remove_source_branch"] is False


# --------------------------------------------------------------------------- #
# 失败闭环
# --------------------------------------------------------------------------- #


async def test_agent_failure_marks_issue_and_skips_mr(make, clock, stub) -> None:
    agent = FakeAgent(
        result=AgentResult(
            exit_code=1,
            completed_event=False,
            last_message_written=False,
            notes=("疑似被中断（exit 1 但没有任何终止事件）",),
        )
    )
    runtime = make(agent=agent)
    task = await run_pipeline(runtime, clock)

    assert task.state is TaskState.FAILED
    assert stub.state.merge_requests == []
    notes = stub.state.notes[ISSUE]
    assert len(notes) == 1
    assert "失败" in notes[0]["body"]
    assert "疑似被中断" in notes[0]["body"]
    assert stub.state.issues[ISSUE]["labels"] == ["ai-fix:failed"]


# --------------------------------------------------------------------------- #
# 权限闸门（真 members/all）
# --------------------------------------------------------------------------- #


async def test_developer_can_trigger_over_http(make, settings, stub) -> None:
    runtime = make()
    app = create_app(
        settings=settings.model_copy(update={"gitlab_url": stub.url}),
        runtime=runtime,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(WEBHOOK, json=issue_payload(), headers=headers())

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert any(
        f"/projects/{PROJECT_ID}/members/all/{ALICE_ID}" in path
        for path in stub.state.request_paths()
    )


async def test_guest_is_denied_by_the_real_member_lookup(make, settings, stub) -> None:
    """Reporter=20 < Developer=30：必须在真 API 上被拒，并且**不入队**。"""
    stub.state.members[ALICE_ID] = 20
    runtime = make()
    app = create_app(
        settings=settings.model_copy(update={"gitlab_url": stub.url}),
        runtime=runtime,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(WEBHOOK, json=issue_payload(), headers=headers())

    assert response.status_code == 200  # 绝不 5xx
    assert response.json()["status"] == "ignored"
    assert "permission" in response.json()["reason"]
    assert runtime.store.count() == 0


async def test_non_member_is_denied(make, settings, stub) -> None:
    stub.state.members.pop(ALICE_ID, None)  # 404 = 非成员
    runtime = make()
    app = create_app(
        settings=settings.model_copy(update={"gitlab_url": stub.url}),
        runtime=runtime,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(WEBHOOK, json=issue_payload(), headers=headers())

    assert response.json()["status"] == "ignored"
    assert runtime.store.count() == 0


async def test_gitlab_outage_does_not_cause_5xx(make, settings, stub) -> None:
    """GitLab 挂了也必须是 200（否则 webhook 会被永久禁用）。

    这里关掉适配器内部重试：重试本身由 `test_transient_errors_are_retried_by_default`
    覆盖，本测试只关心"最终失败时 HTTP 层仍然是 200 + 不入队"。
    """
    stub.state.failures.append(("GET", r"/api/v4/projects/42/members/all/7", 500))
    runtime = make(retry_transient=False)
    app = create_app(
        settings=settings.model_copy(update={"gitlab_url": stub.url}),
        runtime=runtime,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(WEBHOOK, json=issue_payload(), headers=headers())

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert runtime.store.count() == 0


# --------------------------------------------------------------------------- #
# 生产装配（`build_production_runtime` 真的把真件接上了）
# --------------------------------------------------------------------------- #


async def test_production_runtime_wires_the_real_adapters(
    settings, stub: StubGitLab, tmp_path, clock: Clock
) -> None:
    from adapters.codex_cli import CodexCli
    from adapters.git_cli import GitCli
    from adapters.local_workspace import LocalWorkspace
    from adapters.uv_env import UvEnvPreparer
    from gateway.runtime import build_production_runtime

    runtime_settings = settings.model_copy(
        update={
            "gitlab_url": stub.url,
            "gitlab_token": DEFAULT_TOKEN,
            "gitlab_readonly_token": "glpat-readonly-mcp",
            "work_dir": tmp_path / "work",
            "log_dir": tmp_path / "logs",
            "uv_cache_dir": tmp_path / "uv-cache",
        }
    )
    store = SqliteStore(tmp_path / "prod.db")
    try:
        runtime = build_production_runtime(runtime_settings, store=store, clock=clock)
        assert isinstance(runtime.scm, GitLabSCM)
        assert isinstance(runtime.vcs, GitCli)
        assert isinstance(runtime.agent, CodexCli)
        assert isinstance(runtime.workspace, LocalWorkspace)
        assert isinstance(runtime.env, UvEnvPreparer)
        # 权限查询真的走了网络（这是 webhook 闸门的关键依赖）
        assert await runtime.scm.get_member_access_level(PROJECT_ID, ALICE_ID) == 30
        # 只读 MCP：有 read_api token 就启用，并且工具清单会进 context.md
        assert runtime.agent.settings.mcp_enabled is True
        assert len(runtime.orchestrator._config.mcp_tools) == 11  # noqa: SLF001
    finally:
        store.close()


async def test_production_runtime_falls_back_without_a_token(
    settings, stub: StubGitLab, tmp_path, clock: Clock
) -> None:
    """没有 token 时不能拿空 token 去撞 API：退回占位实现 + 启动告警（fail closed）。"""
    from adapters.unavailable import UnavailableSCM
    from gateway.runtime import build_production_runtime

    runtime_settings = settings.model_copy(
        update={
            "gitlab_url": stub.url,
            "gitlab_token": "",
            "work_dir": tmp_path / "work",
            "log_dir": tmp_path / "logs",
        }
    )
    store = SqliteStore(tmp_path / "prod2.db")
    try:
        runtime = build_production_runtime(runtime_settings, store=store, clock=clock)
        assert isinstance(runtime.scm, UnavailableSCM)
        assert any("GITLAB_TOKEN" in item for item in runtime_settings.startup_warnings())
    finally:
        store.close()
