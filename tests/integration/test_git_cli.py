"""`GitCli` 集成测试：真 git、真仓库、真 push（远程是本地 bare 仓，不需要网络）。

覆盖三件容易写错的事：

1. **"零改动"判定**：v0 的 `_has_commits` 对 fresh clone 恒为真（重试逻辑全废），
   这里断言 `commit_all` 在没有新提交时返回 `None`。
2. **内部产物绝不进提交**：`.mini-gitlab-bot/`（context.md / report.json）、`uv sync`
   生成的 `.venv/`、agent 跑测试留下的 `__pycache__/` 都不能出现在 diff 里。
3. **凭据不进 argv**：token 只经 `GIT_ASKPASS` 与子进程环境传递。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from adapters.git_cli import GitCli, GitError
from adapters.proc import AsyncProcessRunner, Scrubber
from core import layout
from core.models import ProjectRef, Task, TriggerKind, Workdir
from tests.helpers import START

TOKEN = "glpat-supersecret-token-1234"
PROJECT_PATH = "group/project-a"


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """本地 bare 远程 + 一个 main 上的初始提交。"""
    remotes = tmp_path / "remotes"
    bare = remotes / f"{PROJECT_PATH}.git"
    bare.parent.mkdir(parents=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--quiet", "--initial-branch=main", str(seed))
    (seed / "app.py").write_text("x = 1\n", encoding="utf-8")
    (seed / "README.md").write_text("# demo\n", encoding="utf-8")
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
def cli(tmp_path: Path, origin: Path) -> GitCli:
    return GitCli(
        jobs_root=tmp_path / "work",
        remote_base=f"file://{tmp_path / 'remotes'}",
        token=TOKEN,
        runner=AsyncProcessRunner(scrubber=Scrubber((TOKEN,))),
    )


def make_task() -> Task:
    return Task(
        id="task-git0001",
        delivery_key="d-1",
        project=ProjectRef(project_id=42, path=PROJECT_PATH),
        issue_iid=12,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


async def prepare(cli: GitCli, *, branch: str = "ai-fix/12-fix-labels") -> Workdir:
    return await cli.prepare_workdir(make_task(), base_branch="main", branch=branch)


# --------------------------------------------------------------------------- #
# clone / 分支
# --------------------------------------------------------------------------- #


async def test_prepare_workdir_clones_and_creates_branch(cli: GitCli) -> None:
    workdir = await prepare(cli)
    path = Path(workdir.path)
    assert path.is_dir()
    assert (path / ".git").is_dir()
    assert (path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) == "ai-fix/12-fix-labels"
    assert workdir.base_branch == "main"


async def test_prepare_workdir_sets_commit_identity(cli: GitCli) -> None:
    """agent 要在沙箱里自己 `git commit`，身份必须由网关在 clone 后就近配好。"""
    workdir = await prepare(cli)
    path = Path(workdir.path)
    assert git("config", "user.name", cwd=path)
    assert git("config", "user.email", cwd=path)


async def test_prepare_workdir_excludes_job_local_artifacts(cli: GitCli) -> None:
    workdir = await prepare(cli)
    exclude = (Path(workdir.path) / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    for pattern in layout.GIT_EXCLUDE_PATTERNS:
        assert pattern in exclude
    assert layout.INTERNAL_DIRNAME in exclude


async def test_prepare_workdir_cleans_previous_job_dir(cli: GitCli, tmp_path: Path) -> None:
    first = await prepare(cli)
    stale = Path(first.path) / "stale.txt"
    stale.write_text("old", encoding="utf-8")

    again = await prepare(cli)
    assert again.path == first.path
    assert not stale.exists()


async def test_clone_failure_raises_with_readable_message(tmp_path: Path) -> None:
    broken = GitCli(
        jobs_root=tmp_path / "work",
        remote_base=f"file://{tmp_path / 'nowhere'}",
        token=TOKEN,
    )
    with pytest.raises(GitError) as excinfo:
        await broken.prepare_workdir(make_task(), base_branch="main", branch="b")
    assert "clone 失败" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 提交与"零改动"判定
# --------------------------------------------------------------------------- #


async def test_commit_all_returns_none_when_nothing_changed(cli: GitCli) -> None:
    """回归 v0 的 `_has_commits`：fresh clone 上必须返回 None，否则会开出无改动 MR。"""
    workdir = await prepare(cli)
    assert await cli.commit_all(make_task(), workdir, message="fix(issue-12): nothing") is None


async def test_commit_all_commits_working_tree_changes(cli: GitCli) -> None:
    workdir = await prepare(cli)
    (Path(workdir.path) / "app.py").write_text("x = 2\n", encoding="utf-8")

    sha = await cli.commit_all(make_task(), workdir, message="fix(issue-12): parse_labels")
    assert sha and len(sha) == 40
    assert git("log", "-1", "--pretty=%s", cwd=Path(workdir.path)) == "fix(issue-12): parse_labels"


async def test_commit_all_keeps_agent_commit_and_commits_leftovers(cli: GitCli) -> None:
    """agent 自己提交过 → 用它那条；还有未提交改动 → 再补一条（网关兜底）。"""
    workdir = await prepare(cli)
    path = Path(workdir.path)
    (path / "app.py").write_text("x = 2\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "fix(issue-12): agent 自己提交的", cwd=path)
    agent_sha = git("rev-parse", "HEAD", cwd=path)

    (path / "extra.py").write_text("y = 1\n", encoding="utf-8")
    sha = await cli.commit_all(make_task(), workdir, message="fix(issue-12): 网关兜底")
    assert sha != agent_sha
    subjects = git("log", "--pretty=%s", "main..HEAD", cwd=path).splitlines()
    assert subjects == ["fix(issue-12): 网关兜底", "fix(issue-12): agent 自己提交的"]


async def test_internal_artifacts_are_never_committed(cli: GitCli) -> None:
    """`.mini-gitlab-bot/`、`.venv/`、`__pycache__/` 都不许进提交。"""
    workdir = await prepare(cli)
    path = Path(workdir.path)
    internal = path / layout.INTERNAL_DIRNAME
    internal.mkdir(parents=True, exist_ok=True)
    (internal / layout.CONTEXT_FILENAME).write_text("上下文", encoding="utf-8")
    (internal / layout.REPORT_FILENAME).write_text("{}", encoding="utf-8")
    (path / ".venv" / "lib").mkdir(parents=True, exist_ok=True)
    (path / ".venv" / "lib" / "site.py").write_text("x", encoding="utf-8")
    (path / "__pycache__").mkdir(exist_ok=True)
    (path / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")
    (path / "app.py").write_text("x = 2\n", encoding="utf-8")

    await cli.commit_all(make_task(), workdir, message="fix(issue-12): 真改动")
    changed = git("show", "--pretty=", "--name-only", "HEAD", cwd=path).splitlines()
    assert changed == ["app.py"]

    diff = await cli.diff(make_task(), workdir)
    assert diff.files == ("app.py",)


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


async def test_diff_is_empty_on_fresh_branch(cli: GitCli) -> None:
    workdir = await prepare(cli)
    diff = await cli.diff(make_task(), workdir)
    assert diff.is_empty
    assert diff.changed_lines == 0


async def test_diff_reports_files_and_line_counts(cli: GitCli) -> None:
    workdir = await prepare(cli)
    path = Path(workdir.path)
    (path / "app.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
    (path / "new.py").write_text("a\nb\nc\n", encoding="utf-8")

    await cli.commit_all(make_task(), workdir, message="fix(issue-12): 改两处")
    diff = await cli.diff(make_task(), workdir)
    assert set(diff.files) == {"app.py", "new.py"}
    assert diff.insertions == 5  # app.py：x=1→2 加一行 y=3（+2/-1）；new.py：+3
    assert diff.deletions == 1
    assert diff.changed_lines == 6


async def test_diff_handles_non_ascii_paths(cli: GitCli) -> None:
    """`core.quotepath=false`：非 ASCII 路径必须原样返回，否则禁改规则会漏判。"""
    workdir = await prepare(cli)
    (Path(workdir.path) / "修复.py").write_text("z = 1\n", encoding="utf-8")
    await cli.commit_all(make_task(), workdir, message="fix(issue-12): 中文文件名")
    diff = await cli.diff(make_task(), workdir)
    assert diff.files == ("修复.py",)


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #


async def test_push_lands_the_branch_in_the_remote(cli: GitCli, origin: Path) -> None:
    workdir = await prepare(cli)
    (Path(workdir.path) / "app.py").write_text("x = 3\n", encoding="utf-8")
    await cli.commit_all(make_task(), workdir, message="fix(issue-12): push 我")

    await cli.push(make_task(), workdir)

    remote_sha = git("--git-dir", str(origin), "rev-parse", workdir.branch)
    assert remote_sha == git("rev-parse", "HEAD", cwd=Path(workdir.path))


async def test_push_does_not_touch_other_branches(cli: GitCli, origin: Path) -> None:
    main_before = git("--git-dir", str(origin), "rev-parse", "main")
    workdir = await prepare(cli)
    (Path(workdir.path) / "app.py").write_text("x = 4\n", encoding="utf-8")
    await cli.commit_all(make_task(), workdir, message="fix(issue-12): 只推自己")
    await cli.push(make_task(), workdir)
    assert git("--git-dir", str(origin), "rev-parse", "main") == main_before


# --------------------------------------------------------------------------- #
# 凭据（PLAN §7 红线 5）
# --------------------------------------------------------------------------- #


async def test_token_never_appears_in_argv(cli: GitCli) -> None:
    await prepare(cli)
    runner = cli._runner  # noqa: SLF001 断言执行记录
    assert runner.calls, "应当真的执行了 git"
    for call in runner.calls:
        assert TOKEN not in " ".join(call["argv"])
        assert not any("oauth2:" in arg for arg in call["argv"])
        assert all(arg.startswith("file://") or TOKEN not in arg for arg in call["argv"])


async def test_token_is_passed_via_askpass_env_only(cli: GitCli, tmp_path: Path) -> None:
    await prepare(cli)
    env = cli._runner.calls[0]["env"]  # noqa: SLF001
    assert env["GIT_ASKPASS"] == str(tmp_path / "work" / ".git-askpass")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # 记录里的值已脱敏，但密钥名必须在（说明确实走的是环境变量）
    assert env["MINI_GITLAB_BOT_GIT_TOKEN"] == "***"

    askpass = tmp_path / "work" / ".git-askpass"
    assert askpass.exists()
    assert (askpass.stat().st_mode & 0o777) == 0o700
    assert TOKEN not in askpass.read_text(encoding="utf-8")  # 脚本里不含密钥


async def test_remote_url_has_no_credentials(cli: GitCli) -> None:
    assert cli.remote_url(PROJECT_PATH) == f"file://{cli._root.parent}/remotes/{PROJECT_PATH}.git"  # noqa: SLF001
    assert TOKEN not in cli.remote_url(PROJECT_PATH)


async def test_git_env_excludes_gateway_secrets(
    cli: GitCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
    env = cli.git_env()
    assert "WEBHOOK_SECRET" not in env
    assert "GITLAB_READONLY_TOKEN" not in env


def task_with_id(task_id: str) -> Task:
    return Task(
        id=task_id,
        delivery_key=f"d-{task_id}",
        project=ProjectRef(project_id=42, path=PROJECT_PATH),
        issue_iid=12,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


async def test_concurrent_jobs_get_independent_directories(cli: GitCli) -> None:
    """并行任务必须有各自的工作目录（否则 `git add` 会互相串味）。"""
    first, second = await asyncio.gather(
        cli.prepare_workdir(task_with_id("task-aaa111"), base_branch="main", branch="ai-fix/12-a"),
        cli.prepare_workdir(task_with_id("task-bbb222"), base_branch="main", branch="ai-fix/12-b"),
    )
    assert first.path != second.path
    assert Path(first.path, "app.py").exists()
    assert Path(second.path, "app.py").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=Path(first.path)) == "ai-fix/12-a"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=Path(second.path)) == "ai-fix/12-b"


async def test_repairing_same_task_is_idempotent(cli: GitCli) -> None:
    """同一任务重复 prepare 会重建目录（重试路径），不留上一次的残留。"""
    first = await prepare(cli)
    (Path(first.path) / "leftover.txt").write_text("old", encoding="utf-8")

    second = await prepare(cli)
    assert second.path == first.path
    assert not (Path(second.path) / "leftover.txt").exists()


async def test_staged_job_artifacts_abort_the_commit(cli: GitCli) -> None:
    """兜底校验：忽略规则若失效，宁可明确失败，也不能把 `.venv/` 提交进 MR。"""
    from adapters.git_cli import JobArtifactLeak

    workdir = await prepare(cli)
    path = Path(workdir.path)
    (path / ".git" / "info" / "exclude").write_text("", encoding="utf-8")  # 模拟忽略规则失效
    (path / ".venv" / "lib").mkdir(parents=True)
    (path / ".venv" / "lib" / "site.py").write_text("x", encoding="utf-8")
    (path / "app.py").write_text("x = 9\n", encoding="utf-8")

    with pytest.raises(JobArtifactLeak) as excinfo:
        await cli.commit_all(make_task(), workdir, message="fix(issue-12): 应被拦下")
    assert any(".venv" in item for item in excinfo.value.paths)
    # 且确实没有留下任何提交（失败是"什么都没发生"，不是"提交了一半"）
    assert git("rev-list", "--count", "main..HEAD", cwd=path) == "0"
    assert git("log", "-1", "--pretty=%s", cwd=path) == "init"
