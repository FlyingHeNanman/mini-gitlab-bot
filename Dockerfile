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

# ---------- uv（pin 版本，单独一层；Docker 不支持在 --from 里用变量，所以先定义成 stage） ----------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

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
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
# 注意：要自己建符号链接。直接 COPY 那个 symlink 会被解引用成普通文件，
# 里面的相对 import 就找不到包了（实测 `EsmLoader` 报错）
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

RUN useradd --system --uid 10001 --home-dir /home/bot --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app /data/logs /work \
    && chown -R bot:bot /app /data /work

WORKDIR /app

# 先只装依赖：改业务代码不会让这一层失效。
# 注意：构建期的 uv 缓存要放到 /tmp —— 否则会在 /work 留下 root 所有的缓存，
# 运行期 bot（uid 10001）写不进去（实测：`Permission denied .../sdists-v6/.git`）
COPY pyproject.toml uv.lock ./
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --frozen --no-dev --no-install-project

# 再拷源码（本仓库不是可安装包，所以 --no-install-project 就够了）
# --chown + chmod：宿主机上文件可能是 600，非 root 用户会读不了（实测踩过）
COPY --chown=bot:bot core ./core
COPY --chown=bot:bot ports ./ports
COPY --chown=bot:bot adapters ./adapters
COPY --chown=bot:bot gateway ./gateway
RUN chmod -R a+rX /app \
    && chown -R bot:bot /app /data /work

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/bot

USER bot

EXPOSE 8080

# 存活探针：进程能响应即 200（不暴露任务信息，见 gateway/app.py）
# 探针与启动都读 PORT（默认 8080），这样换端口不用重建镜像
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3).status == 200 else 1)"

# 用 python -m 启动，确保用镜像里的 venv；`exec` 让 uvicorn 接住信号（PID 1）
CMD ["sh", "-c", "exec python -m uvicorn gateway.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
