"""只读迷你 MCP：真子进程 + 真 stdio JSON-RPC + 真 HTTP 打桩 GitLab。

这是 M4 的核心验证。用 `mock` 挡掉 GitLab 只能证明"我以为的调用"，而这里是：

* **真的起一个 `python -m gateway.mcp_readonly` 进程**，按 MCP stdio 协议一行一条 JSON；
* 它的 GitLab 请求真的发出去（打到 `tests/stub_gitlab.py`），路径/参数可断言；
* 只读与项目绑定这两条硬约束由**进程边界**保证（不是靠自觉）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.stub_gitlab import StubGitLab

REPO_ROOT = Path(__file__).resolve().parents[2]
READONLY = "glpat-readonly-token-123456"
WRITE = "glpat-write-token-should-never-appear"
PROJECT_ID = 42
ISSUE = 12


# --------------------------------------------------------------------------- #
# 夹具：桩 GitLab + 可编程的 MCP 子进程
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub():
    with StubGitLab() as server:
        server.state.token = READONLY  # MCP 必须用只读 token 才通过
        server.state.members[7] = 30
        server.state.add_issue(
            ISSUE,
            title="parse_labels 在空标签时返回 None",
            description="复现：给一个没有任何标签的 issue 调用 parse_labels()",
            labels=["ai-fix", "bug"],
        )
        server.state.add_note(ISSUE, "我也遇到了这个问题", author="bob")
        server.state.add_note(ISSUE, "added ai-fix label", system=True)

        server.state.add_pipeline(900, ref="main", status="failed")
        server.state.add_job(900, 5001, name="pytest", log=_long_log())
        server.state.add_job(
            900, 5002, name="ruff", status="success", failure_reason=None, log="ok\n"
        )
        server.state.test_reports[900] = {
            "total_count": 3,
            "success_count": 2,
            "failed_count": 1,
            "error_count": 0,
            "skipped_count": 0,
            "test_cases": [
                {"name": "test_ok", "status": "success"},
                {
                    "name": "test_empty_labels",
                    "classname": "tests.test_labels",
                    "status": "failed",
                    "message": "AssertionError: None != []",
                    "file": "tests/test_labels.py",
                    "line": 12,
                },
            ],
        }
        server.state.add_commit(
            "a" * 40,
            title="fix(issue-12): parse_labels",
            diffs=[
                {
                    "old_path": "app.py",
                    "new_path": "app.py",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                    "diff": (
                        "@@ -1,3 +1,3 @@\n"
                        "-def parse_labels(x):\n"
                        "+def parse_labels(x):\n"
                        "+    return x or []"
                    ),
                }
            ],
        )
        server.state.languages = {"Python": 97.5, "Shell": 2.5}
        yield server


def _long_log() -> str:
    lines = [f"collecting test {index}" for index in range(300)]
    lines += ["FAILED tests/test_labels.py::test_empty_labels - AssertionError", "2 failed"]
    return "\n".join(lines) + "\n"


class McpProcess:
    """一问一答的 MCP stdio 客户端（够用且没有并发复杂度）。"""

    def __init__(self, env: dict[str, str]) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "gateway.mcp_readonly"],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self.stderr_lines.append(line)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """发一条消息；有 id 就等响应，没有 id（通知）就不等。"""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if method.startswith("notifications/"):
            self._write(message)
            return None
        self._next_id += 1
        message["id"] = self._next_id
        self._write(message)
        return self._read()

    def _write(self, message: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict[str, Any]:
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            detail = "".join(self.stderr_lines)
            raise AssertionError(f"MCP 进程没有响应；stderr={detail}")
        return json.loads(line)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.send("tools/call", {"name": name, "arguments": arguments or {}})
        assert response is not None
        return response["result"]

    def text(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = self.call(name, arguments)
        assert result["isError"] is False, result["content"][0]["text"]
        return result["content"][0]["text"]

    def payload(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return json.loads(self.text(name, arguments))

    def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        try:
            self._proc.wait(timeout=10)
        finally:
            if self._proc.poll() is None:
                self._proc.kill()


def make_env(stub: StubGitLab, **overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "MINI_GITLAB_BOT_PROJECT_ID": str(PROJECT_ID),
        "MINI_GITLAB_BOT_ISSUE_IID": str(ISSUE),
        "GITLAB_URL": stub.url,
        "GITLAB_READONLY_TOKEN": READONLY,
    }
    env.update(overrides)
    return env


@pytest.fixture
def mcp(stub: StubGitLab):
    client = McpProcess(make_env(stub))
    yield client
    client.close()


# --------------------------------------------------------------------------- #
# 握手 + 工具清单
# --------------------------------------------------------------------------- #


def test_handshake_over_real_stdio(mcp: McpProcess) -> None:
    response = mcp.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert response is not None
    assert response["result"]["protocolVersion"] == "2025-06-18"
    mcp.send("notifications/initialized")
    tools = mcp.send("tools/list")["result"]["tools"]  # type: ignore[index]
    assert len(tools) == 11


def test_no_write_requests_and_only_the_readonly_token(stub: StubGitLab, mcp: McpProcess) -> None:
    mcp.send("initialize", {"protocolVersion": "2025-06-18"})
    mcp.payload("get_project")
    mcp.payload("get_issue")

    assert stub.state.requests, "应当真的打了 GitLab"
    assert {item["method"] for item in stub.state.requests} == {"GET"}
    for item in stub.state.requests:
        assert item["headers"]["private-token"] == READONLY
        assert "glpat-readonly" not in item["path"]
        assert WRITE not in item["headers"].get("private-token", "")


# --------------------------------------------------------------------------- #
# 11 个工具逐个验证（真数据）
# --------------------------------------------------------------------------- #


def test_get_issue_returns_body_and_filters_system_notes(mcp: McpProcess) -> None:
    data = mcp.payload("get_issue")
    assert data["iid"] == ISSUE
    assert data["title"].startswith("parse_labels")
    assert data["labels"] == ["ai-fix", "bug"]
    assert [note["body"] for note in data["comments"]] == ["我也遇到了这个问题"]
    assert data["comments_page"] == 1


def test_search_issues_is_scoped_to_the_bound_project(stub: StubGitLab, mcp: McpProcess) -> None:
    items = mcp.payload("search_issues", {"query": "parse_labels"})
    assert len(items) == 1
    assert items[0]["iid"] == ISSUE
    request = [item for item in stub.state.requests if "issues" in item["path"]][-1]
    assert request["query"]["search"] == ["parse_labels"]
    assert f"/projects/{PROJECT_ID}/issues" in request["path"]  # 项目来自 env，不是参数


def test_list_issue_related_merge_requests(mcp: McpProcess) -> None:
    assert mcp.payload("list_issue_related_merge_requests") == []


def test_get_merge_request_with_notes(stub: StubGitLab, mcp: McpProcess) -> None:
    stub.state.merge_requests.append(
        {
            "id": 5001,
            "iid": 3,
            "project_id": PROJECT_ID,
            "title": "Draft: fix(issue-12)",
            "description": "Closes #12",
            "source_branch": "ai-fix/12-x",
            "target_branch": "main",
            "state": "opened",
            "web_url": "https://gitlab.example.com/group/project-a/-/merge_requests/3",
        }
    )
    data = mcp.payload("get_merge_request", {"iid": 3, "include_notes": True})
    assert data["title"].startswith("Draft:")
    assert data["source_branch"] == "ai-fix/12-x"
    assert data["notes"] == []


def test_list_pipelines_and_jobs(mcp: McpProcess) -> None:
    pipelines = mcp.payload("list_pipelines", {"ref": "main"})
    assert [item["id"] for item in pipelines] == [900]
    assert pipelines[0]["status"] == "failed"

    jobs = mcp.payload("list_pipeline_jobs", {"pipeline_id": 900, "scope": "failed"})
    assert [job["id"] for job in jobs] == [5001]
    assert jobs[0]["failure_reason"] == "script_failure"


def test_get_job_log_returns_the_tail(mcp: McpProcess) -> None:
    text = mcp.text("get_job_log", {"job_id": 5001, "tail_lines": 5})
    assert "AssertionError" in text
    assert "collecting test 0" not in text  # 只给尾部
    assert "共 302 行" in text


def test_get_job_log_tail_lines_is_respected(mcp: McpProcess) -> None:
    text = mcp.text("get_job_log", {"job_id": 5001, "tail_lines": 2})
    lines = text.splitlines()
    assert len(lines) == 3  # 标题行 + 2 行
    assert lines[-1] == "2 failed"


def test_get_pipeline_test_report_only_keeps_failures(mcp: McpProcess) -> None:
    report = mcp.payload("get_pipeline_test_report", {"pipeline_id": 900})
    assert report["failed_count"] == 1
    assert [case["name"] for case in report["failed_cases"]] == ["test_empty_labels"]
    assert report["failed_cases"][0]["message"].startswith("AssertionError")


def test_get_commit_diff_includes_patch(mcp: McpProcess) -> None:
    data = mcp.payload("get_commit_diff", {"sha": "a" * 40})
    assert data["files_changed"] == 1
    assert data["files"][0]["path"] == "app.py"
    assert "parse_labels" in data["files"][0]["patch"]


def test_get_recent_commits_with_path_filter(stub: StubGitLab, mcp: McpProcess) -> None:
    commits = mcp.payload("get_recent_commits", {"limit": 5, "path": "app.py"})
    assert len(commits) == 1
    assert commits[0]["sha"] == "a" * 40
    request = [item for item in stub.state.requests if "repository/commits" in item["path"]][-1]
    assert request["query"]["path"] == ["app.py"]
    assert request["query"]["ref_name"] == ["main"]  # 默认分支来自项目元信息


def test_get_project_metadata(mcp: McpProcess) -> None:
    data = mcp.payload("get_project")
    assert data["path_with_namespace"] == "group/project-a"
    assert data["default_branch"] == "main"
    assert data["languages"] == {"Python": 97.5, "Shell": 2.5}


# --------------------------------------------------------------------------- #
# 硬约束：跨项目不可能 / 只读 / 拒绝带写 token 启动
# --------------------------------------------------------------------------- #


def test_project_argument_is_rejected(stub: StubGitLab, mcp: McpProcess) -> None:
    """硬约束 1：工具 schema 没有 project，硬塞一个也必须报错（而不是被静默忽略）。"""
    before = len(stub.state.requests)
    result = mcp.call("get_issue", {"project": 43})
    assert result["isError"] is True
    assert "不支持的参数" in result["content"][0]["text"]
    assert len(stub.state.requests) == before  # 一个请求都没发出去


def test_project_argument_on_every_tool_is_rejected(mcp: McpProcess) -> None:
    cases = [
        ("get_issue", {"project_id": 43}),
        ("search_issues", {"query": "x", "project": "other/repo"}),
        ("list_pipelines", {"ref": "main", "project": 43}),
        ("get_project", {"project": 43}),
    ]
    for name, args in cases:
        result = mcp.call(name, args)
        assert result["isError"] is True, name
        assert "不支持的参数" in result["content"][0]["text"], name


def test_unknown_tool_over_stdio(mcp: McpProcess) -> None:
    result = mcp.call("delete_project", {"project": 43})
    assert result["isError"] is True
    assert "未知工具" in result["content"][0]["text"]


def test_server_refuses_to_start_with_the_write_token(stub: StubGitLab) -> None:
    """硬约束 3（纵深防御）：带着写 token 启动必须直接失败。"""
    env = make_env(stub, GITLAB_TOKEN=WRITE)
    proc = subprocess.run(
        [sys.executable, "-m", "gateway.mcp_readonly"],
        cwd=str(REPO_ROOT),
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 3
    assert "只读" in proc.stderr
    assert WRITE not in proc.stdout


def test_server_refuses_to_start_without_a_token(stub: StubGitLab) -> None:
    env = make_env(stub)
    env.pop("GITLAB_READONLY_TOKEN")
    proc = subprocess.run(
        [sys.executable, "-m", "gateway.mcp_readonly"],
        cwd=str(REPO_ROOT),
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "GITLAB_READONLY_TOKEN" in proc.stderr


def test_server_refuses_to_start_without_a_project(stub: StubGitLab) -> None:
    env = make_env(stub)
    env.pop("MINI_GITLAB_BOT_PROJECT_ID")
    proc = subprocess.run(
        [sys.executable, "-m", "gateway.mcp_readonly"],
        cwd=str(REPO_ROOT),
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "MINI_GITLAB_BOT_PROJECT_ID" in proc.stderr


# --------------------------------------------------------------------------- #
# 输出截断（硬约束 4）
# --------------------------------------------------------------------------- #


def test_output_is_truncated_over_stdio(stub: StubGitLab) -> None:
    client = McpProcess(make_env(stub, MCP_MAX_OUTPUT_CHARS="600"))
    try:
        client.send("initialize", {"protocolVersion": "2025-06-18"})
        text = client.text("get_job_log", {"job_id": 5001, "tail_lines": 300})
        assert len(text) < 900
        assert "截断" in text
        assert "AssertionError" in text  # 尾部信息优先保留
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 接线一致性：CodexCli 生成的 `-c` + 子进程环境能真的把 MCP 跑起来
# --------------------------------------------------------------------------- #


def _mcp_overrides(argv: list[str]) -> dict[str, Any]:
    """从 `-c mcp_servers.mini_gitlab.<key>=<toml>` 里取出键值（值是 JSON 兼容的 TOML）。"""
    values: dict[str, Any] = {}
    prefix = "mcp_servers.mini_gitlab."
    for index, item in enumerate(argv):
        if item != "-c" or index + 1 >= len(argv):
            continue
        assignment = argv[index + 1]
        if not assignment.startswith(prefix):
            continue
        key, _, raw = assignment[len(prefix) :].partition("=")
        values[key] = json.loads(raw)
    return values


async def test_codex_wiring_can_actually_start_the_mcp_server(tmp_path, stub: StubGitLab) -> None:
    """把 CodexCli 的产物直接喂给真进程：`env_vars` 里的名字必须与服务端读的一致。"""
    from adapters.codex_cli import CodexCli, CodexSettings
    from core.models import ProjectRef, Task, TriggerKind, Workdir
    from tests.helpers import START

    workdir = Workdir(path=str(tmp_path / "job"), branch="ai-fix/12-x", base_branch="main")
    Path(workdir.path).mkdir(parents=True)
    task = Task(
        id="task-mcp-wire",
        delivery_key="d",
        project=ProjectRef(project_id=PROJECT_ID, path="group/project-a"),
        issue_iid=ISSUE,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )
    cli = CodexCli(
        settings=CodexSettings(
            bin="codex",
            mcp_enabled=True,
            mcp_token=READONLY,
            mcp_gitlab_url=stub.url,
            mcp_pythonpath=str(REPO_ROOT),
        ),
        log_dir=tmp_path / "logs",
        environ={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    argv = await cli._build_argv(  # noqa: SLF001 直接验证内部产物
        task=task, workdir=workdir, context_name="context.md", last_message=str(tmp_path / "o.md")
    )
    overrides = _mcp_overrides(argv)
    child_env = cli._child_env(  # noqa: SLF001
        task=task, tmp_dir=str(tmp_path / "tmp"), workdir=workdir
    )

    # 1) env_vars 里列的每个名字，子进程环境里都必须有 —— 这就是"名字打错"的防线
    for name in overrides["env_vars"]:
        assert name in child_env, name

    # 2) 只把 codex 会透传的那些变量给 MCP（模拟 codex 的 env_vars 语义）
    passthrough = {name: child_env[name] for name in overrides["env_vars"]}
    server_env = {
        "PATH": child_env["PATH"],
        "HOME": child_env["HOME"],
        "PYTHONPATH": child_env["PYTHONPATH"],
        **passthrough,
    }
    process = subprocess.Popen(
        [overrides["command"], *overrides["args"]],
        cwd=str(workdir.path),
        env=server_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:

        def send(message: dict[str, Any]) -> dict[str, Any]:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            assert process.stdout is not None
            line = process.stdout.readline()
            assert line, "MCP 没有响应"
            return json.loads(line)

        init = send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        assert init["result"]["serverInfo"]["name"]
        project = send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_project", "arguments": {}},
            }
        )
        payload = json.loads(project["result"]["content"][0]["text"])
        assert payload["path_with_namespace"] == "group/project-a"
        assert "只读" in init["result"]["instructions"]
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=10)
