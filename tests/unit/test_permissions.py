"""触发者权限闸门：Developer 下限、白名单、缓存、fail closed。"""

from __future__ import annotations

import pytest

from adapters.fakes import FakeSCM
from gateway.permissions import PermissionChecker

PROJECT_ID = 42
DEVELOPER = 11
REPORTER = 12
STRANGER = 13


def make_checker(scm: FakeSCM, **kwargs) -> PermissionChecker:
    return PermissionChecker(scm, **kwargs)


@pytest.mark.asyncio
async def test_developer_is_allowed() -> None:
    scm = FakeSCM(access_levels={DEVELOPER: 30})
    decision = await make_checker(scm).check(
        project_id=PROJECT_ID, user_id=DEVELOPER, username="dev"
    )
    assert decision.allowed
    assert decision.reason == "ok"
    assert decision.access_level == 30


@pytest.mark.asyncio
async def test_maintainer_is_allowed() -> None:
    scm = FakeSCM(access_levels={DEVELOPER: 40})
    assert (
        await make_checker(scm).check(project_id=PROJECT_ID, user_id=DEVELOPER, username="m")
    ).allowed


@pytest.mark.asyncio
async def test_reporter_is_denied_to_prevent_privilege_escalation() -> None:
    scm = FakeSCM(access_levels={REPORTER: 20})
    decision = await make_checker(scm).check(project_id=PROJECT_ID, user_id=REPORTER, username="pm")
    assert not decision.allowed
    assert decision.reason == "insufficient_access_level(20<30)"


@pytest.mark.asyncio
async def test_non_member_is_denied() -> None:
    scm = FakeSCM(access_levels={})
    decision = await make_checker(scm).check(
        project_id=PROJECT_ID, user_id=STRANGER, username="stranger"
    )
    assert not decision.allowed
    assert decision.reason == "not_a_project_member"


@pytest.mark.asyncio
async def test_missing_user_id_is_denied() -> None:
    decision = await make_checker(FakeSCM()).check(
        project_id=PROJECT_ID, user_id=None, username="ghost"
    )
    assert not decision.allowed
    assert decision.reason == "missing_user_id"


@pytest.mark.asyncio
async def test_allowlist_is_escape_hatch_for_admins_without_membership() -> None:
    scm = FakeSCM(access_levels={})
    checker = make_checker(scm, allowed_users=["admin"])
    decision = await checker.check(project_id=PROJECT_ID, user_id=STRANGER, username="admin")
    assert decision.allowed
    assert decision.reason == "allowlisted_non_member"
    assert decision.access_level is None


@pytest.mark.asyncio
async def test_allowlist_is_anded_with_role() -> None:
    scm = FakeSCM(access_levels={DEVELOPER: 30})
    checker = make_checker(scm, allowed_users=["alice"])
    denied = await checker.check(project_id=PROJECT_ID, user_id=DEVELOPER, username="bob")
    assert not denied.allowed
    assert denied.reason == "not_in_allowlist"
    allowed = await checker.check(project_id=PROJECT_ID, user_id=DEVELOPER, username="alice")
    assert allowed.allowed


@pytest.mark.asyncio
async def test_member_lookup_is_cached() -> None:
    scm = FakeSCM(access_levels={DEVELOPER: 30})
    checker = make_checker(scm, ttl_seconds=60)
    for _ in range(3):
        await checker.check(project_id=PROJECT_ID, user_id=DEVELOPER, username="dev")
    assert len(scm.member_lookups) == 1
    checker.invalidate()
    await checker.check(project_id=PROJECT_ID, user_id=DEVELOPER, username="dev")
    assert len(scm.member_lookups) == 2


@pytest.mark.asyncio
async def test_permission_lookup_failure_fails_closed() -> None:
    scm = FakeSCM(fail_on=["get_member_access_level"])
    decision = await make_checker(scm).check(
        project_id=PROJECT_ID, user_id=DEVELOPER, username="dev"
    )
    assert not decision.allowed
    assert decision.reason == "permission_check_failed"
