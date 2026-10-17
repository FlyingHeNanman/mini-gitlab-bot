"""一个极小的 GitLab API v4 桩服务（真实 HTTP，不是 mock）。

为什么不用 `unittest.mock` 挡掉 python-gitlab：M3 最大的风险不是"我们的逻辑写错了"，
而是**请求形状写错了**（少了 `state=opened`、把 `add_labels` 拼错、`members/all` 写成
`members`、少带 `PRIVATE-TOKEN`）。这些只有让 python-gitlab 真的发 HTTP 才能发现。

能力范围刻意只覆盖我们用到的那几个端点；未实现的路径返回 404（而不是假装成功），
这样"不小心调了别的接口"会立刻暴露。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

DEFAULT_TOKEN = "glpat-stub-token-0123456789"

_API = "/api/v4"


@dataclass
class StubProject:
    project_id: int = 42
    path: str = "group/project-a"
    default_branch: str = "main"


@dataclass
class StubState:
    token: str = DEFAULT_TOKEN
    project: StubProject = field(default_factory=StubProject)
    #: user_id -> access_level（不存在 = 非成员）
    members: dict[int, int] = field(default_factory=dict)
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    notes: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    merge_requests: list[dict[str, Any]] = field(default_factory=list)
    pipelines: list[dict[str, Any]] = field(default_factory=list)
    #: pipeline id -> job 列表
    jobs: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    #: pipeline id -> test_report
    test_reports: dict[int, dict[str, Any]] = field(default_factory=dict)
    commits: list[dict[str, Any]] = field(default_factory=list)
    #: sha -> diff 列表
    commit_diffs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    languages: dict[str, float] = field(default_factory=dict)
    #: job id -> 原始日志文本
    job_logs: dict[int, str] = field(default_factory=dict)
    #: 记录每一次请求，便于断言"发了什么"
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: 注入故障：[(方法, 路径正则, 状态码)]，匹配一次即消耗
    failures: list[tuple[str, str, int]] = field(default_factory=list)
    _note_id: int = 0
    _mr_iid: int = 0

    def next_note_id(self) -> int:
        self._note_id += 1
        return self._note_id

    def next_mr_iid(self) -> int:
        self._mr_iid += 1
        return self._mr_iid

    # ---- 便捷构造 ----

    def add_issue(
        self,
        iid: int = 12,
        *,
        title: str = "parse_labels 在空标签时返回 None",
        description: str = "复现：给一个没有任何标签的 issue 调用 parse_labels()",
        labels: list[str] | None = None,
        author: str = "reporter",
        state: str = "opened",
    ) -> None:
        self.issues[iid] = {
            "id": 1000 + iid,
            "iid": iid,
            "project_id": self.project.project_id,
            "title": title,
            "description": description,
            "labels": list(labels if labels is not None else ["ai-fix"]),
            "state": state,
            "web_url": f"https://gitlab.example.com/{self.project.path}/-/issues/{iid}",
            "author": {"username": author, "name": author},
            "created_at": "2026-10-17T09:00:00.000Z",
            "updated_at": "2026-10-17T09:30:00.000Z",
        }
        self.notes.setdefault(iid, [])

    def add_note(self, iid: int, body: str, *, system: bool = False, author: str = "alice") -> int:
        note_id = self.next_note_id()
        self.notes.setdefault(iid, []).append(
            {
                "id": note_id,
                "body": body,
                "system": system,
                "author": {"username": author},
                "created_at": f"2026-10-17T10:{note_id:02d}:00.000Z",
            }
        )
        return note_id

    def add_pipeline(
        self,
        pipeline_id: int = 900,
        *,
        ref: str = "main",
        status: str = "failed",
        sha: str = "a" * 40,
        jobs: list[dict[str, Any]] | None = None,
        test_report: dict[str, Any] | None = None,
    ) -> int:
        self.pipelines.append(
            {
                "id": pipeline_id,
                "iid": pipeline_id,
                "project_id": self.project.project_id,
                "ref": ref,
                "status": status,
                "sha": sha,
                "source": "push",
                "web_url": (
                    f"https://gitlab.example.com/{self.project.path}/-/pipelines/{pipeline_id}"
                ),
                "duration": 42,
            }
        )
        self.jobs[pipeline_id] = list(jobs or [])
        if test_report is not None:
            self.test_reports[pipeline_id] = test_report
        return pipeline_id

    def add_job(
        self,
        pipeline_id: int = 900,
        job_id: int = 5001,
        *,
        name: str = "pytest",
        status: str = "failed",
        stage: str = "test",
        failure_reason: str | None = "script_failure",
        log: str = "",
    ) -> int:
        self.jobs.setdefault(pipeline_id, []).append(
            {
                "id": job_id,
                "pipeline_id": pipeline_id,
                "name": name,
                "stage": stage,
                "status": status,
                "failure_reason": failure_reason,
                "web_url": (f"https://gitlab.example.com/{self.project.path}/-/jobs/{job_id}"),
                "allow_failure": False,
                "duration": 12.5,
            }
        )
        if log:
            self.job_logs[job_id] = log
        return job_id

    def add_commit(
        self,
        sha: str,
        *,
        title: str = "fix: parse_labels",
        message: str | None = None,
        author: str = "alice",
        created_at: str = "2026-10-17T09:00:00.000Z",
        diffs: list[dict[str, Any]] | None = None,
    ) -> str:
        self.commits.append(
            {
                "id": sha,
                "short_id": sha[:8],
                "title": title,
                "message": message or title,
                "author_name": author,
                "author_email": f"{author}@example.com",
                "created_at": created_at,
                "web_url": f"https://gitlab.example.com/{self.project.path}/-/commit/{sha}",
            }
        )
        self.commit_diffs[sha] = list(diffs or [])
        return sha

    def find_note(self, iid: int, note_id: int) -> dict[str, Any] | None:
        return next((note for note in self.notes.get(iid, []) if note["id"] == note_id), None)

    def request_paths(self) -> list[str]:
        return [f"{item['method']} {urlparse(item['path']).path}" for item in self.requests]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StubGitLab/1.0"

    # ---- 基础设施 ----

    @property
    def state(self) -> StubState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *_args: Any) -> None:  # noqa: D102 静音
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_raw(
        self, status: int, text: str, content_type: str = "text/plain; charset=utf-8"
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: dict[str, Any] | None = None) -> None:
        self.state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "query": parse_qs(urlparse(self.path).query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body or {},
            }
        )

    def _failure(self, path: str) -> int | None:
        """命中注入的故障则返回状态码（一次一条）。"""
        for index, (method, pattern, status) in enumerate(self.state.failures):
            if method == self.command and re.fullmatch(pattern, path):
                self.state.failures.pop(index)
                return status
        return None

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = self._read_json()
        self._record(body)

        if self.headers.get("PRIVATE-TOKEN") != self.state.token:
            self._send(401, {"message": "401 Unauthorized"})
            return

        failure = self._failure(path)
        if failure is not None:
            self._send(failure, {"message": f"injected {failure}"})
            return

        if not path.startswith(_API):
            self._send(404, {"message": f"404 Not Found: {path}"})
            return

        route = path[len(_API) :]
        pid = re.escape(str(self.state.project.project_id))

        if method == "GET" and re.fullmatch(rf"/projects/{pid}", route):
            project = self.state.project
            self._send(
                200,
                {
                    "id": project.project_id,
                    "name": project.path.rsplit("/", 1)[-1],
                    "path": project.path.rsplit("/", 1)[-1],
                    "path_with_namespace": project.path,
                    "default_branch": project.default_branch,
                    "description": "演示项目",
                    "topics": ["demo"],
                    "visibility": "internal",
                    "web_url": f"https://gitlab.example.com/{project.path}",
                },
            )
            return

        match = re.fullmatch(rf"/projects/{pid}/members/all/(\d+)", route)
        if method == "GET" and match:
            user_id = int(match.group(1))
            if user_id not in self.state.members:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(
                200,
                {
                    "id": user_id,
                    "username": f"user{user_id}",
                    "access_level": self.state.members[user_id],
                },
            )
            return

        match = re.fullmatch(rf"/projects/{pid}/issues", route)
        if match and method == "GET":
            items = list(self.state.issues.values())
            search = query.get("search", [None])[0]
            if search:
                items = [
                    item
                    for item in items
                    if search.lower() in (item["title"] + " " + item["description"]).lower()
                ]
            if "state" in query:
                items = [item for item in items if item["state"] == query["state"][0]]
            limit = int(query.get("per_page", ["20"])[0])
            self._send(200, items[:limit])
            return

        match = re.fullmatch(rf"/projects/{pid}/issues/(\d+)", route)
        if match:
            iid = int(match.group(1))
            issue = self.state.issues.get(iid)
            if issue is None:
                self._send(404, {"message": "404 Not found"})
                return
            if method == "GET":
                self._send(200, issue)
                return
            if method == "PUT":
                for key in ("add_labels", "remove_labels"):
                    if key in body:
                        wanted = [
                            item.strip() for item in str(body[key]).split(",") if item.strip()
                        ]
                        current = list(issue["labels"])
                        if key == "add_labels":
                            issue["labels"] = list(dict.fromkeys([*current, *wanted]))
                        else:
                            issue["labels"] = [item for item in current if item not in wanted]
                self._send(200, issue)
                return

        match = re.fullmatch(rf"/projects/{pid}/issues/(\d+)/notes", route)
        if match:
            iid = int(match.group(1))
            if iid not in self.state.issues:
                self._send(404, {"message": "404 Not found"})
                return
            if method == "GET":
                items, headers = _page(
                    self.state.notes.get(iid, []),
                    query,
                    host=self.headers.get("Host", "127.0.0.1"),
                    path=path,
                )
                self._send(200, items, headers)
                return
            if method == "POST":
                note_id = self.state.add_note(iid, str(body.get("body", "")))
                self._send(201, self.state.find_note(iid, note_id))
                return

        match = re.fullmatch(rf"/projects/{pid}/issues/(\d+)/notes/(\d+)", route)
        if match and method == "PUT":
            iid, note_id = int(match.group(1)), int(match.group(2))
            note = self.state.find_note(iid, note_id)
            if note is None:
                self._send(404, {"message": "404 Not found"})
                return
            note["body"] = str(body.get("body", note["body"]))
            self._send(200, note)
            return

        match = re.fullmatch(rf"/projects/{pid}/merge_requests", route)
        if match:
            if method == "GET":
                wanted_branch = query.get("source_branch", [None])[0]
                wanted_state = query.get("state", [None])[0]
                items = [
                    mr
                    for mr in self.state.merge_requests
                    if (wanted_branch is None or mr["source_branch"] == wanted_branch)
                    and (wanted_state is None or mr["state"] == wanted_state)
                ]
                self._send(200, items)
                return
            if method == "POST":
                source = str(body.get("source_branch", ""))
                for mr in self.state.merge_requests:
                    if mr["source_branch"] == source and mr["state"] == "opened":
                        self._send(
                            409,
                            {
                                "message": [
                                    "Another open merge request already exists for this "
                                    f"source branch: !{mr['iid']}"
                                ]
                            },
                        )
                        return
                iid = self.state.next_mr_iid()
                mr = {
                    "id": 5000 + iid,
                    "iid": iid,
                    "project_id": self.state.project.project_id,
                    "title": str(body.get("title", "")),
                    "description": str(body.get("description", "")),
                    "source_branch": source,
                    "target_branch": str(body.get("target_branch", "")),
                    "state": "opened",
                    "web_url": (
                        f"https://gitlab.example.com/{self.state.project.path}"
                        f"/-/merge_requests/{iid}"
                    ),
                }
                self.state.merge_requests.append(mr)
                self._send(201, mr)
                return

        match = re.fullmatch(rf"/projects/{pid}/merge_requests/(\d+)", route)
        if match and method == "PUT":
            iid = int(match.group(1))
            mr = next((item for item in self.state.merge_requests if item["iid"] == iid), None)
            if mr is None:
                self._send(404, {"message": "404 Not found"})
                return
            for key in ("title", "description"):
                if key in body:
                    mr[key] = str(body[key])
            self._send(200, mr)
            return

        match = re.fullmatch(rf"/projects/{pid}/pipelines", route)
        if match and method == "GET":
            items = [
                item
                for item in self.state.pipelines
                if ("ref" not in query or item["ref"] == query["ref"][0])
                and ("status" not in query or item["status"] == query["status"][0])
            ]
            limit = int(query.get("per_page", ["20"])[0])
            self._send(200, items[:limit])
            return

        match = re.fullmatch(rf"/projects/{pid}/pipelines/(\d+)", route)
        if match and method == "GET":
            pipeline_id = int(match.group(1))
            pipeline = next(
                (item for item in self.state.pipelines if item["id"] == pipeline_id), None
            )
            if pipeline is None:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(200, pipeline)
            return

        match = re.fullmatch(rf"/projects/{pid}/pipelines/(\d+)/jobs", route)
        if match and method == "GET":
            pipeline_id = int(match.group(1))
            jobs = list(self.state.jobs.get(pipeline_id, []))
            raw_scope = query.get("scope") or query.get("scope[]") or []
            if raw_scope:
                # python-gitlab 对数组属性发 `scope[]=failed`；真实 GitLab 两者都收
                wanted = set(",".join(raw_scope).split(","))
                jobs = [job for job in jobs if job["status"] in wanted]
            limit = int(query.get("per_page", ["20"])[0])
            self._send(200, jobs[:limit])
            return

        match = re.fullmatch(rf"/projects/{pid}/pipelines/(\d+)/test_report", route)
        if match and method == "GET":
            pipeline_id = int(match.group(1))
            report = self.state.test_reports.get(pipeline_id)
            if report is None:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(200, report)
            return

        match = re.fullmatch(rf"/projects/{pid}/jobs/(\d+)", route)
        if match and method == "GET":
            job_id = int(match.group(1))
            job = next(
                (
                    item
                    for jobs in self.state.jobs.values()
                    for item in jobs
                    if item["id"] == job_id
                ),
                None,
            )
            if job is None:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(200, job)
            return

        match = re.fullmatch(rf"/projects/{pid}/jobs/(\d+)/trace", route)
        if match and method == "GET":
            job_id = int(match.group(1))
            if job_id not in self.state.job_logs:
                self._send(404, {"message": "404 Not found"})
                return
            self._send_raw(200, self.state.job_logs[job_id])
            return

        match = re.fullmatch(rf"/projects/{pid}/repository/commits", route)
        if match and method == "GET":
            items = list(self.state.commits)
            if "ref_name" in query:
                ref = query["ref_name"][0]
                if ref in {item["id"] for item in items} | {item["short_id"] for item in items}:
                    items = [item for item in items if item["id"] == ref or item["short_id"] == ref]
            limit = int(query.get("per_page", ["20"])[0])
            self._send(200, items[:limit])
            return

        match = re.fullmatch(rf"/projects/{pid}/repository/commits/([0-9a-f]{{6,40}})/diff", route)
        if match and method == "GET":
            sha = match.group(1)
            diffs = next(
                (value for key, value in self.state.commit_diffs.items() if key.startswith(sha)),
                None,
            )
            if diffs is None:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(200, diffs)
            return

        match = re.fullmatch(rf"/projects/{pid}/repository/commits/([0-9a-f]{{6,40}})", route)
        if match and method == "GET":
            sha = match.group(1)
            commit = next((item for item in self.state.commits if item["id"].startswith(sha)), None)
            if commit is None:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(200, commit)
            return

        match = re.fullmatch(rf"/projects/{pid}/issues/(\d+)/related_merge_requests", route)
        if match and method == "GET":
            self._send(200, list(self.state.merge_requests))
            return

        match = re.fullmatch(rf"/projects/{pid}/merge_requests/(\d+)", route)
        if match and method == "GET":
            iid = int(match.group(1))
            mr = next((item for item in self.state.merge_requests if item["iid"] == iid), None)
            if mr is None:
                self._send(404, {"message": "404 Not found"})
                return
            self._send(200, mr)
            return

        match = re.fullmatch(rf"/projects/{pid}/merge_requests/(\d+)/notes", route)
        if match and method == "GET":
            iid = int(match.group(1))
            mr = next((item for item in self.state.merge_requests if item["iid"] == iid), None)
            notes = list((mr or {}).get("notes") or [])
            self._send(200, notes)
            return

        match = re.fullmatch(rf"/projects/{pid}/languages", route)
        if match and method == "GET":
            self._send(200, self.state.languages)
            return

        self._send(404, {"message": f"404 Not found: {method} {route}"})


def _page(
    items: list[dict[str, Any]],
    query: dict[str, list[str]],
    *,
    host: str = "127.0.0.1",
    path: str = "",
) -> tuple[list[Any], dict[str, str]]:
    """按 GitLab 的形状分页。

    注意：python-gitlab 8.x 靠 **`Link` 头**（RFC 5988）翻页，而不是 `X-Next-Page`。
    真实 GitLab 两个都发；桩如果只发 `X-Next-Page`，adapter 的翻页逻辑会看起来"没生效"。
    """
    per_page = int(query.get("per_page", ["20"])[0])
    page = int(query.get("page", ["1"])[0])
    ordered = list(items)
    if query.get("sort", [""])[0] == "desc":
        ordered = list(reversed(ordered))
    total_pages = max(1, (len(ordered) + per_page - 1) // per_page)
    start = (page - 1) * per_page
    chunk = ordered[start : start + per_page]
    headers = {
        "X-Page": str(page),
        "X-Per-Page": str(per_page),
        "X-Total": str(len(ordered)),
        "X-Total-Pages": str(total_pages),
        "X-Next-Page": str(page + 1) if page < total_pages else "",
    }
    if page < total_pages:
        next_query = {**{key: value[0] for key, value in query.items()}, "page": str(page + 1)}
        headers["Link"] = f'<http://{host}{path}?{urlencode(next_query)}>; rel="next"'
    return chunk, headers


class StubGitLab:
    """上下文管理器：`with StubGitLab() as stub: ...` 起一个真实 HTTP 服务。"""

    def __init__(self, state: StubState | None = None) -> None:
        self.state = state or StubState()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None, "先 start()"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> StubGitLab:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
