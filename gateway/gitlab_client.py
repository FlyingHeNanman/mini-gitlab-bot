"""v0 参考实现（**M1 起不再是活代码**）：GitLab 操作封装。

保留原因：M3 写 `adapters/gitlab_client.py` 时可以对照这里的调用方式与字段处理。

⚠️ 复制前必须改掉的地方（PLAN §7 加固基线 5 / §8.1）：
* 它把 token 拼进 clone URL 与 `git push` 的参数里 —— **凭据会进入 argv 与日志**，
  正解是凭据只经由 `GIT_ASKPASS`/credential helper 传入，绝不进命令行；
* `from .config import settings` 是模块级单例，无法在测试里替换 —— M3 改成构造函数注入；
* 它直接抛异常给调用方，会让 webhook handler 返回 5xx（GitLab 会因此永久禁用 webhook）。
"""

import subprocess

import gitlab

from .config import settings


class GitLabClient:
    def __init__(self):
        self.gl = gitlab.Gitlab(settings.gitlab_url, private_token=settings.gitlab_token)
        self.gl.auth()  # 启动即校验 token

    # ---------- API 操作 ----------
    def get_issue(self, project: str, iid: int):
        return self.gl.projects.get(project).issues.get(iid)

    def add_issue_note(self, project: str, iid: int, body: str):
        self.gl.projects.get(project).issues.get(iid).notes.create({"body": body})

    def create_draft_mr(
        self, project: str, source_branch: str, issue_iid: int, title: str | None = None
    ):
        project_obj = self.gl.projects.get(project)
        mr = project_obj.mergerequests.create(
            {
                "source_branch": source_branch,
                "target_branch": settings.target_branch,
                "title": f"Draft: fix(issue-{issue_iid}) by bot",
                "description": (
                    f"Closes #{issue_iid}\n\n"
                    "🤖 由 mini-gitlab-bot 自动生成的修复 MR，请人工审批后合并。"
                ),
                "remove_source_branch": True,
            }
        )
        return mr

    # ---------- git 操作（token 内嵌 URL，注意不打印到日志） ----------
    def clone(self, project: str, workdir: str):
        url = self._authed_url(project)
        subprocess.run(
            ["git", "clone", "--depth=1", url, workdir], check=True, capture_output=True, text=True
        )

    def push_branch(self, project: str, workdir: str, branch: str):
        url = self._authed_url(project)
        subprocess.run(
            ["git", "push", url, f"HEAD:{branch}"],
            check=True,
            capture_output=True,
            text=True,
            cwd=workdir,
        )

    def _authed_url(self, project: str) -> str:
        base = settings.gitlab_url.rstrip("/")
        # GitLab 支持 https://oauth2:<token>@host 形式；PAT 同样适用
        return (
            base.replace("https://", f"https://oauth2:{settings.gitlab_token}@", 1)
            + f"/{project}.git"
        )
