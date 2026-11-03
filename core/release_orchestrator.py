"""Tag Push → GitLab Release 编排器（TODO #2）。

流程刻意保持简单且可重入：

1. 读 tag 列表，找到当前 tag 与上一个 tag；
2. 如果 Release 已存在，直接成功（人工改过的说明不覆盖）；
3. 否则取「上一个 tag → 当前 tag」的提交，按 Conventional Commits 分组；
4. 调 Release API 创建说明。重复投递/重试都只会命中最前面的存在检查。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.models import CommitInfo, TagInfo, Task
from core.release import find_previous_tag, release_name, render_release_notes
from core.states import TaskKind, TaskState

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ReleaseConfig:
    max_commits: int = 200
    lookback_days: int = 365


class ReleaseOrchestrator:
    def __init__(
        self,
        *,
        store,
        scm,
        clock: Callable[[], datetime] = _utcnow,
        config: ReleaseConfig | None = None,
        metrics=None,
    ) -> None:
        self._store = store
        self._scm = scm
        self._clock = clock
        self._config = config or ReleaseConfig()
        self._metrics = metrics

    async def process(self, task: Task) -> Task:
        task = self._store.get(task.id) or task
        if task.is_terminal:
            return task
        tag_name = str((task.payload or {}).get("tag_name") or "").strip()
        if not tag_name:
            return await self.finalize_failure(
                task, stage=task.state, reason="Tag Push 任务缺少 tag_name"
            )

        stage = task.state
        try:
            tags = await self._scm.list_tags(task.project, limit=self._config.max_commits)
            tag = next((item for item in tags if item.name == tag_name), None)
            if tag is None:
                # 极少数竞态：tag 刚创建，列表接口还没看到。仍然可以发 Release，
                # 只是没有 compare 链接；不因为读不到列表就判失败。
                tag = TagInfo(name=tag_name, commit_sha=task.commit_sha or "")

            previous = find_previous_tag(tags, tag)
            task = self._store.transition(
                task.id,
                TaskState.RUNNING,
                now=self._clock(),
                report={
                    "tag_name": tag_name,
                    "previous_tag": previous.name if previous else None,
                },
            )
            stage = TaskState.RUNNING

            existing = await self._scm.get_release(task.project, tag_name=tag_name)
            if existing is not None:
                self._store.append_event(
                    task.id,
                    "release_already_exists",
                    {"release_url": existing.web_url},
                    now=self._clock(),
                )
                logger.info("Release %s 已存在，跳过创建（%s）", tag_name, existing.web_url)
                task = self._store.transition(task.id, TaskState.FINALIZING, now=self._clock())
                task = self._store.transition(
                    task.id,
                    TaskState.SUCCEEDED,
                    now=self._clock(),
                    report={"release_url": existing.web_url, "skipped": "existing"},
                )
                self._count(task, skipped=True)
                return task

            commits = await self._collect_commits(task, tag, previous)
            notes = render_release_notes(
                tag=tag, previous=previous, commits=commits, now=self._clock()
            )
            task = self._store.transition(
                task.id,
                TaskState.FINALIZING,
                now=self._clock(),
                report={
                    "commit_count": len(commits),
                    "previous_tag": previous.name if previous else None,
                },
            )
            stage = TaskState.FINALIZING

            release = await self._scm.create_release(
                task.project,
                tag_name=tag_name,
                name=release_name(tag),
                description=notes,
            )
            task = self._store.transition(
                task.id,
                TaskState.SUCCEEDED,
                now=self._clock(),
                report={
                    "release_url": release.web_url,
                    "commit_count": len(commits),
                    "previous_tag": previous.name if previous else None,
                },
                commit_sha=tag.commit_sha or task.commit_sha,
            )
            logger.info(
                "已发布 Release %s（%d 个提交，上一 tag：%s）",
                tag_name,
                len(commits),
                previous.name if previous else "无",
            )
            self._count(task, skipped=False)
            return task
        except Exception as exc:  # noqa: BLE001 绝不让异常冒到 worker
            logger.exception("Release 任务 %s 执行异常", task.id)
            return await self.finalize_failure(
                task,
                stage=stage,
                reason=f"Release 执行异常：{type(exc).__name__}: {exc}",
            )

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
        task = self._store.get(task.id) or task
        if not task.is_terminal:
            task = self._store.fail(
                task.id,
                stage=stage_value,
                reason=reason,
                evidence=evidence,
                log_path=log_path,
                now=self._clock(),
            )
        # Release 没有 issue 可以回帖；失败只进任务表/日志，不污染项目动态
        logger.warning("Release 任务 %s 失败（%s）：%s", task.id, stage_value, reason)
        if self._metrics is not None:
            self._metrics.inc("release_failures_total")
        return task

    # ------------------------------------------------------------------ #

    async def _collect_commits(
        self, task: Task, tag: TagInfo, previous: TagInfo | None
    ) -> tuple[CommitInfo, ...]:
        if previous is not None:
            commits = await self._scm.compare_commits(
                task.project,
                from_ref=previous.name,
                to_ref=tag.name,
                limit=self._config.max_commits,
            )
        else:
            base = (tag.committed_at or self._clock()) - timedelta(days=self._config.lookback_days)
            commits = await self._scm.list_recent_commits(
                task.project,
                ref=tag.name,
                since=base,
                limit=self._config.max_commits,
            )
        # merge 提交只是把已有提交合进来，列在 release notes 里没有信息量
        return tuple(
            commit
            for commit in commits
            if not (commit.title or "").strip().lower().startswith("merge ")
        )

    def _count(self, task: Task, *, skipped: bool) -> None:
        if self._metrics is None:
            return
        self._metrics.inc("release_total")
        if skipped:
            self._metrics.inc("release_skipped_total")
        self._metrics.inc("tasks_total", labels={"kind": str(TaskKind.RELEASE)})
