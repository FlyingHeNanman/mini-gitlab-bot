"""GitLab 操作封装：读 issue / 回帖 / clone / push / 开 draft MR。"""
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

    def create_draft_mr(self, project: str, source_branch: str, issue_iid: int,
                        title: str | None = None):
        project_obj = self.gl.projects.get(project)
        mr = project_obj.mergerequests.create({
            "source_branch": source_branch,
            "target_branch": settings.target_branch,
            "title": f"Draft: fix(issue-{issue_iid}) by bot",
            "description": (
                f"Closes #{issue_iid}\n\n"
                "🤖 由 mini-gitlab-bot 自动生成的修复 MR，请人工审批后合并。"
            ),
            "remove_source_branch": True,
        })
        return mr

    # ---------- git 操作（token 内嵌 URL，注意不打印到日志） ----------
    def clone(self, project: str, workdir: str):
        url = self._authed_url(project)
        subprocess.run(["git", "clone", "--depth=1", url, workdir],
                       check=True, capture_output=True, text=True)

    def push_branch(self, project: str, workdir: str, branch: str):
        url = self._authed_url(project)
        subprocess.run(["git", "push", url, f"HEAD:{branch}"],
                       check=True, capture_output=True, text=True, cwd=workdir)

    def _authed_url(self, project: str) -> str:
        base = settings.gitlab_url.rstrip("/")
        # GitLab 支持 https://oauth2:<token>@host 形式；PAT 同样适用
        return base.replace("https://", f"https://oauth2:{settings.gitlab_token}@", 1) + f"/{project}.git"
