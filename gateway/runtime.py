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
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adapters.codex_cli import CodexCli, CodexSettings
from adapters.comment_notifier import CommentNotifier
from adapters.git_cli import GitCli
from adapters.gitlab_client import GitLabSCM, GitLabSettings
from adapters.janitor import Janitor, RetentionSettings
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
from core.metrics import Metrics
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
    metrics: Metrics
    janitor: Any
    #: 只读任务页的 token；空 = 页面关闭
    admin_token: str


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
    metrics: Metrics | None = None,
    janitor: Any | None = None,
) -> Runtime:
    store = store if store is not None else SqliteStore(settings.db_path)
    scm = scm if scm is not None else UnavailableSCM()
    vcs = vcs if vcs is not None else UnavailableVCS()
    agent = agent if agent is not None else UnavailableAgent()
    workspace = workspace if workspace is not None else UnavailableWorkspace()
    env = env if env is not None else UnavailableEnv()
    notifier = notifier if notifier is not None else CommentNotifier(scm)
    metrics = metrics if metrics is not None else Metrics()
    admin_token = _resolve_admin_token(settings)

    orchestrator = TaskOrchestrator(
        store=store,
        scm=scm,
        vcs=vcs,
        agent=agent,
        workspace=workspace,
        env=env,
        notifier=notifier,
        metrics=metrics,
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
        metrics=metrics,
        janitor=janitor,
        admin_token=admin_token,
    )


def _resolve_admin_token(settings: Settings) -> str:
    """任务页 token：显式配置优先，否则自动生成并持久化（0600）。

    为什么自动生成而不是"默认无鉴权"：任务表里有仓库名、issue 标题、失败堆栈，
    默认裸奔等于给内网任何人一个信息出口。生成一次并落到 `DB_PATH` 同目录，
    重启后 URL 不变；运维只要 `cat /data/admin_token` 就能拿到。
    """
    if not settings.tasks_page_enabled:
        return ""
    if settings.admin_token:
        return settings.admin_token

    token_file = Path(settings.db_path).parent / "admin_token"
    try:
        if token_file.is_file():
            existing = token_file.read_text(encoding="utf-8").strip()
            if len(existing) >= 16:
                return existing
        token = secrets.token_hex(16)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        logger.info("已生成只读任务页 token：%s（%s）", token, token_file)
        return token
    except OSError as exc:
        # 写不了就退回"关闭页面"，绝不在没有鉴权的情况下开门
        logger.warning("无法读写任务页 token 文件（%s），/tasks 已关闭：%s", token_file, exc)
        return ""


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
    if store is None:
        store = SqliteStore(settings.db_path)
    metrics = Metrics()
    janitor = Janitor(
        store=store,
        work_dir=settings.work_dir,
        log_dir=settings.log_dir,
        settings=RetentionSettings(
            log_days=settings.log_retention_days,
            task_days=settings.task_retention_days,
            orphan_job_days=settings.orphan_job_retention_days,
            interval_sec=settings.janitor_interval_sec,
        ),
        clock=clock,
        metrics=metrics,
    )
    workspace = LocalWorkspace(settings.work_dir)
    return build_runtime(
        settings,
        store=store,
        metrics=metrics,
        janitor=janitor,
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
                model_base_url=settings.model_base_url,
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
