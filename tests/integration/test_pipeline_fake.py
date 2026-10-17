"""端到端（假 SCM + 假 agent）：改代码 → commit → 校验 → push → draft MR → 状态流转。

这些测试就是 M1 的完成判据在代码里的样子：不需要 GitLab、不需要 LLM。
"""

from __future__ import annotations

import pytest

from adapters.fakes import FakeAgent, FakeSCM, FakeVCS, default_agent_result
from core.models import DiffSummary
from core.states import TaskState
from tests.helpers import make_trigger


async def run_pipeline(runtime, clock, **trigger_kwargs):
    """入队 → 领一个 → 跑完，返回落库后的任务。"""
    outcome = runtime.store.enqueue(make_trigger(**trigger_kwargs), now=clock())
    assert outcome.task is not None
    assert runtime.worker is not None
    assert await runtime.worker.step() is True
    task = runtime.store.get(outcome.task.id)
    assert task is not None
    return task


# --------------------------------------------------------------------------- #
# 正常闭环
# --------------------------------------------------------------------------- #


async def test_happy_path_opens_draft_mr(harness) -> None:
    task = await run_pipeline(harness.runtime, harness.clock)
    assert task.state is TaskState.SUCCEEDED
    assert task.mr_iid == 1
    assert task.commit_sha
    assert task.finished_at is not None

    assert len(harness.scm.merge_requests) == 1
    mr = harness.scm.merge_requests[0]
    assert mr.draft is True
    assert mr.title.startswith("Draft: ")
    assert "issue-12" in mr.title

    description = harness.scm.mr_descriptions[0]
    assert "Closes #12" in description
    for heading in ("## 改动", "### 修复", "## 验证", "## 风险与未决", "## Agent 元信息"):
        assert heading in description


async def test_happy_path_pushes_branch_from_project_default_branch(harness) -> None:
    task = await run_pipeline(harness.runtime, harness.clock)
    assert task.state is TaskState.SUCCEEDED
    assert task.base_branch == "main"  # 按项目 default_branch，不是全局 TARGET_BRANCH
    assert task.branch is not None and task.branch.startswith("ai-fix/12-")
    assert harness.vcs.pushes == [(task.id, task.branch)]
    assert harness.vcs.prepare_calls[0][1] == "main"


async def test_happy_path_flows_labels_and_updates_single_comment(harness) -> None:
    task = await run_pipeline(harness.runtime, harness.clock)
    assert task.state is TaskState.SUCCEEDED

    assert harness.scm.label_changes == [
        (("ai-fix:running",), ("ai-fix",)),
        (("ai-fix:done",), ("ai-fix:running", "ai-fix")),
    ]
    assert harness.scm.current_labels == {"ai-fix:done"}

    # 状态评论只有一条，且被原地编辑（不是每次新开一条）
    assert len(harness.scm.comments) == 1
    comment = harness.scm.comments[0]
    assert comment["revisions"] >= 2
    assert "已完成" in comment["body"]
    assert "!1" in comment["body"]


async def test_context_md_prefetch_states_the_hard_constraints(harness) -> None:
    await run_pipeline(harness.runtime, harness.clock)
    assert len(harness.workspace.written) == 1
    path, body = next(iter(harness.workspace.written.items()))
    assert path.endswith("/context.md")
    assert "issue #12" in body
    assert "UV_OFFLINE=1" in body  # 离线测试命令
    assert "不要" in body and "git push" in body  # 不许 push / 开 MR
    assert ".gitlab-ci.yml" in body  # 禁改路径写进上下文
    assert "需要新增依赖" in body


async def test_agent_receives_context_path_and_workdir(harness) -> None:
    await run_pipeline(harness.runtime, harness.clock)
    assert len(harness.agent.calls) == 1
    call = harness.agent.calls[0]
    assert call["context_path"].endswith("/context.md")
    assert call["timeout_sec"] == harness.settings.agent_timeout_sec


async def test_commit_message_follows_convention(harness) -> None:
    await run_pipeline(harness.runtime, harness.clock)
    assert harness.vcs.commit_messages == ["fix(issue-12): parse_labels 在空标签时返回 None"]


async def test_job_directory_is_removed_when_finished(harness) -> None:
    await run_pipeline(harness.runtime, harness.clock)
    assert harness.workspace.removed  # PLAN §12 #7：job 目录终态即清


# --------------------------------------------------------------------------- #
# 四条件判定失败
# --------------------------------------------------------------------------- #


async def test_agent_nonzero_exit_fails_with_fixed_wording(make_runtime, clock) -> None:
    agent = FakeAgent(
        result=default_agent_result(exit_code=1, completed_event=False, last_message_written=False)
    )
    runtime, scm = make_runtime(agent=agent, with_worker=True)
    task = await run_pipeline(runtime, clock)

    assert task.state is TaskState.FAILED
    assert task.failure_stage == "running"
    assert task.failure_reason == "codex 退出码 1，未产生 turn.completed 事件"
    assert "exit_code=1" in (task.failure_evidence or "")
    assert scm.merge_requests == []
    assert scm.current_labels == {"ai-fix:failed"}
    assert "失败" in scm.comments[0]["body"]
    assert "如何重试" in scm.comments[0]["body"]


async def test_exit_zero_without_completed_event_fails(make_runtime, clock) -> None:
    agent = FakeAgent(
        result=default_agent_result(exit_code=0, completed_event=False, last_message_written=True)
    )
    runtime, _ = make_runtime(agent=agent, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert task.failure_reason == "codex 已退出但未产生 turn.completed 事件（疑似被中断）"


async def test_missing_output_last_message_fails(make_runtime, clock) -> None:
    agent = FakeAgent(
        result=default_agent_result(exit_code=0, completed_event=True, last_message_written=False)
    )
    runtime, _ = make_runtime(agent=agent, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert task.failure_reason == "codex 未写入 --output-last-message 文件"


async def test_no_file_changes_fails_and_does_not_push(make_runtime, clock) -> None:
    vcs = FakeVCS(commit_sha=None)
    runtime, scm = make_runtime(vcs=vcs, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert task.failure_reason == "agent 未产生任何文件改动（diff 为空）"
    assert vcs.pushes == []
    assert scm.merge_requests == []


# --------------------------------------------------------------------------- #
# diff 策略
# --------------------------------------------------------------------------- #


async def test_forbidden_path_blocks_push(make_runtime, clock) -> None:
    vcs = FakeVCS(diff_summary=DiffSummary(files=(".gitlab-ci.yml",), insertions=1, deletions=0))
    runtime, scm = make_runtime(vcs=vcs, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert "禁改路径" in (task.failure_reason or "")
    assert ".gitlab-ci.yml" in (task.failure_reason or "")
    assert vcs.pushes == []
    assert scm.merge_requests == []


async def test_too_many_files_blocks_push(make_runtime, clock) -> None:
    vcs = FakeVCS(diff_summary=DiffSummary(files=("a.py", "b.py"), insertions=10, deletions=1))
    runtime, _ = make_runtime(vcs=vcs, with_worker=True, diff_max_files=1)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert "改动文件数 2 超过上限 1" in (task.failure_reason or "")


async def test_too_many_lines_blocks_push(make_runtime, clock) -> None:
    vcs = FakeVCS(diff_summary=DiffSummary(files=("a.py",), insertions=500, deletions=0))
    runtime, _ = make_runtime(vcs=vcs, with_worker=True, diff_max_lines=100)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert "改动行数 500 超过上限 100" in (task.failure_reason or "")


# --------------------------------------------------------------------------- #
# 超时与基础设施故障
# --------------------------------------------------------------------------- #


async def test_timeout_fails_the_task(make_runtime, clock) -> None:
    agent = FakeAgent(delay_sec=0.5)
    runtime, _ = make_runtime(agent=agent, with_worker=True, agent_timeout_sec=0.01)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert "任务超时" in (task.failure_reason or "")


async def test_push_failure_fails_the_task(make_runtime, clock) -> None:
    vcs = FakeVCS(fail_on={"push"})
    runtime, scm = make_runtime(vcs=vcs, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "pushing"
    assert "RuntimeError" in (task.failure_reason or "")
    assert scm.merge_requests == []


async def test_prepare_failure_fails_the_task(make_runtime, clock) -> None:
    vcs = FakeVCS(fail_on={"prepare_workdir"})
    runtime, _ = make_runtime(vcs=vcs, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "preparing"


# --------------------------------------------------------------------------- #
# best-effort 通知：通知失败不改变任务结论，但必须留痕
# --------------------------------------------------------------------------- #


async def test_comment_failure_does_not_change_task_result(make_runtime, clock) -> None:
    scm = FakeSCM(access_levels={7: 30}, fail_on=["upsert_comment"])
    runtime, _ = make_runtime(scm=scm, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.SUCCEEDED
    kinds = [event.kind for event in runtime.store.events(task.id)]
    assert "status_comment_failed" in kinds
    assert scm.merge_requests  # MR 仍然开出来了


async def test_label_failure_does_not_change_task_result(make_runtime, clock) -> None:
    scm = FakeSCM(access_levels={7: 30}, fail_on=["set_labels"])
    runtime, _ = make_runtime(scm=scm, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.SUCCEEDED
    kinds = [event.kind for event in runtime.store.events(task.id)]
    assert "start_notify_failed" in kinds and "finish_labels_failed" in kinds


async def test_issue_context_failure_fails_the_task(make_runtime, clock) -> None:
    scm = FakeSCM(access_levels={7: 30}, fail_on=["load_issue_context"])
    runtime, _ = make_runtime(scm=scm, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert "RuntimeError" in (task.failure_reason or "")


@pytest.mark.parametrize("fail_method", ["push", "commit_all", "diff"])
async def test_backend_failures_still_clean_the_job_directory(
    make_runtime, clock, fail_method: str
) -> None:
    """工作目录已建好之后失败，也必须清理（PLAN §12 #7）。"""
    vcs = FakeVCS(fail_on={fail_method})
    runtime, _ = make_runtime(vcs=vcs, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert runtime.workspace.removed


@pytest.mark.parametrize("fail_method", ["load_issue_context", "prepare_workdir"])
async def test_early_failures_never_leak_a_job_directory(
    make_runtime, clock, fail_method: str
) -> None:
    """准备阶段就失败时目录还没建/没建完，不应留下任何残留。"""
    scm = FakeSCM(
        access_levels={7: 30},
        fail_on=["load_issue_context"] if fail_method == "load_issue_context" else [],
    )
    vcs = FakeVCS(fail_on={fail_method} if fail_method != "load_issue_context" else set())
    runtime, _ = make_runtime(scm=scm, vcs=vcs, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert runtime.workspace.removed == []  # 没建过目录 → 也没有清理动作
    assert runtime.store.get(task.id).workdir is None


# --------------------------------------------------------------------------- #
# M2：准备阶段（uv）接进编排器之后的行为
# --------------------------------------------------------------------------- #


async def test_context_and_tmpdir_are_prepared_inside_the_job_dir(harness) -> None:
    """上下文只放在 job 目录内（agent 沙箱唯一可写处），TMPDIR 也指向那里。"""
    from core import layout

    task = await run_pipeline(harness.runtime, harness.clock)
    assert task.state is TaskState.SUCCEEDED

    context_path = f"{task.workdir}/{layout.RELATIVE_CONTEXT_PATH}"
    assert context_path in harness.workspace.written
    assert harness.workspace.temp_dirs == [layout.temp_dir(task.workdir)]
    assert harness.workspace.temp_dirs[0] == f"{task.workdir}/.mini-gitlab-bot/tmp"


async def test_env_preparer_runs_before_the_agent(harness) -> None:
    task = await run_pipeline(harness.runtime, harness.clock)
    assert task.state is TaskState.SUCCEEDED
    assert harness.runtime.env.calls == [task.id]


async def test_env_failure_fails_the_task_at_preparing_stage(make_runtime, clock) -> None:
    """准备阶段失败（拉不到依赖）→ 任务失败在 `preparing`，评论里带 uv 日志尾部。"""
    from adapters.fakes import FakeEnvPreparer

    env = FakeEnvPreparer(
        ok=False,
        failure_reason="准备环境失败：uv sync 退出码 1",
        log_tail="error: No solution found when resolving dependencies",
        log_lines=42,
        log_path="/data/logs/task-x/uv-sync.log",
    )
    runtime, _ = make_runtime(env=env, with_worker=True)
    task = await run_pipeline(runtime, clock)

    assert task.state is TaskState.FAILED
    assert task.failure_stage == TaskState.PREPARING.value
    assert "uv sync" in task.failure_reason
    assert task.log_path == "/data/logs/task-x/uv-sync.log"
    # 准备阶段失败 → 还没跑 agent，也没 push
    assert runtime.agent.calls == []
    assert runtime.vcs.pushes == []
    assert runtime.vcs.commit_messages == []
    assert runtime.scm.merge_requests == []
    assert "No solution found" in runtime.scm.comments[-1]["body"]
    assert "uv sync" in runtime.scm.comments[-1]["body"]


async def test_agent_failure_carries_log_tail_and_notes(make_runtime, clock) -> None:
    from adapters.fakes import FakeAgent
    from core.models import AgentResult

    agent = FakeAgent(
        result=AgentResult(
            exit_code=1,
            completed_event=False,
            last_message_written=False,
            notes=("疑似被中断（exit 1 但没有任何终止事件）",),
            log_tail="interrupted\ncontext canceled",
            log_lines=7,
            log_path="/data/logs/task-y/codex.jsonl",
        )
    )
    runtime, _ = make_runtime(agent=agent, with_worker=True)
    task = await run_pipeline(runtime, clock)

    assert task.state is TaskState.FAILED
    assert task.failure_stage == TaskState.RUNNING.value
    assert task.log_path == "/data/logs/task-y/codex.jsonl"
    assert "疑似被中断" in task.failure_evidence
    comment = runtime.scm.comments[-1]["body"]
    assert "context canceled" in comment  # 日志尾部进了评论（<details> 折叠块）
    assert "疑似被中断" in comment  # 适配器给出的 notes 进了失败评论


async def test_env_failure_evidence_mentions_the_python_version(make_runtime, clock) -> None:
    from adapters.fakes import FakeEnvPreparer

    env = FakeEnvPreparer(ok=False, python_version="3.11", steps=("uv sync --frozen",))
    runtime, _ = make_runtime(env=env, with_worker=True)
    task = await run_pipeline(runtime, clock)
    assert task.state is TaskState.FAILED
    assert "Python 3.11" in task.failure_evidence
