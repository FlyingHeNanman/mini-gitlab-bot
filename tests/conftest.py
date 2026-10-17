"""共享夹具：Settings、可控时钟、以及"假 SCM + 假 agent"的完整运行时。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adapters.fakes import FakeAgent, FakeEnvPreparer, FakeSCM, FakeVCS, FakeWorkspace
from adapters.sqlite_store import SqliteStore
from gateway.config import Settings
from gateway.runtime import Runtime, build_runtime
from tests.helpers import ALICE_ID, PROJECT_PATH, Clock, Harness


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        webhook_secret="s3cr3t",
        repo_whitelist=[PROJECT_PATH],
        ai_fix_label="ai-fix",
        bot_username="mini-gitlab-bot",
        allowed_trigger_users=[],
        min_access_level=30,
        member_cache_ttl_sec=60,
        db_path=tmp_path / "tasks.db",
        work_dir=tmp_path / "work",
        worker_owner="test-worker",
        lease_seconds=60,
        agent_timeout_sec=30.0,
        gitlab_token="glpat-test-token",
        model_name="test-model",
        codex_expected_version="0.163.0",
        log_dir=tmp_path / "logs",
        max_attempts=3,
        poll_interval_sec=0.01,
    )


@pytest.fixture
def harness(settings: Settings, clock: Clock, tmp_path: Path) -> Any:
    """假 SCM + 假 agent 的完整运行时（含 Worker），集成测试的主力夹具。"""
    scm = FakeSCM(access_levels={ALICE_ID: 30})
    store = SqliteStore(tmp_path / "tasks.db")
    runtime = build_runtime(
        settings,
        store=store,
        scm=scm,
        vcs=FakeVCS(),
        agent=FakeAgent(),
        workspace=FakeWorkspace(),
        env=FakeEnvPreparer(),
        with_worker=True,
        clock=clock,
    )
    assert runtime.worker is not None
    yield Harness(
        settings=settings,
        clock=clock,
        store=store,
        scm=scm,
        vcs=runtime.vcs,
        agent=runtime.agent,
        workspace=runtime.workspace,
        runtime=runtime,
        orchestrator=runtime.orchestrator,
        worker=runtime.worker,
    )
    store.close()


@pytest.fixture
def make_runtime(settings: Settings, clock: Clock, tmp_path: Path) -> Any:
    """按需装配运行时（HTTP 测试用 `with_worker=False` 保持确定性）。"""

    created: list[Runtime] = []

    def _make(
        *,
        scm: Any | None = None,
        vcs: Any | None = None,
        agent: Any | None = None,
        workspace: Any | None = None,
        env: Any | None = None,
        with_worker: bool = False,
        **overrides: Any,
    ) -> tuple[Runtime, Any]:
        runtime_settings = settings.model_copy(update=overrides) if overrides else settings
        runtime = build_runtime(
            runtime_settings,
            store=SqliteStore(tmp_path / f"tasks-{len(created)}.db"),
            scm=scm if scm is not None else FakeSCM(access_levels={ALICE_ID: 30}),
            vcs=vcs if vcs is not None else FakeVCS(),
            agent=agent if agent is not None else FakeAgent(),
            workspace=workspace if workspace is not None else FakeWorkspace(),
            env=env if env is not None else FakeEnvPreparer(),
            with_worker=with_worker,
            clock=clock,
        )
        created.append(runtime)
        return runtime, runtime.scm

    yield _make
    for runtime in created:
        runtime.store.close()
