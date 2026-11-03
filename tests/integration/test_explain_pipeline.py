"""合并后解释：不 clone、不装环境，只读 MR diff + issue，写一条解释评论。"""

from __future__ import annotations

from adapters.fakes import (
    FakeAgent,
    FakeEnvPreparer,
    FakeSCM,
    FakeVCS,
    FakeWorkspace,
    default_agent_result,
)
from core import labels
from core.explain import explain_marker
from core.models import TriggerKind
from core.states import TaskKind, TaskState
from tests.helpers import ALICE_ID, Clock, make_trigger


def build(make_runtime, clock: Clock, *, agent=None, scm=None, **overrides):
    runtime, _ = make_runtime(
        scm=scm or FakeSCM(access_levels={ALICE_ID: 30}, project_labels=()),
        vcs=FakeVCS(),
        agent=agent
        or FakeAgent(
            result=default_agent_result(
                last_message="## 改动说明\n把空输入分支从 None 改成 []，根因是提前 return。",
                report={},
            )
        ),
        workspace=FakeWorkspace(),
        env=FakeEnvPreparer(),
        with_worker=True,
        **overrides,
    )
    outcome = runtime.store.enqueue(
        make_trigger(
            delivery_key="merge-1",
            kind=TriggerKind.MERGE,
            issue_iid=12,
            mr_iid=3,
            subject_sha="deadbeefcafe",
        ),
        now=clock(),
    )
    assert outcome.created and outcome.task is not None
    return runtime, outcome.task


async def test_merge_explains_and_marks_merged(make_runtime, clock: Clock) -> None:
    runtime, queued = build(make_runtime, clock, model_explain="small-explain")
    assert await runtime.worker.drain() == 1
    task = runtime.store.get(queued.id)
    assert task.kind is TaskKind.EXPLAIN
    assert task.mr_iid == 3
    assert task.state is TaskState.SUCCEEDED

    # 不 clone、不装环境、不碰 git
    assert runtime.vcs.prepare_calls == []
    assert runtime.env.calls == []
    assert runtime.agent.calls[0]["model"] == "small-explain"

    body = next(c["body"] for c in runtime.scm.comments if c["marker"] == explain_marker(12))
    assert "合并说明" in body
    assert "空输入分支" in body
    assert "未经二次复跑验证" in body
    assert labels.STATUS_MERGED in runtime.scm.current_labels


async def test_non_default_branch_is_skipped(make_runtime, clock: Clock) -> None:
    from core.models import MergeRequestContext

    scm = FakeSCM(
        access_levels={ALICE_ID: 30},
        project_labels=(),
        merge_request=MergeRequestContext(
            iid=3,
            title="feature",
            description="Closes #12",
            source_branch="feature",
            target_branch="develop",
            merge_commit_sha="cafebabe",
            web_url="https://gitlab.example.com/group/project-a/-/merge_requests/3",
            diff="--- app.py\n+ pass",
            closes_issues=(12,),
        ),
    )
    runtime, queued = build(make_runtime, clock, scm=scm)
    assert await runtime.worker.drain() == 1
    task = runtime.store.get(queued.id)
    assert task.state is TaskState.SUCCEEDED
    assert runtime.agent.calls == []
    assert scm.comments == []
