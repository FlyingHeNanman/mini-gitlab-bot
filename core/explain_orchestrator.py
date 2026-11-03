"""ExplainOrchestrator：MR 合并后的「解释响应」。

不 clone、不装环境、不跑测试：网关只预取 MR diff + issue + 最近一次 triage 结论，
让 agent 生成一段人话解释，然后原地更新 issue 评论并打 `状态：已合并`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from core import labels, layout
from core.explain import (
    clip_diff,
    explain_marker,
    render_explain_comment,
    render_explain_context,
    render_explain_failure,
)
from core.label_sync import LabelEnsurer
from core.models import IssueContext, Task, Workdir
from core.policy import describe_agent_failure
from core.states import TaskKind, TaskState

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ExplainOrchestrator:
    def __init__(
        self,
        *,
        store,
        scm,
        agent,
        workspace,
        metrics=None,
        clock: Callable[[], datetime] = _utcnow,
        timeout_sec: float = 300.0,
        model: str = "",
    ) -> None:
        self._store = store
        self._scm = scm
        self._agent = agent
        self._workspace = workspace
        self._metrics = metrics
        self._clock = clock
        self._timeout_sec = timeout_sec
        self._model = model
        self._labels = LabelEnsurer(scm)

    async def process(self, task: Task) -> Task:
        task = self._store.get(task.id) or task
        if task.is_terminal:
            return task
        issue: IssueContext | None = None
        job_dir: Workdir | None = None
        try:
            mr = await self._scm.load_merge_request_context(task)
            default_branch = await self._scm.default_branch(task.project)
            if mr.target_branch and mr.target_branch != default_branch:
                # 只解释合入默认分支的 MR（webhook 层无法知道默认分支，这里兜底）
                self._store.append_event(
                    task.id,
                    "explain_skipped",
                    {"reason": "not_default_branch", "target": mr.target_branch},
                    now=self._clock(),
                )
                task = self._store.transition(task.id, TaskState.RUNNING, now=self._clock())
                task = self._store.transition(task.id, TaskState.FINALIZING, now=self._clock())
                return self._store.transition(task.id, TaskState.SUCCEEDED, now=self._clock())

            issue = await self._scm.load_issue_context(task)
            await self._ensure_labels(task)
            triage_payload = self._triage_payload(task)
            job_dir = await self._workspace.create_job_dir(task)
            await self._workspace.write_text(
                job_dir,
                layout.RELATIVE_CONTEXT_PATH,
                render_explain_context(
                    task, issue, mr, triage_payload=triage_payload, now=self._clock()
                ),
            )
            task = self._store.transition(task.id, TaskState.RUNNING, now=self._clock())

            result = await asyncio.wait_for(
                self._agent.run(
                    task,
                    workdir=job_dir,
                    context_path=job_dir.context_path,
                    timeout_sec=self._timeout_sec,
                    model=self._model or None,
                ),
                timeout=self._timeout_sec,
            )
            if not result.ok:
                return await self.finalize_failure(
                    task,
                    issue,
                    stage=TaskState.RUNNING,
                    reason=describe_agent_failure(result),
                    log_tail=result.log_tail,
                    log_lines=result.log_lines,
                    log_path=result.log_path or "",
                )

            explanation = result.last_message.strip() or str(result.report.get("summary") or "")
            if not explanation.strip():
                return await self.finalize_failure(
                    task, issue, stage=TaskState.RUNNING, reason="模型没有产出解释文本"
                )

            task = self._store.transition(
                task.id,
                TaskState.FINALIZING,
                now=self._clock(),
                report=dict(result.report),
                payload={
                    "mr_iid": mr.iid,
                    "merge_commit_sha": mr.merge_commit_sha,
                    "closes_issues": list(mr.closes_issues),
                },
            )
            current = issue.labels
            remove = labels.exclusive_removals(current, keep=labels.STATUS_MERGED)
            await self._best_effort(
                task,
                "explain_labels",
                self._scm.set_labels(task, add=(labels.STATUS_MERGED,), remove=remove),
            )
            body = render_explain_comment(
                task,
                issue,
                mr,
                explanation,
                truncated=clip_diff(mr.diff).truncated,
                now=self._clock(),
            )
            await self._best_effort(
                task,
                "explain_comment",
                self._scm.upsert_comment(task, body, marker=explain_marker(issue.iid)),
            )
            task = self._store.transition(task.id, TaskState.SUCCEEDED, now=self._clock())
            self._count_finished(TaskState.SUCCEEDED, task)
            if self._metrics is not None:
                self._metrics.inc("explain_total")
            return task
        except TimeoutError:
            return await self.finalize_failure(
                task,
                issue,
                stage=TaskState.RUNNING,
                reason=f"解释生成超时（>{self._timeout_sec:.0f}s）",
            )
        except Exception as exc:  # noqa: BLE001 绝不把异常抛给 worker
            logger.exception("解释任务 %s 执行异常", task.id)
            return await self.finalize_failure(
                task,
                issue,
                stage=task.state if not task.is_terminal else TaskState.PREPARING,
                reason=f"解释执行异常：{type(exc).__name__}: {exc}",
            )
        finally:
            if job_dir is not None:
                try:
                    await self._workspace.remove(job_dir)
                except Exception:  # noqa: BLE001
                    logger.warning("清理解释工作目录失败：%s", job_dir.path, exc_info=True)

    async def finalize_failure(
        self,
        task: Task,
        issue: IssueContext | None = None,
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
        self._count_finished(TaskState.FAILED, task)
        if issue is None:
            issue = IssueContext(
                iid=task.issue_iid, title="", description="", author="", web_url=""
            )
        await self._best_effort(
            task,
            "explain_failure_comment",
            self._scm.upsert_comment(
                task,
                render_explain_failure(task, issue, reason=reason, now=self._clock()),
                marker=explain_marker(task.issue_iid),
            ),
        )
        return task

    # ------------------------------------------------------------------ #

    def _triage_payload(self, task: Task) -> dict | None:
        latest = self._store.latest_task(
            task.project.project_id,
            TaskKind.TRIAGE,
            f"issue:{task.issue_iid}",
        )
        return dict(latest.payload) if latest is not None else None

    async def _ensure_labels(self, task: Task) -> None:
        try:
            await self._labels.ensure(task.project.project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建机器人标签失败（不影响解释）：%s", exc)

    def _count_finished(self, state: TaskState, task: Task) -> None:
        if self._metrics is None:
            return
        duration = max(0.0, (self._clock() - task.created_at).total_seconds())
        self._metrics.task_finished(state.value, duration)

    async def _best_effort(self, task: Task, kind: str, awaitable) -> None:
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001
            logger.error("解释任务 %s 的 %s 失败（不影响结论）：%s", task.id, kind, exc)
            self._store.append_event(
                task.id,
                f"{kind}_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
                now=self._clock(),
            )
