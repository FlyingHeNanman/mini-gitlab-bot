"""分阶段模型：triage/explain 与小模型、fix 与大模型（空 = 回落 MODEL_NAME）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.codex_cli import CodexCli, CodexSettings
from core.models import ProjectRef, Task, TriggerKind, Workdir


def make_task() -> Task:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    return Task(
        id="task-model",
        delivery_key="d",
        project=ProjectRef(project_id=1, path="group/project"),
        issue_iid=1,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=now,
        updated_at=now,
    )


async def build_argv(model: str | None) -> str:
    cli = CodexCli(
        settings=CodexSettings(
            bin="codex",
            model_provider="internal",
            model_name="big-model",
        )
    )
    argv = await cli._build_argv(  # noqa: SLF001 断言命令行本身
        task=make_task(),
        workdir=Workdir(path="/tmp/job", branch="main", base_branch="main"),
        context_name="context.md",
        last_message="/tmp/last.md",
        model=model,
    )
    return " ".join(argv)


@pytest.mark.asyncio
async def test_stage_model_overrides_default() -> None:
    argv = await build_argv("small-model")
    assert 'model="small-model"' in argv
    assert 'model="big-model"' not in argv


@pytest.mark.asyncio
async def test_empty_stage_model_falls_back_to_model_name() -> None:
    argv = await build_argv("")
    assert 'model="big-model"' in argv
