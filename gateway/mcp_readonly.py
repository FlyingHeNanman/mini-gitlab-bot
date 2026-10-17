"""只读迷你 MCP server（stdio / JSON-RPC 2.0）。

对应 PLAN §3.3（决策 A3）。**为什么自研**：`glab mcp serve` 已弃用、217 个工具、
~114 KB 固定上下文、无只读模式；而我们只需要 11 个只读工具（约 4–6 KB）。

四条硬约束（缺一不可）：

1. **工具 schema 里没有 `project` 参数** —— 项目在启动时由网关经环境变量注入
   （`MINI_GITLAB_BOT_PROJECT_ID`），所以 agent **物理上无法**跨项目读取；
   连"多传一个 project 参数想覆盖"都不行：未知参数一律报错。
2. **只读**：只有 GET，没有任何 create/update/delete 路径。
3. **只读 token**：只认 `GITLAB_READONLY_TOKEN`（`read_api`）。如果环境里出现写 token
   （`GITLAB_TOKEN`），**直接退出**而不是带着写权限跑起来。
4. **输出截断**（默认 20k 字符/次），避免把 agent 的上下文塞爆。

协议要点（MCP stdio transport）：

* 一行一条 JSON-RPC 消息，stdout 只能有 JSON（日志一律走 stderr）；
* `initialize` → `notifications/initialized` → `tools/list` → `tools/call`；
* 通知（没有 `id`）不回任何东西；
* 错误分两层：协议错误走 JSON-RPC `error`，工具失败走 `result.isError=true`
  （后者能让模型看到"为什么失败"并自己调整，而不是整个会话崩掉）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from typing import Any

import gitlab
from gitlab.exceptions import GitlabError, GitlabGetError

from core.mcp_spec import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_PROTOCOL_VERSION,
    ENV_GITLAB_URL,
    ENV_ISSUE_IID,
    ENV_MAX_OUTPUT_CHARS,
    ENV_PROJECT_ID,
    ENV_READONLY_TOKEN,
    SERVER_NAME,
    SERVER_VERSION,
    TOOL_NAMES,
    TOOLS,
    truncate,
)

logger = logging.getLogger("mini_mcp")

#: 单个 diff 文件最多保留多少字符（一个超大 diff 会把上下文吃光）
MAX_DIFF_CHARS_PER_FILE = 4_000
#: job 日志最多回读多少字节（从尾部滚动保留，避免把巨型日志读进内存）
MAX_LOG_BYTES = 512 * 1024


class ToolError(RuntimeError):
    """工具层面的失败（参数不对、对象不存在）。以 `isError` 结果返回给模型。"""


# --------------------------------------------------------------------------- #
# 工具定义（11 个，schema 里绝不出现 project）
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 参数校验（未知参数一律报错：这也堵死了"多传 project 想跨项目"的幻想）
# --------------------------------------------------------------------------- #


def _arg(
    args: dict[str, Any], key: str, kind: type, default: Any = None, **constraints: Any
) -> Any:
    if key not in args:
        return default
    value = args[key]
    if kind is bool:
        if not isinstance(value, bool):
            raise ToolError(f"参数 {key} 应该是布尔值")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"参数 {key} 应该是整数")
        if "minimum" in constraints and value < constraints["minimum"]:
            raise ToolError(f"参数 {key} 不能小于 {constraints['minimum']}")
        if "maximum" in constraints and value > constraints["maximum"]:
            raise ToolError(f"参数 {key} 不能大于 {constraints['maximum']}")
    elif kind is str:
        if not isinstance(value, str):
            raise ToolError(f"参数 {key} 应该是字符串")
        if "minLength" in constraints and len(value) < constraints["minLength"]:
            raise ToolError(f"参数 {key} 太短")
        if "maxLength" in constraints and len(value) > constraints["maxLength"]:
            raise ToolError(f"参数 {key} 太长")
        if "enum" in constraints and value not in constraints["enum"]:
            raise ToolError(f"参数 {key} 只能是 {constraints['enum']} 之一")
    return value


def _reject_unknown(args: dict[str, Any], allowed: Iterable[str]) -> None:
    unknown = set(args) - set(allowed)
    if unknown:
        # 注意：`project` 走到这里会明确报错，而不是被静默忽略
        raise ToolError(f"不支持的参数：{'、'.join(sorted(unknown))}")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 只读读取器（项目在构造时绑定，之后没有任何参数能改变它）
# --------------------------------------------------------------------------- #


class ReadOnlyGitLab:
    def __init__(
        self,
        *,
        project_id: int,
        issue_iid: int | None,
        url: str,
        token: str,
        timeout_sec: float = 30.0,
    ) -> None:
        self._project_id = project_id
        self._issue_iid = issue_iid
        self._client = gitlab.Gitlab(
            url, private_token=token, timeout=timeout_sec, retry_transient_errors=True
        )
        self._project_obj: Any | None = None

    # ---- 基础设施 ----

    def _project(self) -> Any:
        if self._project_obj is None:
            self._project_obj = self._client.projects.get(self._project_id)
        return self._project_obj

    def _require_issue(self) -> int:
        if self._issue_iid is None:
            raise ToolError("本次任务没有绑定 issue（不是由 issue 触发的）")
        return self._issue_iid

    def _default_branch(self) -> str:
        return str(getattr(self._project(), "default_branch", None) or "main")

    # ---- 11 个工具 ----

    def get_issue(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("include_comments", "page"))
        include_comments = _arg(args, "include_comments", bool, True)
        page = _arg(args, "page", int, 1, minimum=1)
        iid = self._require_issue()
        issue = self._project().issues.get(iid)
        data: dict[str, Any] = {
            "iid": issue.iid,
            "title": issue.title,
            "state": issue.state,
            "labels": list(issue.labels or ()),
            "author": (issue.author or {}).get("username"),
            "created_at": getattr(issue, "created_at", None),
            "updated_at": getattr(issue, "updated_at", None),
            "web_url": issue.web_url,
            "description": issue.description,
        }
        if include_comments:
            notes = issue.notes.list(page=page, per_page=20, sort="asc", order_by="created_at")
            data["comments"] = [
                {
                    "author": (note.author or {}).get("username"),
                    "created_at": note.created_at,
                    "body": note.body,
                }
                for note in notes
                if not getattr(note, "system", False)
            ]
            data["comments_page"] = page
            data["comments_per_page"] = 20
        return _json(data)

    def search_issues(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("query", "state", "limit"))
        query = _arg(args, "query", str, None, minLength=1)
        if query is None:
            raise ToolError("缺少参数 query")
        state = _arg(args, "state", str, "all", enum=["all", "opened", "closed"])
        limit = _arg(args, "limit", int, 10, minimum=1, maximum=50)
        kwargs: dict[str, Any] = {"search": query, "per_page": limit, "order_by": "updated_at"}
        if state != "all":
            kwargs["state"] = state
        issues = self._project().issues.list(**kwargs)
        return _json(
            [
                {
                    "iid": issue.iid,
                    "title": issue.title,
                    "state": issue.state,
                    "labels": list(issue.labels or ()),
                    "updated_at": getattr(issue, "updated_at", None),
                    "web_url": getattr(issue, "web_url", None),
                }
                for issue in issues
            ]
        )

    def list_issue_related_merge_requests(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ())
        iid = self._require_issue()
        issue = self._project().issues.get(iid)
        related = issue.related_merge_requests()
        return _json(
            [
                {
                    "iid": item.get("iid"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "source_branch": item.get("source_branch"),
                    "target_branch": item.get("target_branch"),
                    "draft": item.get("draft"),
                    "web_url": item.get("web_url"),
                }
                for item in related
            ]
        )

    def get_merge_request(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("iid", "include_notes"))
        iid = _arg(args, "iid", int, None, minimum=1)
        if iid is None:
            raise ToolError("缺少参数 iid")
        include_notes = _arg(args, "include_notes", bool, False)
        mr = self._project().mergerequests.get(iid)
        data: dict[str, Any] = {
            "iid": mr.iid,
            "title": mr.title,
            "state": mr.state,
            "draft": getattr(mr, "draft", None),
            "source_branch": getattr(mr, "source_branch", None),
            "target_branch": getattr(mr, "target_branch", None),
            "author": (getattr(mr, "author", None) or {}).get("username"),
            "web_url": getattr(mr, "web_url", None),
            "description": mr.description,
            "merge_status": getattr(mr, "merge_status", None),
        }
        if include_notes:
            notes = mr.notes.list(per_page=50, sort="asc", order_by="created_at")
            data["notes"] = [
                {
                    "author": (note.author or {}).get("username"),
                    "created_at": note.created_at,
                    "body": note.body,
                }
                for note in notes
                if not getattr(note, "system", False)
            ]
        return _json(data)

    def list_pipelines(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("ref", "status", "limit"))
        limit = _arg(args, "limit", int, 10, minimum=1, maximum=50)
        kwargs: dict[str, Any] = {"per_page": limit}
        ref = _arg(args, "ref", str, None)
        if ref:
            kwargs["ref"] = ref
        status = _arg(args, "status", str, None, enum=list(_PIPELINE_STATUSES))
        if status:
            kwargs["status"] = status
        pipelines = self._project().pipelines.list(**kwargs)
        return _json(
            [
                {
                    "id": pipeline.id,
                    "ref": pipeline.ref,
                    "status": pipeline.status,
                    "sha": pipeline.sha,
                    "source": getattr(pipeline, "source", None),
                    "duration": getattr(pipeline, "duration", None),
                    "created_at": getattr(pipeline, "created_at", None),
                    "web_url": getattr(pipeline, "web_url", None),
                }
                for pipeline in pipelines
            ]
        )

    def list_pipeline_jobs(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("pipeline_id", "scope", "limit"))
        pipeline_id = _arg(args, "pipeline_id", int, None, minimum=1)
        if pipeline_id is None:
            raise ToolError("缺少参数 pipeline_id")
        limit = _arg(args, "limit", int, 50, minimum=1, maximum=100)
        scope = _arg(
            args,
            "scope",
            str,
            None,
            enum=["created", "pending", "running", "failed", "success", "canceled"],
        )
        pipeline = self._project().pipelines.get(pipeline_id)
        kwargs: dict[str, Any] = {"per_page": limit}
        if scope:
            kwargs["scope"] = scope
        jobs = pipeline.jobs.list(**kwargs)
        return _json(
            [
                {
                    "id": job.id,
                    "name": job.name,
                    "stage": job.stage,
                    "status": job.status,
                    "failure_reason": getattr(job, "failure_reason", None),
                    "allow_failure": getattr(job, "allow_failure", None),
                    "web_url": getattr(job, "web_url", None),
                }
                for job in jobs
            ]
        )

    def get_job_log(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("job_id", "tail_lines"))
        job_id = _arg(args, "job_id", int, None, minimum=1)
        if job_id is None:
            raise ToolError("缺少参数 job_id")
        tail_lines = _arg(args, "tail_lines", int, 200, minimum=1, maximum=2000)
        job = self._project().jobs.get(job_id)
        raw = _read_trace_tail(job)
        lines = raw.splitlines()
        tail = lines[-tail_lines:]
        header = (
            f"# job {job_id}（{job.name}）日志尾部：共 {len(lines)} 行，显示最后 {len(tail)} 行"
        )
        return f"{header}\n" + "\n".join(tail)

    def get_pipeline_test_report(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("pipeline_id",))
        pipeline_id = _arg(args, "pipeline_id", int, None, minimum=1)
        if pipeline_id is None:
            raise ToolError("缺少参数 pipeline_id")
        pipeline = self._project().pipelines.get(pipeline_id)
        report = pipeline.test_report.get()
        raw = report.asdict() if hasattr(report, "asdict") else dict(report._attrs)
        # 只保留失败用例：成功的用例对"修 bug"没有信息量
        failed = [
            {
                "name": case.get("name"),
                "classname": case.get("classname"),
                "status": case.get("status"),
                "message": case.get("message"),
                "file": case.get("file"),
                "line": case.get("line"),
            }
            for case in raw.get("test_cases") or []
            if case.get("status") != "success"
        ]
        return _json(
            {
                "total_count": raw.get("total_count"),
                "success_count": raw.get("success_count"),
                "failed_count": raw.get("failed_count"),
                "error_count": raw.get("error_count"),
                "skipped_count": raw.get("skipped_count"),
                "failed_cases": failed,
            }
        )

    def get_commit_diff(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("sha",))
        sha = _arg(args, "sha", str, None, minLength=6, maxLength=40)
        if sha is None:
            raise ToolError("缺少参数 sha")
        commit = self._project().commits.get(sha)
        diffs = commit.diff()
        files = []
        for item in diffs[:20]:
            patch = str(item.get("diff") or "")
            if len(patch) > MAX_DIFF_CHARS_PER_FILE:
                patch = patch[:MAX_DIFF_CHARS_PER_FILE] + "\n…（本文件 diff 已截断）"
            files.append(
                {
                    "path": item.get("new_path") or item.get("old_path"),
                    "new_file": item.get("new_file"),
                    "deleted_file": item.get("deleted_file"),
                    "renamed_file": item.get("renamed_file"),
                    "patch": patch,
                }
            )
        return _json(
            {
                "sha": commit.id,
                "short_id": getattr(commit, "short_id", None),
                "title": commit.title,
                "message": commit.message,
                "author": getattr(commit, "author_name", None),
                "created_at": getattr(commit, "created_at", None),
                "files_changed": len(diffs),
                "files": files,
            }
        )

    def get_recent_commits(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ("limit", "path"))
        limit = _arg(args, "limit", int, 20, minimum=1, maximum=50)
        path = _arg(args, "path", str, None)
        params: dict[str, Any] = {"ref_name": self._default_branch(), "per_page": limit}
        if path:
            params["path"] = path
        # 注意：不能写 `project.commits.list(path=...)` —— python-gitlab 的 list() 把
        # `path` 当成"自定义列表端点"，会去请求字面量 "app.py"（实测 404）。
        # 这里用 http_list 显式传查询参数，语义才等于 GitLab 的 path 过滤。
        commits = self._project().manager.gitlab.http_list(
            f"/projects/{self._project_id}/repository/commits", params
        )
        return _json(
            [
                {
                    "sha": commit.get("id"),
                    "short_id": commit.get("short_id"),
                    "title": commit.get("title"),
                    "author": commit.get("author_name"),
                    "created_at": commit.get("created_at"),
                }
                for commit in commits
            ]
        )

    def get_project(self, args: dict[str, Any]) -> str:
        _reject_unknown(args, ())
        project = self._project()
        data: dict[str, Any] = {
            "id": project.id,
            "path_with_namespace": project.path_with_namespace,
            "default_branch": project.default_branch,
            "description": getattr(project, "description", None),
            "topics": list(getattr(project, "topics", None) or ()),
            "web_url": getattr(project, "web_url", None),
            "visibility": getattr(project, "visibility", None),
        }
        try:
            data["languages"] = project.languages()
        except GitlabError:  # 语言统计可能被实例关闭，不是致命错误
            data["languages"] = None
        return _json(data)


_PIPELINE_STATUSES = (
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
    "success",
    "failed",
    "canceled",
    "skipped",
    "manual",
    "scheduled",
)


def _read_trace_tail(job: Any) -> str:
    """只保留日志尾部的字节（滚动 buffer），避免把巨型 trace 全读进内存。"""
    try:
        chunks = job.trace(streamed=True, iterator=True, chunk_size=8192)
        buffer = bytearray()
        for chunk in chunks:  # type: ignore[union-attr]
            buffer.extend(chunk)
            if len(buffer) > MAX_LOG_BYTES:
                del buffer[: len(buffer) - MAX_LOG_BYTES]
        return buffer.decode("utf-8", "replace")
    except TypeError:  # pragma: no cover - 兼容不同 python-gitlab 版本
        raw = job.trace()
        data = raw if isinstance(raw, bytes) else str(raw).encode()
        return data[-MAX_LOG_BYTES:].decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP 协议层
# --------------------------------------------------------------------------- #

_ERROR_PARSE = -32700
_ERROR_INVALID_REQUEST = -32600
_ERROR_METHOD_NOT_FOUND = -32601
_ERROR_INTERNAL = -32603


class McpServer:
    def __init__(
        self,
        reader: ReadOnlyGitLab,
        *,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        stdout: Any = None,
    ) -> None:
        self._reader = reader
        self._max_output_chars = max_output_chars
        self._stdout = stdout or sys.stdout
        self._handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "get_issue": reader.get_issue,
            "search_issues": reader.search_issues,
            "list_issue_related_merge_requests": reader.list_issue_related_merge_requests,
            "get_merge_request": reader.get_merge_request,
            "list_pipelines": reader.list_pipelines,
            "list_pipeline_jobs": reader.list_pipeline_jobs,
            "get_job_log": reader.get_job_log,
            "get_pipeline_test_report": reader.get_pipeline_test_report,
            "get_commit_diff": reader.get_commit_diff,
            "get_recent_commits": reader.get_recent_commits,
            "get_project": reader.get_project,
        }

    # ---- 传输循环 ----

    def serve(self, lines: Iterable[str]) -> None:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            response = self.handle_line(line)
            if response is not None:
                self._write(response)

    def handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return _error(None, _ERROR_PARSE, "无法解析的 JSON")
        if isinstance(message, list):
            return _error(None, _ERROR_INVALID_REQUEST, "不支持批量请求")
        if not isinstance(message, dict):
            return _error(None, _ERROR_INVALID_REQUEST, "消息必须是 JSON 对象")
        return self.handle(message)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        is_request = "id" in message
        msg_id = message.get("id")
        if not isinstance(method, str):
            return _error(msg_id, _ERROR_INVALID_REQUEST, "缺少 method") if is_request else None

        if not is_request:
            # 通知：`notifications/initialized` 等，一律不回
            return None

        if method == "initialize":
            return _ok(msg_id, self._initialize(message.get("params") or {}))
        if method == "tools/list":
            return _ok(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            return _ok(msg_id, self._call_tool(message.get("params") or {}))
        if method == "ping":
            return _ok(msg_id, {})
        return _error(msg_id, _ERROR_METHOD_NOT_FOUND, f"未知方法：{method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested
            if isinstance(requested, str)
            else DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "只读 GitLab 工具。你无法切换项目（工具 schema 里没有 project 参数），"
                "也无法做任何写操作。输出超过 "
                f"{self._max_output_chars} 字符会被截断。"
            ),
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            return _tool_error("缺少工具名")
        if not isinstance(args, dict):
            return _tool_error("arguments 必须是对象")
        handler = self._handlers.get(name)
        if handler is None:
            return _tool_error(f"未知工具：{name}（可用工具：{'、'.join(TOOL_NAMES)}）")
        try:
            text = handler(args)
        except ToolError as exc:
            return _tool_error(str(exc))
        except GitlabGetError as exc:
            status = getattr(exc, "response_code", None)
            return _tool_error(f"GitLab 返回 {status}：对象不存在或无权访问")
        except GitlabError as exc:
            return _tool_error(f"GitLab 调用失败：{_short(str(exc))}")
        except OSError as exc:
            return _tool_error(f"无法连接 GitLab：{type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 单个工具出错不能炸掉整个 MCP 会话
            logger.exception("工具 %s 执行失败", name)
            return _tool_error(
                f"工具 {name} 执行失败：{type(exc).__name__}: {_short(str(exc), 200)}"
            )
        keep = "tail" if name == "get_job_log" else "head"
        return {
            "content": [
                {"type": "text", "text": truncate(text, self._max_output_chars, keep=keep)}
            ],
            "isError": False,
        }

    def _write(self, payload: dict[str, Any]) -> None:
        self._stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._stdout.flush()


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


EXIT_BAD_CONFIG = 2
EXIT_WRITE_TOKEN_PRESENT = 3


def _refuse(message: str, code: int) -> None:
    """拒绝启动：消息进 stderr（stdout 只留给 JSON-RPC），并用明确的退出码。"""
    print(f"mini-gitlab-bot MCP: {message}", file=sys.stderr)
    raise SystemExit(code)


def build_reader_from_env() -> ReadOnlyGitLab:
    project_id = _env_int(ENV_PROJECT_ID)
    if project_id is None:
        _refuse(
            f"{ENV_PROJECT_ID} 未设置：拒绝启动（项目绑定是硬约束，不能由 agent 指定）",
            EXIT_BAD_CONFIG,
        )
    token = os.environ.get(ENV_READONLY_TOKEN) or ""
    if not token:
        _refuse(
            f"{ENV_READONLY_TOKEN} 未设置：拒绝启动（必须使用 read_api 只读 token）",
            EXIT_BAD_CONFIG,
        )
    if os.environ.get("GITLAB_TOKEN"):
        # 纵深防御：写 token 绝不该出现在 MCP 进程环境里
        _refuse(
            "检测到 GITLAB_TOKEN 出现在 MCP 进程环境中：拒绝启动"
            "（这里只允许只读 token，见 PLAN §7 红线 3）",
            EXIT_WRITE_TOKEN_PRESENT,
        )
    url = os.environ.get(ENV_GITLAB_URL) or ""
    if not url:
        _refuse(f"{ENV_GITLAB_URL} 未设置：拒绝启动", EXIT_BAD_CONFIG)
    return ReadOnlyGitLab(
        project_id=project_id,
        issue_iid=_env_int(ENV_ISSUE_IID),
        url=url,
        token=token,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # stdout 只留给 JSON-RPC：把 print 重定向到 stderr，避免任何库的意外输出污染协议
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        reader = build_reader_from_env()
        max_chars = _env_int(ENV_MAX_OUTPUT_CHARS, DEFAULT_MAX_OUTPUT_CHARS) or (
            DEFAULT_MAX_OUTPUT_CHARS
        )
        server = McpServer(reader, max_output_chars=max_chars, stdout=real_stdout)
        logger.info(
            "只读迷你 MCP 就绪：project=%s issue=%s 工具数=%d",
            os.environ.get(ENV_PROJECT_ID),
            os.environ.get(ENV_ISSUE_IID) or "-",
            len(TOOLS),
        )
        server.serve(sys.stdin)
        return 0
    finally:
        sys.stdout = real_stdout


if __name__ == "__main__":
    raise SystemExit(main())
