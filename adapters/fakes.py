"""假适配器：让整条链路在**没有 GitLab、没有 LLM** 的情况下端到端跑通（PLAN §8）。

它们同时是"契约的可执行文档"：集成测试断言的一切（draft 前缀、`Closes #N`、
状态评论原地编辑、标签流转）都通过这些记录暴露出来。

`fail_on` 用于注入故障，验证"绝不 5xx"和失败路径。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from adapters.proc import CompletedProcess
from core.models import (
    AgentResult,
    DiffSummary,
    EnvReport,
    IssueContext,
    MergeRequestRef,
    ProjectRef,
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
        labels: Sequence[str] = ("ai-fix",),
        issue_title: str = "parse_labels 在空标签时返回 None",
        issue_body: str = "复现：给一个没有任何标签的 issue 调用 parse_labels()",
        comments: Sequence[str] = ("我也遇到了这个问题",),
        access_levels: dict[int, int] | None = None,
        fail_on: Sequence[str] = (),
    ) -> None:
        self.default_branch_value = default_branch
        self.current_labels: set[str] = set(labels)
        self.issue_title = issue_title
        self.issue_body = issue_body
        self.issue_comments = tuple(comments)
        self.access_levels = dict(access_levels or {})
        self.fail_on = set(fail_on)

        # 记录
        self.label_changes: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.member_lookups: list[tuple[int, int]] = []
        self.comments: list[dict[str, Any]] = []
        self.mr_descriptions: list[str] = []
        self.merge_requests: list[MergeRequestRef] = []
        self.issue_loads: list[str] = []

    # ---- 内部 ----

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise RuntimeError(f"FakeSCM.{method} 注入故障")

    # ---- 读 ----

    async def default_branch(self, project: ProjectRef) -> str:
        self._maybe_fail("default_branch")
        return self.default_branch_value

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

    # ---- 写 ----

    async def set_labels(
        self, task: Task, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self._maybe_fail("set_labels")
        self.label_changes.append((tuple(add), tuple(remove)))
        self.current_labels -= set(remove)
        self.current_labels |= set(add)

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


@dataclass
class FakeVCS:
    diff_summary: DiffSummary = field(
        default_factory=lambda: DiffSummary(files=("gateway/config.py",), insertions=3, deletions=1)
    )
    commit_sha: str | None = "abc1234def5678"
    fail_on: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.prepare_calls: list[tuple[str, str, str]] = []
        self.commit_messages: list[str] = []
        self.pushes: list[tuple[str, str]] = []

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise RuntimeError(f"FakeVCS.{method} 注入故障")

    async def prepare_workdir(self, task: Task, *, base_branch: str, branch: str) -> Workdir:
        self._maybe_fail("prepare_workdir")
        self.prepare_calls.append((task.id, base_branch, branch))
        return Workdir(path=f"/tmp/fake-jobs/{task.id}", branch=branch, base_branch=base_branch)

    async def commit_all(self, task: Task, workdir: Workdir, *, message: str) -> str | None:
        self._maybe_fail("commit_all")
        self.commit_messages.append(message)
        return self.commit_sha

    async def diff(self, task: Task, workdir: Workdir) -> DiffSummary:
        self._maybe_fail("diff")
        return self.diff_summary

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
        self, task: Task, *, workdir: Workdir, context_path: str, timeout_sec: float
    ) -> AgentResult:
        self.calls.append(
            {
                "task_id": task.id,
                "workdir": workdir.path,
                "context_path": context_path,
                "timeout_sec": timeout_sec,
            }
        )
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeWorkspace:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.removed: list[str] = []
        self.temp_dirs: list[str] = []

    async def write_text(self, workdir: Workdir, relative_path: str, text: str) -> str:
        path = f"{workdir.path}/{relative_path}"
        self.written[path] = text
        return path

    async def temp_dir(self, workdir: Workdir) -> str:
        path = workdir.temp_dir
        self.temp_dirs.append(path)
        return path

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
