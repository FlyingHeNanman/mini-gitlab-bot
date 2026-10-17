"""触发者权限校验（PLAN §7 加固基线 8）。

**为什么必须有下限**：Guest/Reporter 只能读 issue、不能推代码。若不设下限，
他们就能命令机器人用 Developer 凭据往仓库推分支 —— 这是**权限提升**
（privilege escalation），而且审计上署名还是机器人。

所以默认要求 `access_level >= 30`（Developer），与"能推代码"对齐；
`ALLOWED_TRIGGER_USERS` 是可选白名单（AND 关系），同时兼任实例管理员的逃生门 ——
管理员可能不在项目成员表里（`members/all` 也查不到，返回 404/None）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from core.models import PermissionDecision

logger = logging.getLogger(__name__)

ACCESS_LEVEL_DEVELOPER = 30


@dataclass(slots=True)
class _CacheEntry:
    level: int | None
    expires_at: float


class PermissionChecker:
    def __init__(
        self,
        scm,
        *,
        min_access_level: int = ACCESS_LEVEL_DEVELOPER,
        allowed_users: Iterable[str] = (),
        ttl_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scm = scm
        self._min_access_level = min_access_level
        self._allowed_users = frozenset(allowed_users)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[int, int], _CacheEntry] = {}

    async def check(
        self, *, project_id: int, user_id: int | None, username: str
    ) -> PermissionDecision:
        if user_id is None:
            return PermissionDecision(False, "missing_user_id")

        try:
            level = await self._access_level(project_id, user_id)
        except Exception as exc:  # noqa: BLE001 查不到就拒绝（fail closed）
            logger.warning("成员权限查询失败，按拒绝处理：%s", exc)
            self._cache.pop((project_id, user_id), None)
            return PermissionDecision(False, "permission_check_failed")

        allowlisted = username in self._allowed_users
        if level is None:
            if allowlisted:
                return PermissionDecision(True, "allowlisted_non_member")
            return PermissionDecision(False, "not_a_project_member")

        if level < self._min_access_level:
            return PermissionDecision(
                False, f"insufficient_access_level({level}<{self._min_access_level})", level
            )
        if self._allowed_users and not allowlisted:
            return PermissionDecision(False, "not_in_allowlist", level)
        return PermissionDecision(True, "ok", level)

    async def _access_level(self, project_id: int, user_id: int) -> int | None:
        key = (project_id, user_id)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.level

        level = await self._scm.get_member_access_level(project_id, user_id)
        self._cache[key] = _CacheEntry(level=level, expires_at=now + self._ttl_seconds)
        return level

    def invalidate(self) -> None:
        self._cache.clear()
