"""TriageOrchestrator：新 issue 的「排查·定位」流水线（只读、不 push、不建 MR）。

与 fix 的差异刻意写在一处，避免两套流程漂移：

* 工作区**只读检出**（`branch=None`），agent 不 commit / 不 push；
* 结论来自 `.mini-gitlab-bot/report.json`，由 `core.triage` 做契约校验；
* 副作用只有标签与一条巡检评论，原地更新（marker 按 issue 号）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from core import labels, layout
from core.models import EnvReport, IssueContext, Task, Workdir
from core.policy import describe_agent_failure
from core.states import TaskState
from core.triage import (
    TriageResultError,
    label_plan,
    parse_triage_result,
    render_triage_comment,
    render_triage_failure,
    triage_marker,
)
from core.triage_context import render_triage_context

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _status_labels() -> tuple[str, ...]:
    return tuple(label.name for label in labels.LABELS if label.group == labels.STATUS_GROUP)


class TriageOrchestrator:
    def __init__(
        self,
        *,
        store,
        scm,
        vcs,
        agent,
        workspace,
        env=None,
        metrics=None,
        clock: Callable[[], datetime] = _utcnow,
        timeout_sec: float = 600.0,
    ) -> None:
        self._store = store
        self._scm = scm
        self._vcs = vcs
        self._agent = agent
        self._workspace = workspace
        self._env = env
        self._metrics = metrics
        self._clock = clock
        self._timeout_sec = timeout_sec
        self._labels_ready: set[int] = set()

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def process(self, task: Task) -> Task:
        issue: IssueContext | None = None
        workdir: Workdir | None = None
        agent_result = None
        try:
            issue = await self._scm.load_issue_context(task)
            await self._ensure_labels(task.project.project_id)
            await self._set_status(task, issue.labels, labels.STATUS_TRIAGING)
            current_labels = tuple(dict.fromkeys((*issue.labels, labels.STATUS_TRIAGING)))

            base_branch = await self._scm.default_branch(task.project)
            workdir = await self._vcs.prepare_workdir(task, base_branch=base_branch, branch=None)

            env_report: EnvReport = await self._env.prepare(task, workdir)
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

            await self._workspace.write_text(
                workdir,
                layout.RELATIVE_CONTEXT_PATH,
                render_triage_context(task, issue, now=self._clock()),
            )
            task = self._store.transition(
                task.id,
                TaskState.RUNNING,
                now=self._clock(),
                base_branch=base_branch,
                workdir=workdir.path,
            )

            agent_result = await asyncio.wait_for(
                self._agent.run(
                    task,
                    workdir=workdir,
                    context_path=workdir.context_path,
                    timeout_sec=self._timeout_sec,
                ),
                timeout=self._timeout_sec,
            )
            if not agent_result.ok:
                return await self.finalize_failure(
                    task,
                    stage=TaskState.RUNNING,
                    reason=describe_agent_failure(agent_result),
                    evidence="；".join(
                        [f"未通过：{'；'.join(agent_result.failed_conditions())}"]
                        + list(agent_result.notes)
                    ),
                    log_tail=agent_result.log_tail,
                    log_lines=agent_result.log_lines,
                    log_path=agent_result.log_path or "",
                )

            triage = parse_triage_result(agent_result.report)
            add, remove = label_plan(current_labels, triage)
            task = self._store.transition(
                task.id,
                TaskState.FINALIZING,
                now=self._clock(),
                report=dict(agent_result.report),
                payload={
                    "verdict": triage.verdict.value,
                    "confidence": triage.confidence,
                    "component": triage.component,
                    "files": list(triage.files),
                    "repro": {
                        "command": triage.repro_command,
                        "result": triage.repro_result,
                    },
                },
            )
            await self._best_effort(
                task, "triage_labels", self._scm.set_labels(task, add=add, remove=remove)
            )
            body = render_triage_comment(task, issue, triage, now=self._clock())
            await self._best_effort(
                task,
                "triage_comment",
                self._scm.upsert_comment(task, body, marker=triage_marker(issue.iid)),
            )

            task = self._store.transition(task.id, TaskState.SUCCEEDED, now=self._clock())
            self._count_finished(TaskState.SUCCEEDED, task)
            if self._metrics is not None:
                self._metrics.inc("triage_total", verdict=triage.verdict.value)
            return task
        except TriageResultError as exc:
            log_tail = agent_result.log_tail if agent_result is not None else ""
            return await self.finalize_failure(
                task,
                stage=TaskState.RUNNING,
                reason=f"巡检结论格式不合法：{exc}",
                evidence="模型没有按契约写 report.json，结论已丢弃",
                log_tail=log_tail,
            )
        except TimeoutError:
            return await self.finalize_failure(
                task,
                stage=TaskState.RUNNING,
                reason=f"巡检超时（>{self._timeout_sec:.0f}s）",
                evidence="agent 未在超时前完成",
            )
        except Exception as exc:  # noqa: BLE001 绝不把异常抛给 worker
            logger.exception("巡检任务 %s 执行异常", task.id)
            return await self.finalize_failure(
                task,
                stage=task.state if not task.is_terminal else TaskState.PREPARING,
                reason=f"巡检执行异常：{type(exc).__name__}: {exc}",
                evidence="参见日志",
            )
        finally:
            if workdir is not None:
                try:
                    await self._workspace.remove(workdir)
                except Exception:  # noqa: BLE001 清理失败不影响结论
                    logger.warning("清理巡检工作目录失败：%s", workdir.path, exc_info=True)

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
        stage_value = stage.value if isinstance(stage, TaskState) else str(stage)
        if self._metrics is not None:
            self._metrics.task_failed(stage_value)
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
        self._count_finished(TaskState.FAILED, task)

        current = await self._safe_current_labels(task)
        remove = tuple(
            name for name in _status_labels() if name != labels.STATUS_FAILED and name in current
        )
        await self._best_effort(
            task,
            "failure_labels",
            self._scm.set_labels(task, add=(labels.STATUS_FAILED,), remove=remove),
        )
        issue = IssueContext(
            iid=task.issue_iid,
            title="",
            description="",
            author="",
            web_url="",
        )
        body = render_triage_failure(
            task,
            issue,
            stage=stage_value,
            reason=reason,
            evidence=evidence,
            log_tail=log_tail,
            now=self._clock(),
        )
        await self._best_effort(
            task,
            "failure_comment",
            self._scm.upsert_comment(task, body, marker=triage_marker(task.issue_iid)),
        )
        return task

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _set_status(self, task: Task, current: tuple[str, ...], status: str) -> None:
        remove = labels.exclusive_removals(current, keep=status)
        await self._best_effort(
            task, "triage_status", self._scm.set_labels(task, add=(status,), remove=remove)
        )

    async def _safe_current_labels(self, task: Task) -> tuple[str, ...]:
        try:
            return (await self._scm.load_issue_context(task)).labels
        except Exception:  # noqa: BLE001 失败收尾时读不到标签就当作空
            return ()

    async def _ensure_labels(self, project_id: int) -> None:
        """幂等创建机器人自己的标签（颜色 + 描述）；失败不影响巡检结论。"""
        if project_id in self._labels_ready:
            return
        try:
            existing = await self._scm.list_labels(project_id)
            for label in labels.missing_labels(existing):
                await self._scm.create_label(
                    project_id,
                    name=label.name,
                    color=label.color,
                    description=label.description,
                )
            self._labels_ready.add(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建机器人标签失败（不影响巡检）：%s", exc)

    def _count_finished(self, state: TaskState, task: Task) -> None:
        if self._metrics is None:
            return
        duration = max(0.0, (self._clock() - task.created_at).total_seconds())
        self._metrics.task_finished(state.value, duration)

    async def _best_effort(self, task: Task, kind: str, awaitable) -> None:
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001
            logger.error("巡检任务 %s 的 %s 失败（不影响结论）：%s", task.id, kind, exc)
            self._store.append_event(
                task.id,
                f"{kind}_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
                now=self._clock(),
            )
