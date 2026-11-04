"""登录 session 的签名、过期与开放重定向防护。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gateway.auth import create_session, safe_next, verify_session
from gateway.config import Settings


def settings(password: str = "s3cr3t-pass") -> Settings:
    return Settings(dashboard_username="admin", dashboard_password=password)


def test_session_round_trip() -> None:
    current = Settings(dashboard_username="admin", dashboard_password="s3cr3t-pass")
    now = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
    value = create_session(current, "admin", now=now)
    assert verify_session(current, value, now=now + timedelta(minutes=5)) == "admin"
    assert verify_session(current, value, now=now + timedelta(hours=13)) is None


def test_session_rejects_tampering_and_password_change() -> None:
    current = Settings(dashboard_username="admin", dashboard_password="s3cr3t-pass")
    now = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
    value = create_session(current, "admin", now=now)
    assert value
    assert verify_session(current, value[:-1] + "x", now=now) is None
    changed = Settings(dashboard_username="admin", dashboard_password="another-pass")
    assert verify_session(changed, value, now=now) is None
    assert verify_session(current, None, now=now) is None


def test_empty_password_never_creates_session() -> None:
    current = Settings(dashboard_username="admin", dashboard_password="")
    assert create_session(current, "admin") == ""
    assert verify_session(current, "anything") is None


def test_safe_next_only_allows_internal_paths() -> None:
    assert safe_next("/dashboard") == "/dashboard"
    assert safe_next("/settings?saved=1") == "/settings?saved=1"
    assert safe_next("//evil.example") == "/dashboard"
    assert safe_next("https://evil.example") == "/dashboard"
    assert safe_next(None) == "/dashboard"
