# =============================================================================
# mini-gitlab-bot 运行镜像（M5 重写；v0 遗留版本已删除）
# =============================================================================
# 设计要点：
#   * 版本全部 pin 死（PLAN §7 基线 6）：Python / uv / codex / node
#   * 不给 agent 装 glab（决策 A3：不用 glab mcp serve，工具面由自研 MCP 收口）
#   * 非 root 运行（uid 10001），只挂 /data 与 /work
#   * 依赖用 `uv sync --frozen`（uv.lock 是唯一事实来源），分两步以便复用缓存层
#   * HEALTHCHECK 打 /healthz（存活）；/readyz 给编排层与监控用
#   * 需要两个卷：/data（SQLite + 日志）、/work（job 目录 + uv 缓存）
# =============================================================================

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.5.14
ARG CODEX_VERSION=0.163.0
ARG NODE_VERSION=20

# ---------- codex CLI（npm 全局包；单独一层，避免把整条 node 工具链塞进运行时） ----------
FROM node:${NODE_VERSION}-slim AS codex
ARG CODEX_VERSION
RUN npm install -g --no-fund --no-audit "@openai/codex@${CODEX_VERSION}" \
    && codex --version

# ---------- 运行时 ----------
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG UV_VERSION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    LOG_DIR=/data/logs \
    UV_CACHE_DIR=/work/.uv-cache

# git 是准备阶段 clone/commit/push 的刚需；ca-certificates 用于内网 TLS 端点
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv（pin）与 codex（pin）；node 只给 codex 的 npm 包装用
COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION} /uv /uvx /usr/local/bin/
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex

RUN useradd --system --uid 10001 --home-dir /home/bot --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app /data/logs /work \
    && chown -R bot:bot /app /data /work

WORKDIR /app

# 先只装依赖：改业务代码不会让这一层失效
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 再拷源码（本仓库不是可安装包，所以 --no-install-project 就够了）
COPY core ./core
COPY ports ./ports
COPY adapters ./adapters
COPY gateway ./gateway

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/bot

USER bot

EXPOSE 8080

# 存活探针：进程能响应即 200（不暴露任务信息，见 gateway/app.py）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]

# 用 python -m 启动，确保用镜像里的 venv；访问日志交给结构化配置
CMD ["python", "-m", "uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
