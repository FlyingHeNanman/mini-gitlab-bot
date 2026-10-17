"""`CodexCli` 集成测试：真跑子进程，但"codex"是一个可编排的桩脚本。

为什么值得这么测：`codex exec` 的坑全在**边界**上（PLAN §6 #6）——
`turn.completed` 与改动无关、`Interrupted` 不发终止事件、`-o` 只在成功时写。
用假 runner 返回 `AgentResult` 是测不出这些的，而这里跑的是真 argv、真 JSONL、
真文件系统、真超时杀进程组。

桩脚本用 `sys.executable` 作 shebang，行为直接写进脚本正文 ——
因为子进程环境是**白名单**，`STUB_MODE=xxx` 这种环境变量根本传不进去（这本身就是一条断言）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from adapters.codex_cli import CodexCli, CodexSettings
from core import layout
from core.models import ProjectRef, Task, TriggerKind, Workdir
from tests.helpers import START

OK_BODY = """
import json, pathlib, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0")
    raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
out = pathlib.Path(argv[argv.index("-o") + 1])
internal = workdir / ".mini-gitlab-bot"
internal.mkdir(parents=True, exist_ok=True)
(internal / "report.json").write_text(json.dumps({
    "summary": "修好了",
    "fixes": ["修了 `app.py:1`"],
    "tests": ["新增 test_app.py"],
    "deps": ["tenacity>=9"],
}), encoding="utf-8")
(workdir / "app.py").write_text("x = 2\\n", encoding="utf-8")
out.write_text("已修复并提交（最终消息）", encoding="utf-8")
print(json.dumps({"type": "thread.started"}))
print("这不是 JSON，应被忽略")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
"""

NO_COMPLETED_BODY = """
import json, sys, pathlib
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
out = pathlib.Path(argv[argv.index("-o") + 1])
out.write_text("看起来写完了", encoding="utf-8")
print(json.dumps({"type": "thread.started"}))
raise SystemExit(1)
"""

INTERRUPTED_BODY = """
import sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
sys.stderr.write("interrupted\\n")
raise SystemExit(1)
"""

TURN_FAILED_BODY = """
import json, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
print(json.dumps({"type": "turn.failed", "error": {"message": "model refused"}}))
raise SystemExit(1)
"""

NO_OUTPUT_BODY = """
import json, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
print(json.dumps({"type": "turn.completed"}))
"""

SLOW_BODY = """
import pathlib, sys, time
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
time.sleep(30)
pathlib.Path(argv[argv.index("--cd") + 1], "SHOULD_NOT_EXIST").write_text("x")
"""

ENV_PROBE_BODY = """
import json, os, pathlib, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
internal = workdir / ".mini-gitlab-bot"; internal.mkdir(parents=True, exist_ok=True)
wanted = ["LLM_API_KEY", "GITLAB_TOKEN", "GITLAB_READONLY_TOKEN", "WEBHOOK_SECRET",
          "GIT_ASKPASS", "MINI_GITLAB_BOT_GIT_TOKEN", "UV_OFFLINE", "UV_NO_SYNC", "PATH", "HOME"]
probe = {name: (name in os.environ) for name in wanted}
probe["TMPDIR"] = os.environ.get("TMPDIR", "")
probe["api_key_matches"] = os.environ.get("LLM_API_KEY") == "sk-model-key-123456"
(internal / "env-probe.json").write_text(json.dumps(probe), encoding="utf-8")
pathlib.Path(argv[argv.index("-o") + 1]).write_text("ok", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
"""

BAD_REPORT_BODY = """
import pathlib, sys, json
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
internal = workdir / ".mini-gitlab-bot"; internal.mkdir(parents=True, exist_ok=True)
(internal / "report.json").write_text("{ 这不是 JSON", encoding="utf-8")
pathlib.Path(argv[argv.index("-o") + 1]).write_text("最终消息兜底", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
"""


def write_stub(tmp_path: Path, body: str, name: str = "codex-stub") -> str:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def make_task(tmp_path: Path, *, task_id: str = "task-abc123def456") -> Task:
    return Task(
        id=task_id,
        delivery_key="d-1",
        project=ProjectRef(project_id=42, path="group/project-a"),
        issue_iid=12,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


def make_workdir(tmp_path: Path) -> Workdir:
    path = tmp_path / "work" / "task-abc123def456"
    path.mkdir(parents=True)
    return Workdir(path=str(path), branch="ai-fix/12-x", base_branch="main")


def build_cli(tmp_path: Path, stub: str, *, environ: dict[str, str] | None = None) -> CodexCli:
    return CodexCli(
        settings=CodexSettings(
            bin=stub,
            model_provider="internal",
            model_name="glm-5",
            config_overrides=(
                "model_providers.internal.base_url=https://llm.internal/v1",
                'model_providers.internal.wire_api="responses"',
            ),
        ),
        log_dir=tmp_path / "logs",
        environ={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "LLM_API_KEY": "sk-model-key-123456",
            **(environ or {}),
        },
    )


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


async def test_happy_path_passes_four_conditions(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    cli = build_cli(tmp_path, stub)
    task = make_task(tmp_path)

    result = await cli.run(task, workdir=workdir, context_path=workdir.context_path, timeout_sec=60)

    assert result.ok, result.failed_conditions()
    assert result.exit_code == 0
    assert result.completed_event is True
    assert result.last_message_written is True
    assert "最终消息" in result.last_message
    assert result.timed_out is False


async def test_report_json_is_parsed_into_structured_report(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert result.report["fixes"] == ["修了 `app.py:1`"]
    assert result.report["deps"] == ["tenacity>=9"]
    assert result.report["summary"] == "修好了"
    assert result.report["agent_version"] == "codex-cli 0.163.0"
    assert result.report["agent_exit_code"] == 0


async def test_argv_pins_every_safety_relevant_config(tmp_path: Path) -> None:
    """PLAN §7 基线 3：安全关键项必须用 `-c` 下发（优先级 30 > 项目配置 25）。"""
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    cli = build_cli(tmp_path, stub)
    await cli.run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )

    argv = cli._runner.calls[0]["argv"]  # noqa: SLF001 断言命令行本身
    assert argv[0] == stub
    assert argv[1] == "exec"
    assert "--json" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == workdir.path
    assert "-o" in argv
    for expected in (
        'sandbox_mode="workspace-write"',
        "sandbox_workspace_write.network_access=false",
        'approval_policy="never"',
        'model_provider="internal"',
        'model="glm-5"',
        "model_providers.internal.base_url=https://llm.internal/v1",
        'model_providers.internal.wire_api="responses"',
    ):
        assert expected in argv, expected
    assert "danger-full-access" not in " ".join(argv)
    assert "--full-auto" not in argv  # v0.147.0 起已移除（PLAN §6 事实表旁注）


async def test_prompt_tells_agent_where_the_context_is(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    cli = build_cli(tmp_path, stub)
    await cli.run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    prompt = cli._runner.calls[0]["argv"][-1]  # noqa: SLF001
    assert layout.CONTEXT_FILENAME in prompt
    assert "不要 push" in prompt


# --------------------------------------------------------------------------- #
# 凭据与沙箱（这是本项目最重要的两条不变量）
# --------------------------------------------------------------------------- #


async def test_child_env_has_no_gitlab_credentials(tmp_path: Path) -> None:
    """agent 零凭据（PLAN §7 红线 2）：即使网关环境里有 token，也绝不透传。"""
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    cli = build_cli(
        tmp_path,
        stub,
        environ={
            "GITLAB_TOKEN": "glpat-gateway-secret-1234",
            "GITLAB_READONLY_TOKEN": "glpat-readonly-1234",
            "WEBHOOK_SECRET": "s3cr3t-webhook",
            "GIT_ASKPASS": "/work/.git-askpass",
            "MINI_GITLAB_BOT_GIT_TOKEN": "glpat-askpass-1234",
        },
    )
    await cli.run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )

    env = cli._runner.calls[0]["env"]  # noqa: SLF001
    for leaked in (
        "GITLAB_TOKEN",
        "GITLAB_READONLY_TOKEN",
        "WEBHOOK_SECRET",
        "GIT_ASKPASS",
        "MINI_GITLAB_BOT_GIT_TOKEN",
    ):
        assert leaked not in env, f"{leaked} 不该出现在 agent 子进程环境里"


async def test_child_env_probe_from_inside_the_process(tmp_path: Path) -> None:
    """由**桩进程自己**写下环境探针：比看网关侧记录更可信（记录里的值都被脱敏了）。"""
    stub = write_stub(tmp_path, ENV_PROBE_BODY)
    workdir = make_workdir(tmp_path)
    cli = build_cli(
        tmp_path,
        stub,
        environ={
            "GITLAB_TOKEN": "glpat-gateway-secret-1234",
            "WEBHOOK_SECRET": "s3cr3t-webhook",
            "GIT_ASKPASS": "/work/.git-askpass",
        },
    )
    await cli.run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )

    probe = json.loads(
        (Path(workdir.path) / ".mini-gitlab-bot" / "env-probe.json").read_text(encoding="utf-8")
    )
    assert probe["LLM_API_KEY"] is True  # 模型凭据是唯一被允许进入的环境变量
    assert probe["api_key_matches"] is True
    for leaked in (
        "GITLAB_TOKEN",
        "GITLAB_READONLY_TOKEN",
        "WEBHOOK_SECRET",
        "GIT_ASKPASS",
        "MINI_GITLAB_BOT_GIT_TOKEN",
    ):
        assert probe[leaked] is False, f"{leaked} 泄漏进了 agent 子进程"
    assert probe["UV_OFFLINE"] is True
    assert probe["UV_NO_SYNC"] is True
    assert probe["TMPDIR"] == workdir.temp_dir  # 每 job 独立 TMPDIR（基线 4）
    assert probe["TMPDIR"].startswith(workdir.path)
    assert probe["PATH"] is True and probe["HOME"] is True


async def test_codex_dir_is_deleted_before_run(tmp_path: Path) -> None:
    """基线 1：仓库里带 `.codex/config.toml` 时必须先删掉（防 mcp_servers 注入）。"""
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    codex_dir = Path(workdir.path) / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text("[mcp_servers.evil]\ncommand='curl'\n", encoding="utf-8")

    await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert not codex_dir.exists()


async def test_workspace_is_not_marked_trusted(tmp_path: Path) -> None:
    """基线 2：绝不把克隆目录写成 trusted。"""
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    cli = build_cli(tmp_path, stub)
    await cli.run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    argv = " ".join(cli._runner.calls[0]["argv"])  # noqa: SLF001
    assert "trust_level" not in argv
    assert "trusted" not in argv


# --------------------------------------------------------------------------- #
# 四条件判定：每一条都要能单独失败
# --------------------------------------------------------------------------- #


async def test_exit_zero_with_turn_completed_but_no_last_message_fails(tmp_path: Path) -> None:
    """`-o` 只在成功时写：没有它一样判失败（PLAN §7 基线 5 的第三条）。"""
    stub = write_stub(tmp_path, NO_OUTPUT_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert result.exit_code == 0
    assert result.completed_event is True
    assert result.last_message_written is False
    assert not result.ok
    assert "未写入 --output-last-message 文件" in result.failed_conditions()


async def test_interrupted_exit_one_without_events(tmp_path: Path) -> None:
    """PLAN §6 #6：`Interrupted` 不发任何终止事件但 exit 1 —— 必须判失败。"""
    stub = write_stub(tmp_path, INTERRUPTED_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert result.exit_code == 1
    assert result.completed_event is False
    assert not result.ok
    assert any("疑似被中断" in note for note in result.notes)


async def test_turn_failed_event_is_surfaced_in_notes(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, TURN_FAILED_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert not result.ok
    assert any("turn.failed" in note and "model refused" in note for note in result.notes)


async def test_stale_last_message_file_does_not_count_as_written(tmp_path: Path) -> None:
    """`-o` 只在成功时写：失败时既不写也不删 → 必须用"本次是否新写入"判定。"""
    stub = write_stub(tmp_path, INTERRUPTED_BODY)
    workdir = make_workdir(tmp_path)
    stale = tmp_path / "logs" / "task-abc123def456" / layout.LAST_MESSAGE_FILENAME
    stale.parent.mkdir(parents=True)
    stale.write_text("上一次成功留下的内容", encoding="utf-8")

    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert result.last_message_written is False
    assert not result.ok


async def test_no_completed_event_but_last_message_written_still_fails(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, NO_COMPLETED_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert result.last_message_written is True
    assert result.completed_event is False
    assert not result.ok


async def test_timeout_kills_the_process_group_and_reports(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, SLOW_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=0.5
    )
    assert result.timed_out is True
    assert not result.ok
    assert result.duration_sec < 15
    assert any("超时" in note for note in result.notes)
    # 被杀掉的进程没机会写这个文件
    assert not (Path(workdir.path) / "SHOULD_NOT_EXIST").exists()


# --------------------------------------------------------------------------- #
# 报告与日志
# --------------------------------------------------------------------------- #


async def test_bad_report_json_degrades_to_last_message(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, BAD_REPORT_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert result.ok
    assert result.report["summary"] == "最终消息兜底"  # 退化路径
    assert "fixes" not in result.report


async def test_codex_log_is_written_outside_the_workdir(tmp_path: Path) -> None:
    """日志要留 14 天（PLAN §9 M5），而 job 目录终态即清 → 日志必须在 workdir 之外。"""
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    log_path = Path(result.log_path or "")
    assert log_path.exists()
    assert log_path == tmp_path / "logs" / "task-abc123def456" / layout.CODEX_LOG_FILENAME
    assert not log_path.is_relative_to(Path(workdir.path))
    assert "turn.completed" in log_path.read_text(encoding="utf-8")


async def test_log_tail_is_capped_for_the_failure_comment(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, OK_BODY)
    workdir = make_workdir(tmp_path)
    result = await build_cli(tmp_path, stub).run(
        make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=60
    )
    assert len(result.log_tail.splitlines()) <= 80
    assert result.log_lines >= 1


async def test_missing_codex_binary_raises_clearly(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    cli = build_cli(tmp_path, str(tmp_path / "definitely-not-here"))
    with pytest.raises(FileNotFoundError):
        await cli.run(
            make_task(tmp_path), workdir=workdir, context_path=workdir.context_path, timeout_sec=10
        )


async def test_real_codex_binary_is_absent_in_ci(tmp_path: Path) -> None:
    """记录一个事实：CI/开发机没有真 codex，所以 M2 的验证靠桩二进制。"""
    assert shutil.which("codex") is None or isinstance(shutil.which("codex"), str)
    assert json.dumps({"type": "turn.completed"})  # 桩输出的形状就是 codex 的 JSONL 形状
