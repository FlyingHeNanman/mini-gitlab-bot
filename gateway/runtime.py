"""运行时装配：把 `Settings` 变成一组已连线的适配器 + 编排器 + Worker。

两种装配入口：

* `build_runtime(settings, ...)`：**测试入口**。未显式注入的适配器一律落在
  `Unavailable*` 占位上 —— 网关能启动、webhook 立刻 2xx，但任务会在明确位置失败并
  写明"适配器未实现"，绝不静默卡住（PLAN 成功标准 3）。
* `build_production_runtime(settings)`：**生产入口**，装上真实适配器
  （`GitLabSCM` + `GitCli` + `CodexCli` + `LocalWorkspace` + `UvEnvPreparer`）。
  没有 `GITLAB_TOKEN` 时 SCM 退回 `UnavailableSCM`（fail closed，但服务仍可启动）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adapters.codex_cli import CodexCli, CodexSettings
from adapters.comment_notifier import CommentNotifier
from adapters.git_cli import GitCli
from adapters.gitlab_client import GitLabSCM, GitLabSettings
from adapters.local_workspace import LocalWorkspace
from adapters.sqlite_store import SqliteStore
from adapters.unavailable import (
    UnavailableAgent,
    UnavailableEnv,
    UnavailableSCM,
    UnavailableVCS,
    UnavailableWorkspace,
)
from adapters.uv_env import UvEnvPreparer, UvSettings
from core.mcp_spec import TOOL_NAMES
from core.models import Task
from core.orchestrator import OrchestratorConfig, TaskOrchestrator
from core.policy import DiffPolicy
from core.worker import Worker
from gateway.config import Settings

logger = logging.getLogger(__name__)

#: 仓库根（容器里是 /srv）：MCP 子进程靠它找到 `gateway` 包
_REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    store: Any
    scm: Any
    vcs: Any
    agent: Any
    workspace: Any
    env: Any
    notifier: Any
    orchestrator: TaskOrchestrator
    worker: Worker | None
    clock: Callable[[], datetime]


def build_runtime(
    settings: Settings,
    *,
    store: Any | None = None,
    scm: Any | None = None,
    vcs: Any | None = None,
    agent: Any | None = None,
    workspace: Any | None = None,
    env: Any | None = None,
    notifier: Any | None = None,
    with_worker: bool = True,
    clock: Callable[[], datetime] = _utcnow,
) -> Runtime:
    store = store if store is not None else SqliteStore(settings.db_path)
    scm = scm if scm is not None else UnavailableSCM()
    vcs = vcs if vcs is not None else UnavailableVCS()
    agent = agent if agent is not None else UnavailableAgent()
    workspace = workspace if workspace is not None else UnavailableWorkspace()
    env = env if env is not None else UnavailableEnv()
    notifier = notifier if notifier is not None else CommentNotifier(scm)

    orchestrator = TaskOrchestrator(
        store=store,
        scm=scm,
        vcs=vcs,
        agent=agent,
        workspace=workspace,
        env=env,
        notifier=notifier,
        config=OrchestratorConfig(
            max_attempts=settings.max_attempts,
            agent_timeout_sec=settings.agent_timeout_sec,
            diff_policy=DiffPolicy(
                max_files=settings.diff_max_files,
                max_lines=settings.diff_max_lines,
                forbidden_paths=tuple(settings.forbidden_paths),
            ),
            ai_fix_label=settings.ai_fix_label,
            mcp_tools=(
                tuple(TOOL_NAMES) if settings.mcp_enabled and settings.gitlab_readonly_token else ()
            ),
        ),
        clock=clock,
    )

    worker: Worker | None = None
    if with_worker and settings.worker_enabled:

        async def _on_task_failed(task: Task) -> None:
            """看门狗判失败后的收尾：状态由 store 记录，这里补标签与评论。"""
            fresh = store.get(task.id) or task
            await orchestrator.finalize_failure(
                fresh,
                stage=fresh.failure_stage or "watchdog",
                reason=fresh.failure_reason or "任务租约过期",
                evidence=fresh.failure_evidence or "",
            )

        worker = Worker(
            store=store,
            orchestrator=orchestrator,
            owner=settings.worker_owner,
            concurrency=settings.agent_concurrency,
            lease_seconds=settings.lease_seconds,
            poll_interval_sec=settings.poll_interval_sec,
            clock=clock,
            on_task_failed=_on_task_failed,
        )

    return Runtime(
        settings=settings,
        store=store,
        scm=scm,
        vcs=vcs,
        agent=agent,
        workspace=workspace,
        env=env,
        notifier=notifier,
        orchestrator=orchestrator,
        worker=worker,
        clock=clock,
    )


def build_production_runtime(
    settings: Settings,
    *,
    store: Any | None = None,
    scm: Any | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> Runtime:
    """生产装配：真实适配器（GitLab / git / codex / uv / 本地工作目录）。

    没有 `GITLAB_TOKEN` 时退回 `UnavailableSCM`：网关仍能启动、webhook 仍立刻 2xx，
    但权限闸门会 fail-closed（fail closed 优于拿空 token 去撞 API）。
    """
    if scm is None:
        if settings.gitlab_token:
            scm = GitLabSCM(
                settings=GitLabSettings(
                    url=settings.gitlab_url,
                    token=settings.gitlab_token,
                    timeout_sec=settings.gitlab_timeout_sec,
                    comment_limit=settings.issue_comment_limit,
                )
            )
        else:
            logger.warning("GITLAB_TOKEN 未设置：SCM 退回占位实现，任务会在权限闸门被拒绝")
            scm = UnavailableSCM()
    workspace = LocalWorkspace(settings.work_dir)
    return build_runtime(
        settings,
        store=store,
        scm=scm,
        vcs=GitCli(
            jobs_root=settings.work_dir,
            remote_base=settings.gitlab_url,
            token=settings.gitlab_token,
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
            clone_depth=settings.git_clone_depth,
            timeout_sec=settings.git_timeout_sec,
            git_bin=settings.git_bin,
        ),
        agent=CodexCli(
            settings=CodexSettings(
                bin=settings.codex_bin,
                model_provider=settings.model_provider,
                model_name=settings.model_name,
                api_key_env=settings.api_key_env,
                config_overrides=tuple(settings.codex_config),
                timeout_sec=settings.agent_timeout_sec,
                expected_version=settings.codex_expected_version,
                # 只读迷你 MCP：没有 read_api token 就不启用（绝不退而求其次用写 token）
                mcp_enabled=settings.mcp_enabled and bool(settings.gitlab_readonly_token),
                mcp_python=settings.mcp_python,
                mcp_pythonpath=_REPO_ROOT,
                mcp_token=settings.gitlab_readonly_token,
                mcp_gitlab_url=settings.gitlab_url,
                mcp_max_output_chars=settings.mcp_max_output_chars,
            ),
            log_dir=settings.log_dir,
        ),
        workspace=workspace,
        env=UvEnvPreparer(
            settings=UvSettings(
                bin=settings.uv_bin,
                cache_dir=str(settings.effective_uv_cache_dir),
                default_index=settings.uv_default_index,
                sync_timeout_sec=settings.uv_sync_timeout_sec,
            ),
            log_dir=settings.log_dir,
        ),
        with_worker=True,
        clock=clock,
    )
