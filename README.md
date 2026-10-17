# mini-gitlab-bot

内网自托管 GitLab 的轻量仓库维护机器人：**Codex CLI 做 AI agent 内核，Python 网关做触发器 / 上下文准备 / 收尾编排**。

给 Issue 打上 `ai-fix` 标签即自动闭环：入队 → 准备环境 → Codex 自主修复 → 网关校验 → push → 开 **draft MR** → 人工审批合并。

> **状态：M2（Agent 链路）已完成** —— 在 M1（主干 / 触发器 / 持久队列 / 崩溃恢复）之上接入了
> **真实适配器**：`GitCli`（clone → 建分支 → 兜底 commit → diff → push，凭据只走 `GIT_ASKPASS`）、
> `UvEnvPreparer`（网关侧联网 `uv sync` + 离线自检）、`CodexCli`（`-c` 钉死安全项、删 `.codex/`、
> `--json` 落盘、四条件判定、超时杀进程组）、`LocalWorkspace`（job 目录内的原子写 + 独立 `TMPDIR`）。
> `uv run pytest` → **278 项全绿**（含真 git 本地仓库、真 uv 离线 `uv sync`、桩 codex 的端到端链路）。
> **设计的唯一事实来源是 [PLAN.md](PLAN.md)**，事实核查依据在 [docs/research/](docs/research/README.md)，
> 文案模板在 [docs/templates.md](docs/templates.md)。
>
> 尚未落地：**真 GitLab 读写**（M3：push 之后的开 MR / 状态评论 / 标签流转仍是 `UnavailableSCM` 占位）、
> 只读迷你 MCP（M4）、镜像加固（M5）。也就是说：现在**能真 clone、真跑 codex、真 push 分支**，
> 但收尾会以"适配器未实现"明确失败并留痕（绝不静默）。
>
> **诚实声明**：CI / 开发机没有 codex 二进制、没有 LLM 端点，所以"真 agent 至少一次端到端成功"这条
> M2 判据**无法在本仓库内验证**；测试用的是一个桩 codex（真子进程、真 JSONL、真超时），
> 它验证的是**网关这一侧的契约**（argv、环境、四条件、日志、凭据不外泄）。

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
- **只读迷你 MCP（自研）**：给 agent 11 个只读工具（issue/评论/仓库树/文件内容/提交/分支/MR/diff/CI 作业与日志，工具清单见 [PLAN.md](PLAN.md) §3.3），**工具 schema 里没有 project 参数**（项目由网关注入，物理上无法跨项目），并使用独立的 `read_api` token。不采用 `glab mcp serve`（官方已弃用、217 个工具、无只读模式）。
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
   - URL：`http://<bot容器>:8080/webhook/gitlab`
   - Secret token：与 `WEBHOOK_SECRET` 一致
   - 勾选 **Issues events**（若要用评论命令，再勾 **Comments**；要用机密 issue 再勾 Confidential work items events）
5. 健康检查：`GET /healthz`（只回 `{"status":"ok"}`，故意不暴露任何任务信息）。

## 配置项（环境变量）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `WEBHOOK_SECRET` | GitLab Webhook 的 Secret token；**未配置时一律 401（fail closed）** | 空 |
| `GITLAB_URL` | GitLab 实例地址 | `https://gitlab.example.com` |
| `GITLAB_TOKEN` | 主 token（scope: `api`） | — |
| `GITLAB_READONLY_TOKEN` | 只读 token（scope: `read_api`，供迷你 MCP，M4 使用） | — |
| `REPO_WHITELIST` | 允许处理的项目，逗号分隔；**空 = 全部拒绝（fail closed）** | 空 |
| `AI_FIX_LABEL` | 触发标签 | `ai-fix` |
| `BOT_USERNAME` | 机器人自己的用户名，用于忽略自己产生的事件 | 空 |
| `ALLOWED_TRIGGER_USERS` | 可选额外白名单，逗号分隔（与角色下限是 AND；成员查不到时的逃生门） | 空 |
| `MIN_ACCESS_LEVEL` | 触发者角色下限（Developer = 30） | `30` |
| `MEMBER_CACHE_TTL_SEC` | 成员权限查询缓存秒数 | `60` |
| `MODEL_PROVIDER` / `MODEL_NAME` | Codex 的 model_provider / 模型名 | `internal` / — |
| `CODEX_BIN` / `API_KEY_ENV` | codex 可执行文件 / 模型 key 所在的**变量名**（值只进子进程环境） | `codex` / `LLM_API_KEY` |
| `CODEX_EXPECTED_VERSION` | 期望的 codex 版本；不符时启动告警（PLAN §7 基线 6） | 空 |
| `CODEX_CONFIG` | 追加的 `-c key=value`，逗号分隔（如内网 `base_url` / `wire_api="responses"`） | 空 |
| `GIT_BIN` / `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | git 可执行文件与提交身份 | `git` / `mini-gitlab-bot` / `mini-gitlab-bot@localhost` |
| `GIT_CLONE_DEPTH` / `GIT_TIMEOUT_SEC` | 浅克隆深度（0 = 完整）/ 单条 git 命令超时 | `0` / `600` |
| `UV_BIN` / `UV_CACHE_DIR` | uv 可执行文件 / 缓存目录（空 = `WORK_DIR/.uv-cache`，必须在容器卷内） | `uv` / 空 |
| `UV_DEFAULT_INDEX` / `UV_SYNC_TIMEOUT_SEC` | 内网 kkRepo 源 / `uv sync` 超时 | — / `1800` |
| `LOG_DIR` | codex / uv 日志目录（`<LOG_DIR>/<task_id>/`，留 14 天） | `/data/logs` |
| `AGENT_CONCURRENCY` | 并发处理的 Issue 数 | `2` |
| `AGENT_TIMEOUT_SEC` | 单次 codex 会话超时 | `1800` |
| `LEASE_SECONDS` / `MAX_ATTEMPTS` | 任务租约与最大重试次数（崩溃/超时恢复） | `60` / `3` |
| `DB_PATH` / `WORK_DIR` | SQLite 队列位置 / 任务工作目录根 | `/data/tasks.db` / `/work` |
| `DIFF_MAX_FILES` / `DIFF_MAX_LINES` | 单次任务允许的改动规模上限（超出则拒绝推送） | `50` / `2000` |
| `FORBIDDEN_PATHS` | 禁改路径，逗号分隔（CI 配置、密钥等） | 见 `.env.example` |

> `TARGET_BRANCH` 已删除：MR 目标分支改为按项目取 `default_branch`。

## 安全边界（务必阅读）

1. **沙箱**：只用 `codex exec --sandbox workspace-write`；**禁止** `danger-full-access`。
2. **agent 零凭据零网络**：token 只存在于网关进程；安全关键配置全部用 `-c` 下发（优先级高于仓库自带配置）。
3. **只读 token 物理隔离**：迷你 MCP 只有 `read_api`，即使被利用也无法写。
4. **人工审批**：只开 draft MR；配合 GitLab 审批规则 + 受保护分支，禁止 bot 直接合并。
5. **必须落实（M2 已全部落地，均有测试锁定）**：启动前删除 workdir 里的 `.codex/`；每个 job 独立 `TMPDIR`；永不把克隆目录标记为 trusted；pin 死 codex 版本。
6. **任务本地产物绝不进提交**：`.git/info/exclude`（第一层）+ `git add` 之后的后置校验（第二层，发现即失败）—— `.mini-gitlab-bot/`、`.venv/`、`__pycache__/` 都不可能进 MR。
7. **失败要能解释**：每个失败任务都记录 `failure_stage` / `failure_reason` / `failure_evidence` / `log_path`，并把日志尾部折叠进 issue 评论。

## 开发

```bash
# Python 3.13（uv 管理）
export UV_CACHE_DIR=$PWD/.uv-cache        # 沙箱/容器里缓存必须可写
uv sync --group dev
uv run ruff check . && uv run ruff format --check .
uv run pytest                              # 无需 GitLab、无需 LLM：全用假适配器

# 本地起网关（当前是占位适配器，能启动、webhook 立刻 2xx）
WEBHOOK_SECRET=dev REPO_WHITELIST=group/project-a BOT_USERNAME=mini-gitlab-bot \
DB_PATH=$PWD/.dev/tasks.db WORK_DIR=$PWD/.dev/work \
  uv run uvicorn gateway.app:app --port 8080
```

分层约定（必须遵守，[PLAN.md](PLAN.md) §2）：

- `core/` 纯逻辑，不 import 任何 IO；状态机、分支命名、diff 策略、文案渲染、编排流程都在这里。
- `ports/` 只有 `Protocol`；`adapters/` 是真实现 + 假实现（**假实现也是契约的一部分**）。
- `gateway/` 只做 HTTP / 配置 / 权限 / 装配，不含业务判断。

测试策略分三层：

- **单元**：状态机、解析规则、配置、脱敏、Python 版本规划、workspace 越界防护。
- **集成（假件）**：`FakeSCM + FakeVCS + FakeAgent + FakeWorkspace` 把整条流水线（含失败路径、租约恢复、崩溃重启）跑穿。
- **集成（真件，M2 起）**：`test_git_cli`（本地 bare 仓真 clone/commit/diff/push）、
  `test_pipeline_real_adapters`（真 git + 真 `uv sync` + **桩 codex** 端到端）、
  `test_codex_cli`（桩 codex 真子进程：四条件、环境白名单、超时杀进程组）。
  桩 codex 之所以必须存在：CI 里没有 codex 二进制也没有 LLM 端点，但**网关侧的契约**必须被真子进程验证。

## License

Apache License 2.0
