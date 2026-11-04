"""周期扫描（TODO #1）：补偿巡检 + commit 引用检测 → 回帖已解决并关闭。

两个职责都刻意做成**确定性代码**，不经过模型：

1. **补偿巡检**：webhook 可能漏投（GitLab 连续失败 4 次就临时禁用），所以周期扫
   「open 且没有任何 `状态：` 标签」的 issue，按配额补建 triage 任务。
2. **commit 引用回收**：扫默认分支近期的提交，识别 `Closes/Fixes/Resolves #N`，
   对仍然 open 的 issue 回帖「已在哪个提交修复」、打 `状态：已解决` 并关闭。

为什么不是每次 webhook 都查一遍：issue 数量与提交数量都会增长，周期扫描天然限流；
`状态：` 标签与评论 marker 让重跑幂等（重跑只会更新同一条评论，不会刷屏）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core import labels
from core.label_sync import LabelEnsurer
from core.models import (
    CommitInfo,
    IssueSummary,
    ProjectRef,
    SweepReport,
    Task,
    Trigger,
    TriggerActor,
    TriggerKind,
)
from core.render import BOT_SIGNATURE
from core.states import TaskKind

logger = logging.getLogger(__name__)

RESOLUTION_MARKER_PREFIX = "mini-gitlab-bot:resolved"

#: GitLab 自己的关闭关键字（`Closes #12` / `Fixes #12, #13`，大小写不敏感）
_CLOSING_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+((?:#\d+\s*(?:[,，]|and|和)?\s*)+)"
)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def resolution_marker(issue_iid: int) -> str:
    """「已解决」评论的 marker（按 issue 号，重跑原地更新）。"""
    return f"<!-- {RESOLUTION_MARKER_PREFIX} #{issue_iid} -->"


def commit_references(message: str) -> tuple[int, ...]:
    """从提交信息里取出被关闭关键字引用的 issue 号（去重保序）。"""
    seen: dict[int, None] = {}
    for match in _CLOSING_RE.finditer(message or ""):
        for ref in _ISSUE_REF_RE.finditer(match.group(1)):
            seen[int(ref.group(1))] = None
    return tuple(seen)


def _date(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d") if value else "—"


def render_resolution_comment(
    issue: IssueSummary, commit: CommitInfo, *, now: datetime | None = None
) -> str:
    """回帖正文：说清「由哪个提交、在什么时候解决」，并提示可重新打开。"""
    now = now or datetime.now(UTC)
    short = (commit.sha or "")[:8] or "unknown"
    reference = f"[`{short}`]({commit.web_url})" if commit.web_url else f"`{short}`"
    title = commit.title or (commit.message.splitlines()[0] if commit.message else "")
    return "\n".join(
        [
            resolution_marker(issue.iid),
            "## ✅ 已由提交修复",
            "",
            "这个 issue 已在默认分支的提交 "
            f"{reference}（{_date(commit.created_at)}）中被引用修复：",
            "",
            f"> {title}",
            "",
            "机器人已把它标记为 `状态：已解决` 并关闭。",
            "如果问题仍然存在，直接重新打开这个 issue 即可。",
            "",
            f"<sub>扫描时间 {_date(now)}</sub>",
            BOT_SIGNATURE,
        ]
    )


@dataclass(frozen=True, slots=True)
class SweepConfig:
    """周期扫描配置。默认值与 `gateway.Settings` 保持一致。"""

    triage_enabled: bool = True
    resolve_enabled: bool = True
    interval_sec: float = 1800.0
    triage_scan_limit: int = 100
    triage_quota_per_hour: int = 20
    commit_lookback_days: int = 7
    commit_scan_limit: int = 100
    min_body_chars: int = 10
    max_attempts: int = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _status_labels() -> frozenset[str]:
    return frozenset(label.name for label in labels.labels_in_group(labels.STATUS_GROUP))


def _ephemeral_task(project: ProjectRef, issue_iid: int, *, now: datetime) -> Task:
    """给 SCM 写操作用的临时 Task（不落库，只承载 project + issue 定位）。"""
    return Task(
        id=f"resolve-{project.project_id}-{issue_iid}",
        delivery_key=f"resolve:{project.project_id}:{issue_iid}",
        project=project,
        issue_iid=issue_iid,
        trigger_kind=TriggerKind.SWEEP,
        trigger_actor="sweeper",
        kind=TaskKind.TRIAGE,
        created_at=now,
        updated_at=now,
    )


class Sweeper:
    """周期扫描器：`run_once()` 可单测，`run_forever()` 挂到 worker 常驻循环。"""

    def __init__(
        self,
        *,
        store,
        scm,
        projects: Sequence[str],
        settings: SweepConfig | None = None,
        clock: Callable[[], datetime] = _utcnow,
        metrics=None,
    ) -> None:
        self._store = store
        self._scm = scm
        self._projects = tuple(projects)
        self._settings = settings or SweepConfig()
        self._clock = clock
        self._metrics = metrics
        # 周期扫描可能先于任何 triage 任务运行；`状态：已解决` 标签必须自己确保存在，
        # 否则 GitLab 的 add_labels 会因标签不存在而直接 400。
        self._labels = LabelEnsurer(scm)

    async def run_once(self) -> SweepReport:
        now = self._clock()
        triaged: list[int] = []
        resolved: list[tuple[int, str]] = []
        errors: list[str] = []
        quota_skipped = 0

        for path in self._projects:
            try:
                project = await self._scm.resolve_project(path)
            except Exception as exc:  # noqa: BLE001 单个项目失败不能拖垮整轮扫描
                errors.append(f"{path}: resolve_project: {type(exc).__name__}: {exc}")
                logger.warning("周期扫描解析项目 %s 失败：%s", path, exc)
                continue
            if project is None:
                errors.append(f"{path}: 项目不存在或无权访问")
                continue

            if self._settings.resolve_enabled:
                try:
                    await self._labels.ensure(project.project_id)
                except Exception as exc:  # noqa: BLE001 标签失败不阻止后续尝试
                    errors.append(f"{path}: ensure_labels: {type(exc).__name__}: {exc}")
                    logger.warning("周期扫描创建 %s 的标签失败：%s", path, exc)

            issues: tuple[IssueSummary, ...] = ()
            try:
                issues = await self._scm.list_open_issues(
                    project, limit=self._settings.triage_scan_limit
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: list_open_issues: {type(exc).__name__}: {exc}")
                logger.warning("周期扫描读取 %s 的 open issue 失败：%s", path, exc)

            resolved_here: set[int] = set()
            if self._settings.resolve_enabled:
                done, error = await self._sweep_resolution(project, issues, now)
                resolved.extend(done)
                resolved_here = {issue_iid for issue_iid, _ in done}
                if error:
                    errors.append(error)

            if self._settings.triage_enabled:
                # 已被 commit 引用并关闭的 issue 不再补建 triage（避免关完又跑一遍）
                remaining = [issue for issue in issues if issue.iid not in resolved_here]
                added, skipped, error = await self._sweep_triage(project, remaining, now)
                triaged.extend(added)
                quota_skipped += skipped
                if error:
                    errors.append(error)

        report = SweepReport(
            triaged=tuple(triaged),
            resolved=tuple(resolved),
            quota_skipped=quota_skipped,
            errors=tuple(errors),
        )
        if self._metrics is not None:
            self._metrics.inc("sweep_runs_total")
            self._metrics.inc("sweep_triaged_total", len(triaged))
            self._metrics.inc("sweep_resolved_total", len(resolved))
            self._metrics.inc("sweep_errors_total", len(errors))
        if triaged or resolved or errors:
            logger.info(
                "周期扫描完成：补建巡检 %d、commit 回收 %d、配额跳过 %d、错误 %d",
                len(triaged),
                len(resolved),
                quota_skipped,
                len(errors),
            )
        return report

    async def run_forever(self) -> None:
        interval = max(60.0, self._settings.interval_sec)
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 扫描失败不能拖垮 worker
                logger.exception("周期扫描异常")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #

    async def _sweep_triage(
        self, project: ProjectRef, issues: Sequence[IssueSummary], now: datetime
    ) -> tuple[list[int], int, str | None]:
        statuses = _status_labels()
        recent = self._store.count_tasks_since(
            project.project_id, TaskKind.TRIAGE, now - timedelta(hours=1)
        )
        added: list[int] = []
        skipped = 0
        for issue in issues:
            if issue.state != "opened":
                continue
            if labels.SKIP_LABEL in issue.labels:
                continue
            if any(label in statuses for label in issue.labels):
                continue
            if len((issue.description or "").strip()) < self._settings.min_body_chars:
                continue
            quota = self._settings.triage_quota_per_hour
            if quota > 0 and recent >= quota:
                skipped += 1
                continue
            trigger = Trigger(
                kind=TriggerKind.SWEEP,
                project=project,
                issue_iid=issue.iid,
                actor=TriggerActor(user_id=None, username="sweeper"),
                delivery_key=(f"sweep:{project.project_id}:{issue.iid}:{int(now.timestamp())}"),
                issue_labels=issue.labels,
                source=issue.web_url,
            )
            try:
                outcome = self._store.enqueue(
                    trigger,
                    now=now,
                    max_attempts=self._settings.max_attempts,
                    payload={"sweep": True},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("补偿巡检入队 issue #%s 失败：%s", issue.iid, exc)
                return added, skipped, f"{project.path}: enqueue issue #{issue.iid}: {exc}"
            if outcome.created:
                added.append(issue.iid)
                recent += 1
        return added, skipped, None

    async def _sweep_resolution(
        self, project: ProjectRef, issues: Sequence[IssueSummary], now: datetime
    ) -> tuple[list[tuple[int, str]], str | None]:
        open_by_iid = {issue.iid: issue for issue in issues if issue.state == "opened"}
        if not open_by_iid:
            return [], None
        try:
            branch = await self._scm.default_branch(project)
            commits = await self._scm.list_recent_commits(
                project,
                ref=branch,
                since=now - timedelta(days=self._settings.commit_lookback_days),
                limit=self._settings.commit_scan_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("周期扫描读取 %s 的提交失败：%s", project.path, exc)
            return [], f"{project.path}: list_recent_commits: {type(exc).__name__}: {exc}"

        # 提交按新→旧给；同一 issue 被多个提交引用时取最新那个
        referenced: dict[int, CommitInfo] = {}
        for commit in commits:
            for issue_iid in commit_references(commit.message):
                if issue_iid in open_by_iid and issue_iid not in referenced:
                    referenced[issue_iid] = commit

        resolved: list[tuple[int, str]] = []
        failures: list[str] = []
        for issue_iid, commit in referenced.items():
            issue = open_by_iid[issue_iid]
            if labels.SKIP_LABEL in issue.labels:
                continue
            try:
                await self._resolve_issue(project, issue, commit, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("自动关闭 issue #%s 失败：%s", issue_iid, exc)
                failures.append(f"{project.path}: resolve issue #{issue_iid}: {exc}")
                continue
            resolved.append((issue_iid, commit.sha))
            logger.info(
                "issue #%s 已由提交 %s 修复，已回帖并关闭",
                issue_iid,
                (commit.sha or "")[:8],
            )
        return resolved, ("；".join(failures) if failures else None)

    async def _resolve_issue(
        self,
        project: ProjectRef,
        issue: IssueSummary,
        commit: CommitInfo,
        now: datetime,
    ) -> None:
        task = _ephemeral_task(project, issue.iid, now=now)
        remove = labels.exclusive_removals(issue.labels, keep=labels.STATUS_RESOLVED)
        await self._scm.set_labels(task, add=(labels.STATUS_RESOLVED,), remove=remove)
        await self._scm.upsert_comment(
            task,
            render_resolution_comment(issue, commit, now=now),
            marker=resolution_marker(issue.iid),
        )
        await self._scm.close_issue(task)
