"""只读迷你 MCP 的**契约**（工具清单与环境变量名）。

放在 `core/` 而不是 `gateway/`：`adapters/codex_cli.py` 要用工具名与变量名下发
`-c` 配置，但适配器层**不允许**反向依赖 `gateway/`（PLAN §2 分层）。所以把"有什么工具、
用什么变量名"这类纯数据放这里，`gateway/mcp_readonly.py` 只负责协议与实现。
"""

from __future__ import annotations

from typing import Any

SERVER_NAME = "mini-gitlab-bot-readonly"
SERVER_VERSION = "0.1.0"  # 与包版本保持一致
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: MCP 子进程用环境变量拿到"绑定的项目/issue + 只读 token"（值绝不进 argv）
ENV_PROJECT_ID = "MINI_GITLAB_BOT_PROJECT_ID"
ENV_ISSUE_IID = "MINI_GITLAB_BOT_ISSUE_IID"
ENV_GITLAB_URL = "GITLAB_URL"
ENV_READONLY_TOKEN = "GITLAB_READONLY_TOKEN"
ENV_MAX_OUTPUT_CHARS = "MCP_MAX_OUTPUT_CHARS"

DEFAULT_MAX_OUTPUT_CHARS = 20_000

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_issue",
        "description": (
            "取**当前** issue 的详情与评论（评论按时间正序，分页 20 条/页）。"
            "用于确认复现步骤、已有结论、以及别人是否已经在修。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_comments": {"type": "boolean", "default": True},
                "page": {"type": "integer", "minimum": 1, "default": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_issues",
        "description": (
            "在**当前项目内**按关键词搜索 issue（GitLab 的 search 是标题+描述的子串匹配）。"
            "用于找重复报告或历史讨论。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "state": {"type": "string", "enum": ["all", "opened", "closed"], "default": "all"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_issue_related_merge_requests",
        "description": (
            "当前 issue 关联的 MR（带 Closes #N 或手工关联），用于判断是否已有在途修复。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_merge_request",
        "description": "按 iid 取 MR 详情；可选带上讨论（人工 review 意见）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "iid": {"type": "integer", "minimum": 1},
                "include_notes": {"type": "boolean", "default": False},
            },
            "required": ["iid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_pipelines",
        "description": "列出 pipeline（可按分支 ref、状态过滤），用于判断主线当前是否绿。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
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
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_pipeline_jobs",
        "description": (
            "列出某 pipeline 的 job（拿失败 job 的 id）。**这是 `get_job_log` 的前置步骤**："
            "pipeline_id 在 CI 失败时通常已出现在上下文里，job_id 必须从这里取。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "integer", "minimum": 1},
                "scope": {
                    "type": "string",
                    "enum": ["created", "pending", "running", "failed", "success", "canceled"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "required": ["pipeline_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_job_log",
        "description": (
            "读当前项目某个 CI job 的日志尾部（默认最后 200 行）。"
            "用于看真实失败堆栈，而不是凭猜测改代码。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "minimum": 1},
                "tail_lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_pipeline_test_report",
        "description": (
            "取 pipeline 的结构化测试报告（只含失败/错误用例与汇总计数）。"
            "比读原始 trace 精准得多，上下文占用小。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pipeline_id": {"type": "integer", "minimum": 1}},
            "required": ["pipeline_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_commit_diff",
        "description": (
            "单个 commit 的完整 patch + message（按文件列表，单文件 diff 有长度上限）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"sha": {"type": "string", "minLength": 6, "maxLength": 40}},
            "required": ["sha"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_commits",
        "description": "当前项目默认分支上的最近提交，可按路径过滤（想知道某文件最近谁改过）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "path": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project",
        "description": "当前项目元信息：默认分支、描述、topics、语言构成。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(tool["name"] for tool in TOOLS)

#: 发给 agent 的提示：这些工具能干什么、不能干什么（写进 context.md）
TOOLS_HINT = """你有 {count} 个**只读**工具（不能改任何东西，也不能换项目）：
{tools}

推荐用法：
- CI 红了：`list_pipelines` → `list_pipeline_jobs`（拿失败 job 的 id）→ `get_job_log`
- 想看某次改动：`get_commit_diff` / `get_recent_commits`
- 判断是否已有人修：`list_issue_related_merge_requests`

拿不到的东西（比如整仓库源码）就在本地 clone 里 grep —— 你手里有完整仓库，比远程搜索准。"""


def truncate(text: str, max_chars: int, *, keep: str = "head") -> str:
    """截断输出，并明确告诉模型"被截断了"（否则它会以为自己看到的是全部）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    where = "末尾" if keep == "tail" else "开头"
    note = f"\n…（输出被截断：原始 {len(text)} 字符，只保留{where}部分）"
    budget = max(0, max_chars - len(note))
    body = text[-budget:] if keep == "tail" else text[:budget]
    return f"{body}{note}"
