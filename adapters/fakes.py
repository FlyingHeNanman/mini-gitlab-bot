"""假适配器：让整条链路在**没有 GitLab、没有 LLM** 的情况下端到端跑通（PLAN §8）。

它们同时是"契约的可执行文档"：集成测试断言的一切（draft 前缀、`Closes #N`、
状态评论原地编辑、标签流转）都通过这些记录暴露出来。

`fail_on` 用于注入故障，验证"绝不 5xx"和失败路径。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from adapters.proc import CompletedProcess
from core.labels import TRIGGER_LABEL
from core.models import (
    AgentResult,
    CommitInfo,
    DiffSummary,
    EnvReport,
    IssueContext,
    IssueSummary,
    MergeRequestContext,
    MergeRequestRef,
    ProjectRef,
    ReleaseInfo,
    TagInfo,
    Task,
    Workdir,
)

DEFAULT_REPORT: dict[str, Any] = {
    "fixes": ["修复 `parse_labels` 空标签返回 None 的问题（`gateway/config.py:42`）"],
    "tests": ["新增 `tests/test_config.py::test_parse_labels_empty`"],
    "test_command": "UV_OFFLINE=1 uv run --no-sync pytest -q",
    "test_summary": "128 passed, 2 skipped",
    "test_duration": "4.2s",
    "not_run": ["ruff", "mypy"],
    "risks": ["并发路径未覆盖测试"],
    "deps": [],
    "agent_version": "codex fake-0.0.1",
}


def default_agent_result(**overrides: Any) -> AgentResult:
    payload: dict[str, Any] = {
        "exit_code": 0,
        "completed_event": True,
        "last_message_written": True,
        "last_message": "已修复并提交",
        "duration_sec": 12.5,
        "log_path": "/tmp/fake-codex.jsonl",
        "report": dict(DEFAULT_REPORT),
    }
    payload.update(overrides)
    return AgentResult(**payload)


class FakeSCM:
    def __init__(
        self,
        *,
        default_branch: str = "main",
        labels: Sequence[str] = (TRIGGER_LABEL,),
        issue_title: str = "parse_labels 在空标签时返回 None",
        issue_body: str = "复现：给一个没有任何标签的 issue 调用 parse_labels()",
        comments: Sequence[str] = ("我也遇到了这个问题",),
        access_levels: dict[int, int] | None = None,
        user_ids: dict[str, int] | None = None,
        merge_request: MergeRequestContext | None = None,
        project_labels: Sequence[str] = (),
        fail_on: Sequence[str] = (),
        project_refs: Mapping[str, ProjectRef] | None = None,
        open_issues: Sequence[IssueSummary] = (),
        closed_issue_summaries: Sequence[IssueSummary] = (),
        recent_commits: Sequence[CommitInfo] = (),
        tags: Sequence[TagInfo] = (),
        commits_between: Mapping[tuple[str, str], Sequence[CommitInfo]] | None = None,
        existing_releases: Mapping[str, ReleaseInfo] | None = None,
    ) -> None:
        self.default_branch_value = default_branch
        self.current_labels: set[str] = set(labels)
        self.project_labels: set[str] = set(project_labels)
        self.issue_title = issue_title
        self.issue_body = issue_body
        self.issue_comments = tuple(comments)
        self.access_levels = dict(access_levels or {})
        self.user_ids = dict(user_ids or {})
        self.merge_request = merge_request
        self.fail_on = set(fail_on)
        self.project_refs = dict(project_refs or {})
        self.open_issues = list(open_issues)
        self.closed_issue_summaries = list(closed_issue_summaries)
        self.recent_commits = list(recent_commits)
        self.tags = list(tags)
        self.commits_between = {key: tuple(value) for key, value in (commits_between or {}).items()}
        self.releases: dict[str, ReleaseInfo] = dict(existing_releases or {})

        # 记录
        self.label_changes: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.member_lookups: list[tuple[int, int]] = []
        self.comments: list[dict[str, Any]] = []
        self.created_labels: list[dict[str, Any]] = []
        self.mr_descriptions: list[str] = []
        self.merge_requests: list[MergeRequestRef] = []
        self.issue_loads: list[str] = []
        self.assignments: list[tuple[str, int]] = []
        self.linked_issues: list[tuple[str, int]] = []
        self.closed_issues: set[str] = set()
        self.closed_issue_iids: list[int] = []
        self.project_lookups: list[str] = []
        self.release_creations: list[ReleaseInfo] = []

    # ---- 内部 ----

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise RuntimeError(f"FakeSCM.{method} 注入故障")

    # ---- 读 ----

    async def default_branch(self, project: ProjectRef) -> str:
        self._maybe_fail("default_branch")
        return self.default_branch_value

    async def resolve_project(self, path: str) -> ProjectRef | None:
        self._maybe_fail("resolve_project")
        self.project_lookups.append(path)
        return self.project_refs.get(path) or ProjectRef(project_id=42, path=path)

    async def list_open_issues(
        self, project: ProjectRef, *, limit: int = 100
    ) -> tuple[IssueSummary, ...]:
        self._maybe_fail("list_open_issues")
        return tuple(self.open_issues[:limit])

    async def list_closed_issues(
        self, project: ProjectRef, *, since: datetime, limit: int = 100
    ) -> tuple[IssueSummary, ...]:
        self._maybe_fail("list_closed_issues")
        return tuple(
            issue
            for issue in self.closed_issue_summaries
            if issue.state == "closed" and (issue.updated_at is None or issue.updated_at >= since)
        )[:limit]

    async def list_recent_commits(
        self, project: ProjectRef, *, ref: str, since: datetime, limit: int = 50
    ) -> tuple[CommitInfo, ...]:
        self._maybe_fail("list_recent_commits")
        return tuple(self.recent_commits[:limit])

    async def list_tags(self, project: ProjectRef, *, limit: int = 100) -> tuple[TagInfo, ...]:
        self._maybe_fail("list_tags")
        return tuple(self.tags[:limit])

    async def compare_commits(
        self, project: ProjectRef, *, from_ref: str, to_ref: str, limit: int = 200
    ) -> tuple[CommitInfo, ...]:
        self._maybe_fail("compare_commits")
        return self.commits_between.get((from_ref, to_ref), ())[:limit]

    async def get_release(self, project: ProjectRef, *, tag_name: str) -> ReleaseInfo | None:
        self._maybe_fail("get_release")
        return self.releases.get(tag_name)

    async def get_member_access_level(self, project_id: int, user_id: int) -> int | None:
        self._maybe_fail("get_member_access_level")
        self.member_lookups.append((project_id, user_id))
        return self.access_levels.get(user_id)

    async def load_issue_context(self, task: Task) -> IssueContext:
        self._maybe_fail("load_issue_context")
        self.issue_loads.append(task.id)
        return IssueContext(
            iid=task.issue_iid,
            title=self.issue_title,
            description=self.issue_body,
            author="reporter",
            web_url=f"https://gitlab.example.com/{task.project.path}/-/issues/{task.issue_iid}",
            labels=tuple(sorted(self.current_labels)),
            comments=self.issue_comments,
        )

    async def load_merge_request_context(self, task: Task) -> MergeRequestContext:
        self._maybe_fail("load_merge_request_context")
        if self.merge_request is not None:
            return self.merge_request
        return MergeRequestContext(
            iid=task.mr_iid or 1,
            title="Draft: fix(issue)",
            description=f"Closes #{task.issue_iid}",
            source_branch="ai-fix/12",
            target_branch="main",
            merge_commit_sha="deadbeefcafe",
            web_url=f"https://gitlab.example.com/{task.project.path}/-/merge_requests/1",
            diff="--- app.py\n+ return []",
            closes_issues=(task.issue_iid,),
        )

    async def list_labels(self, project_id: int) -> tuple[str, ...]:
        self._maybe_fail("list_labels")
        return tuple(sorted(self.project_labels))

    async def create_label(
        self, project_id: int, *, name: str, color: str, description: str
    ) -> None:
        self._maybe_fail("create_label")
        self.project_labels.add(name)
        self.created_labels.append({"name": name, "color": color, "description": description})

    async def find_user_id(self, username: str) -> int | None:
        self._maybe_fail("find_user_id")
        return self.user_ids.get(username)

    # ---- 写 ----

    async def set_labels(
        self, task: Task, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self._maybe_fail("set_labels")
        self.label_changes.append((tuple(add), tuple(remove)))
        self.current_labels -= set(remove)
        self.current_labels |= set(add)
        for items in (self.open_issues, self.closed_issue_summaries):
            for index, issue in enumerate(items):
                if issue.iid != task.issue_iid:
                    continue
                labels = set(issue.labels)
                labels -= set(remove)
                labels |= set(add)
                items[index] = IssueSummary(
                    iid=issue.iid,
                    title=issue.title,
                    labels=tuple(sorted(labels)),
                    state=issue.state,
                    web_url=issue.web_url,
                    description=issue.description,
                    updated_at=issue.updated_at,
                )

    async def upsert_comment(self, task: Task, body: str, *, marker: str) -> int:
        self._maybe_fail("upsert_comment")
        for comment in self.comments:
            if comment["marker"] == marker:
                comment["body"] = body
                comment["revisions"] += 1
                return int(comment["id"])
        comment = {"id": len(self.comments) + 1, "marker": marker, "body": body, "revisions": 1}
        self.comments.append(comment)
        return int(comment["id"])

    async def find_comment(self, task: Task, *, marker: str) -> int | None:
        self._maybe_fail("find_comment")
        for comment in self.comments:
            if comment["marker"] == marker:
                return int(comment["id"])
        return None

    async def create_draft_mr(
        self,
        task: Task,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestRef:
        self._maybe_fail("create_draft_mr")
        mr_iid = len(self.merge_requests) + 1
        base = f"https://gitlab.example.com/{task.project.path}/-/merge_requests"
        mr = MergeRequestRef(
            iid=mr_iid,
            url=f"{base}/{mr_iid}",
            title=title,
            draft=title.startswith("Draft:"),
        )
        self.merge_requests.append(mr)
        self.mr_descriptions.append(description)
        return mr

    async def assign_issue(self, task: Task, user_id: int) -> None:
        self._maybe_fail("assign_issue")
        self.assignments.append((task.id, user_id))

    async def link_issue(self, task: Task, *, target_issue_iid: int) -> None:
        self._maybe_fail("link_issue")
        self.linked_issues.append((task.id, target_issue_iid))

    async def close_issue(self, task: Task) -> None:
        self._maybe_fail("close_issue")
        self.closed_issues.add(task.id)
        self.closed_issue_iids.append(task.issue_iid)
        self.open_issues = [issue for issue in self.open_issues if issue.iid != task.issue_iid]

    async def create_release(
        self, project: ProjectRef, *, tag_name: str, name: str, description: str
    ) -> ReleaseInfo:
        self._maybe_fail("create_release")
        if tag_name in self.releases:
            raise RuntimeError(f"Release {tag_name} 已存在（FakeSCM 注入）")
        release = ReleaseInfo(
            tag_name=tag_name,
            name=name,
            web_url=f"https://gitlab.example.com/{project.path}/-/releases/{tag_name}",
            description=description,
        )
        self.releases[tag_name] = release
        self.release_creations.append(release)
        return release


@dataclass
class FakeVCS:
    diff_summary: DiffSummary = field(
        default_factory=lambda: DiffSummary(files=("gateway/config.py",), insertions=3, deletions=1)
    )
    commit_sha: str | None = "abc1234def5678"
    dirty: bool = False
    fail_on: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.prepare_calls: list[tuple[str, str, str]] = []
        self.commit_messages: list[str] = []
        self.pushes: list[tuple[str, str]] = []

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise RuntimeError(f"FakeVCS.{method} 注入故障")

    async def prepare_workdir(self, task: Task, *, base_branch: str, branch: str | None) -> Workdir:
        self._maybe_fail("prepare_workdir")
        self.prepare_calls.append((task.id, base_branch, branch))
        return Workdir(
            path=f"/tmp/fake-jobs/{task.id}", branch=branch or base_branch, base_branch=base_branch
        )

    async def commit_all(self, task: Task, workdir: Workdir, *, message: str) -> str | None:
        self._maybe_fail("commit_all")
        self.commit_messages.append(message)
        return self.commit_sha

    async def diff(self, task: Task, workdir: Workdir) -> DiffSummary:
        self._maybe_fail("diff")
        return self.diff_summary

    async def worktree_dirty(self, task: Task, workdir: Workdir) -> bool:
        self._maybe_fail("worktree_dirty")
        return self.dirty

    async def push(self, task: Task, workdir: Workdir) -> None:
        self._maybe_fail("push")
        self.pushes.append((task.id, workdir.branch))


@dataclass
class FakeAgent:
    result: AgentResult = field(default_factory=default_agent_result)
    delay_sec: float = 0.0
    raises: BaseException | None = None

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        task: Task,
        *,
        workdir: Workdir,
        context_path: str,
        timeout_sec: float,
        model: str | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "task_id": task.id,
                "workdir": workdir.path,
                "context_path": context_path,
                "timeout_sec": timeout_sec,
                "model": model,
            }
        )
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeWorkspace:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.written: dict[str, str] = {}
        self.files: dict[str, str] = dict(files or {})
        self.removed: list[str] = []
        self.temp_dirs: list[str] = []

    async def write_text(self, workdir: Workdir, relative_path: str, text: str) -> str:
        path = f"{workdir.path}/{relative_path}"
        self.written[path] = text
        return path

    async def read_file(self, workdir: Workdir, relative_path: str) -> str | None:
        path = f"{workdir.path}/{relative_path}"
        if path in self.files:
            return self.files[path]
        return self.written.get(path)

    async def temp_dir(self, workdir: Workdir) -> str:
        path = workdir.temp_dir
        self.temp_dirs.append(path)
        return path

    async def create_job_dir(self, task) -> Workdir:
        return Workdir(path=f"/tmp/fake-jobs/{task.id}", branch="", base_branch="")

    async def remove(self, workdir: Workdir) -> None:
        self.removed.append(workdir.path)


@dataclass
class FakeEnvPreparer:
    """准备阶段的假实现（默认成功）。

    `ok=False` 用来验证"准备阶段失败即任务失败，并带上日志尾部"。
    """

    ok: bool = True
    failure_reason: str = "准备环境失败：uv sync 退出码 1"
    python_version: str = "3.13"
    steps: tuple[str, ...] = ("uv sync --frozen", "offline check ok")
    log_tail: str = "error: No solution found when resolving dependencies"
    log_lines: int = 12
    log_path: str | None = "memory://uv-sync.log"
    delay_sec: float = 0.0
    raises: BaseException | None = None

    def __post_init__(self) -> None:
        self.calls: list[str] = []

    async def prepare(self, task: Task, workdir: Workdir) -> EnvReport:
        self.calls.append(task.id)
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.raises is not None:
            raise self.raises
        return EnvReport(
            ok=self.ok,
            python_version=self.python_version,
            steps=self.steps,
            failure_reason="" if self.ok else self.failure_reason,
            log_tail="" if self.ok else self.log_tail,
            log_lines=0 if self.ok else self.log_lines,
            log_path=self.log_path,
        )


@dataclass
class FakeProcessRunner:
    """可编排的子进程结果，用来在**不装 git / codex / uv** 的情况下测适配器。

    * `scripted`：按调用顺序返回的结果；
    * `default`：`scripted` 用完后统一返回的结果；
    * `hook`：每次调用前执行，可用来模拟"CLI 写出了 `-o` 文件 / `report.json`"。
    """

    scripted: list[CompletedProcess] = field(default_factory=list)
    default: CompletedProcess | None = None
    raises: BaseException | None = None
    hook: Callable[[Sequence[str], Mapping[str, str]], None] | None = None

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        label: str = "",
        max_output_chars: int = 200_000,
    ) -> CompletedProcess:
        record = {
            "argv": tuple(str(item) for item in argv),
            "cwd": cwd,
            "env": dict(env or {}),
            "timeout_sec": timeout_sec,
            "label": label,
        }
        self.calls.append(record)
        if self.hook is not None:
            self.hook(record["argv"], record["env"])
        if self.raises is not None:
            raise self.raises
        if self.scripted:
            result = self.scripted.pop(0)
        elif self.default is not None:
            result = self.default
        else:
            result = CompletedProcess(argv=record["argv"], exit_code=0)
        return result
