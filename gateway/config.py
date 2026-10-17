"""环境变量配置（pydantic-settings）。

与 v0 的差异（PLAN §12）：
* **删除 `TARGET_BRANCH`** —— 目标分支改为按项目取 `default_branch`；
* **删除 `RENOVATE_TOKEN`** —— Renovate 不进本项目（F-1）；
* **删除 `MAX_ITERATIONS`** —— 重试由任务租约 + `max_attempts` 负责；
* 新增触发闸门（白名单 / 最小权限 / 机器人自身用户名 / 成员缓存 TTL）与 diff 策略。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from core.policy import DEFAULT_FORBIDDEN_PATHS

#: 逗号分隔的列表字段。必须加 `NoDecode`：否则 pydantic-settings 会先按 JSON 解析
#: 环境变量（`group/a,group/b` 直接报 JSONDecodeError），`mode="before"` 的校验器
#: 根本没机会执行。这个坑只有走真实环境变量才会暴露，单测直接用 init 参数是测不到的。
CsvList = Annotated[list[str], NoDecode]

#: 这些默认值等于"没配"，启动时会打警告（fail closed，不会静默放行）
UNSET_SECRET = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # ---------- 网关自身 ----------
    webhook_secret: str = UNSET_SECRET
    agent_concurrency: int = 2
    work_dir: Path = Path("/work")
    db_path: Path = Path("/data/tasks.db")
    worker_enabled: bool = True
    worker_owner: str = "worker-1"
    port: int = 8080

    # ---------- 触发闸门 ----------
    repo_whitelist: CsvList = Field(default_factory=list)
    ai_fix_label: str = "ai-fix"
    bot_username: str = ""
    allowed_trigger_users: CsvList = Field(default_factory=list)
    min_access_level: int = 30  # Developer（PLAN §7 加固基线 8）
    member_cache_ttl_sec: int = 60

    # ---------- GitLab（真实适配器 M3 落地） ----------
    gitlab_url: str = "https://gitlab.example.com"
    gitlab_token: str = ""
    gitlab_readonly_token: str = ""  # M4：只读迷你 MCP 专用
    gitlab_timeout_sec: float = 30.0
    #: 进 context.md 的 issue 评论条数（只取最新 N 条人类评论）
    issue_comment_limit: int = 20

    # ---------- 只读迷你 MCP（M4，PLAN §3.3） ----------
    #: 总开关；没有 GITLAB_READONLY_TOKEN 时自动失效（无法认证就没有意义）
    mcp_enabled: bool = True
    #: 启动 MCP server 的解释器（空 = 与网关同一个）
    mcp_python: str = ""
    #: 单次工具输出上限（字符）
    mcp_max_output_chars: int = 20000

    # ---------- 任务与租约 ----------
    agent_timeout_sec: float = 1800.0
    lease_seconds: int = 60
    poll_interval_sec: float = 1.0
    max_attempts: int = 3

    # ---------- 工作目录与日志（M2） ----------
    log_dir: Path = Path("/data/logs")  # codex / uv 日志（job 目录终态即清，日志留 14 天）
    git_bin: str = "git"
    git_author_name: str = "mini-gitlab-bot"
    git_author_email: str = "mini-gitlab-bot@localhost"
    git_clone_depth: int = 0  # 0 = 完整 clone（浅克隆省时间但依赖更多服务端能力）
    git_timeout_sec: float = 600.0
    uv_bin: str = "uv"
    uv_cache_dir: Path | None = None  # 空 = <WORK_DIR>/.uv-cache（必须在容器卷内）
    uv_default_index: str = ""  # 内网 kkRepo
    uv_sync_timeout_sec: float = 1800.0

    # ---------- Codex（M2） ----------
    codex_bin: str = "codex"
    model_provider: str = "internal"
    model_name: str = ""
    api_key_env: str = "LLM_API_KEY"  # 只传变量名给 codex（`env_key`），值只进子进程环境
    codex_expected_version: str = ""  # pin 版本自检（PLAN §7 基线 6）
    #: 追加的 `-c key=value`，例如 `model_providers.internal.base_url=https://llm.internal/v1`
    codex_config: CsvList = Field(default_factory=list)

    # ---------- diff 策略 ----------
    diff_max_files: int = 50
    diff_max_lines: int = 2000
    forbidden_paths: CsvList = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN_PATHS))

    @field_validator(
        "repo_whitelist",
        "allowed_trigger_users",
        "forbidden_paths",
        "codex_config",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """支持 `a,b,c` 形式的逗号分隔（比 JSON 数组更符合 .env 习惯）。"""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # ---------- 派生 ----------

    @property
    def effective_uv_cache_dir(self) -> Path:
        return self.uv_cache_dir or (self.work_dir / ".uv-cache")

    def startup_warnings(self) -> list[str]:
        """配置体检：宁可启动时吵，也不要静默拒绝一切或静默放行。"""
        warnings: list[str] = []
        if not self.webhook_secret:
            warnings.append("WEBHOOK_SECRET 未设置：所有 webhook 都会被拒绝（fail closed）")
        if not self.repo_whitelist:
            warnings.append("REPO_WHITELIST 为空：所有项目都会被拒绝（fail closed）")
        if not self.bot_username:
            warnings.append("BOT_USERNAME 未设置：无法忽略机器人自己产生的事件（有自触发风险）")
        if self.min_access_level < 30:
            warnings.append(
                f"MIN_ACCESS_LEVEL={self.min_access_level} 低于 Developer(30)："
                "只读成员可以让机器人往仓库推分支（权限提升）"
            )
        if not self.gitlab_token:
            warnings.append("GITLAB_TOKEN 未设置：SCM 退回占位实现，权限闸门会拒绝所有触发")
        if not self.model_name:
            warnings.append("MODEL_NAME 为空：codex 会用内网端点的默认模型（建议显式指定）")
        if self.mcp_enabled and not self.gitlab_readonly_token:
            warnings.append(
                "GITLAB_READONLY_TOKEN 未设置：只读迷你 MCP 将被关闭"
                "（agent 只能靠 clone 与 context.md）"
            )
        if not self.codex_expected_version:
            warnings.append(
                "CODEX_EXPECTED_VERSION 为空：内核版本可能漂移（PLAN §7 基线 6 要求 pin）"
            )
        return warnings
