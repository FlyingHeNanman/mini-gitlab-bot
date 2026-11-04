"""配置文件读写、表单合并与首次初始化判定。"""

from __future__ import annotations

import json
import os

from gateway.config import load_settings
from gateway.config_store import (
    form_values,
    is_initialized,
    public_values,
    read_config,
    secret_state,
    validate_config,
    write_config,
)


def complete() -> dict[str, object]:
    return {
        "gitlab_url": "https://gitlab.example.com",
        "gitlab_token": "glpat-0123456789",
        "webhook_secret": "w" * 32,
        "bot_username": "bot",
        "repo_whitelist": ["group/a"],
        "model_base_url": "http://llm.internal/v1",
        "model_name": "glm-5",
        "llm_api_key": "sk-test",
        "dashboard_username": "admin",
        "dashboard_password": "s3cr3t-pass",
    }


def test_write_read_and_permissions(tmp_path) -> None:
    path = tmp_path / "config.json"
    write_config(path, complete())
    assert path.read_text(encoding="utf-8")
    assert (path.stat().st_mode & 0o777) == 0o600
    data = read_config(path)
    assert data["gitlab_url"] == "https://gitlab.example.com"
    assert data["repo_whitelist"] == ["group/a"]
    assert is_initialized(path)


def test_corrupt_or_partial_config_is_not_initialized(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_config(path) == {}
    assert not is_initialized(path)
    write_config(path, {"gitlab_url": "https://gitlab.example.com"})
    assert not is_initialized(path)


def test_form_values_keeps_blank_secrets_and_unchecked_booleans() -> None:
    current = {**complete(), "gitlab_token": "old-token", "sweep_enabled": True}
    form = {
        "gitlab_url": "https://gitlab.example.com",
        "gitlab_token": "",
        "sweep_enabled": "0",
        "repo_whitelist": "group/a, group/b",
    }
    merged = form_values(form, current)
    assert merged["gitlab_token"] == "old-token"
    assert merged["sweep_enabled"] is False
    assert merged["repo_whitelist"] == ["group/a", "group/b"]


def test_public_values_never_reveals_secrets() -> None:
    data = complete()
    values = public_values(data, {})
    for key in ("gitlab_token", "webhook_secret", "llm_api_key", "dashboard_password"):
        assert values[key] == ""
    assert secret_state(data)["dashboard_password"] is True


def test_validation_reports_required_and_secret_problems() -> None:
    errors = validate_config({})
    assert errors["gitlab_url"]
    assert errors["dashboard_password"]
    assert "llm_api_key" in errors
    bad = {**complete(), "webhook_secret": "short", "dashboard_password": "short"}
    errors = validate_config(bad)
    assert "webhook_secret" in errors
    assert "dashboard_password" in errors


def test_load_settings_overlays_config_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    data = complete()
    data["model_name"] = "from-config"
    write_config(path, data)
    monkeypatch.setenv("CONFIG_FILE", str(path))
    settings = load_settings()
    assert settings.model_name == "from-config"
    assert settings.gitlab_token == data["gitlab_token"]
    assert settings.config_file == path


def test_load_settings_falls_back_when_config_is_corrupt(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(path))
    settings = load_settings()
    assert settings.config_file == path
    assert settings.gitlab_token == ""


def test_write_config_is_atomic_and_ignores_unknown_keys(tmp_path) -> None:
    path = tmp_path / "nested" / "config.json"
    write_config(path, {**complete(), "evil": "x"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "evil" not in payload
    assert os.path.exists(path)


def test_load_settings_ignores_semantically_invalid_config(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    write_config(path, {**complete(), "agent_concurrency": "many"})
    monkeypatch.setenv("CONFIG_FILE", str(path))
    settings = load_settings()
    assert settings.agent_concurrency == 2
