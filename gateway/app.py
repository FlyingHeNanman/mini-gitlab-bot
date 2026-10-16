"""FastAPI 入口：GitLab Webhook → 校验 → 入队。"""
import hmac

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db import TaskStore
from .gitlab_client import GitLabClient
from .worker import Bot

app = FastAPI(title="mini-gitlab-bot")

store = TaskStore(settings.db_path)
gl = GitLabClient()
bot = Bot(store, gl)


@app.post("/webhook")
async def webhook(request: Request, x_gitlab_token: str = Header(default="")):
    """GitLab Issues events 入口。

    校验：Secret token（X-Gitlab-Token）→ issue open/reopen → 仓库白名单 → ai-fix 标签。
    """
    if not hmac.compare_digest(x_gitlab_token, settings.webhook_secret):
        raise HTTPException(401, "invalid webhook token")

    payload = await request.json()
    if payload.get("object_kind") != "issue":
        return {"ok": True, "skipped": "not-issue"}

    attrs = payload.get("object_attributes", {})
    if attrs.get("action") not in ("open", "reopen"):
        return {"ok": True, "skipped": "not-open"}

    project = payload["project"]["path_with_namespace"]
    if settings.repo_whitelist and project not in settings.repo_whitelist:
        return {"ok": True, "skipped": "not-whitelisted"}

    labels = attrs.get("labels", []) or []
    label_names = {l.get("title") or l.get("name") for l in labels}
    if settings.ai_fix_label not in label_names:
        return {"ok": True, "skipped": "no-ai-fix-label"}

    iid = attrs["iid"]
    queued, _ = bot.submit(project, iid)
    return {"ok": True, "queued": queued}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/tasks")
async def tasks(limit: int = 50):
    return JSONResponse(store.list(limit=limit))
