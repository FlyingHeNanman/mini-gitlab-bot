"""`GitLabSCM`：SCM 端口的真实实现（python-gitlab）。

对照 PLAN §7 红线与 §8.1 里对 v0 的三条批评，这里逐条改掉：

1. **凭据不进 argv / URL / 日志**：token 只走 `PRIVATE-TOKEN` 请求头；错误消息统一过
   `Scrubber`（v0 的 `_authed_url` 把 token 拼进 clone URL，是本项目明令禁止的）。
2. **可注入**：客户端由构造函数传入，测试可以指到本地桩服务，不需要真 GitLab。
3. **不把异常直接抛给 webhook**：所有失败统一成 `ScmError`（编排器按 best-effort 处理，
   HTTP 层永远 2xx）。

另外三条实现约束：

* **只开 draft、从不合并、从不删分支**：draft 靠标题 `Draft: ` 前缀（GitLab API 没有
  `draft` 参数）；`remove_source_branch=False`，合并后也保留分支作为审计证据。
* **评论原地编辑**：按 marker 找机器人自己那条 note 改，绝不每次新开一条。
* **python-gitlab 是同步库**：所有调用走 `asyncio.to_thread`；并且用一把锁串行化 ——
  `requests.Session` 不保证线程安全，而我们的并发度只有 `AGENT_CONCURRENCY`（默认 2），
  串行化的吞吐损失可以忽略，换来的确定性更重要。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import gitlab
from gitlab.exceptions import (
    GitlabAuthenticationError,
    GitlabCreateError,
    GitlabError,
    GitlabGetError,
    GitlabHttpError,
)

from adapters.proc import Scrubber
from core.models import IssueContext, MergeRequestRef, ProjectRef, Task

logger = logging.getLogger(__name__)

#: 扫描已有 note 找状态评论时的硬上限（防止超长 issue 把一次调用拖成几十页请求）
NOTE_SCAN_LIMIT = 500


class ScmError(RuntimeError):
    """GitLab API 调用失败。消息已脱敏，可直接写进 issue 评论。"""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass(slots=True)
class GitLabSettings:
    url: str = "https://gitlab.example.com"
    token: str = ""
    timeout_sec: float = 30.0
    #: 进 context.md 的 issue 评论条数（取最新 N 条人类评论；system note 一律丢弃）
    comment_limit: int = 20
    #: 遇到 5xx / 连接错误时由 python-gitlab 重试
    retry_transient: bool = True


@dataclass
class GitLabSCM:
    settings: GitLabSettings = field(default_factory=GitLabSettings)
    client: Any = None
    scrubber: Scrubber | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = gitlab.Gitlab(
                self.settings.url,
                private_token=self.settings.token,
                timeout=self.settings.timeout_sec,
                retry_transient_errors=self.settings.retry_transient,
            )
        if self.scrubber is None:
            self.scrubber = Scrubber((self.settings.token,) if self.settings.token else ())
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    async def default_branch(self, project: ProjectRef) -> str:
        proj = await self._run("读取项目信息", self._get_project, project.project_id)
        branch = getattr(proj, "default_branch", None) or "main"
        return str(branch)

    async def get_member_access_level(self, project_id: int, user_id: int) -> int | None:
        try:
            member = await self._run("查询成员权限", self._get_member, project_id, user_id)
        except ScmError as exc:
            if exc.not_found:
                return None
            raise
        level = getattr(member, "access_level", None)
        return int(level) if level is not None else None

    async def load_issue_context(self, task: Task) -> IssueContext:
        return await self._run("读取 issue", self._load_issue, task)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    async def set_labels(
        self,
        task: Task,
        *,
        add: tuple[str, ...] | list[str] = (),
        remove: tuple[str, ...] | list[str] = (),
    ) -> None:
        add = tuple(item for item in add if item)
        remove = tuple(item for item in remove if item)
        if not add and not remove:
            return
        await self._run("更新标签", self._set_labels, task, add, remove)

    async def upsert_comment(self, task: Task, body: str, *, marker: str) -> int:
        return await self._run("写状态评论", self._upsert_comment, task, body, marker)

    async def create_draft_mr(
        self,
        task: Task,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestRef:
        mr = await self._run(
            "创建 MR", self._create_mr, task, source_branch, target_branch, title, description
        )
        return MergeRequestRef(
            iid=int(mr.iid),
            url=str(getattr(mr, "web_url", "")),
            title=str(getattr(mr, "title", title)),
            draft=str(getattr(mr, "title", title)).startswith("Draft:"),
        )

    # ------------------------------------------------------------------ #
    # 同步实现（全部在 to_thread 里跑）
    # ------------------------------------------------------------------ #

    def _get_project(self, project_id: int) -> Any:
        return self.client.projects.get(project_id)

    def _get_member(self, project_id: int, user_id: int) -> Any:
        # members_all 才会包含从 group 继承来的成员；members 只看直接成员
        return self._get_project(project_id).members_all.get(user_id)

    def _load_issue(self, task: Task) -> IssueContext:
        issue = self._get_project(task.project.project_id).issues.get(task.issue_iid)
        comments: list[str] = []
        scanned = 0
        # 倒序翻页，命中够数就停：长 issue 也只花一两页请求
        for note in issue.notes.list(
            iterator=True, per_page=100, sort="desc", order_by="created_at"
        ):
            scanned += 1
            if scanned > NOTE_SCAN_LIMIT:  # pragma: no cover - 防御性
                logger.warning("issue #%s 的评论超过 %s 条，只取最新部分", task.issue_iid, scanned)
                break
            if getattr(note, "system", False):
                continue  # 标签变更等系统 note 不进上下文（PLAN §5 决策）
            text = (getattr(note, "body", "") or "").strip()
            if not text:
                continue
            comments.append(text)
            if len(comments) >= self.settings.comment_limit:
                break
        comments.reverse()  # 还原成时间正序
        author = getattr(issue, "author", None) or {}
        return IssueContext(
            iid=int(getattr(issue, "iid", task.issue_iid)),
            title=str(getattr(issue, "title", "") or ""),
            description=str(getattr(issue, "description", "") or ""),
            author=str(author.get("username", "unknown")),
            web_url=str(getattr(issue, "web_url", "") or ""),
            labels=tuple(getattr(issue, "labels", None) or ()),
            state=str(getattr(issue, "state", "opened") or "opened"),
            comments=tuple(comments),
        )

    def _set_labels(self, task: Task, add: tuple[str, ...], remove: tuple[str, ...]) -> Any:
        payload: dict[str, str] = {}
        if add:
            payload["add_labels"] = ",".join(add)
        if remove:
            payload["remove_labels"] = ",".join(remove)
        project = self._get_project(task.project.project_id)
        # 用 add_labels/remove_labels 而不是"读出全部标签再整体写回"：
        # 后者会覆盖并发修改（人手动加的标签会被抹掉）
        return project.issues.update(task.issue_iid, payload)

    def _upsert_comment(self, task: Task, body: str, marker: str) -> int:
        issue = self._get_project(task.project.project_id).issues.get(task.issue_iid)
        scanned = 0
        for note in issue.notes.list(
            iterator=True, per_page=100, sort="desc", order_by="created_at"
        ):
            scanned += 1
            if scanned > NOTE_SCAN_LIMIT:  # pragma: no cover - 防御性
                break
            if getattr(note, "system", False):
                continue
            if marker and marker in (getattr(note, "body", "") or ""):
                note.body = body
                note.save()
                return int(note.id)
        created = issue.notes.create({"body": body})
        return int(created.id)

    def _create_mr(
        self,
        task: Task,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> Any:
        project = self._get_project(task.project.project_id)
        existing = self._open_mr_for(project, source_branch)
        if existing is not None:
            # 重试 / 崩溃恢复后的幂等路径：复用同一条 MR，只把描述刷新到最新
            return self._update_mr(existing, title, description)
        try:
            return project.mergerequests.create(
                {
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description,
                    # 合并后保留分支：审计与回滚都要能看到当初改了什么
                    "remove_source_branch": False,
                }
            )
        except GitlabCreateError as exc:
            if getattr(exc, "response_code", 0) == 409:
                # 并发下另一个 worker 刚创建：退化成"取回并更新"
                existing = self._open_mr_for(project, source_branch)
                if existing is not None:
                    return self._update_mr(existing, title, description)
            raise

    def _open_mr_for(self, project: Any, source_branch: str) -> Any | None:
        matches = project.mergerequests.list(
            source_branch=source_branch, state="opened", per_page=5
        )
        return matches[0] if matches else None

    @staticmethod
    def _update_mr(mr: Any, title: str, description: str) -> Any:
        changed = False
        if getattr(mr, "title", None) != title:
            mr.title = title
            changed = True
        if getattr(mr, "description", None) != description:
            mr.description = description
            changed = True
        if changed:
            mr.save()
        return mr

    # ------------------------------------------------------------------ #
    # 线程 + 错误映射
    # ------------------------------------------------------------------ #

    async def _run(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            async with self._lock:
                return await asyncio.to_thread(fn, *args)
        except GitlabAuthenticationError as exc:
            raise ScmError(
                f"{what}失败：GitLab 拒绝了这个 token（401，检查 scope 是否是 api）"
            ) from exc
        except GitlabGetError as exc:
            code = getattr(exc, "response_code", 0)
            if code == 404:
                raise ScmError(f"{what}失败：对象不存在（404）", not_found=True) from exc
            raise ScmError(f"{what}失败：{self._describe(exc)}") from exc
        except GitlabHttpError as exc:
            raise ScmError(f"{what}失败：{self._describe(exc)}") from exc
        except GitlabError as exc:
            raise ScmError(f"{what}失败：{self._describe(exc)}") from exc
        except (OSError, TimeoutError) as exc:
            raise ScmError(f"{what}失败：无法连接 GitLab（{type(exc).__name__}）") from exc

    def _describe(self, exc: BaseException) -> str:
        """把 GitLab 的错误压成一行，并确保 token 不会出现在里面。"""
        code = getattr(exc, "response_code", None)
        message = getattr(exc, "error_message", "") or str(exc)
        text = self.scrubber(str(message)) if self.scrubber else str(message)
        text = " ".join(text.split())
        if len(text) > 300:
            text = f"{text[:300]}…"
        return f"HTTP {code}：{text}" if code else text
