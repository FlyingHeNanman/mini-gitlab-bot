"""MCP 协议层：握手、工具清单、错误分层、截断。

这里用假 reader，只验证**协议与约束**；真数据与真 HTTP 在
`tests/integration/test_mcp_tools.py` 里用真子进程打桩 GitLab 验证。
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from core.mcp_spec import DEFAULT_MAX_OUTPUT_CHARS, TOOLS, truncate
from gateway.mcp_readonly import McpServer, ToolError

MARKER = "<!-- marker -->"


class FakeReader:
    """记录调用参数的假读取器。"""

    def __init__(self, *, fail: Exception | None = None, payload: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail
        self._payload = payload

    def __getattr__(self, name: str):
        def handler(args: dict[str, Any]) -> str:
            self.calls.append((name, args))
            if self._fail is not None:
                raise self._fail
            return json.dumps(self._payload if self._payload is not None else {"tool": name})

        return handler


def build(reader: Any, *, max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS):
    out = io.StringIO()
    return McpServer(reader, max_output_chars=max_output_chars, stdout=out), out


def rpc(server: McpServer, **message: Any) -> dict[str, Any] | None:
    return server.handle_line(json.dumps(message))


def request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(server: McpServer, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    response = rpc(
        server,
        **request(1, "tools/call", {"name": name, "arguments": arguments or {}}),
    )
    assert response is not None
    return response["result"]


# --------------------------------------------------------------------------- #
# 握手与清单
# --------------------------------------------------------------------------- #


def test_initialize_echoes_protocol_version() -> None:
    server, _ = build(FakeReader())
    response = rpc(
        server,
        **request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}),
    )
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"]
    assert "只读" in result["instructions"]


def test_notifications_get_no_response() -> None:
    server, out = build(FakeReader())
    assert rpc(server, jsonrpc="2.0", method="notifications/initialized") is None
    assert out.getvalue() == ""


def test_tools_list_has_exactly_eleven_read_only_tools() -> None:
    server, _ = build(FakeReader())
    response = rpc(server, **request(1, "tools/list"))
    assert response is not None
    tools = response["result"]["tools"]
    assert len(tools) == 11
    assert {tool["name"] for tool in tools} == {
        "get_issue",
        "search_issues",
        "list_issue_related_merge_requests",
        "get_merge_request",
        "list_pipelines",
        "list_pipeline_jobs",
        "get_job_log",
        "get_pipeline_test_report",
        "get_commit_diff",
        "get_recent_commits",
        "get_project",
    }
    # 工具面要小：定义体积远小于 glab 的 ~114 KB（PLAN §3.3）
    assert len(json.dumps(tools, ensure_ascii=False)) < 12_000


def test_no_tool_schema_mentions_project() -> None:
    """硬约束 1：schema 里没有 project 参数，agent 物理上换不了项目。"""
    for tool in TOOLS:
        props = tool["inputSchema"].get("properties", {})
        assert "project" not in props, tool["name"]
        assert "project_id" not in props, tool["name"]
        assert "project_path" not in props, tool["name"]


def test_no_tool_schema_is_a_write_operation() -> None:
    """硬约束 2：不含任何写操作。"""
    for tool in TOOLS:
        name = tool["name"]
        assert not name.startswith(("create_", "update_", "delete_", "set_", "add_")), name


def test_every_tool_has_a_description_and_object_schema() -> None:
    for tool in TOOLS:
        assert tool["description"].strip()
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


# --------------------------------------------------------------------------- #
# 调用与错误分层
# --------------------------------------------------------------------------- #


def test_tool_call_returns_text_content() -> None:
    reader = FakeReader(payload={"ok": True})
    server, _ = build(reader)
    result = call(server, "get_project")
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"ok": True}
    assert reader.calls == [("get_project", {})]


def test_unknown_tool_is_a_tool_error_not_a_protocol_error() -> None:
    """工具级失败用 isError，让模型看到原因并调整；不要炸掉整个会话。"""
    server, _ = build(FakeReader())
    result = call(server, "rm_rf")
    assert result["isError"] is True
    assert "未知工具" in result["content"][0]["text"]


def test_tool_exception_becomes_tool_error() -> None:
    server, _ = build(FakeReader(fail=ToolError("参数不合法")))
    result = call(server, "get_project")
    assert result["isError"] is True
    assert "参数不合法" in result["content"][0]["text"]


def test_unexpected_gitlab_error_is_reported_without_crashing() -> None:
    from gitlab.exceptions import GitlabGetError

    server, _ = build(FakeReader(fail=GitlabGetError("boom", 404)))
    result = call(server, "get_job_log", {"job_id": 1})
    assert result["isError"] is True
    assert "404" in result["content"][0]["text"]


def test_unknown_method_is_a_jsonrpc_error() -> None:
    server, _ = build(FakeReader())
    response = rpc(server, **request(9, "resources/list"))
    assert response is not None
    assert response["error"]["code"] == -32601
    assert response["id"] == 9


def test_malformed_json_is_a_parse_error() -> None:
    server, _ = build(FakeReader())
    response = server.handle_line("{ not json")
    assert response is not None
    assert response["error"]["code"] == -32700


def test_batch_requests_are_rejected_explicitly() -> None:
    server, _ = build(FakeReader())
    response = server.handle_line(json.dumps([request(1, "tools/list")]))
    assert response is not None
    assert response["error"]["code"] == -32600


def test_ping_answers() -> None:
    server, _ = build(FakeReader())
    response = rpc(server, **request(1, "ping"))
    assert response is not None
    assert response["result"] == {}


# --------------------------------------------------------------------------- #
# 截断（硬约束 4）
# --------------------------------------------------------------------------- #


def test_truncate_keeps_head_by_default() -> None:
    text = "A" * 100 + "B" * 100
    out = truncate(text, 120)
    assert out.startswith("A")
    assert "截断" in out
    assert len(out) <= 200


def test_truncate_tail_is_used_for_logs() -> None:
    text = "old\n" * 50 + "THE FAILURE IS HERE\n"
    out = truncate(text, 60, keep="tail")
    assert "THE FAILURE IS HERE" in out
    assert "截断" in out


def test_truncate_leaves_short_text_untouched() -> None:
    assert truncate("short", 100) == "short"


def test_server_truncates_tool_output() -> None:
    reader = FakeReader(payload={"blob": "x" * 5000})
    server, _ = build(reader, max_output_chars=200)
    result = call(server, "get_project")
    text = result["content"][0]["text"]
    assert len(text) < 400
    assert "截断" in text


def test_job_log_keeps_the_tail_when_truncated() -> None:
    class LogReader(FakeReader):
        def get_job_log(self, args: dict[str, Any]) -> str:
            return "\n".join(f"line-{index}" for index in range(500)) + "\nFAILED HERE\n"

    server, _ = build(LogReader(), max_output_chars=120)
    result = call(server, "get_job_log", {"job_id": 1})
    text = result["content"][0]["text"]
    assert "FAILED HERE" in text  # 失败信息在末尾，必须保留
    assert "截断" in text


@pytest.mark.parametrize("keep", ["head", "tail"])
def test_truncate_never_exceeds_max_by_much(keep: str) -> None:
    out = truncate("y" * 10_000, 500, keep=keep)
    assert len(out) <= 500
