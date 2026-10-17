"""`Settings` 的**真实环境变量**解析。

回归背景：pydantic-settings 对 `list[str]` 会先做 JSON 解码，`mode="before"` 的校验器
根本没机会跑，于是 `REPO_WHITELIST=group/a,group/b` 直接抛 JSONDecodeError。
用 init 参数构造 Settings 是测不出这个坑的 —— 所以这里一律走 monkeypatch.setenv。
"""

from __future__ import annotations

import pytest

from gateway.config import Settings

ENV_NAMES = (
    "WEBHOOK_SECRET",
    "REPO_WHITELIST",
    "ALLOWED_TRIGGER_USERS",
    "FORBIDDEN_PATHS",
    "MIN_ACCESS_LEVEL",
    "DB_PATH",
    "AI_FIX_LABEL",
    "BOT_USERNAME",
    "AGENT_CONCURRENCY",
    "AGENT_TIMEOUT_SEC",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_comma_separated_env_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPO_WHITELIST", "group/project-a,group/project-b")
    assert Settings().repo_whitelist == ["group/project-a", "group/project-b"]


def test_single_value_env_becomes_one_item_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPO_WHITELIST", "group/only-one")
    assert Settings().repo_whitelist == ["group/only-one"]


def test_whitespace_and_trailing_comma_are_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_TRIGGER_USERS", " alice , bob ,")
    assert Settings().allowed_trigger_users == ["alice", "bob"]


def test_empty_env_means_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPO_WHITELIST", "")
    assert Settings().repo_whitelist == []


def test_forbidden_paths_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORBIDDEN_PATHS", ".gitlab-ci.yml,*.pem")
    assert Settings().forbidden_paths == [".gitlab-ci.yml", "*.pem"]


def test_scalar_env_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_ACCESS_LEVEL", "40")
    monkeypatch.setenv("DB_PATH", "/tmp/tasks.db")
    monkeypatch.setenv("AGENT_TIMEOUT_SEC", "90")
    settings = Settings()
    assert settings.min_access_level == 40
    assert str(settings.db_path) == "/tmp/tasks.db"
    assert settings.agent_timeout_sec == 90.0


def test_defaults_are_the_decided_ones() -> None:
    settings = Settings()
    assert settings.ai_fix_label == "ai-fix"
    assert settings.min_access_level == 30  # Developer
    assert settings.member_cache_ttl_sec == 60
    assert settings.max_attempts == 3
    assert settings.lease_seconds == 60
    assert settings.agent_concurrency == 2
    assert ".gitlab-ci.yml" in settings.forbidden_paths
    assert not hasattr(settings, "target_branch")  # 已删除：改为按项目 default_branch
    assert not hasattr(settings, "renovate_token")  # Renovate 不进本项目


def test_startup_warnings_flag_missing_config() -> None:
    settings = Settings(webhook_secret="", repo_whitelist=[], bot_username="")
    warnings = settings.startup_warnings()
    assert any("WEBHOOK_SECRET" in item for item in warnings)
    assert any("REPO_WHITELIST" in item for item in warnings)
    assert any("BOT_USERNAME" in item for item in warnings)


def test_startup_warnings_flag_unpinned_kernel_and_missing_model() -> None:
    settings = Settings(
        webhook_secret="x",
        repo_whitelist=["a/b"],
        bot_username="bot",
        gitlab_token="",
        model_name="",
        codex_expected_version="",
    )
    warnings = settings.startup_warnings()
    assert any("GITLAB_TOKEN" in item for item in warnings)
    assert any("MODEL_NAME" in item for item in warnings)
    assert any("CODEX_EXPECTED_VERSION" in item for item in warnings)


def test_startup_warnings_flag_privilege_escalation_risk() -> None:
    settings = Settings(
        webhook_secret="x", repo_whitelist=["a/b"], bot_username="bot", min_access_level=20
    )
    assert any("MIN_ACCESS_LEVEL" in item for item in settings.startup_warnings())


def test_no_warnings_when_properly_configured() -> None:
    settings = Settings(
        webhook_secret="x",
        repo_whitelist=["a/b"],
        bot_username="bot",
        gitlab_token="glpat-x",
        model_name="glm-5",
        codex_expected_version="0.163.0",
    )
    assert settings.startup_warnings() == []
