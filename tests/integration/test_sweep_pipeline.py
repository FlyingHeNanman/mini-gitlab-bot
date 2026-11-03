"""周期扫描 + Worker 的闭环：补偿巡检真的能补建并跑完 triage。"""

from __future__ import annotations

from typing import Any

from adapters.fakes import (
    FakeAgent,
    FakeEnvPreparer,
    FakeSCM,
    FakeVCS,
    FakeWorkspace,
    default_agent_result,
)
from core import labels
from core.models import IssueSummary
from core.triage import triage_marker

REPORT: dict[str, Any] = {
    "verdict": "confirmed",
    "confidence": 0.8,
    "summary": "空标签返回 None",
    "repro": {
        "command": "UV_OFFLINE=1 uv run --no-sync pytest -q",
        "result": "reproduced",
        "evidence": "AttributeError: 'NoneType' object is not iterable",
    },
    "location": {"files": ["app.py:3"]},
    "similar": [],
    "labels": {"type": "缺陷", "severity": "一般"},
    "recommendation": "assign",
}


async def test_sweep_enqueues_triage_and_worker_completes_it(make_runtime, clock) -> None:
    issue = IssueSummary(
        iid=71,
        title="parse_labels 在空标签时返回 None",
        description="复现：给一个没有任何标签的 issue 调用 parse_labels()",
        labels=("类型：新功能",),
    )
    scm = FakeSCM(open_issues=[issue], project_labels=())
    runtime, _ = make_runtime(
        scm=scm,
        vcs=FakeVCS(),
        agent=FakeAgent(result=default_agent_result(report=REPORT)),
        workspace=FakeWorkspace(),
        env=FakeEnvPreparer(),
        with_worker=True,
    )
    assert runtime.sweeper is not None
    report = await runtime.sweeper.run_once()
    assert report.triaged == (71,)

    handled = await runtime.worker.drain()
    assert handled == 1
    tasks = runtime.store.list_tasks()
    task = tasks[0]
    assert task.kind.value == "triage"
    assert task.issue_iid == 71
    assert labels.STATUS_NEEDS_HUMAN in scm.current_labels
    assert labels.STATUS_TRIAGING not in scm.current_labels
    assert any(item["marker"] == triage_marker(71) for item in scm.comments)
