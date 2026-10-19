"""FastAPI 入口：GitLab Webhook → 校验 → 权限闸门 → 幂等入队（永远尽快 2xx）。

铁律（PLAN §6 #9、§7 基线 7）：**绝不返回 5xx**。GitLab 连续失败 4 次就临时禁用
webhook、40 次永久禁用，所以任何异常都只落日志、响应仍是 200。

唯一有意返回非 2xx 的情况是 `X-Gitlab-Token` 不匹配（401）—— 那是**运维信号**，
正常运行时不会出现；比"静默 200 装作收到了"更容易排查。

`/tasks` 状态页在 v1 **故意不暴露**（PLAN §12 #7）：只留无信息的 `/healthz`。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from core.logging_setup import attach_uvicorn_loggers, configure_logging
from core.states import ACTIVE_STATES, TaskState
from core.tasks_view import TaskFilters, render_task_detail, render_tasks_page, task_to_dict
from gateway.config import Settings
from gateway.permissions import PermissionChecker
from gateway.runtime import Runtime, build_production_runtime
from gateway.webhook import parse_webhook, verify_token

logger = logging.getLogger(__name__)


def create_app(*, settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """构建应用。

    测试通过 `runtime=build_runtime(..., scm=FakeSCM(), vcs=FakeVCS())` 注入假适配器；
    不注入时装配 M2 的真实适配器（git / codex / uv）。
    """
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    attach_uvicorn_loggers()
    rt = runtime or build_production_runtime(settings)
    checker = PermissionChecker(
        rt.scm,
        min_access_level=settings.min_access_level,
        allowed_users=settings.allowed_trigger_users,
        ttl_seconds=settings.member_cache_ttl_sec,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # uvicorn 在 import 之后才应用它自己的 logging 配置，所以这里再收一次
        attach_uvicorn_loggers()
        for message in settings.startup_warnings():
            logger.warning("启动检查：%s", message)
        if rt.admin_token:
            logger.info("只读任务页已启用：GET /tasks（首次带 ?token=… 或 X-Admin-Token 头）")
        else:
            logger.info("只读任务页已关闭（TASKS_PAGE_ENABLED=false 或无法持久化 token）")
        worker_job: asyncio.Task[None] | None = None
        if rt.worker is not None:
            worker_job = asyncio.create_task(rt.worker.run_forever(), name="mini-gitlab-bot-worker")
            logger.info(
                "Worker 已启动（owner=%s, concurrency=%d）",
                settings.worker_owner,
                settings.agent_concurrency,
            )
        else:
            logger.warning("Worker 未启用：仅接收 webhook，任务会留在队列里")
        try:
            yield
        finally:
            if worker_job is not None:
                worker_job.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_job
            rt.store.close()

    app = FastAPI(title="mini-gitlab-bot", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """存活探针：进程还在、事件循环还能响应就 200。

        刻意**不含任何任务信息**（v1 不暴露 `/tasks`，避免泄露仓库名 / issue 标题 / 堆栈）。
        """
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """就绪探针：数据库可读、worker 在跑、没有任务卡死在过期租约上。

        只回状态类别（`store` / `worker_disabled` / `stalled`），不回任务 id ——
        排障需要细节时去看日志（已带 task_id），而不是把这个口子变成信息出口。
        """
        try:
            queued = rt.store.count(state=TaskState.QUEUED)
        except Exception:  # noqa: BLE001 就绪探针本身绝不能抛
            logger.exception("就绪检查失败：存储不可用")
            return JSONResponse({"status": "degraded", "reason": "store"}, status_code=503)

        if rt.worker is None:
            return JSONResponse(
                {"status": "degraded", "reason": "worker_disabled", "queued": queued},
                status_code=503,
            )

        if await _has_stalled_task(rt, settings.ready_stale_sec):
            return JSONResponse({"status": "degraded", "reason": "stalled"}, status_code=503)
        return JSONResponse({"status": "ok", "queued": queued})

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """Prometheus 文本格式。标签只有状态/阶段这类有限枚举，不含项目或任务 id。"""
        if not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="Not Found")
        metrics_registry = rt.metrics
        try:
            metrics_registry.set(
                "queue_depth", rt.store.count(state=TaskState.QUEUED), state="queued"
            )
            metrics_registry.set(
                "queue_depth",
                sum(
                    rt.store.count(state=state)
                    for state in ACTIVE_STATES
                    if state != TaskState.QUEUED
                ),
                state="active",
            )
            metrics_registry.set(
                "worker_inflight", rt.worker.inflight if rt.worker is not None else 0
            )
        except Exception:  # noqa: BLE001 指标采集失败不能把 /metrics 变成 500 噪音
            logger.exception("采集队列指标失败")
        return PlainTextResponse(metrics_registry.render(), media_type="text/plain; version=0.0.4")

    # ---------------- 只读任务页（默认关闭，ADMIN_TOKEN 保护） ----------------
    #
    # v1 默认不暴露任何任务信息；只有显式配置了 ADMIN_TOKEN 才打开。
    # 这样"升级镜像"不会意外把仓库名/issue 标题/失败堆栈暴露出去。

    ADMIN_COOKIE = "mgb_admin"

    def _supplied_token(request: Request) -> str:
        return (
            request.headers.get("x-admin-token")
            or request.cookies.get(ADMIN_COOKIE)
            or request.query_params.get("token")
            or ""
        )

    def _admin_denied(request: Request) -> JSONResponse | None:
        """None = 通过；否则返回应当直接返回的响应。"""
        expected = rt.admin_token
        if not expected:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not hmac.compare_digest(_supplied_token(request), expected):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return None

    def _cookie_redirect(request: Request) -> RedirectResponse | None:
        """用 `?token=` 首次访问时，换成一个 HttpOnly Cookie 再 303 掉。

        目的：token 只出现在一行访问日志里（登录那一下），之后不再进 URL / 浏览器历史。
        """
        if not request.query_params.get("token") or request.cookies.get(ADMIN_COOKIE):
            return None
        target = request.url.path
        extra = {key: value for key, value in request.query_params.items() if key != "token"}
        url = f"{target}?{urlencode(extra)}" if extra else target
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            ADMIN_COOKIE,
            rt.admin_token,
            httponly=True,
            samesite="strict",
            path="/",  # `/tasks.json` 不在 `/tasks` 前缀下，所以用根路径
        )
        return response

    def _list_for(state: str, limit: int):
        wanted = TaskState(state) if state else None
        return rt.store.list_tasks(state=wanted, limit=limit)

    @app.get("/tasks")
    async def tasks_page(request: Request) -> Any:
        denied = _admin_denied(request)
        if denied is not None:
            return denied
        redirect = _cookie_redirect(request)
        if redirect is not None:
            return redirect
        state = request.query_params.get("state", "").strip()
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
            tasks = _list_for(state, limit)
        except ValueError:
            return JSONResponse({"detail": "invalid state or limit"}, status_code=400)
        filters = TaskFilters(state=state, limit=limit)
        return HTMLResponse(
            render_tasks_page(
                tasks,
                filters=filters,
                token=rt.admin_token,
                refresh_sec=10,
                now=rt.clock(),
            )
        )

    @app.get("/tasks.json")
    async def tasks_json(request: Request) -> Any:
        denied = _admin_denied(request)
        if denied is not None:
            return denied
        redirect = _cookie_redirect(request)
        if redirect is not None:
            return redirect
        state = request.query_params.get("state", "").strip()
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
            tasks = _list_for(state, limit)
        except ValueError:
            return JSONResponse({"detail": "invalid state or limit"}, status_code=400)
        return JSONResponse({"tasks": [task_to_dict(task) for task in tasks]})

    @app.get("/tasks/{task_id}")
    async def task_detail(request: Request, task_id: str) -> Any:
        denied = _admin_denied(request)
        if denied is not None:
            return denied
        redirect = _cookie_redirect(request)
        if redirect is not None:
            return redirect
        task = rt.store.get(task_id)
        if task is None:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # 成功任务不会写 task.log_path，但 codex 日志按约定落在 <LOG_DIR>/<task>/codex.jsonl
        log_path = task.log_path or str(settings.log_dir / task.id / "codex.jsonl")
        log_tail, log_lines = _read_log_tail(log_path)
        return HTMLResponse(
            render_task_detail(task, token=rt.admin_token, log_tail=log_tail, log_lines=log_lines)
        )

    @app.post("/webhook/gitlab")
    async def gitlab_webhook(request: Request) -> JSONResponse:
        try:
            raw = await request.body()

            if not verify_token(request.headers.get("x-gitlab-token"), settings.webhook_secret):
                logger.warning("webhook 校验失败：X-Gitlab-Token 不匹配或 WEBHOOK_SECRET 未配置")
                return JSONResponse({"status": "unauthorized"}, status_code=401)

            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                logger.warning("webhook 请求体不是合法 JSON（%d 字节）", len(raw))
                return JSONResponse({"status": "error", "reason": "invalid_json"})

            parsed = parse_webhook(
                headers=request.headers, payload=payload, settings=settings, raw_body=raw
            )
            if parsed.trigger is None:
                return JSONResponse({"status": "ignored", "reason": parsed.reason})

            trigger = parsed.trigger
            decision = await checker.check(
                project_id=trigger.project.project_id,
                user_id=trigger.actor.user_id,
                username=trigger.actor.username,
            )
            if not decision.allowed:
                logger.warning(
                    "触发被权限闸门拒绝：user=%s project=%s issue=#%s reason=%s",
                    trigger.actor.username,
                    trigger.project.path,
                    trigger.issue_iid,
                    decision.reason,
                )
                return JSONResponse(
                    {"status": "ignored", "reason": f"permission_denied:{decision.reason}"}
                )

            outcome = rt.store.enqueue(trigger, now=rt.clock(), max_attempts=settings.max_attempts)
            logger.info(
                "入队结果 %s：task=%s project=%s issue=#%s actor=%s",
                outcome.reason,
                outcome.task.id if outcome.task else None,
                trigger.project.path,
                trigger.issue_iid,
                trigger.actor.username,
            )
            return JSONResponse(
                {
                    "status": "queued" if outcome.created else "duplicate",
                    "reason": outcome.reason,
                    "task_id": outcome.task.id if outcome.task else None,
                }
            )
        except Exception:  # noqa: BLE001 绝不 5xx：异常只落库/落日志
            logger.exception("webhook 处理异常（按设计仍返回 200）")
            return JSONResponse({"status": "error"})

    return app


def _read_log_tail(log_path: str | None, *, limit: int = 40) -> tuple[str, int]:
    """读日志文件末尾若干行。路径来自数据库，可能是宿主机路径 —— 失败一律当"没有"。"""
    if not log_path:
        return "", 0
    candidate = Path(log_path)
    try:
        if not candidate.is_file():
            return "", 0
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    lines = text.splitlines()
    return "\n".join(lines[-limit:]), len(lines)


async def _has_stalled_task(runtime: Runtime, stale_sec: int) -> bool:
    """有非终态任务的租约过期超过 `stale_sec` → 视为卡死（看门狗没在工作）。"""
    cutoff = runtime.clock() - timedelta(seconds=stale_sec)
    for state in ACTIVE_STATES:
        for task in runtime.store.list_tasks(state=state, limit=50):
            if task.lease_expires_at is not None and task.lease_expires_at < cutoff:
                return True
    return False


#: uvicorn 目标。构造过程无副作用（`SqliteStore` 懒连接），所以导入即可用。
app = create_app()
