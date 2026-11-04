"""首次初始化 / 配置页的持久化与校验（只碰配置文件，不碰运行时）。

配置文件默认 `/data/config.json`，权限 0600；只在启动时由
`Settings` 叠加到环境变量之上。页面保存后必须重启进程才生效。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

#: 基础配置：第一次初始化必须看懂并填完这些
BASIC_KEYS: tuple[str, ...] = (
    "gitlab_url",
    "gitlab_token",
    "gitlab_readonly_token",
    "webhook_secret",
    "bot_username",
    "repo_whitelist",
    "model_base_url",
    "model_name",
    "llm_api_key",
    "dashboard_username",
    "dashboard_password",
    "agent_concurrency",
)

#: 高级配置：有合理默认值，放在折叠区
ADVANCED_KEYS: tuple[str, ...] = (
    "triage_enabled",
    "autofix_enabled",
    "sweep_enabled",
    "release_enabled",
    "mcp_enabled",
    "metrics_enabled",
    "min_access_level",
    "triage_quota_per_hour",
    "autofix_max_files",
    "autofix_max_lines",
    "diff_max_files",
    "diff_max_lines",
    "agent_timeout_sec",
    "gitlab_timeout_sec",
    "issue_comment_limit",
    "sweep_interval_sec",
    "sweep_commit_lookback_days",
    "release_lookback_days",
    "log_retention_days",
    "task_retention_days",
    "model_triage",
    "model_explain",
    "model_fix",
    "uv_default_index",
    "codex_expected_version",
    "forbidden_paths",
)

CONFIG_KEYS: tuple[str, ...] = BASIC_KEYS + ADVANCED_KEYS

SECRET_KEYS: frozenset[str] = frozenset(
    {"gitlab_token", "gitlab_readonly_token", "webhook_secret", "llm_api_key", "dashboard_password"}
)

BOOLEAN_KEYS: frozenset[str] = frozenset(
    {
        "triage_enabled",
        "autofix_enabled",
        "sweep_enabled",
        "release_enabled",
        "mcp_enabled",
        "metrics_enabled",
    }
)

LIST_KEYS: frozenset[str] = frozenset({"repo_whitelist", "forbidden_paths"})

#: 初始化判定：这 8 项为空就仍处于“首次部署”
REQUIRED_KEYS: tuple[str, ...] = (
    "gitlab_url",
    "gitlab_token",
    "webhook_secret",
    "bot_username",
    "repo_whitelist",
    "model_base_url",
    "model_name",
    "dashboard_password",
)


class ConfigError(ValueError):
    """配置校验失败；`errors` 是字段级错误，可直接渲染到表单旁。"""

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in errors.items()))
        self.errors = dict(errors)


def read_config(path: Path) -> dict[str, Any]:
    """读取配置；文件不存在或损坏时返回空 dict（不静默写坏文件）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in CONFIG_KEYS}


def is_initialized(path: Path) -> bool:
    """配置文件存在且必填项齐全，才算初始化完成。"""
    data = read_config(path)
    return bool(data) and not missing_keys(data)


def missing_keys(data: Mapping[str, Any]) -> list[str]:
    """返回仍为空的基础配置项。"""
    missing: list[str] = []
    for key in REQUIRED_KEYS:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing


def write_config(path: Path, data: Mapping[str, Any]) -> None:
    """原子写入 0600 配置文件，绝不把部分写入留给下一次启动。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: data[key] for key in CONFIG_KEYS if key in data}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        # 删除临时文件失败时不要覆盖真正的保存结果。
        with suppress(OSError):
            os.unlink(tmp_name)


def form_values(form: Mapping[str, str], current: Mapping[str, Any]) -> dict[str, Any]:
    """把表单合并到当前配置；secret 留空表示“保持不变”。"""
    merged: dict[str, Any] = {key: current.get(key) for key in CONFIG_KEYS}
    for key in CONFIG_KEYS:
        if key not in form:
            continue
        raw = form[key].strip()
        if key in SECRET_KEYS and not raw:
            continue
        if key in BOOLEAN_KEYS:
            merged[key] = raw.lower() in {"1", "true", "on", "yes"}
        elif key in LIST_KEYS:
            merged[key] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            merged[key] = raw
    return {key: value for key, value in merged.items() if value is not None}


def validate_config(data: Mapping[str, Any]) -> dict[str, str]:
    """字段级校验；返回空 dict 表示可保存。"""
    from gateway.config import Settings  # 延迟导入，避免 config <-> config_store 循环

    errors: dict[str, str] = {}
    missing = missing_keys(data)
    labels = {
        "gitlab_url": "GitLab 地址",
        "gitlab_token": "GitLab 写 token",
        "webhook_secret": "Webhook Secret",
        "bot_username": "机器人用户名",
        "repo_whitelist": "项目白名单",
        "model_base_url": "模型端点",
        "model_name": "模型名",
        "dashboard_password": "Dashboard 密码",
    }
    for key in missing:
        if key in labels:
            errors[key] = f"请填写{labels[key]}"
    if not data.get("llm_api_key") and not os.environ.get("LLM_API_KEY"):
        errors["llm_api_key"] = "请填写模型 API Key（或保留环境变量 LLM_API_KEY）"

    token = str(data.get("gitlab_token") or "")
    if token and (len(token) < 12 or any(char.isspace() for char in token)):
        errors["gitlab_token"] = "GitLab token 看起来不合法（至少 12 位且不能有空格）"
    secret = str(data.get("webhook_secret") or "")
    if secret and len(secret) < 16:
        errors["webhook_secret"] = "Webhook Secret 至少 16 位，建议 32 位以上"
    password = str(data.get("dashboard_password") or "")
    if password and len(password) < 8:
        errors["dashboard_password"] = "Dashboard 密码至少 8 位"
    url = str(data.get("gitlab_url") or "")
    parsed = urlparse(url)
    if url and parsed.scheme not in {"http", "https"}:
        errors["gitlab_url"] = "GitLab 地址必须以 http:// 或 https:// 开头"

    try:
        settings = Settings(**{key: data[key] for key in CONFIG_KEYS if key in data})
    except Exception as exc:  # noqa: BLE001 表单错误统一展示，不把堆栈带到页面
        errors.setdefault("_form", f"配置格式不合法：{exc}")
        return errors
    if settings.model_name and not data.get("model_base_url"):
        errors.setdefault("model_base_url", "填写模型名时必须同时填写模型端点")
    return errors


def public_values(data: Mapping[str, Any], fallback: Mapping[str, Any]) -> dict[str, Any]:
    """配置页展示值：secret 永远不回显，只显示“已配置”。"""
    values: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        value = data.get(key, fallback.get(key, ""))
        if key in SECRET_KEYS:
            values[key] = ""
        elif key in LIST_KEYS and isinstance(value, (list, tuple)):
            values[key] = ", ".join(str(item) for item in value)
        elif value is None:
            values[key] = ""
        else:
            values[key] = str(value)
    return values


def secret_state(data: Mapping[str, Any]) -> dict[str, bool]:
    """secret 是否已存在；用于输入框 placeholder 与“留空保持”提示。"""
    return {key: bool(str(data.get(key) or "").strip()) for key in SECRET_KEYS}
