"""真实适配器端到端：真 git（本地 bare 远程）+ 真 uv（离线 `uv sync`）+ 桩 codex + 假 SCM。

这是 M2 完成判据里"改代码 → commit → 校验通过"那条链路的**最高保真**版本：
除了 codex 二进制本身（CI 里没有、也不该有），其余全是真件。

桩 codex 的构造方式与 `test_codex_cli.py` 一致：子进程环境是白名单，所以行为只能
写进脚本正文，不能靠环境变量开关。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.sqlite_store import SqliteStore
from core import layout
from core.states import TaskState
from gateway.config import Settings
from gateway.runtime import build_production_runtime
from tests.helpers import ALICE_ID, Clock, make_trigger

PROJECT_PATH = "group/project-a"
UV = shutil.which("uv") or "uv"
TOKEN = "glpat-e2e-secret-token-1234"
PROJECT_FILES = {
    "pyproject.toml": (
        "[project]\n"
        'name = "demo"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = []\n"
    ),
    "app.py": "def add(a, b):\n    return a - b\n",
    "tests/test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    ".gitlab-ci.yml": "stages: [test]\n",
}

#: 桩 codex：改代码 → 自己提交 → 写结构化报告 → 写最终消息（真实的 codex 也是这样）
AGENT_BODY = """
import json, pathlib, subprocess, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
pathlib.Path(workdir, "app.py").write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
subprocess.run(["git", "add", "-A"], cwd=workdir, check=True)
subprocess.run(["git", "commit", "-qm", "fix(issue-12): add() 改成加法"], cwd=workdir, check=True)
subprocess.run(["uv", "run", "--no-sync", "pytest", "-q"], cwd=workdir, check=False)
internal = workdir / ".mini-gitlab-bot"; internal.mkdir(parents=True, exist_ok=True)
(internal / "report.json").write_text(json.dumps({
    "summary": "把 add 里的减法改成加法",
    "fixes": ["`app.py`：`add()` 由减法改为加法"],
    "tests": ["复用已有 `tests/test_app.py::test_add`"],
    "test_command": "uv run --no-sync pytest -q",
    "test_summary": "1 passed",
    "test_duration": "0.4s",
    "not_run": ["类型检查"],
    "risks": ["未覆盖浮点精度"],
    "deps": [],
}), encoding="utf-8")
pathlib.Path(argv[argv.index("-o") + 1]).write_text("已修复 add 并跑通测试", encoding="utf-8")
print(json.dumps({"type": "thread.started"}))
print(json.dumps({"type": "turn.completed"}))
"""

#: 只写报告、一行代码都不改（四条件过了但第四条"diff 非空"必须拦下）
NO_CHANGE_BODY = """
import json, pathlib, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
internal = workdir / ".mini-gitlab-bot"; internal.mkdir(parents=True, exist_ok=True)
(internal / "report.json").write_text(
    json.dumps({"summary": "看起来修好了", "fixes": []}), encoding="utf-8"
)
pathlib.Path(argv[argv.index("-o") + 1]).write_text("已检查，无需改动", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
"""

#: 动 CI 配置（PLAN §7 禁改清单）→ 必须在 push 之前被拦下
FORBIDDEN_PATH_BODY = """
import json, pathlib, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
pathlib.Path(workdir, ".gitlab-ci.yml").write_text("stages: []\\n", encoding="utf-8")
pathlib.Path(argv[argv.index("-o") + 1]).write_text("我顺手改了 CI", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
"""

#: 大改动：60 个文件（超过 diff_max_files=50）
HUGE_DIFF_BODY = """
import json, pathlib, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
workdir = pathlib.Path(argv[argv.index("--cd") + 1])
for index in range(60):
    pathlib.Path(workdir, f"generated_{index}.py").write_text(
        f"VALUE = {index}\\n", encoding="utf-8"
    )
pathlib.Path(argv[argv.index("-o") + 1]).write_text("顺手生成了一堆文件", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}))
"""


#: 睡够久，逼出超时路径
SLOW_BODY = """
import sys, time
argv = sys.argv[1:]
if "--version" in argv:
    print("codex-cli 0.163.0"); raise SystemExit(0)
time.sleep(30)
"""


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def write_stub(tmp_path: Path, body: str, name: str) -> str:
    path = tmp_path / f"stub-{name}"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """bare 远程 + 一个带 `uv.lock` 的初始提交（离线 `uv sync` 需要 lock 已存在）。"""
    bare = tmp_path / "remotes" / f"{PROJECT_PATH}.git"
    bare.parent.mkdir(parents=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    for relative, content in PROJECT_FILES.items():
        target = seed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    git("init", "--quiet", "--initial-branch=main", str(seed))
    # 无依赖项目：`uv lock` 不需要网络，产物让沙箱内的 `uv sync --frozen` 可离线执行。
    # 这里只是准备夹具（不是 agent 沙箱），沿用当前环境最省事。
    subprocess.run([UV, "lock", "--quiet"], cwd=seed, check=True)
    git("add", "-A", cwd=seed)
    git(
        "-c",
        "user.email=seed@example.com",
        "-c",
        "user.name=seed",
        "commit",
        "-qm",
        "init",
        cwd=seed,
    )
    git("init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)
    return bare


@pytest.fixture
def e2e(tmp_path: Path, origin: Path, clock: Clock):
    """装配一套真实适配器的运行时，返回 (runtime, scm, clock, tmp_path)。"""

    def _make(body: str, name: str = "agent", **settings_overrides):
        from adapters.fakes import FakeSCM

        stub = write_stub(tmp_path, body, name)
        overrides = {
            "webhook_secret": "s3cr3t",
            "repo_whitelist": [PROJECT_PATH],
            "ai_fix_label": "ai-fix",
            "bot_username": "mini-gitlab-bot",
            "min_access_level": 30,
            "db_path": tmp_path / f"tasks-{name}.db",
            "work_dir": tmp_path / "work",
            "log_dir": tmp_path / "logs",
            "poll_interval_sec": 0.01,
            "agent_timeout_sec": 120.0,
            # 真实适配器需要的接线：git 指向本地 bare 仓，codex 指向桩脚本
            "gitlab_url": f"file://{tmp_path / 'remotes'}",
            "gitlab_token": TOKEN,
            "git_author_name": "mini-gitlab-bot",
            "git_author_email": "bot@localhost",
            "codex_bin": stub,
            "model_provider": "internal",
            "model_name": "glm-5",
            "codex_expected_version": "0.163.0",
            "uv_cache_dir": tmp_path / "uv-cache",
            "uv_bin": UV,
        }
        overrides.update(settings_overrides)
        settings = Settings(**overrides)
        scm = FakeSCM(access_levels={ALICE_ID: 30})
        store = SqliteStore(settings.db_path)
        runtime = build_production_runtime(settings, store=store, scm=scm, clock=clock)
        assert runtime.worker is not None
        return runtime, scm, store

    created: list = []

    def factory(body: str, name: str = "agent", **overrides):
        runtime, scm, store = _make(body, name, **overrides)
        created.append(store)
        return runtime, scm

    yield factory
    for store in created:
        store.close()


async def run_one(runtime, clock) -> object:
    outcome = runtime.store.enqueue(make_trigger(), now=clock())
    assert outcome.task is not None
    assert await runtime.worker.step() is True
    task = runtime.store.get(outcome.task.id)
    assert task is not None
    return task


# --------------------------------------------------------------------------- #
# 成功链路
# --------------------------------------------------------------------------- #


async def test_real_pipeline_agent_commits_and_gateway_pushes(e2e, clock, origin: Path) -> None:
    runtime, scm = e2e(AGENT_BODY)
    task = await run_one(runtime, clock)

    assert task.state is TaskState.SUCCEEDED, task.failure_reason
    # 分支真的推到了远程，且推上去的就是 agent 自己的那条提交
    remote_sha = git("--git-dir", str(origin), "rev-parse", task.branch)
    assert remote_sha == task.commit_sha
    assert git("--git-dir", str(origin), "log", "-1", "--pretty=%s", task.branch) == (
        "fix(issue-12): add() 改成加法"
    )


async def test_real_pipeline_never_pushes_job_artifacts(e2e, clock, origin: Path) -> None:
    runtime, _ = e2e(AGENT_BODY)
    task = await run_one(runtime, clock)
    assert task.state is TaskState.SUCCEEDED

    changed = git(
        "--git-dir", str(origin), "show", "--pretty=", "--name-only", task.branch
    ).splitlines()
    assert changed == ["app.py"]  # .mini-gitlab-bot/、.venv/、__pycache__/ 都不在
    assert all(not layout.matches_git_exclude(path) for path in changed)


async def test_real_pipeline_report_json_reaches_the_mr_description(e2e, clock) -> None:
    runtime, scm = e2e(AGENT_BODY)
    task = await run_one(runtime, clock)
    assert task.state is TaskState.SUCCEEDED

    description = scm.mr_descriptions[0]
    assert "**摘要**：把 add 里的减法改成加法" in description
    assert "`app.py`：`add()` 由减法改为加法" in description
    assert "复用已有 `tests/test_app.py::test_add`" in description
    assert "1 passed" in description
    assert "0.4s" in description
    assert "类型检查" in description
    assert "覆盖浮点精度" in description
    assert "codex-cli 0.163.0" in description  # Agent 元信息表里上报了内核版本


async def test_real_pipeline_prepares_environment_with_uv(e2e, clock) -> None:
    """真 uv：在克隆出来的仓库里离线装好依赖，agent 才能在沙箱里跑测试。"""
    runtime, _ = e2e(AGENT_BODY)
    task = await run_one(runtime, clock)
    assert task.state is TaskState.SUCCEEDED

    # 成功任务也留 uv 日志（14 天）：事后能回答"当时到底装了什么"
    uv_log = Path(runtime.settings.log_dir) / task.id / layout.UV_LOG_FILENAME
    assert uv_log.exists()
    log_text = uv_log.read_text(encoding="utf-8")
    assert "uv sync" in log_text
    assert "offline" in log_text.lower() or "自检" in log_text


async def test_real_pipeline_cleans_workdir_but_keeps_logs(e2e, clock) -> None:
    runtime, _ = e2e(AGENT_BODY)
    task = await run_one(runtime, clock)
    assert task.state is TaskState.SUCCEEDED

    assert not Path(task.workdir).exists()  # job 目录终态即清
    codex_log = Path(runtime.settings.log_dir) / task.id / layout.CODEX_LOG_FILENAME
    assert codex_log.exists()
    assert "turn.completed" in codex_log.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 四种必须被拦下的情况
# --------------------------------------------------------------------------- #


async def test_no_diff_is_refused(e2e, clock, origin: Path) -> None:
    """四条件之外还要"diff 非空"：agent 说修好了但一行没改 → 失败，不开 MR。"""
    runtime, scm = e2e(NO_CHANGE_BODY, name="nochange")
    task = await run_one(runtime, clock)

    assert task.state is TaskState.FAILED
    assert "未产生任何文件改动" in task.failure_reason
    assert scm.merge_requests == []
    assert git("--git-dir", str(origin), "branch", "--list", "ai-fix/*") == ""  # 没推任何分支


async def test_forbidden_path_is_refused_before_push(e2e, clock, origin: Path) -> None:
    runtime, scm = e2e(FORBIDDEN_PATH_BODY, name="forbidden")
    task = await run_one(runtime, clock)

    assert task.state is TaskState.FAILED
    assert task.failure_stage == TaskState.VALIDATING.value
    assert ".gitlab-ci.yml" in task.failure_evidence
    assert scm.merge_requests == []
    # 关键：没有推到远程（校验在 push 之前）
    assert git("--git-dir", str(origin), "branch", "--list", "ai-fix/*") == ""


async def test_oversized_diff_is_refused(e2e, clock) -> None:
    runtime, scm = e2e(HUGE_DIFF_BODY, name="huge")
    task = await run_one(runtime, clock)

    assert task.state is TaskState.FAILED
    assert task.failure_stage == TaskState.VALIDATING.value
    assert "超过上限 50" in task.failure_reason  # diff_max_files=50
    assert "generated_0.py" in task.failure_evidence  # 证据里给出命中的文件样例
    assert scm.merge_requests == []


async def test_codex_timeout_fails_the_task_and_kills_the_process(e2e, clock) -> None:
    runtime, scm = e2e(SLOW_BODY, name="slow", agent_timeout_sec=1.0)
    task = await run_one(runtime, clock)

    assert task.state is TaskState.FAILED
    assert task.failure_stage == TaskState.RUNNING.value
    assert "超时" in task.failure_reason
    assert scm.merge_requests == []
