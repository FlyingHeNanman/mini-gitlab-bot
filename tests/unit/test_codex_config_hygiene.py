"""仓库自带的 codex 用户级配置不能自相矛盾地违反我们自己的安全基线。

`codex/config.toml` 会被挂进容器当 `~/.codex/config.toml`（优先级 20，是**我们自己的**
输入，所以不能用"项目配置不可信"那套来推脱）。v0 版本在这里挂 `glab mcp serve` 并把
机器人 `api` token 写进 `env`，等于把 GitLab 写权限交给 agent —— 这个测试就是防止它复活。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "codex" / "config.toml"


def load() -> dict:
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_config_is_valid_toml() -> None:
    assert load()["model_provider"]


def test_no_mcp_servers_at_all() -> None:
    """agent 的额外上下文只能来自只读迷你 MCP（M4，由网关安全装配）。"""
    assert "mcp_servers" not in load()


def test_never_marks_the_workspace_trusted() -> None:
    assert "trust_level" not in load()
    assert "trusted" not in CONFIG_PATH.read_text(encoding="utf-8")


def test_no_credentials_anywhere_in_the_file() -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for forbidden in ("glpat-", "GITLAB_TOKEN", "WEBHOOK_SECRET", "password", "api_key ="):
        assert forbidden not in text, f"{forbidden} 不该出现在用户级 codex 配置里"


def test_wire_api_is_responses() -> None:
    """PLAN §6 实测：`wire_api = "chat"` 在新版 codex 里是硬错误。"""
    provider = load()["model_providers"]["internal"]
    assert provider["wire_api"] == "responses"


def test_safety_critical_keys_are_not_delegated_to_this_file() -> None:
    """安全关键项必须由 `-c` 下发（优先级 30），不能只写在这里（20）。"""
    data = load()
    assert "sandbox_mode" not in data
    assert "approval_policy" not in data
    assert "danger-full-access" not in CONFIG_PATH.read_text(encoding="utf-8")
