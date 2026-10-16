# mini-gitlab-bot 交接文档

> 用途：记录「内网 GitLab 仓库维护机器人」头脑风暴的完整结论、决策依据、技术事实与当前待办，供后续接手者快速接管。
> 更新日期：2026-10-16　状态：**骨架已建，端到端验证未完成**

---

## 1. 项目一句话

内网自托管 GitLab 的轻量仓库维护机器人：**Codex CLI 做 AI agent 内核，Python 网关做触发器/收尾编排，Renovate 独立管依赖升级**。带 `ai-fix` 标签的 Issue 自动闭环：抓取 → 回帖"已接单" → 建分支 → Codex 自主修复 → 网关 push → 开 **draft MR** → 人工审批合并。

## 2. 背景调研结论（起点：kkRepo 机制拆解）

调研对象 [klboke/kkRepo](https://github.com/klboke/kkRepo)，验证其"为什么 PR/issue 处理这么快"：

| 环节 | 实际机制 | 证据 |
| --- | --- | --- |
| Issue 回复 | **作者本人**（klboke 账号），非机器人 | Issue #414/#400 评论作者为 OWNER |
| PR 审查 | **OpenAI Codex bot**（`chatgpt-codex-connector[bot]`） | PR #415 有 "Codex Review Summary" 评论 |
| 依赖 PR | Dependabot + GitHub 原生 auto-merge | PR #406 合并者 klboke，自动合并 |
| 快速手动合并 | 105 项 CI 全绿支撑下的低风险合并 | `.github/workflows` 12+ 个 |
| 工程化基建 | `.github/agents/`（security-reviewer 等 4 个 agent）、prompts、ISSUE_TEMPLATE(agent_task.yml) | 仓库 `.github/` 目录 |

**结论**：不是全自动，而是「机器人干脏活 + 作者低风险快速拍板」的组合。

## 3. 最终架构定案（用户已拍板，勿再推翻）

```
GitLab Issue (ai-fix 标签)
   │ Webhook
   ▼
Python 网关（常驻单容器）─────────────────────┐
   │ ① 校验/幂等入队                           │
   │ ② 唤醒 codex exec --sandbox workspace-write │
   ▼                                          │
Codex CLI（自主 agent 会话）
   │ ① glab MCP 抓 Issue + 回帖"已接单"        │
   │ ② 建分支 fix/issue-N、写代码、跑测试、commit│
   ▼                                          │
网关收尾：git push → 创建 draft MR（Closes #N）→ 回帖 MR 链接
   ▼                                          │
GitLab：CI → 人工审批 → 合并 ◄────────────────┘

Renovate（独立容器）：定时扫描依赖，单独开 MR，互不干扰
```

**分工边界（严格遵守）**：

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| Python 网关 | Webhook 校验、幂等、状态机、唤醒 codex、**收尾 push + 开 draft MR + 回帖** | 不写业务代码 |
| Codex CLI | 抓 issue、回帖、建分支、写代码、跑测试、commit | 不 push、不开 MR（shell 沙箱禁网也做不了） |
| Renovate | 依赖升级 MR | 不碰业务代码 |
| 人工 | 审批 draft MR、处理失败 issue | —— |

**关键决策记录**：

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| Agent 内核 | **Codex CLI**（否决 OpenHands） | OpenHands 太重（前端+沙箱+编排）；轻量优先 |
| 编排层 | Python 网关（FastAPI + SQLite + 线程池） | 最小实现，核心 3 个动作 |
| 依赖升级 | **Renovate 独立容器**（不重复造轮子） | 替代 Dependabot，docker 一行起 |
| GitLab 工具通道 | **glab mcp serve（MCP）** | 沙箱禁网下唯一"自主+安全"通道；自托管通用 |
| 沙箱档位 | `workspace-write`（无网络、写限工作目录） | 绝不开放 danger-full-access |
| MR 形态 | 永远 **draft MR + 人工审批** | 人工兜底原则 |
| 部署形态 | 常驻单容器（docker compose: bot + renovate） | 用户明确不要"每次拉起容器" |
| 命名 | `mini-gitlab-bot`（备选曾考虑 issue-doctor） | 用户拍板 |
| 仓库可见性 | 私有（后续可公开） | 用户拍板 |
| License | Apache-2.0 | 开源路线 |

## 4. 关键技术事实（均已核实，接手直接用）

1. **`codex exec --full-auto` / `--sandbox workspace-write` 是官方 headless 模式**：无 TUI、prompt 进结果出，适合脚本/CI；`workspace-write` 沙箱 = 网络禁用 + 写入仅限工作目录。
2. **MCP 进程不受 shell 沙箱网络限制**：Codex 调 MCP 工具时，网络请求由 MCP server 进程发出——这是 Codex 能"自主操作 GitLab 且保持沙箱安全"的唯一通道。
3. **`glab mcp serve` 是客户端 MCP，与 GitLab 服务器是否支持 MCP 无关**：glab 用普通 REST API（/api/v4/）与 GitLab 通信，自托管任何版本/CE/EE 通用，只需机器人 PAT。
4. **GitLab 官方 MCP endpoint（/api/v4/mcp）内网大概率走不通**：需 GitLab Duo + beta/experimental 特性，且 Duo 要求连 GitLab 云 AI Gateway——官方 issue [#587846](https://gitlab.com/gitlab-org/gitlab/-/work_items/587846) 承认这是 air-gapped 客户障碍。**不要走这条路**。
5. **Codex CLI 可接任意 OpenAI 兼容内网端点**：`config.toml` 的 `[model_providers.<name>]` + `base_url`（Ollama/vLLM/NIM/Azure 均有官方文档）。
6. **glab 动态内省全部 glab 命令为 MCP 工具**：抓 issue、加 note、操作 MR 等 schema 自动生成；首次运行让 Codex `tools/list` 确认工具名。
7. **Renovate 自托管**：`docker run renovate/renovate --platform gitlab --endpoint <gitlab>/api/v4/ --token <bot-token> --autodiscover`；`automerge: true` 配合 GitLab MWPS。
8. **命名雷区**：`glabot` 已被他人注册商标（Trademarkia #8103150），已排除。

## 5. 项目结构（已落盘，14 个文件）

```
mini-gitlab-bot/
├── README.md            # 项目说明 + 快速开始 + 安全边界
├── LICENSE              # Apache-2.0
├── .gitignore           # 排除 .env / *.db / data/ / work/
├── requirements.txt     # fastapi / uvicorn / python-gitlab / pydantic
├── .env.example         # 全部环境变量模板
├── docker-compose.yml   # bot + renovate 双容器，常驻部署
├── Dockerfile           # python:3.12-slim + glab + Codex CLI
├── codex/
│   └── config.toml      # 内网模型 provider + [mcp_servers.glabs]
└── gateway/             # Python 网关（FastAPI）
    ├── app.py           # Webhook 入口：secret 校验→白名单→ai-fix 标签→入队
    ├── config.py        # 环境变量（Settings dataclass）
    ├── db.py            # SQLite 任务状态机（幂等 + 崩溃恢复）
    ├── gitlab_client.py # python-gitlab：读 issue/回帖/开 draft MR + git clone/push
    └── worker.py        # 唤醒 codex exec + 迭代重试 + 收尾 push/MR/回帖
```

已验证：Python 语法编译通过（py_compile SYNTAX_OK）；曾推送到 GitHub 并回读文件树完整。

## 6. 快速上手（端到端）

1. **GitLab 侧**：建机器人账号（目标项目 **Developer** 角色，勿用管理员）→ PAT scope `api` → 项目加标签 `ai-fix` → Webhook 配 `http://<bot>:8080/webhook` + Secret token + 勾选 **Issues events**。
2. **填配置**：`cp .env.example .env`（GITLAB_URL/GITLAB_TOKEN/WEBHOOK_SECRET/MODEL_NAME）；改 `codex/config.toml`（内网模型 base_url、GITLAB_HOST/GITLAB_TOKEN）。
3. **启动**：`docker compose up -d --build`；`curl localhost:8080/health` → `{"status":"ok"}`。
4. **验证**：建带 `ai-fix` 标签的 Issue，观察：bot 回帖"已接单" → 分支 `fix/issue-N` → draft MR（Closes #N）→ 回帖 MR 链接。

## 7. 安全边界（红线）

1. 沙箱只用 `workspace-write`；**禁止** `danger-full-access`（等于把容器交给 agent）。
2. 机器人 token 最小权限：专号专用、项目级 Developer、scope 仅 `api`。
3. 只开 draft MR；配合 GitLab 审批规则 + 受保护分支，禁止 bot 直接合并。
4. git clone/push 用 token 内嵌 URL，**不得打印到日志**（代码已 capture_output）。

## 8. 当前待办（重要！交接状态）

### 账号迁移进行中（2026-10-16 发现）
- [ ] **删除 Vickynsx/mini-gitlab-bot**（建错账号，用户手动：仓库 Settings → Danger Zone → Delete this repository）
- [ ] **FlyingHeNanman 下建空仓库 `mini-gitlab-bot`（Private）**，推送本地 14 个文件
  - 本地路径：`C:\Users\60346\DoubaoWork\chats\2026-10-16\new-chat-2\mini-gitlab-bot`
  - 命令：`git init && git add . && git commit -m "Initial commit" && git branch -M main && git remote add origin https://github.com/FlyingHeNanman/mini-gitlab-bot.git && git push -u origin main`

### 接入前必办
- [ ] 确认 GitLab REST API 连通：`curl -s -H "PRIVATE-TOKEN: <pat>" https://<gitlab>/api/v4/version`
- [ ] 确认内网模型端点（vLLM/Ollama/NIM/公司网关）与 `MODEL_NAME`
- [ ] Dockerfile 内网化：glab 二进制、npm registry 改内网制品库（当前走公网源）
- [ ] 确认项目语言栈/测试命令（worker 里测试步骤依赖仓库实际情况）

### 后续迭代候选
- [ ] 网关暴露 `git_push` / `create_draft_mr` 迷你 MCP 工具，让 Codex 全自主（当前收尾在网关）
- [ ] 失败 issue 升级机制（人工兜底已回帖，可加标签 `ai-fix-failed` 自动转人工队列）
- [ ] Webhook 重试幂等压测；`/tasks` 状态页接入监控

## 9. 参考资料

- glab MCP serve：https://docs.gitlab.com/cli/mcp/serve/index.md
- glab auth（自托管 GITLAB_HOST）：https://docs.gitlab.com/cli/auth/login/
- GitLab 官方 MCP server（Duo 前提）：https://docs.gitlab.com/user/gitlab_duo/model_context_protocol/mcp_server/
- 官方 MCP 内网障碍 issue：https://gitlab.com/gitlab-org/gitlab/-/work_items/587846
- Codex 沙箱三档：https://mintlify.wiki/openai/codex/concepts/sandboxing
- Codex exec headless/batch：https://codex.danielvaughan.com/2026/04/18/codex-cli-headless-batch-mode-automation/
- Codex CI/GitLab 自愈实践：https://codex.danielvaughan.com/2026/04/20/codex-cli-ci-autofix-self-healing-pipelines-github-actions-gitlab/
- Ollama + Codex 集成：https://docs.ollama.com/integrations/codex.md
- NIM + Codex 集成：https://docs.nvidia.com/nim/large-language-models/latest/ai-assistant-integrations/codex-cli.html
- Renovate 自托管：https://docs.renovatebot.com/examples/self-hosting/
- zereight/gitlab-mcp（Codex 集成示例）：https://github.com/zereight/gitlab-mcp
- kkRepo（调研对象）：https://github.com/klboke/kkRepo
