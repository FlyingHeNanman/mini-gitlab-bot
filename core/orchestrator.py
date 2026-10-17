"""TaskOrchestrator：一个任务从 `preparing` 到 `succeeded` / `failed` 的完整编排。

只依赖 `ports/` 协议 + 已注入的适配器，因此整条链路可以在**没有 GitLab、没有 LLM**
的情况下端到端跑通（假 SCM + 假 agent，见 PLAN §8）。

职责边界（PLAN §3.2）：网关独占 GitLab 读写；agent 只改代码 + `git commit`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core import layout, render
from core.branch import branch_name
from core.context import render_context
from core.models import EnvReport, Task, Workdir
from core.policy import DiffPolicy, describe_agent_failure
from core.states import STAGE_LABELS, TaskState

logger = logging.getLogger(__name__)

AI_FIX_LABEL = "ai-fix"
RUNNING_LABEL = "ai-fix:running"
DONE_LABEL = "ai-fix:done"
FAILED_LABEL = "ai-fix:failed"


class _NoopEnv:
    """默认的准备阶段实现：不装依赖（没有 uv / 不需要 uv 的测试与假适配器路径用）。"""

    async def prepare(self, task: Task, workdir: Workdir) -> EnvReport:
        return EnvReport(ok=True, steps=("skipped（NoopEnv）",))


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class OrchestratorConfig:
    max_attempts: int = 3
    agent_timeout_sec: float = 1800.0
    diff_policy: DiffPolicy = field(default_factory=DiffPolicy)
    ai_fix_label: str = AI_FIX_LABEL
    running_label: str = RUNNING_LABEL
    done_label: str = DONE_LABEL
    failed_label: str = FAILED_LABEL
    #: 只读 MCP 可用工具名；空 = 没启用（就不会在 context.md 里告诉 agent 有这些工具）
    mcp_tools: tuple[str, ...] = ()


class TaskOrchestrator:
    def __init__(
        self,
        *,
        store,
        scm,
        vcs,
        agent,
        workspace,
        notifier,
        env=None,
        config: OrchestratorConfig | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._scm = scm
        self._vcs = vcs
        self._agent = agent
        self._workspace = workspace
        self._notifier = notifier
        self._env = env or _NoopEnv()
        self._config = config or OrchestratorConfig()
        self._clock = clock

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def process(self, task: Task) -> Task:
        """执行一个**已被 claim** 的任务（入参 state 应为 `preparing`）。"""
        task = self._store.get(task.id) or task
        if task.is_terminal:
            logger.warning("任务 %s 已是终态 %s，跳过", task.id, task.state)
            return task

        stage: TaskState = TaskState.PREPARING
        workdir: Workdir | None = None
        try:
            await self.start_task(task)

            issue = await self._scm.load_issue_context(task)
            base_branch = await self._scm.default_branch(task.project)
            branch = branch_name(
                task.issue_iid, issue.title, attempt=max(1, task.attempts), task_id=task.id
            )

            workdir = await self._vcs.prepare_workdir(task, base_branch=base_branch, branch=branch)

            # 上下文与 TMPDIR 先备好：agent 的唯一上下文入口 + 每 job 独立临时目录
            # （PLAN §7 基线 4；路径由 core.layout 统一给出，适配器与这里始终一致）
            await self._workspace.temp_dir(workdir)
            await self._workspace.write_text(
                workdir,
                layout.RELATIVE_CONTEXT_PATH,
                render_context(
                    task,
                    issue,
                    policy=self._config.diff_policy,
                    mcp_tools=self._config.mcp_tools,
                    now=self._clock(),
                ),
            )

            # ---- 准备阶段（网关联网）：装依赖 + 离线自检（PLAN §4 两阶段模型）----
            env_report = await self._env.prepare(task, workdir)
            if not env_report.ok:
                steps = "；".join(env_report.steps)
                if env_report.python_version:
                    steps = f"{steps}（Python {env_report.python_version}）" if steps else ""
                return await self.finalize_failure(
                    task,
                    stage=TaskState.PREPARING,
                    reason=env_report.failure_reason or "准备环境失败",
                    evidence=steps or "准备阶段失败",
                    log_tail=env_report.log_tail,
                    log_lines=env_report.log_lines,
                    log_path=env_report.log_path or "",
                )

            task = self._store.transition(
                task.id,
                TaskState.RUNNING,
                now=self._clock(),
                branch=workdir.branch,
                base_branch=workdir.base_branch,
                workdir=workdir.path,
            )

            # ---- agent 阶段：完全离线、零凭据 ----
            stage = TaskState.RUNNING
            result = await asyncio.wait_for(
                self._agent.run(
                    task,
                    workdir=workdir,
                    context_path=workdir.context_path,
                    timeout_sec=self._config.agent_timeout_sec,
                ),
                timeout=self._config.agent_timeout_sec,
            )

            # 四条件判定（PLAN §7 基线 5）：exit 0 且 turn.completed 且 -o 被写入
            if not result.ok:
                return await self.finalize_failure(
                    task,
                    stage=TaskState.RUNNING,
                    reason=describe_agent_failure(result),
                    evidence="；".join(
                        [f"未通过：{'；'.join(result.failed_conditions())}", *result.notes]
                    ),
                    log_tail=result.log_tail,
                    log_lines=result.log_lines,
                    log_path=result.log_path or "",
                )

            # 网关兜底提交：agent 应自己 commit，漏了就由这里补
            commit_sha = await self._vcs.commit_all(
                task, workdir, message=f"fix(issue-{task.issue_iid}): {issue.title}"
            )
            if commit_sha is None:
                return await self.finalize_failure(
                    task,
                    stage=TaskState.VALIDATING,
                    reason="agent 未产生任何文件改动（diff 为空）",
                    evidence="分支相对基线没有新提交",
                )

            task = self._store.transition(
                task.id,
                TaskState.VALIDATING,
                now=self._clock(),
                commit_sha=commit_sha,
                report=dict(result.report),
            )
            stage = TaskState.VALIDATING

            # ---- diff 策略硬校验；最终裁判仍是 GitLab CI ----
            diff = await self._vcs.diff(task, workdir)
            violation = self._config.diff_policy.check(diff)
            if violation is not None:
                return await self.finalize_failure(
                    task,
                    stage=TaskState.VALIDATING,
                    reason=violation.detail,
                    evidence=f"改动 {len(diff.files)} 个文件、{diff.changed_lines} 行"
                    + (f"；命中 {', '.join(violation.paths)}" if violation.paths else ""),
                )

            # ---- 收尾：push → draft MR ----
            task = self._store.transition(task.id, TaskState.PUSHING, now=self._clock())
            stage = TaskState.PUSHING
            await self._vcs.push(task, workdir)

            task = self._store.transition(task.id, TaskState.OPENING_MR, now=self._clock())
            stage = TaskState.OPENING_MR
            mr = await self._scm.create_draft_mr(
                task,
                source_branch=workdir.branch,
                target_branch=workdir.base_branch,
                title=render.mr_title(task, issue_title=issue.title),
                description=render.render_mr_description(task, issue=issue, now=self._clock()),
            )

            task = self._store.transition(
                task.id,
                TaskState.SUCCEEDED,
                now=self._clock(),
                mr_iid=mr.iid,
                mr_url=mr.url,
            )
            await self._finish(task)
            await self._notify(task, render.render_succeeded(task, mr=mr, now=self._clock()))
            return task

        except TimeoutError:
            return await self._fail(
                task,
                stage=stage,
                reason=f"任务超时（>{self._config.agent_timeout_sec:.0f}s）",
                evidence="agent 运行超时，已被取消；进程组已终止",
            )
        except Exception as exc:  # noqa: BLE001 兜底：任何异常都必须落库 + 通知，绝不静默
            logger.exception("任务 %s 在 %s 阶段异常", task.id, stage)
            return await self._fail(
                task,
                stage=stage,
                reason=f"{type(exc).__name__}: {exc}",
                evidence=f"{type(exc).__name__} raised in {STAGE_LABELS[stage]}",
            )
        finally:
            # PLAN §12 #7：job 目录终态即清，否则磁盘必爆
            if workdir is not None:
                try:
                    await self._workspace.remove(workdir)
                except Exception:  # noqa: BLE001 清理失败不影响任务结论
                    logger.warning("清理工作目录失败：%s", workdir.path, exc_info=True)

    # ------------------------------------------------------------------ #
    # 失败收尾（也供看门狗调用）
    # ------------------------------------------------------------------ #

    async def finalize_failure(
        self,
        task: Task,
        *,
        stage: TaskState | str,
        reason: str,
        evidence: str = "",
        log_tail: str = "",
        log_lines: int = 0,
        log_path: str = "",
    ) -> Task:
        return await self._fail(
            task,
            stage=stage,
            reason=reason,
            evidence=evidence,
            log_tail=log_tail,
            log_lines=log_lines,
            log_path=log_path,
        )

    async def _fail(
        self,
        task: Task,
        *,
        stage: TaskState | str,
        reason: str,
        evidence: str = "",
        log_tail: str = "",
        log_lines: int = 0,
        log_path: str = "",
    ) -> Task:
        stage_value = stage.value if isinstance(stage, TaskState) else str(stage)
        if not task.is_terminal:
            task = self._store.fail(
                task.id,
                stage=stage_value,
                reason=reason,
                evidence=evidence,
                log_path=log_path,
                now=self._clock(),
            )
        else:
            task = self._store.get(task.id) or task
        await self._finish(task)
        await self._notify(
            task,
            render.render_failed(task, log_tail=log_tail, log_lines=log_lines, now=self._clock()),
        )
        return task

    # ------------------------------------------------------------------ #
    # 状态评论 / 标签（best-effort：失败只记录，不改变任务结论）
    # ------------------------------------------------------------------ #

    async def start_task(self, task: Task) -> None:
        """标记运行中：`ai-fix` → `ai-fix:running`，并贴出状态评论。"""
        await self._best_effort(
            task,
            "start_notify",
            self._scm.set_labels(
                task, add=(self._config.running_label,), remove=(self._config.ai_fix_label,)
            ),
        )
        await self._notify(task, render.render_running(task, now=self._clock()))

    async def _finish(self, task: Task) -> None:
        add = (
            self._config.done_label
            if task.state is TaskState.SUCCEEDED
            else self._config.failed_label
        )
        await self._best_effort(
            task,
            "finish_labels",
            self._scm.set_labels(
                task,
                add=(add,),
                remove=(self._config.running_label, self._config.ai_fix_label),
            ),
        )

    async def _notify(self, task: Task, body: str) -> None:
        await self._best_effort(task, "status_comment", self._notifier.publish(task, body))

    async def _best_effort(self, task: Task, kind: str, awaitable) -> None:
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001
            logger.error("任务 %s 的 %s 失败（不影响任务结论）：%s", task.id, kind, exc)
            self._store.append_event(
                task.id,
                f"{kind}_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
                now=self._clock(),
            )
