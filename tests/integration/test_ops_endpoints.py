"""运维端点：存活 / 就绪 / 指标，以及"绝不泄露任务信息"这条边界。"""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from core.states import TaskState
from gateway.app import create_app
from tests.helpers import make_trigger


async def get(app, path: str) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --------------------------------------------------------------------------- #
# /healthz 与 /readyz
# --------------------------------------------------------------------------- #


async def test_healthz_is_information_free(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    response = await get(create_app(settings=settings, runtime=runtime), "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok_with_worker_and_empty_queue(make_runtime, settings) -> None:
    runtime, _ = make_runtime(with_worker=True)
    response = await get(create_app(settings=settings, runtime=runtime), "/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "queued": 0}


async def test_readyz_reports_worker_disabled(make_runtime, settings) -> None:
    runtime, _ = make_runtime(with_worker=False)
    response = await get(create_app(settings=settings, runtime=runtime), "/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "worker_disabled"


async def test_readyz_flags_stalled_leases(make_runtime, settings, clock) -> None:
    """任务被领走、租约过期很久、看门狗也没动 → 就绪探针必须报 stalled。"""
    runtime, _ = make_runtime(with_worker=True)
    runtime.store.enqueue(make_trigger(), now=clock())
    claimed = runtime.store.claim_next(owner="other", now=clock(), lease_seconds=60)
    assert claimed is not None
    # 把时钟推到很久以后，但不动看门狗：模拟 worker 卡死
    for _ in range(400):
        clock.advance(seconds=1)

    app = create_app(settings=settings, runtime=runtime)
    response = await get(app, "/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "stalled"


async def test_readyz_does_not_leak_task_details(make_runtime, settings, clock) -> None:
    runtime, _ = make_runtime(with_worker=True)
    runtime.store.enqueue(make_trigger(), now=clock())
    body = (await get(create_app(settings=settings, runtime=runtime), "/readyz")).text
    assert "group/project-a" not in body
    assert "task-" not in body
    assert "12" not in body


# --------------------------------------------------------------------------- #
# /metrics
# --------------------------------------------------------------------------- #


async def test_metrics_exposes_task_counters_after_a_run(harness) -> None:
    from tests.integration.test_pipeline_fake import run_pipeline

    task = await run_pipeline(harness.runtime, harness.clock)
    assert task.state is TaskState.SUCCEEDED

    app = create_app(settings=harness.settings, runtime=harness.runtime)
    response = await get(app, "/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'mini_gitlab_bot_tasks_total{state="succeeded"} 1' in body
    assert 'mini_gitlab_bot_queue_depth{state="queued"} 0' in body
    assert "mini_gitlab_bot_worker_inflight 0" in body
    # 指标里绝不能有项目/任务级信息（高基数 + 泄露面）
    assert "group/project-a" not in body
    assert task.id not in body


async def test_metrics_counts_failures_by_stage(harness) -> None:
    from adapters.fakes import FakeAgent
    from core.models import AgentResult
    from tests.integration.test_pipeline_fake import run_pipeline

    harness.runtime.agent = FakeAgent(  # type: ignore[assignment]
        result=AgentResult(exit_code=1, completed_event=False, last_message_written=False)
    )
    # 编排器已持有旧 agent，直接换引用不会生效 → 换一种方式：走失败注入
    harness.vcs.fail_on.add("diff")
    await run_pipeline(harness.runtime, harness.clock)

    body = (
        await get(create_app(settings=harness.settings, runtime=harness.runtime), "/metrics")
    ).text
    assert 'mini_gitlab_bot_tasks_total{state="failed"} 1' in body
    assert 'mini_gitlab_bot_task_failures_total{stage="validating"} 1' in body


async def test_metrics_can_be_disabled(make_runtime, settings) -> None:
    runtime, _ = make_runtime()
    app = create_app(
        settings=settings.model_copy(update={"metrics_enabled": False}), runtime=runtime
    )
    response = await get(app, "/metrics")
    assert response.status_code == 404


async def test_metrics_survive_a_broken_store(make_runtime, settings) -> None:
    """存储炸了也不能让 /metrics 500（否则监控本身变成噪音源）。"""

    class ExplodingStore:
        def close(self) -> None:  # 生命周期收尾要能正常关
            return None

        def __getattr__(self, name):
            def boom(*_args, **_kwargs):
                raise RuntimeError("store down")

            return boom

    runtime, _ = make_runtime()
    runtime.store = ExplodingStore()  # type: ignore[assignment]
    response = await get(create_app(settings=settings, runtime=runtime), "/metrics")
    assert response.status_code == 200
    assert "mini_gitlab_bot_tasks_total" in response.text or response.text == ""
