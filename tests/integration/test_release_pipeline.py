"""Tag Push → Release 端到端：HTTP 入队 → worker 执行 → GitLab Release。"""

from __future__ import annotations

import httpx

from adapters.fakes import FakeSCM
from core.models import CommitInfo, TagInfo
from core.states import TaskKind, TaskState
from gateway.app import create_app
from tests.helpers import PROJECT_PATH
from tests.payloads import headers, tag_push_payload


def _tag(name: str, sha: str) -> TagInfo:
    return TagInfo(
        name=name,
        commit_sha=sha,
        web_url=f"https://gitlab.example.com/{PROJECT_PATH}/-/tags/{name}",
    )


def _commit(sha: str, title: str) -> CommitInfo:
    return CommitInfo(
        sha=sha,
        title=title,
        message=title,
        web_url=f"https://gitlab.example.com/{PROJECT_PATH}/-/commit/{sha}",
    )


async def test_tag_push_drives_release_end_to_end(make_runtime, settings) -> None:
    scm = FakeSCM(
        tags=[_tag("v1.1.0", "bbb222"), _tag("v1.0.0", "aaa111")],
        commits_between={
            ("v1.0.0", "v1.1.0"): [
                _commit("c1", "feat: 支持 tag 自动发布"),
                _commit("c2", "fix: 修复发布说明里的空小节"),
            ]
        },
    )
    runtime, _ = make_runtime(scm=scm, with_worker=True)
    app = create_app(settings=settings, runtime=runtime)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/gitlab",
            json=tag_push_payload(tag="v1.1.0"),
            headers=headers(event="Tag Push Hook", delivery="tag-release-1"),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"

    assert await runtime.worker.drain() == 1
    task = runtime.store.list_tasks()[0]
    assert task.kind is TaskKind.RELEASE
    assert task.state is TaskState.SUCCEEDED
    assert task.report["previous_tag"] == "v1.0.0"
    assert task.report["commit_count"] == 2
    assert len(scm.release_creations) == 1
    release = scm.release_creations[0]
    assert release.tag_name == "v1.1.0"
    assert "支持 tag 自动发布" in release.description
    assert "修复发布说明里的空小节" in release.description
    assert task.report["release_url"] == release.web_url
