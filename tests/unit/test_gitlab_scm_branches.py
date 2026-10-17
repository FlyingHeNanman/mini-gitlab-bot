"""`GitLabSCM` 的分支逻辑：用极小的假对象覆盖 HTTP 桩不容易构造的路径。

桩服务能验证"请求形状对不对"，但构造不出"先查为空、再创建却 409"这种时序。
那类分支用假对象钉死，避免以后重构时静默退化。
"""

from __future__ import annotations

from typing import Any

import pytest
from gitlab.exceptions import GitlabCreateError, GitlabGetError

from adapters.gitlab_client import GitLabSCM, GitLabSettings, ScmError
from core.models import ProjectRef, Task, TriggerKind
from tests.helpers import START

MARKER = "<!-- mini-gitlab-bot:status task-x -->"


def make_task() -> Task:
    return Task(
        id="task-x",
        delivery_key="d",
        project=ProjectRef(project_id=42, path="group/project-a"),
        issue_iid=12,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


class FakeMR:
    def __init__(self, iid: int = 1, title: str = "Draft: x", description: str = "d") -> None:
        self.iid = iid
        self.title = title
        self.description = description
        self.web_url = f"https://gitlab.example.com/group/project-a/-/merge_requests/{iid}"
        self.saves = 0

    def save(self) -> None:
        self.saves += 1


class FakeMRManager:
    def __init__(self) -> None:
        self.visible: list[FakeMR] = []
        self.create_error: BaseException | None = None
        self.list_calls = 0
        self.created: list[dict[str, Any]] = []

    def list(self, **_kwargs: Any) -> list[FakeMR]:
        self.list_calls += 1
        # 第一次查为空；创建撞 409 之后再查才看得见（模拟并发 worker）
        if self.list_calls == 1:
            return []
        return list(self.visible)

    def create(self, data: dict[str, Any]) -> FakeMR:
        self.created.append(data)
        if self.create_error is not None:
            raise self.create_error
        mr = FakeMR(
            iid=99, title=str(data.get("title", "")), description=str(data.get("description", ""))
        )
        self.visible.append(mr)
        return mr


class FakeProject:
    def __init__(self) -> None:
        self.mergerequests = FakeMRManager()


class FakeProjects:
    def __init__(self, project: FakeProject) -> None:
        self._project = project

    def get(self, _project_id: int) -> FakeProject:
        return self._project


class FakeClient:
    def __init__(self, project: FakeProject) -> None:
        self.projects = FakeProjects(project)


def build_scm(project: FakeProject, *, retry: bool = False) -> GitLabSCM:
    return GitLabSCM(
        settings=GitLabSettings(url="http://stub", token="glpat-x" * 3, retry_transient=retry),
        client=FakeClient(project),
    )


async def test_concurrent_409_falls_back_to_the_existing_mr() -> None:
    project = FakeProject()
    existing = FakeMR(iid=7, title="Draft: fix(issue-12)", description="别人建的")
    project.mergerequests.visible.append(existing)
    project.mergerequests.create_error = GitlabCreateError("dup", 409)
    scm = build_scm(project)

    mr = await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-x",
        target_branch="main",
        title="Draft: fix(issue-12)",
        description="我的描述",
    )

    assert mr.iid == 7
    assert project.mergerequests.list_calls == 2  # 查 → 撞 409 → 再查
    assert existing.description == "我的描述"  # 退化成"取回并更新"
    assert existing.saves == 1


async def test_update_is_skipped_when_nothing_changed() -> None:
    project = FakeProject()
    existing = FakeMR(iid=3, title="Draft: 同", description="同")
    project.mergerequests.visible.append(existing)
    scm = build_scm(project)

    await scm.create_draft_mr(
        make_task(),
        source_branch="ai-fix/12-x",
        target_branch="main",
        title="Draft: 同",
        description="同",
    )
    assert existing.saves == 0  # 幂等重试不该产生多余的写请求


async def test_404_on_issue_is_not_found() -> None:
    project = FakeProject()

    def boom(_iid: int) -> Any:
        raise GitlabGetError("404 Not found", 404)

    project.issues = type("Issues", (), {"get": staticmethod(boom)})()  # type: ignore[attr-defined]
    scm = build_scm(project)
    with pytest.raises(ScmError) as excinfo:
        await scm.load_issue_context(make_task())
    assert excinfo.value.not_found is True


async def test_error_message_is_scrubbed_and_truncated() -> None:
    token = "glpat-verysecret-token-123456"

    class BoomClient:
        class projects:  # noqa: N801 模拟 python-gitlab 的属性访问风格
            @staticmethod
            def get(_project_id: int) -> Any:
                raise GitlabGetError(f"leaked {token} in a very long message " + "x" * 600, 500)

    scm = GitLabSCM(settings=GitLabSettings(url="http://stub", token=token), client=BoomClient())

    with pytest.raises(ScmError) as excinfo:
        await scm.default_branch(ProjectRef(project_id=42, path="group/project-a"))
    message = str(excinfo.value)
    assert token not in message
    assert "500" in message
    assert len(message) < 400
