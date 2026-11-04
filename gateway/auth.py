"""Dashboard 登录：HMAC 签名 HttpOnly session，不再用浏览器 Basic 弹框。

session 里只放用户名与过期时间，不含密码、token 或任何 GitLab 数据。
签名 key 由 Dashboard 密码派生：改密码即让所有旧 session 失效。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from gateway.config import Settings

SESSION_COOKIE = "mgb_session"
SESSION_TTL = timedelta(hours=12)


def _key(settings: Settings) -> bytes:
    return hashlib.sha256(settings.dashboard_password.encode("utf-8")).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def create_session(settings: Settings, username: str, *, now: datetime | None = None) -> str:
    """生成签名 cookie 值；Dashboard 未配置密码时返回空串（fail closed）。"""
    if not settings.dashboard_password:
        return ""
    stamp = (now or datetime.now(UTC)) + SESSION_TTL
    payload = json.dumps(
        {"u": username, "e": int(stamp.timestamp())}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    encoded = _b64encode(payload)
    signature = _b64encode(
        hmac.new(_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def verify_session(
    settings: Settings, value: str | None, *, now: datetime | None = None
) -> str | None:
    """校验签名与过期时间；成功返回用户名，否则 None。"""
    if not value or not settings.dashboard_password:
        return None
    encoded, separator, signature = value.partition(".")
    if not separator:
        return None
    expected = _b64encode(
        hmac.new(_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("u")
    expires = payload.get("e")
    if not isinstance(username, str) or not isinstance(expires, int):
        return None
    if expires <= int((now or datetime.now(UTC)).timestamp()):
        return None
    return username


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    """常数时间比较用户名与密码；用户名也做比较避免提前返回泄露。"""
    user_ok = hmac.compare_digest(username.encode("utf-8"), settings.dashboard_username.encode())
    pass_ok = hmac.compare_digest(password.encode("utf-8"), settings.dashboard_password.encode())
    return user_ok and pass_ok


def safe_next(candidate: str | None) -> str:
    """只允许站内绝对路径，挡住 `//evil.com` 与完整 URL 的开放重定向。"""
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/dashboard"
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return "/dashboard"
    return candidate
