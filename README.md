# mini-gitlab-bot

内网 GitLab 的轻量仓库维护机器人：**Codex CLI 做 agent 内核，Python 网关做触发器/收尾，Renovate 管依赖升级**。

收到带 `ai-fix` 标签的 Issue 后自动闭环：抓取 Issue → 回帖"已接单" → 建分支 → Codex 自主修复 → 网关 push → 开 **draft MR** → 人工审批合并。

## 架构

```
GitLab Issue(ai-fix 标签)
   │  Webhook
   ▼
Python 网关（常驻容器）──────────────────┐
   │  唤醒                               │
   ▼                                    │
Codex CLI（codex exec --sandbox workspace-write）
   │  ① glab MCP 抓 Issue + 回帖          │
   │  ② 建分支、修代码、跑测试、commit     │
   ▼                                    │
网关收尾：git push → 创建 draft MR → 回帖  │
   ▼                                    │
GitLab：CI → 人工审批 → 合并 ◄────────────┘

Renovate（独立容器）：定时扫描依赖，单独开 MR，互不干扰。
```

关键设计：

- **Codex 通过 MCP 拿 GitLab 工具**（`glab mcp serve`，stdio），shell 沙箱保持 `workspace-write`（无网络、写限工作目录）——MCP 进程不受 shell 沙箱网络限制，token 只被 glab 使用。
- **网关只管三件事**：接 Webhook 校验入队、唤醒 `codex exec`、收尾 push + 开 draft MR + 回帖。
- **人工兜底**：永远只开 draft MR，合并必须人工审批。

## 快速开始

### 1. 准备（GitLab 侧）

1. 建机器人账号（如 `gitlab-bot`），角色给目标项目 **Developer**（不要给管理员）。
2. 生成 Personal Access Token，scope 勾选 `api`。
3. 目标项目加标签 `ai-fix`。
4. 项目 → Settings → Webhooks → 添加：
   - URL：`http://<bot容器>:8080/webhook`
   - Secret token：与 `WEBHOOK_SECRET` 一致
   - 勾选触发事件 **Issues events**

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env：GITLAB_URL / GITLAB_TOKEN / WEBHOOK_SECRET / MODEL_NAME 等
```

内网模型端点与 glab MCP 配置见 `codex/config.toml`（挂载到容器内 `~/.codex/config.toml`）。

### 3. 启动

```bash
docker compose up -d --build
curl localhost:8080/health   # → {"status":"ok"}
```

### 4. 验证

在任意白名单项目里建一个带 `ai-fix` 标签的 Issue，观察：

```
Issue 出现 bot 回帖"已接单" → 分支 fix/issue-N 出现 → draft MR 创建（Closes #N）→ 回帖 MR 链接
```

## 配置项（环境变量）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `WEBHOOK_SECRET` | GitLab Webhook 的 Secret token | `change-me` |
| `GITLAB_URL` | GitLab 实例地址 | `https://gitlab.example.com` |
| `GITLAB_TOKEN` | 机器人 PAT（scope: api） | — |
| `REPO_WHITELIST` | 允许处理的项目，逗号分隔；空 = 全部 | — |
| `AI_FIX_LABEL` | 触发标签 | `ai-fix` |
| `TARGET_BRANCH` | MR 目标分支 | `main` |
| `MODEL_PROVIDER` | Codex 的 model_provider（对应 config.toml） | `internal` |
| `MODEL_NAME` | 模型名（如 `qwen3-coder`） | — |
| `AGENT_CONCURRENCY` | 并发处理的 Issue 数 | `2` |
| `MAX_ITERATIONS` | Codex 修复失败后的重试轮数 | `3` |
| `AGENT_TIMEOUT_SEC` | 单次 codex 会话超时 | `1800` |
| `RENOVATE_TOKEN` | Renovate 的机器人 token | — |

## 安全边界（务必阅读）

1. **沙箱**：`codex exec --sandbox workspace-write`——agent 无网络、只能写工作目录；GitLab 操作全部走 glab MCP 进程。
2. **Token 最小权限**：机器人账号只加目标项目 Developer；token scope 只用 `api`，绝不使用管理员。
3. **人工审批**：只开 draft MR；建议配合 GitLab 审批规则 + 受保护分支，禁止 bot 直接合并。
4. **代码审查**：MR 描述固定带 `Closes #N`，合并前人工过 diff。
5. **不要**：让 Codex 使用 `--sandbox danger-full-access`（等于把容器交给 agent）。

## Renovate（依赖升级）

```bash
docker compose up -d renovate
```

定时扫描依赖并自动开 MR；需要自动合并时在 `renovate.json` 配 `automerge: true`（配合 GitLab MWPS）。

## 开发

```bash
pip install -r requirements.txt
uvicorn gateway.app:app --reload --port 8080
```

任务状态存 SQLite（`/data/tasks.db`），崩溃后自动去重恢复。

## License

Apache License 2.0
