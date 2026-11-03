"""一键巡检闭环：issue open 入队 → worker 领取 → 只读 clone → 结论 → 打标/评论。"""

from __future__ import annotations

from typing import Any

import pytest

from adapters.fakes import (
    FakeAgent,
    FakeEnvPreparer,
    FakeSCM,
    FakeVCS,
    FakeWorkspace,
    default_agent_result,
)
from core import labels, layout
from core.label_sync import FALLBACK_COMPONENT_LABEL
from core.labels import TRIGGER_LABEL
from core.models import TriggerKind
from core.states import TaskKind, TaskState
from core.triage import triage_marker
from tests.helpers import ALICE_ID, Clock, make_trigger

REPORT: dict[str, Any] = {
    "verdict": "confirmed",
    "confidence": 0.78,
    "summary": "空标签会把 None 当迭代对象，导致 gateway 崩溃",
    "repro": {
        "command": "UV_OFFLINE=1 uv run --no-sync pytest tests/unit/test_config.py -q",
        "result": "reproduced",
        "evidence": "AttributeError: 'NoneType' object is not iterable",
    },
    "location": {"files": ["gateway/config.py:42"]},
    "similar": [],
    "labels": {"type": "缺陷", "severity": "一般"},
    "recommendation": "assign",
}


class Harness:
    def __init__(self, runtime: Any, scm: FakeSCM, clock: Clock) -> None:
        self.runtime = runtime
        self.scm = scm
        self.clock = clock

    async def run_triage(self, *, issue_iid: int = 12, delivery: str = "d-triage"):
        outcome = self.runtime.store.enqueue(
            make_trigger(
                delivery_key=delivery,
                issue_iid=issue_iid,
                kind=TriggerKind.ISSUE_OPEN,
            ),
            now=self.clock(),
        )
        assert outcome.created and outcome.task is not None
        handled = await self.runtime.worker.drain()
        task = self.runtime.store.get(outcome.task.id)
        return task, handled


def build(make_runtime: Any, clock: Clock, **overrides: Any) -> Harness:
    scm = overrides.pop("scm", None) or FakeSCM(
        access_levels={ALICE_ID: 30},
        labels=(TRIGGER_LABEL, "类型：新功能", "严重度：严重"),
        project_labels=(),
    )
    runtime, _ = make_runtime(
        scm=scm,
        vcs=overrides.pop("vcs", None) or FakeVCS(),
        agent=overrides.pop("agent", None) or FakeAgent(result=default_agent_result(report=REPORT)),
        workspace=overrides.pop("workspace", None) or FakeWorkspace(),
        env=overrides.pop("env", None) or FakeEnvPreparer(),
        with_worker=True,
        **overrides,
    )
    return Harness(runtime, scm, clock)


@pytest.mark.asyncio
async def test_triage_end_to_end(make_runtime, clock: Clock) -> None:
    h = build(make_runtime, clock)
    task, handled = await h.run_triage()
    assert handled == 1
    assert task.state is TaskState.SUCCEEDED
    assert task.kind is TaskKind.TRIAGE

    # 只读检出：branch=None，不建分支
    assert h.runtime.vcs.prepare_calls == [(task.id, "main", None)]

    # 标签：类型/严重度被替换，组件来自命中文件的顶层目录，状态与证据落地
    assert "类型：缺陷" in h.scm.current_labels
    assert "严重度：一般" in h.scm.current_labels
    assert "组件：gateway" in h.scm.current_labels
    assert labels.STATUS_NEEDS_HUMAN in h.scm.current_labels
    assert labels.EVIDENCE_REPRODUCED in h.scm.current_labels
    assert labels.STATUS_TRIAGING not in h.scm.current_labels
    assert "类型：新功能" not in h.scm.current_labels
    assert "严重度：严重" not in h.scm.current_labels

    # 巡检评论原地写了一条，且带 marker
    triage_comments = [c for c in h.scm.comments if c["marker"] == triage_marker(12)]
    assert len(triage_comments) == 1
    body = triage_comments[0]["body"]
    assert "自动巡检结论：确认成立" in body
    assert "`组件：gateway`" in body

    # context.md 里确实写了巡检约束与报告契约
    context_path = f"{task.workdir}/{layout.RELATIVE_CONTEXT_PATH}"
    context = h.runtime.workspace.written[context_path]
    assert "巡检阶段" in context
    assert "结构化结论" in context
    assert "只读" in context

    # 任务详情里带了结构化 payload
    assert task.payload["verdict"] == "confirmed"
    assert task.payload["component"] == "gateway"
    assert task.report["summary"].startswith("空标签")


@pytest.mark.asyncio
async def test_triage_creates_its_own_labels_once(make_runtime, clock: Clock) -> None:
    h = build(make_runtime, clock)
    await h.run_triage()
    created = {item["name"] for item in h.scm.created_labels}
    assert created == {label.name for label in labels.LABELS} | {FALLBACK_COMPONENT_LABEL.name}

    # 同一项目第二次巡检不再重复创建
    await h.run_triage(issue_iid=13, delivery="d-2")
    assert len(h.scm.created_labels) == len(labels.LABELS) + 1


@pytest.mark.asyncio
async def test_invalid_report_fails_task_and_writes_failure_comment(
    make_runtime, clock: Clock
) -> None:
    h = build(
        make_runtime,
        clock,
        agent=FakeAgent(result=default_agent_result(report={})),
    )
    task, _ = await h.run_triage()
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "running"
    assert "格式不合法" in (task.failure_reason or "")
    assert labels.STATUS_FAILED in h.scm.current_labels
    assert any(
        c["marker"] == triage_marker(12) and "巡检结论格式不合法" in c["body"]
        for c in h.scm.comments
    )


@pytest.mark.asyncio
async def test_agent_failure_marks_task_failed(make_runtime, clock: Clock) -> None:
    h = build(
        make_runtime,
        clock,
        agent=FakeAgent(result=default_agent_result(exit_code=1)),
    )
    task, _ = await h.run_triage()
    assert task.state is TaskState.FAILED
    assert "退出码" in (task.failure_reason or "")
    assert labels.STATUS_FAILED in h.scm.current_labels
    assert any(c["marker"] == triage_marker(12) for c in h.scm.comments)


@pytest.mark.asyncio
async def test_env_failure_marks_task_failed_before_agent(make_runtime, clock: Clock) -> None:
    agent = FakeAgent(result=default_agent_result(report=REPORT))
    h = build(
        make_runtime,
        clock,
        env=FakeEnvPreparer(ok=False),
        agent=agent,
    )
    task, _ = await h.run_triage()
    assert task.state is TaskState.FAILED
    assert task.failure_stage == "preparing"
    assert agent.calls == []  # 环境没准备好就不该唤醒 agent
    assert "准备环境失败" in (task.failure_reason or "")


@pytest.mark.asyncio
async def test_label_creation_failure_does_not_fail_triage(make_runtime, clock: Clock) -> None:
    scm = FakeSCM(
        access_levels={ALICE_ID: 30},
        labels=(TRIGGER_LABEL,),
        project_labels=(TRIGGER_LABEL,),
        fail_on=("create_label",),
    )
    h = build(make_runtime, clock, scm=scm)
    task, _ = await h.run_triage()
    assert task.state is TaskState.SUCCEEDED
    assert "组件：gateway" in scm.current_labels


@pytest.mark.asyncio
async def test_scm_failure_fails_cleanly_without_raising(make_runtime, clock: Clock) -> None:
    scm = FakeSCM(
        access_levels={ALICE_ID: 30},
        labels=(TRIGGER_LABEL,),
        fail_on=("load_issue_context",),
    )
    h = build(make_runtime, clock, scm=scm)
    task, _ = await h.run_triage()
    assert task.state is TaskState.FAILED
    assert "巡检执行异常" in (task.failure_reason or "")


@pytest.mark.asyncio
async def test_dirty_worktree_is_flagged_in_comment(make_runtime, clock: Clock) -> None:
    """模型没守只读纪律时，评论必须警告：临时改动已丢弃，不代表仓库现状。"""
    h = build(make_runtime, clock, vcs=FakeVCS(dirty=True))
    task, _ = await h.run_triage()
    assert task.state is TaskState.SUCCEEDED
    assert task.payload["worktree_dirty"] is True
    body = next(c["body"] for c in h.scm.comments if c["marker"] == triage_marker(12))
    assert "未提交的修改" in body
    assert "不代表仓库现状" in body
