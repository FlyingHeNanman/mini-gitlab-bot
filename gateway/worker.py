"""Worker：唤醒 Codex 自主闭环，网关收尾 push + 开 draft MR + 回帖。"""
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .config import settings
from .db import TaskStore
from .gitlab_client import GitLabClient


class Bot:
    def __init__(self, store: TaskStore, gl: GitLabClient):
        self.store = store
        self.gl = gl
        self.pool = ThreadPoolExecutor(max_workers=settings.agent_concurrency)

    def submit(self, project: str, issue_iid: int) -> tuple[bool, str]:
        inserted, _ = self.store.insert_if_absent(project, issue_iid)
        if not inserted:
            return False, "already-queued"
        self.pool.submit(self._run, project, issue_iid)
        return True, "queued"

    def _run(self, project: str, issue_iid: int):
        try:
            self._handle(project, issue_iid)
        except Exception as exc:  # noqa: BLE001 兜底：任何异常落库并回帖
            self._record_failure(project, issue_iid, exc)

    # ---------- 主流程 ----------
    def _handle(self, project: str, issue_iid: int):
        issue = self.gl.get_issue(project, issue_iid)
        workdir = os.path.join(settings.work_dir, f"job-{issue_iid}-{os.getpid()}")
        branch = f"fix/issue-{issue_iid}"
        shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)
        try:
            self._log(project, issue_iid, "running")
            self.gl.clone(project, workdir)
            subprocess.run(["git", "checkout", "-b", branch], cwd=workdir,
                           check=True, capture_output=True, text=True)

            prompt = self._build_prompt(project, issue_iid, issue.title, issue.description)
            for attempt in range(1, settings.max_iterations + 1):
                self._log(project, issue_iid, "running", attempts=attempt)
                self._run_codex(workdir, prompt)
                if self._has_commits(workdir):
                    break
                prompt += (
                    "\n\n你刚才没有产生任何代码改动和 git commit。"
                    "请实际完成修复并 git add + git commit，不要 push。"
                )
            else:
                self.gl.add_issue_note(project, issue_iid,
                                       "🤖 多次尝试未产生有效提交，请人工处理。")
                self._log(project, issue_iid, "failed", error="no commits after retries")
                return

            # 收尾：push + draft MR + 回帖
            self.gl.push_branch(project, workdir, branch)
            mr = self.gl.create_draft_mr(project, branch, issue_iid)
            self._log(project, issue_iid, "done", branch=branch, mr_iid=mr.iid)
            self.gl.add_issue_note(project, issue_iid,
                                   f"🤖 修复完成，已创建 draft MR：{mr.web_url}\n请人工审批后合并。")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---------- Codex ----------
    def _run_codex(self, workdir: str, prompt: str):
        cmd = ["codex", "exec", "--sandbox", "workspace-write",
               "-c", f"model_provider={settings.model_provider}"]
        if settings.model_name:
            cmd += ["-c", f"model={settings.model_name}"]
        cmd += [prompt]
        subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                       timeout=settings.agent_timeout_sec)

    def _build_prompt(self, project: str, issue_iid: int, title: str, description: str) -> str:
        return (
            f"你是 {project} 仓库的 GitLab issue 修复机器人。请处理 issue #{issue_iid}：{title}\n\n"
            f"{description}\n\n"
            "请按以下流程执行：\n"
            "1) 用 glab MCP 工具获取该 issue 详情并回帖“已接单”；\n"
            "2) 分析代码定位问题并实现修复，保持项目既有风格，只改必要文件；\n"
            "3) 本地运行测试（如 mvn test / npm test）确认通过；\n"
            "4) git add + git commit，提交信息以 fix(issue-{iid}): 开头。\n\n"
            "约束：不要 git push（收尾由网关完成），不要新建 MR。"
        )

    # ---------- 辅助 ----------
    def _has_commits(self, workdir: str) -> bool:
        r = subprocess.run(["git", "log", "--oneline", "-n", "1"],
                           cwd=workdir, capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())

    def _log(self, project: str, issue_iid: int, state: str, **extra):
        task_id = self._lookup_task_id(project, issue_iid)
        if task_id is not None:
            self.store.set_state(task_id, state, **extra)

    def _lookup_task_id(self, project: str, issue_iid: int) -> int | None:
        for t in self.store.list(limit=500):
            if t["project"] == project and t["issue_iid"] == issue_iid:
                return t["id"]
        return None

    def _record_failure(self, project: str, issue_iid: int, exc: Exception):
        task_id = self._lookup_task_id(project, issue_iid)
        if task_id is not None:
            self.store.set_state(task_id, "failed", error=str(exc)[:2000])
        try:
            self.gl.add_issue_note(project, issue_iid,
                                   f"🤖 处理失败：{type(exc).__name__}: {str(exc)[:200]}，请人工处理。")
        except Exception:  # noqa: BLE001 回帖失败不再抛
            pass
