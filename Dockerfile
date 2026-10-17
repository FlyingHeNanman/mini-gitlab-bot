# ⚠️ v0 遗留文件，**M5 会重写**（PLAN §9）。当前缺陷，请勿直接部署：
#   * 基础镜像是 python:3.12-slim，而本项目要求 3.13（PLAN §10 假设 4）
#   * 没有装 uv（M2 的准备阶段 `uv sync` 依赖它）
#   * 装了 glab（PLAN 已决定不用 glab MCP，M5 删除）
#   * 没有建 /data/logs（M2 的 LOG_DIR 默认值），日志会被写进容器可写层
#   * `COPY gateway ./gateway` 没有带上 core/ ports/ adapters/，启动会 ImportError
#   * codex 版本未 pin（PLAN §7 基线 6 要求 pin 死）
# 现阶段开发/联调请在宿主上用 `uv run uvicorn ...`（见 README「开发」）。
FROM python:3.12-slim

# 基础工具
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# glab（GitLab CLI，提供 glab mcp serve）。内网无外网时改为从内网制品库下载。
# 资产名/版本以 https://gitlab.com/gitlab-org/cli/-/releases 为准
ARG GLAB_VERSION=1.76.2
RUN mkdir -p /tmp/glab-src \
 && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.tar.gz" -o /tmp/glab.tgz \
 && tar -xzf /tmp/glab.tgz -C /tmp/glab-src \
 && find /tmp/glab-src -type f -name glab -exec mv {} /usr/local/bin/glab \; \
 && chmod +x /usr/local/bin/glab \
 && rm -rf /tmp/glab-src /tmp/glab.tgz

# Codex CLI（node 环境）。内网无外网时改为内网 npm 镜像
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y nodejs \
 && npm install -g @openai/codex \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway ./gateway
COPY codex/config.toml /root/.codex/config.toml
RUN mkdir -p /data /work

EXPOSE 8080
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
