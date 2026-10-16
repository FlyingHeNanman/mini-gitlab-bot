# mini-gitlab-bot

内网自托管 GitLab 的轻量仓库维护机器人：**Codex CLI 做 AI agent 内核，Python 网关做触发器 / 上下文准备 / 收尾编排**。

给 Issue 打上 `ai-fix` 标签即自动闭环：入队 → 准备环境 → Codex 自主修复 → 网关校验 → push → 开 **draft MR** → 人工审批合并。

> ⚠️ **状态：架构已定案，代码待重写。**
> 仓库内的 `gateway/` 是 v0 骨架（可作参考，存在已实测的静默失效缺陷）。
> **设计的唯一事实来源是 [PLAN.md](PLAN.md)**，事实核查依据在 [docs/research/](docs/research/README.md)。

## 架构

```
GitLab Issue ── 打上 ai-fix 标签（或评论 /ai-fix）
   │  Webhook
   ▼
Python 网关（常驻单容器）────────────────────────────────────┐
   │  校验/幂等入队 → 建分支 clone → 准备环境(uv, 联网)        │
   │  → 预取 context.md → 唤醒 codex → 校验 diff → push        │
   │  → 开 draft MR → 状态评论 → 标签流转                       │
   ▼                                                         │
Codex CLI（沙箱 workspace-write：零网络、零凭据、只读写 workdir）│
   │  读 context.md、改代码、跑测试、git commit                │
   │  需要更多上下文 → 调「只读迷你 MCP」（限当前项目）          │
   ▼                                                         │
GitLab：CI → 人工审批 → 合并 ◄───────────────────────────────┘
```

## 关键设计

- **agent 零凭据、零网络**：sandbox 用 `workspace-write`（网络默认关闭，写限工作目录），且不向 agent 暴露任何 token —— 这样即使 issue 正文携带 prompt injection，也没有外泄通道。
- **网关独占 GitLab 读写**：单一写者，审计清晰；正确方向是"开只读的洞，把写权限攥在手里"。
- **只读迷你 MCP（自研）**：给 agent 4 个只读工具（读 issue/搜历史/读 CI 日志/查近期提交），**工具 schema 里没有 project 参数**（项目由网关注入，物理上无法跨项目），并使用独立的 `read_api` token。不采用 `glab mcp serve`（官方已弃用、217 个工具、无只读模式）。
- **两阶段执行**：装依赖在网关侧（可访问内网 kkRepo），跑 agent 时完全离线（`UV_OFFLINE=1 uv run --no-sync`）。
- **人工兜底**：永远只开 draft MR，合并必须人工审批。
- **失败不静默**：状态用单条评论原地编辑，标签流转 `ai-fix:running` → `ai-fix:done` / `ai-fix:failed`。

## 快速开始（GitLab 侧准备）

1. 建机器人账号（如 `gitlab-bot`），角色给目标项目 **Developer**（不要给管理员）。
2. 生成 Personal Access Token：
   - 主 token：scope `api`（网关读写）
   - 只读 token：scope `read_api`（只读迷你 MCP 用）
3. 目标项目加标签：`ai-fix`、`ai-fix:running`、`ai-fix:done`、`ai-fix:failed`。
4. 项目 → Settings → Webhooks → 添加：
   - URL：`http://<bot容器>:8080/webhook`
   - Secret token：与 `WEBHOOK_SECRET` 一致
   - 勾选 **Issues events**（若要用评论命令，再勾 **Comments**；要用机密 issue 再勾 Confidential work items events）

## 配置项（环境变量）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `WEBHOOK_SECRET` | GitLab Webhook 的 Secret token | `change-me` |
| `GITLAB_URL` | GitLab 实例地址 | `https://gitlab.example.com` |
| `GITLAB_TOKEN` | 主 token（scope: `api`） | — |
| `GITLAB_READ_TOKEN` | 只读 token（scope: `read_api`，供迷你 MCP） | — |
| `REPO_WHITELIST` | 允许处理的项目，逗号分隔；空 = 全部 | — |
| `AI_FIX_LABEL` | 触发标签 | `ai-fix` |
| `TARGET_BRANCH` | MR 目标分支 | `main` |
| `MODEL_PROVIDER` | Codex 的 model_provider（对应 config.toml） | `internal` |
| `MODEL_NAME` | 模型名 | — |
| `UV_DEFAULT_INDEX` | 内网 kkRepo 源 | — |
| `AGENT_CONCURRENCY` | 并发处理的 Issue 数 | `2` |
| `AGENT_TIMEOUT_SEC` | 单次 codex 会话超时 | `1800` |

> `wire_api` 必须为 `responses`（`chat` 已是 Codex 硬错误）。

## 安全边界（务必阅读）

1. **沙箱**：只用 `codex exec --sandbox workspace-write`；**禁止** `danger-full-access`。
2. **agent 零凭据零网络**：token 只存在于网关进程；安全关键配置全部用 `-c` 下发（优先级高于仓库自带配置）。
3. **只读 token 物理隔离**：迷你 MCP 只有 `read_api`，即使被利用也无法写。
4. **人工审批**：只开 draft MR；配合 GitLab 审批规则 + 受保护分支，禁止 bot 直接合并。
5. **必须落实**：启动前删除 workdir 里的 `.codex/`；每个 job 独立 `TMPDIR`；永不把克隆目录标记为 trusted；pin 死 codex 版本。

## 开发

```bash
pip install -r requirements.txt
uvicorn gateway.app:app --reload --port 8080
```

主干将按**端口 / 适配器**重构（`core/` `ports/` `adapters/`），以便用假 SCM + 假 agent 在 CI 中端到端测试。任务状态存 SQLite。

## License

Apache License 2.0
