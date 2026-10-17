"""`LocalWorkspace`：写文件只能落在 job 目录内（agent 沙箱之外的又一道边界）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.local_workspace import LocalWorkspace, WorkspaceViolation
from core import layout
from core.models import Workdir


@pytest.fixture
def workspace(tmp_path: Path) -> LocalWorkspace:
    return LocalWorkspace(tmp_path / "jobs")


@pytest.fixture
def workdir(tmp_path: Path) -> Workdir:
    path = tmp_path / "jobs" / "task-1"
    path.mkdir(parents=True)
    return Workdir(path=str(path), branch="ai-fix/12-x", base_branch="main")


async def test_write_text_creates_parent_dirs(workspace: LocalWorkspace, workdir: Workdir) -> None:
    path = await workspace.write_text(workdir, layout.RELATIVE_CONTEXT_PATH, "上下文内容")
    assert path == f"{workdir.path}/{layout.RELATIVE_CONTEXT_PATH}"
    assert Path(path).read_text(encoding="utf-8") == "上下文内容"


async def test_write_text_is_atomic(workspace: LocalWorkspace, workdir: Workdir) -> None:
    """临时文件 + os.replace：不会出现"写了一半"的 context.md 被 agent 读到。"""
    target = Path(workdir.path) / "a.txt"
    await workspace.write_text(workdir, "a.txt", "one")
    await workspace.write_text(workdir, "a.txt", "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert list(target.parent.glob(".a.txt*")) == []


@pytest.mark.parametrize("relative", ["../escape.txt", "../../etc/passwd", "a/../../b.txt"])
async def test_write_text_refuses_to_escape_job_dir(
    workspace: LocalWorkspace, workdir: Workdir, relative: str
) -> None:
    with pytest.raises(WorkspaceViolation):
        await workspace.write_text(workdir, relative, "nope")


async def test_write_text_refuses_absolute_path(
    workspace: LocalWorkspace, workdir: Workdir
) -> None:
    with pytest.raises(WorkspaceViolation):
        await workspace.write_text(workdir, "/etc/passwd", "nope")


async def test_write_text_refuses_symlink_escape(
    workspace: LocalWorkspace, workdir: Workdir, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(workdir.path) / "link").symlink_to(outside)
    with pytest.raises(WorkspaceViolation):
        await workspace.write_text(workdir, "link/evil.txt", "nope")


async def test_temp_dir_is_inside_the_job_dir_and_private(
    workspace: LocalWorkspace, workdir: Workdir
) -> None:
    path = Path(await workspace.temp_dir(workdir))
    assert path.is_dir()
    assert path == Path(workdir.temp_dir)
    assert path.is_relative_to(Path(workdir.path))
    assert (path.stat().st_mode & 0o777) == 0o700


async def test_remove_deletes_the_job_dir(workspace: LocalWorkspace, workdir: Workdir) -> None:
    (Path(workdir.path) / "f.txt").write_text("x", encoding="utf-8")
    await workspace.remove(workdir)
    assert not Path(workdir.path).exists()


async def test_remove_refuses_the_jobs_root(workspace: LocalWorkspace, tmp_path: Path) -> None:
    """手滑传错对象时不能把整个 jobs 根目录删掉。"""
    root = tmp_path / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceViolation):
        await workspace.remove(Workdir(path=str(root), branch="b", base_branch="main"))


async def test_remove_is_idempotent(workspace: LocalWorkspace, workdir: Workdir) -> None:
    await workspace.remove(workdir)
    await workspace.remove(workdir)  # 不抛
    assert not Path(workdir.path).exists()


def test_matches_git_exclude_covers_all_patterns() -> None:
    for path in (
        ".mini-gitlab-bot/context.md",
        ".venv/lib/site.py",
        "__pycache__/app.cpython-313.pyc",
        "src/__pycache__/x.pyc",
        "app.pyc",
        ".pytest_cache/v/cache",
        ".coverage",
        "htmlcov/index.html",
        ".uv-cache/x",
    ):
        assert layout.matches_git_exclude(path), path
    for path in ("app.py", "src/main.py", ".gitlab-ci.yml", "docs/report.json"):
        assert not layout.matches_git_exclude(path), path
