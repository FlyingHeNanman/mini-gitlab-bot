# mini-gitlab-bot 交接文档（v2 · 架构定案）

> 用途：记录「内网 GitLab 仓库维护机器人」的最终架构、决策依据、已验证技术事实与实施计划，供后续接手者直接开工。
> 更新日期：2026-10-17　状态：**v1 全部里程碑完成（M1–M5，417 项测试全绿）**；下一步是上线前的三项人工验证与镜像首建（§8.5 末段）；v0 骨架中被取代的部分已删除（§8.1）
> 事实核查详见 [`docs/research/`](docs/research/README.md)；本文只保留结论。

---

## 1. 项目一句话

内网自托管 GitLab 的轻量仓库维护机器人：**Codex CLI 做 AI agent 内核，Python 网关做触发器 / 上下文准备 / 收尾编排**。给 Issue 打上 `ai-fix` 标签即自动闭环：入队 → 准备环境 → Codex 自主修复 → 网关校验 → push → 开 **draft MR** → 人工审批合并。

## 2. 目标、成功标准与非目标

**成功标准**

1. 打上 `ai-fix` 标签 → 全程无人干预 → 出现带 `Closes #N` 的 draft MR
2. 全程可追溯：谁触发、agent 干了什么、为什么失败，都能查到
3. 失败不静默：issue 上有明确状态 + `ai-fix:failed` 标签 + 可人工接管
4. 绝不自动合并；agent 手里**没有任何凭据**、**没有任何网络**
5. 常驻单容器部署（不是每次拉起），崩溃后任务可恢复

**非目标**：通用 CI/CD、代码托管、多 agent 编排平台、Web UI（最多一个状态页）、非 Python 项目的环境管理。

## 3. 最终架构（定案，勿再推翻）

### 3.1 数据流

```
GitLab Issue ── 打上 ai-fix 标签（或评论 /ai-fix）
   │  ① Issue Hook: action=update + changes.labels（或 Note Hook）
   ▼
Python 网关（常驻单容器）──────────────────────────────────────┐
   │  ② 校验 secret / 白名单 / 评论者权限 → 入队（持久队列，立即 2xx）│
   │  ③ Worker 领取：建分支、clone 到独立 job 目录                  │
   │  ④ 准备阶段（网关联网）：uv python install / uv sync（kkRepo） │
   │  ⑤ 预取上下文 → workdir/context.md（issue 正文+评论+相关文件）  │
   │  ⑥ 唤醒 codex exec（沙箱 workspace-write，零网络，-c 钉死）    │
   ▼                                                            │
Codex CLI（只读写 workdir，零凭据零网络）                        │
   │  · 读 context.md、改代码、uv run --no-sync pytest、git commit │
   │  · 需要更多上下文 → 调「只读迷你 MCP」工具（限当前项目）        │
   ▼                                                            │
网关收尾：⑦ 校验 diff 策略 → ⑧ git push → ⑨ 开 draft MR（Closes #N）
        → ⑩ 原地编辑状态评论 → ⑪ 标签切 ai-fix:done / ai-fix:failed
   ▼
GitLab：CI → 人工审批 → 合并 ◄──────────────────────────────────┘
```

### 3.2 分工边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| **Python 网关** | Webhook 校验、幂等入队、状态机、**全部 GitLab 读写**、环境准备、上下文预取、diff 校验、push、开 draft MR、状态评论、标签流转 | 不写业务代码 |
| **Codex CLI** | 读上下文、定位问题、改代码、跑测试、`git commit` | **不 push、不开 MR、不碰 GitLab、不能出网** |
| **只读迷你 MCP**（本项目自研） | 给 agent 按需读**当前项目**的 issue/评论/CI 日志/近期提交 | 任何写操作、跨项目读取 |
| **Renovate** | —— | **本项目不包含**（见 §5 F-1） |
| 人工 | 审批 draft MR、处理 `ai-fix:failed` | —— |

### 3.3 只读迷你 MCP（决策 A3）规格

**为什么自研**：`glab mcp serve` 已官方弃用、暴露 217 个工具（约 114 KB 固定上下文）、无只读模式、且我们 pin 的版本早于"静默写错项目"修复，故弃用（证据见 [`docs/research/glab-mcp-serve.md`](docs/research/glab-mcp-serve.md)）。

**形态**：Python 实现的 stdio MCP server（与网关同镜像，如 `python -m gateway.mcp_readonly`），只读、用途单一、工具面极小。

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `get_issue` | 当前 issue 详情 + 评论（分页） | `include_comments`、`page` |
| `search_issues` | 在**当前项目内**按关键词搜历史 issue | `query`、`state` |
| `list_issue_related_merge_requests` | 当前 issue 关联的 MR（判断是否已有在途修复） | —— |
| `get_merge_request` | MR 详情 + 讨论 | `iid`、`include_notes` |
| `list_pipelines` | 分支/最近 pipeline 及状态（判断主线是否绿） | `ref`、`status`、`limit` |
| `list_pipeline_jobs` | 某 pipeline 的 job 列表 → **拿到失败 job_id** | `pipeline_id`、`scope` |
| `get_job_log` | 读当前项目某 CI job 的失败日志 | `job_id`、`tail_lines` |
| `get_pipeline_test_report` | 结构化失败用例（比 raw trace 精准省上下文） | `pipeline_id` |
| `get_commit_diff` | 单个 commit 的完整 patch + message | `sha` |
| `get_recent_commits` | 最近提交（可按路径过滤） | `limit`、`path` |
| `get_project` | 项目元信息（默认分支、描述、topics、语言） | —— |

> 共 **11 个只读工具**（工具定义约 4–6 KB；对比 glab 的 217 工具 / ~114 KB）。
> `list_pipeline_jobs` 是**必需项**：缺了它 agent 无从获得 `job_id`，`get_job_log` 等于不可用。
> **明确不做**：`search_code`（自托管常无 advanced search，且 agent 手里已有完整 clone，自己 grep 更准）、`get_job_artifacts`（体积不可控，会把 MCP 通道变成文件搬运工）。

**四条硬约束（缺一不可）**

1. **工具 schema 里没有 `project` 参数** —— 项目由网关在启动 MCP server 时通过环境变量注入，agent **物理上无法跨项目读取**
2. **只读工具集**：不含任何 create / update / delete
3. **使用独立的 `read_api` scope token**（第二个 PAT）—— 即使 server 有漏洞也物理上不能写
4. **输出截断**（如 20k 字符/次），避免塞爆上下文

> ⚠️ 注意：MCP 子进程**在 Codex 沙箱之外、有网络**（已核实，见 §6 #2）。这正是必须用自研 + 只读 token + 项目绑定的原因。

## 4. 两阶段执行模型（联网 / 隔离分离）

**核心约束**：`--sandbox workspace-write` 的 `network_access=false` 意味着**任何网络都不通，内网也不通**。因此必须把"装依赖"和"跑 agent"分成两个阶段：

| 阶段 | 执行者 | 网络 | 动作 |
| --- | --- | --- | --- |
| **准备阶段** | 网关进程（沙箱外） | 有（内网 kkRepo） | `uv python install`、`uv sync --frozen`（`UV_DEFAULT_INDEX` 指向 kkRepo）、预置测试依赖 |
| **agent 阶段** | codex 沙箱内 | **完全无网** | `UV_OFFLINE=1 uv run --no-sync pytest` |

**必须注意的细节**

1. `uv` 缓存在 `~/.cache/uv`（workspace 之外 → 沙箱内不可写）→ 网关必须把 `UV_CACHE_DIR` 设到 workdir 内，或在镜像里预置缓存
2. **禁止 agent 自行 `uv add` 新依赖**（无网装不了，且改依赖属高影响操作）→ agent 应在报告中说明"需要新增依赖 X"，由网关写入 MR 描述交人工判断
3. **kkRepo 能出网**，所以"给 agent 开网络"等于把它变成潜在出网跳板 —— 这加固了"agent 保持零网络"这条硬边界

## 5. 关键决策记录

| 决策点 | 选择 | 理由 | 相对 v1 |
| --- | --- | --- | --- |
| Agent 内核 | **Codex CLI** | 内网模型端点**支持 `/v1/responses`**，headless 模式成熟（`exec --json`） | 沿用（依据更新） |
| **GitLab 工具通道** | **自研只读迷你 MCP（A3）** | glab 方案已弃用、217 工具、无只读模式、有写错项目历史 bug；自研可做到项目绑定 + 只读 token + 极小工具面 | **推翻 v1**（原定 glab mcp serve） |
| **GitLab 写权限** | **网关独占** | 单一写者、审计清晰、agent 零凭据；正确方向是"开只读的洞，把写权限攥在手里" | **推翻 v1**（原为 agent 回帖 + 网关收尾的双写） |
| 任务模型 | SQLite 持久队列 + 状态机 + 租约心跳 + 看门狗 | 消除"重启丢单 / 重投被吞"；不引入 Celery/Redis | **升级 v1** |
| Webhook 语义 | 永远尽快返回 2xx，绝不 5xx；用 `webhook-id`/`Idempotency-Key` 幂等 | GitLab 连续失败 4 次即临时禁用、40 次**永久禁用** | 新增 |
| 触发协议 | 标签状态机 + 可选评论命令；状态用单条评论原地编辑 | 打标签是主要用法（v1 只认 open/reopen，永远不触发） | **修正 v1** |
| 验证环节 | diff 策略硬校验 + agent 自跑测试留日志；最终裁判是 GitLab CI | 消除"agent 自称修好就开 MR" | 新增 |
| 环境管理 | **仅 Python + uv**，两阶段模型 | 用户明确首期只支持 Python | 新增 |
| 沙箱档位 | `workspace-write` + 关键项用 `-c` 钉死 | 绝不开放 `danger-full-access`；`-c` 优先级高于仓库自带配置 | 沿用 + 加固 |
| MR 形态 | 永远 **draft MR + 人工审批** | 人工兜底原则 | 沿用 |
| **Renovate** | **本项目不包含（F-1）** | 它是独立产品（定时/跨项目/改版本号），与 issue 修复无交集；先打通闭环 | **推翻 v1**（原为 compose 内常驻容器） |
| 部署形态 | 常驻单容器 | 用户明确不要"每次拉起容器" | 沿用 |
| **触发者权限** | **Developer (30) 及以上**（可叠加用户白名单） | 与"能推代码"对齐，避免 Guest/Reporter 借机器人提权 | 新增 |
| **目标分支** | **项目 `default_branch`**（v1）；将来由 `.mini-gitlab-bot.yaml` 覆盖 | 零配置、与 GitLab 设置自动同步 | 修正 v1（原为全局 `TARGET_BRANCH`） |
| **Python 版本** | 镜像**只预置 3.13**，其他版本准备阶段从 kkRepo 补装 | 用户明确；补装失败即 failed 并写明原因 | 新增 |
| **状态页 `/tasks`** | **v1 不暴露**（仅无信息 `/healthz`） | 不提前决定鉴权方案；磁盘风险靠"job 目录终态即清"解决 | 新增 |
| 命名 / License / 可见性 | `mini-gitlab-bot` / Apache-2.0 / 私有 | —— | 沿用 |

## 6. 已验证技术事实（核查基准：Codex repo `6ae8b8aa` ≈ rust-v0.163.0；glab v1.123.0 真实二进制）

完整证据与来源见 [`docs/research/README.md`](docs/research/README.md)。

| # | 事实 | 对本项目的要求 |
| --- | --- | --- |
| 1 | `wire_api = "chat"` 已是**硬错误**，只接受 `"responses"`（[discussion #7782](https://github.com/openai/codex/discussions/7782)） | 配置改 `responses`；内网端点支持 Responses（**已确认可用**） |
| 2 | MCP stdio server **在沙箱外 spawn**（裸 `Command::new`，无 Landlock/seccomp） | agent shell 无网、但 MCP 通道有网 —— 所以自研 MCP 必须自己收口（§3.3） |
| 3 | `workspace-write` 只限写与网络，**读是全盘可读**；`network_access` 默认 `false` | 只要 agent 手里有凭据就能被注入外泄 → **零凭据**是硬要求 |
| 4 | 项目层配置被 **trust 门控，默认 untrusted**；trusted 仓库可注入 `[mcp_servers.evil]` → 无沙箱任意命令执行（denylist **不含** `mcp_servers`/`sandbox_mode`/`network_access`） | 见 §7 加固基线 1–3 |
| 5 | 配置层优先级：用户 20 < **项目 25** < **`-c` 30** < managed 40/50；合并语义＝表深合并、标量覆盖、**数组整体替换** | 安全关键项**全部用 `-c` 下发** |
| 6 | `turn.completed` 与"有没有改文件"**无关**；`Interrupted` **不发任何终止事件**但 exit 1；`-o/--output-last-message` **只在成功时写入** | 成功判定＝四者与运算（§7 基线 5） |
| 7 | `[mcp_servers.*]` 支持 `env_vars`（按名透传，值不落 config）与 `enabled_tools`/`disabled_tools` | 用 `env_vars` 传 token，不硬编码 |
| 8 | `requires_openai_auth` 默认 `false` + `env_key` | headless 无需 `codex login` |
| 9 | GitLab webhook **无自动重试，但会自我禁用**：连续失败 4 次 → 临时禁用（最长 24h）；**40 次 → 永久禁用** | 永不返回 5xx |
| 10 | 标签变更**无独立事件**：issue hook `action="update"` + `changes.labels.{previous,current}`（元素是对象，取 `title`） | 触发条件必须含 `update` + `changes.labels` |
| 11 | Draft MR ＝ **标题前缀**（`Draft:`/`[Draft]`/`(Draft)`）；API 无 `draft` 参数，`WIP:` 已废弃 | MR 标题写法；源分支无新 commit 时 API 行为未证实 → 更要自查 diff |
| 12 | **Renovate OSS 镜像跑完即退出**，官方推荐 cron/每小时，无常驻模式 | 本项目不含 Renovate；将来若要，必须用调度器而非 `restart: unless-stopped` |
| 13 | GitLab 服务端 MCP 已 GA、非 Duo 专属，但需管理员开关 + OAuth `mcp` scope / 细粒度 PAT（**拒绝传统 PAT**） | 对本项目（PAT + 自管实例）仍不适用；v1 的"内网必然走不通"说法已过时 |
| 14 | `glab mcp serve`：官方弃用、217 工具 / ~114 KB 固定上下文、无只读模式、无 allowlist | 弃用（见 §3.3） |

## 7. 安全边界与加固基线

**红线（不可协商）**

1. 沙箱只用 `workspace-write`；**禁止** `danger-full-access`
2. agent 手里**没有任何凭据**、**没有任何网络**
3. 机器人 token 最小权限：专号专用、项目级、`api`（自研 MCP 另用只读 `read_api` token）
4. 只开 draft MR + 人工审批；禁止 bot 直接合并
5. git 凭据**不得打印到日志**（也不宜留在 argv / `ps` 可见处）

**加固基线（每条都要落实）**

1. codex 启动前**删除 workdir 里的 `.codex/`**（纵深防御，不依赖 trust 语义不变）
2. **永不**把克隆目录写进 `[projects.*] trust_level='trusted'`
3. 安全关键配置**全部走 `-c`**：`sandbox_mode`、`sandbox_workspace_write.network_access`、`approval_policy`
4. **每个 job 独立 `TMPDIR`**（`workspace-write` 默认可写 `/tmp`，并发任务可经 `/tmp` 交换文件）
5. 成功判定＝ `exit 0` **且** 见到 `turn.completed` **且** `-o` 文件**被新写入** **且** 自查 diff 非空
6. **pin 版本**：codex 精确版本 + glab（若仍需要）明确版本 —— 现在 `npm install -g @openai/codex` 不 pin，等于内核可随时漂移
7. webhook handler **绝不返回 5xx**；入队即 2xx，异常只落库
8. 触发者权限校验（**评论命令与标签触发走同一闸门**）：`access_level >= 30`（Developer），可再叠加 `ALLOWED_TRIGGER_USERS` 白名单（AND）；用 `GET /projects/:id/members/all/:user_id` 查成员（含继承自 group 的成员），**404 一律拒绝**（若配置了白名单，白名单是实例管理员的逃生门）；成员关系缓存 60s

## 8. 代码现状

**v0 骨架 13 个文件已在仓库，存在 12 项已实测缺陷**（完整清单见 [`docs/research/README.md`](docs/research/README.md) §C）。其中 4 项是"静默失效"级：

- `_has_commits` 对 fresh clone 恒为真 → **重试逻辑完全失效**（可能开出无改动的 MR）
- 任务只在内存线程池 + 重投被唯一约束吞掉 → **重启即静默丢单**
- 触发只认 `open`/`reopen` → **打标签这个主要用法永远不触发**
- handler 可能抛 500 → **累积到 GitLab 永久禁用 webhook**

### 8.1 M1 落地结果（2026-10-17）

v0 中已被完全取代的 `gateway/db.py`、`gateway/worker.py`、`gateway/gitlab_client.py` **均已删除**（最后一个是 M3 用 `adapters/gitlab_client.py` 取代后删的；它把 token 拼进 clone URL，违反 §7 加固基线 5）。

当前目录（`uv run pytest` → **417 项全绿**；`uv run ruff check` 干净）：

```
core/     states(状态机) · models · branch · policy(diff 策略) · context(context.md) ·
          render(文案) · orchestrator(编排) · worker(租约/看门狗/恢复) · errors ·
          layout(内部产物路径与 git 忽略规则) · mcp_spec(MCP 工具契约) ·
          logging_setup(JSON 日志 + task_id 上下文) · metrics(指标注册表)      ← 纯逻辑，零 IO
ports/    store · scm · vcs · agent · workspace · env · notifier · janitor   ← 仅 Protocol
adapters/ sqlite_store(持久队列+状态机) · fakes(假 SCM/VCS/Agent/Env/Workspace/ProcessRunner) ·
          git_cli(M2 真件) · uv_env(M2 真件) · codex_cli(M2 真件) · proc(子进程+脱敏) ·
          local_workspace(M2 真件) · gitlab_client(M3 真件，python-gitlab) ·
          comment_notifier · janitor(保留策略) ·
          unavailable(占位：未实现时明确失败，不静默放行)
gateway/  app(HTTP) · config(pydantic-settings) · webhook(解析/幂等/闸门) ·
          permissions(Developer 下限 + 白名单) · runtime(装配：build_runtime / build_production_runtime) ·
          mcp_readonly(只读迷你 MCP：stdio JSON-RPC + 11 个只读工具) ·
          app 里的 /healthz /readyz /metrics
tests/    unit(状态机/分支策略/diff 策略/文案/context/MCP 协议/webhook 解析/权限/SQLite/
               配置/脱敏/Python 版本规划/workspace 越界/SCM 分支时序/日志/指标/
               Janitor/容器资产静态检查) ·
          integration(假适配器全链路 / 崩溃恢复 / HTTP 层 / 真 git / 真 uv / 真 GitLab API 桩 /
              桩 codex 全链路 / 真 SCM 闭环 / 真 MCP 子进程 / 运维端点) ·
          stub_gitlab(可编程的本地 GitLab API v4)
```

M1 关键行为（全部有测试锁定）：

- **幂等三层**：`delivery_key`（header 优先，否则 body 哈希）唯一 → `duplicate_delivery`；每个 issue 只允许一个活跃任务（部分唯一索引）→ `active_task_exists`；失败后重新打标签可重试。
- **标签事件没有专门事件**：只认 Issue Hook `action="update"` + `changes.labels`，标签元素取 `title`；标签变更产生的 system note 被忽略。
- **状态机守卫**：`queued→preparing→running→validating→pushing→opening_mr→succeeded` 逐条校验，终态不可再流转；`requeue` 只作为恢复边（重启/租约过期）。
- **崩溃恢复**：启动时只把**已被领走**的在飞任务重置为 `queued`（排队中的不动）；租约过期由看门狗重排队，重试次数耗尽则判失败并走完整收尾（切标签 + 失败评论）。
- **绝不 5xx**：除 secret 不匹配返回 401 外，一切情况都 200（含 `{"status":"error"}`），并有"适配器抛异常 / 存储抛异常 / 非法 JSON"三类回归测试。
- **失败不静默**：切标签与状态评论都是 best-effort，失败不改变任务结论但写审计事件（`*_failed`）。
- **回归修复**：`lstrip("./")` 曾把 `.env`、`.gitlab-ci.yml` 的前导点吃掉 → 禁改路径静默失效（已改为正规化函数 + 回归测试）；`REPO_WHITELIST=a,b` 曾被 pydantic-settings 当 JSON 解析直接崩（已加 `NoDecode` + 真环境变量测试）。

**结论**：v0 作为参考实现保留（`config.py` / `gitlab_client.py` 的部分逻辑可借鉴），主干按**端口 / 适配器**重写：

```
core/     TaskOrchestrator + 状态机        ← 纯逻辑，无 IO，可单测
ports/    SCM · VCS · AgentRunner · Store · Notifier
adapters/ GitLabClient · GitCli · CodexCli（CODEX_BIN 可换成假 agent）· SqliteStore · MiniMcp
```

这样 CI 里能用 **假 SCM + 假 agent** 把整条链路端到端跑通，不需要 LLM、不需要真 GitLab。

### 8.2 M2 落地结果（2026-10-17）

**落地的四个真实适配器**（全部走端口，全部有测试）：

| 适配器 | 文件 | 关键行为 |
| --- | --- | --- |
| `GitCli` | `adapters/git_cli.py` | `clone --branch <default> --single-branch` → `checkout -b ai-fix/N-slug`；**凭据只经 `GIT_ASKPASS` + `MINI_GITLAB_BOT_GIT_TOKEN` 环境变量**（argv / URL / 日志里都没有）；写 `.git/info/exclude`；`commit_all` 以 `base...HEAD` 提交数判定"零改动"（修掉 v0 `_has_commits` 恒真）；提交前做本地产物后置校验 |
| `UvEnvPreparer` | `adapters/uv_env.py` | 网关侧联网阶段：按 `requires-python` 规划解释器（镜像预置 3.13 满足时**不联网补装**）→ `uv sync --no-progress [--frozen] [--python X]` → 校验 `.venv` → `UV_OFFLINE=1 uv run --no-sync python -c ''` 自检；日志落 `<LOG_DIR>/<task>/uv-sync.log` |
| `CodexCli` | `adapters/codex_cli.py` | `codex exec --json --sandbox workspace-write --cd <job> -o <file> -c …`；安全项全部用 `-c` 钉死（优先级 30 > 项目配置 25）；启动前删 `.codex/`；子进程环境是**显式白名单**（GitLab token 不可能进入）；`--json` 落 `<LOG_DIR>/<task>/codex.jsonl` |
| `LocalWorkspace` | `adapters/local_workspace.py` | `context.md` / `report.json` 原子写；路径必须落在 **job 目录**内（挡 `../` 与符号链接逃逸）；`TMPDIR` 指向 job 内（每 job 隔离）；终态清理 job 目录但拒绝删 jobs 根 |

**四条件判定**（PLAN §7 基线 5，逐条有独立测试）：`exit 0` ∧ 见到 `turn.completed` ∧
`-o` 文件**本次新写入**（失效语义：失败时既不写也不删）∧ `base...HEAD` diff 非空。

**关键回归修复（M2 期间实测发现）**：

- `git add -A -- . ':(exclude).venv'` 会因"显式添加被忽略路径"**直接退出 1** → 放弃 pathspec 排除，改为 `.git/info/exclude` + add 之后的 `_assert_no_job_artifacts` 后置校验（一旦发现 `.venv/` 等被暂存就明确失败，而不是把虚拟环境提交进 MR）。
- `commit_all` 原逻辑"仅在零提交且脏时才提交" → agent 自己提交过时**遗留改动永远不会被提交** → 改为"脏就提交，再按提交数判定"。
- `LocalWorkspace._resolve` 只校验 jobs 根 → `../其他任务/context.md` 能跨任务写文件 → 写入路径改为相对 **job 目录** 做包含检查。
- 子进程被 SIGKILL 时退出码是 -9 → 统一归一化为 `-1` 并置 `timed_out=True`（消费者只需看 `timed_out`）。
- 超时 / 取消（编排器 `wait_for`）都必须杀掉**整个进程组**，否则会留下孤儿 codex 进程。
- 沙箱环境里 `uv` 的默认缓存目录 `~/.cache/uv` 在沙箱里不可写 → `UV_CACHE_DIR` 必须落在 workspace 内（配置项 `UV_CACHE_DIR`，默认 `WORK_DIR/.uv-cache`）。

**诚实声明（M2 判据的第 2 条）**：判据要求"真 agent 至少一次端到端成功"，但本仓库的
CI / 开发机**没有 codex 二进制、没有 LLM 端点**（内网环境前提），因此该条**未能在仓库内验证**。
替代验证：桩 codex（真子进程、真 JSONL、真超时杀进程组）把网关侧契约全部跑穿 ——
真正的"真 agent 首跑"必须在有内网模型端点的机器上补做；M3 之后它与"真 GitLab 首跑"一起
构成 v1 上线前必须完成的两项人工验证（见 §8.3 末段）。

### 8.3 M3 落地结果（2026-10-17）

**`adapters/gitlab_client.py`（`GitLabSCM`，python-gitlab 8.x）**

| 端口方法 | 真实语义 | 关键细节 |
| --- | --- | --- |
| `default_branch` | `GET /projects/:id` | 目标分支取项目 `default_branch`（PLAN §12 #5），零配置 |
| `get_member_access_level` | `GET /projects/:id/members/all/:uid` | **`members/all` 才含 group 继承成员**；404 → `None` → 闸门拒绝 |
| `load_issue_context` | `GET /projects/:id/issues/:iid` + `/notes` | 只取最新 N 条人类评论（倒序翻页、命中即停）；`system=True` 的 note 全部丢弃 |
| `set_labels` | `PUT /issues/:iid` with `add_labels` / `remove_labels` | 增量改，不整体写回（避免抹掉并发加的标签） |
| `upsert_comment` | `PUT /issues/:iid/notes/:nid` | 按 marker 找**机器人自己那条**，原地编辑；找不到才 POST |
| `create_draft_mr` | `POST /merge_requests` | `Draft: ` 标题前缀（API 无 draft 参数）；`remove_source_branch=false`（保留分支作审计） |

**M3 期间实测发现并处理的问题**

- `python-gitlab` 8.x 的迭代翻页靠 **`Link` 响应头**（RFC 5988），不是 `X-Next-Page`。桩服务最初只发后者 → 状态评论一旦被挤出第一页就"找不到、于是新开一条"。已按真实 GitLab 形状补 `Link`，并加回归测试（150 条评论之后仍能命中并原地编辑）。
- `retry_transient_errors=True` 会让注入的 500 被自动重试（测试里表现为"以为没抛错"）。这本身是想要的行为：单独测重试，另开测试关掉重试验证 5xx 映射。
- MR 创建必须**先查后建 + 409 兜底**：重试/崩溃恢复会用到同一分支，只 create 会得到"Another open merge request already exists"。
- `_update_mr` 只在 title/description 真的变了才 `save()`，否则每次重试都产生一条多余写请求。

**测试策略（M3 新增）**：`tests/stub_gitlab.py` 是一个真实 HTTP 的 GitLab API v4 桩
（可注入故障、记录每个请求的路径/查询/头/body）。`tests/integration/test_gitlab_scm.py`
用它验证**请求形状**；`tests/integration/test_pipeline_real_scm.py` 用它跑完整闭环
（成功 → draft MR + 单条评论原地编辑 + 标签流转；失败 → `ai-fix:failed` 且不开 MR；
权限 → Developer 放行 / Reporter 拒绝 / 非成员拒绝 / GitLab 5xx 仍是 200 且不入队）。

**诚实声明（M3 判据）**：判据是"真 GitLab 上一次完整闭环"。本仓库的 CI / 开发机
**没有可用的 GitLab 实例**，因此该条**未能在仓库内验证**。替代验证是"真 HTTP + 真 python-gitlab
客户端 + 桩服务端"，能证明请求形状、幂等、错误映射与凭据边界正确；但**真实实例上的
group 权限继承、note 编辑权限、受保护分支策略**仍必须在目标环境首跑时确认。

### 8.4 M4 落地结果（2026-10-17）

**形态**：`python -m gateway.mcp_readonly`（stdio / JSON-RPC 2.0，一行一条消息，
stdout 只留协议、日志一律走 stderr）。工具契约（11 个 schema + 环境变量名 + 提示文案）
放在 `core/mcp_spec.py`，因为 `adapters/codex_cli.py` 要用它下发 `-c` 配置，而
**适配器层不允许反向依赖 `gateway/`**（§2 分层）。

| 硬约束（§3.3） | 落实方式 | 如何验证 |
| --- | --- | --- |
| 工具 schema 无 `project` 参数 | 项目/issue 由网关经 `MINI_GITLAB_BOT_PROJECT_ID` / `..._ISSUE_IID` 注入；未知参数**直接报错**（不静默忽略） | 真子进程测试：`get_issue(project=43)` 返回 `isError` 且**一个请求都没发出** |
| 只读工具集 | 只有 GET；没有 create/update/delete 工具 | 桩 GitLab 断言所有请求方法都是 `GET` |
| 独立 `read_api` token | 只认 `GITLAB_READONLY_TOKEN`；环境里出现写 token `GITLAB_TOKEN` → **退出码 3 拒绝启动** | 真子进程测试（退出码 + stderr） |
| 输出截断 | 默认 20k 字符；`get_job_log` 从**尾部**截断（失败信息在末尾） | 契约测试 + 真子进程（`MCP_MAX_OUTPUT_CHARS=600`） |

**Codex 侧接线**（`adapters/codex_cli.py`）：

```
-c mcp_servers.mini_gitlab.command="<python>"
-c mcp_servers.mini_gitlab.args=["-m", "gateway.mcp_readonly"]
-c mcp_servers.mini_gitlab.env_vars=["MINI_GITLAB_BOT_PROJECT_ID", ..., "GITLAB_READONLY_TOKEN"]
-c mcp_servers.mini_gitlab.enabled_tools=["get_issue", ...]
```

全部走 `-c`（优先级 30 > 项目配置 25，§6 #5）；token **只以变量名出现**（`env_vars`
按名透传，§6 #7），值留在子进程环境里 —— argv / 配置 / 日志里都没有凭据。

**M4 期间实测发现并处理的问题**

- `commits.list(path=...)` 里的 `path` 会被 python-gitlab 当成"自定义列表端点"，真的去请求
  字面量 `app.py`（404）。改用 `http_list` 显式传查询参数。
- python-gitlab 对数组属性发的是 `scope[]=failed`（不是 `scope=failed`）——桩服务必须两者都收，否则
  "按状态过滤 job"在测试里静默失效。
- 单个工具抛非预期异常会把整个 MCP 会话炸掉 → 在 `tools/call` 里兜底成 `isError` 结果
  （模型能看到原因并调整；协议层不崩）。
- `gitlab` 对象字段用 `getattr` 兜底：真实 API 一定返回的字段（`web_url`/`created_at`）
  在桩里缺失时不该让工具 500。

**测试策略（M4 新增）**：`tests/integration/test_mcp_tools.py` 真的 `subprocess.Popen`
起一个 MCP server，按 MCP 协议一问一答，让它用真 HTTP 打桩 GitLab —— 覆盖 11 个工具、
分页/截断、`README` 里那四条硬约束，以及"拒绝带写 token 启动"。

**诚实声明（M4 判据）**：判据是"agent 能按需取到 CI 日志"。本仓库能验证的是**服务端**：
真子进程、真协议、真 HTTP，`get_job_log` 能拿到失败尾部。**"真 codex 在真 GitLab 上主动
调用这些工具"未能在仓库内验证**（无 codex 二进制、无 LLM 端点），必须与 M2/M3 的两项
人工验证一起，在内网机器上补做。

### 8.5 M5 落地结果（2026-10-17）

| 项 | 做法 | 验证 |
| --- | --- | --- |
| 镜像 | 多阶段：`node` 层装并 pin codex → `python:3.13-slim` 运行时；**非 root（uid 10001）**、`cap_drop: ALL`、`no-new-privileges`、`init: true`；`uv sync --frozen` 两层缓存；`HEALTHCHECK` 打 `/healthz` | 静态校验（`tests/unit/test_container_assets.py`）：pin 版本、非 root、`COPY` 四个包、无 glab、健康检查、compose 无 renovate/TARGET_BRANCH |
| 结构化日志 | `core/logging_setup.py`：JSON 一行一条，`ts/level/logger/msg/task_id`，异常带 `exc`；`task_id` 用 `contextvars` 由 worker 注入（协程级隔离） | 单测 + 真机烟测：uvicorn 全量日志行 **0 条非 JSON** |
| 就绪/存活 | `/healthz` 只回 `{"status":"ok"}`；`/readyz` 检查 存储可读 / worker 在跑 / 无卡死租约，只回状态类别（不泄露任务细节） | 集成测试：ok / worker_disabled / store / stalled 四种分支 |
| 指标 | `core/metrics.py`（自研，无 prometheus_client）：`tasks_total{state}`、`task_duration_seconds`、`task_failures_total{stage}`、`queue_depth`、`worker_inflight`、`janitor_*`；标签只用有限枚举 | 单测 + 集成：跑一个任务后能看到计数；存储炸了也不 500 |
| 保留策略 | `adapters/janitor.py`：日志 14 天、终态任务 90 天（`store.purge_terminal`）、孤儿 job 目录 1 天；只删 `task-` 前缀且不属于任何非终态任务 | 单测：老目录删、新目录留、非 `task-` 不动、在飞任务目录绝不动 |

**M5 期间实测发现并处理的问题**

- `configure_logging` 之后 uvicorn 仍用自己的 handler → 每行日志**输出两遍**（纯文本 + JSON）。
  修法：`attach_uvicorn_loggers()` 摘掉 uvicorn 的 handler 并接回根 logger，且在 **lifespan 里再叫一次**
  （uvicorn 在 import 之后才应用它的 logging 配置）。
- `/readyz` 的"卡死"判定最初用 `datetime.now()`，与注入时钟不一致、测试不可控 → 改为用 `runtime.clock()`（单一时钟源）。

**M5 收尾复盘时补的一个漏洞**：重写镜像时删掉了 v0 的 `COPY codex/config.toml`，但当时网关
并没有用 `-c` 下发 provider 端点 —— 也就是说**镜像里没有任何 `[model_providers.internal]`
定义，开箱即跑不起来**。已改为由 `MODEL_BASE_URL` 驱动、四个 provider 键全部用 `-c` 下发
（`name` / `base_url` / `wire_api="responses"` / `env_key`），镜像不再预置 codex 配置；
`codex/config.toml` 降级为"本地跑 codex 的示例"。

**M5 镜像已在真机 build 并跑通（2026-10-18）**：`docker build` 成功（1.16 GB），
镜像内 `codex 0.157.1` / `uv 0.5.14` / `git` 齐全，以 uid 10001 运行；用镜像起容器 +
真 codex + 真 GitLab 跑完了完整闭环（issue #13 → MR !7），agent 还自己补了两个测试
（`4 passed`）。**真机暴露并修复的三个镜像缺陷**：

1. `COPY --from=<image>:${VAR}` 不支持变量展开 → 改成先 `FROM ... AS uv` 再 `COPY --from=uv`
2. 直接 `COPY` 那个指向 `codex.js` 的符号链接会被解引用成普通文件，包内相对 import 失效
   （`EsmLoader` 报错）→ 显式 `ln -s`
3. 构建期 `uv sync` 以 root 写 `/work/.uv-cache`，运行期 bot 写不进去
   （`Permission denied ... sdists-v6/.git`）→ 构建期缓存放 `/tmp`，并 `chown -R bot:bot /app /data /work`

4. **运行期硬性要求**：codex 的 bwrap 沙箱需要非特权 user namespace，Docker 默认 seccomp
   会拒绝（`bwrap: No permissions to create a new namespace`）→ compose 必须
   `seccomp=unconfined` + `apparmor=unconfined`，否则 agent 无法执行任何命令。

**诚实声明（M5 判据）**：镜像与 compose 只做了**静态**校验 —— 本仓库环境没有 docker
也没有外网，`uv`/codex 的 pin 版本是否可拉取、`@openai/codex` 的 npm 包装是否还需其它
运行时依赖，都必须在那台构建机上实际 `docker compose build` 一次才能确认。这是上线前
除三项人工验证之外的第四件事。

### 8.6 真机验证结果（2026-10-18）

在本机用 **GitLab CE 19.5.0**（docker）+ **真 codex 0.157.1** + **Ark `/api/coding/v3`
的 `deepseek-v4-1-flash-260910`**，把 M2/M3 的判据真跑了一遍。脚本见
[`scripts/e2e/`](scripts/e2e/README.md)。

**结果：15 项断言全过**（另 1 项 CI 需要 runner，未开）。

真实 agent 的表现（不是桩）：
- 读 `context.md` → 定位 `app.py` 的空输入分支 → `return None` 改 `return []`
- **自己跑测试**：`UV_OFFLINE=1 uv run --no-sync pytest -q` → `2 passed`
- 写 `.mini-gitlab-bot/report.json`（summary/fixes/tests/test_summary/risks）
- 遵守约束：没有 push、没有建 MR；网关兜底提交 `fix(issue-8): ...` → 推分支 → draft MR !5
  → 单条状态评论 → 标签 `ai-fix` → `ai-fix:done`
- 全程 41s，模型调用 ~100k 输入 token（含 codex 系统提示）

**真机暴露、仓库内测不出来的三件事**

1. **codex 的 `workspace-write` 把 `.git` 设为只读**，agent **无法自己 commit**
   （`Unable to create .git/index.lock: Read-only file system`）。我们 M2 设计的
   `commit_all` 兜底提交正好接住；已把 `context.md` 与 `TASK_PROMPT` 改成
   "改完文件即可，提交由网关完成"，避免模型浪费轮次去折腾。
2. **`model_providers.<name>.name` 不能为空**：codex 启动时校验，报
   `provider name must not be empty`。M5 的 `MODEL_BASE_URL` 逻辑已经带上了 `name`。
3. **Ark 的模型开通/计费与路径有关**：`/api/v3` 下 `deepseek-v4-1-flash-260910` 返回
   `ModelNotOpen`，而 **`/api/coding/v3` 下同一个模型 id 可用**（`/responses` 也通）。
   所以 `MODEL_BASE_URL` 必须指向实际开通的那条路径。

**仍未完成**：M4 的 CI 工具（pipeline/job/trace）需要 runner，脚本支持 `--with-runner`
但本次未开；镜像首次 build 仍未做。



## 9. 里程碑与待办

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| **M0 文档定案** | 本文 + `docs/research/` 归档 | ✅ 完成 |
| **M1 主干与触发器** | 端口/适配器骨架、SQLite 持久队列 + 状态机 + 心跳、webhook（secret/白名单/标签 update/评论命令/权限校验）、幂等、假 SCM + 假 agent 集成测试 | ✅ 完成：169 项测试全绿、ruff 干净；重复投递不重复处理、重启与租约过期后任务恢复，均有测试覆盖 |
| **M2 Agent 链路** | job 工作目录与隔离、`context.md` 预取、uv 两阶段、分支创建、codex 调用（`-c` 钉死 + 删 `.codex/` + `--json` 落盘 + 四条件判定）、diff 策略校验 | ✅ 代码完成：278 项测试全绿。真件链路（本地 bare 仓 + 真 `uv sync` + 桩 codex）端到端跑通"改代码 → commit → 校验通过"，并覆盖四条件各条失败、禁改路径拦截、超时杀进程组、凭据不外泄；**"真 agent 至少一次成功"待内网机器补做**（本环境无 LLM 端点，见 §8.2） |
| **M3 收尾与状态** | push（凭据不进 argv）、draft MR、单条状态评论原地编辑、标签状态机流转、失败升级 | ✅ 代码完成：319 项测试全绿。`GitLabSCM` 用 python-gitlab 真发 HTTP，打到可编程的本地桩 GitLab（`tests/stub_gitlab.py`）验证请求形状与幂等；**"真 GitLab 实例上一次完整闭环"待内网机器补做**（见 §8.3） |
| **M4 只读迷你 MCP** | 11 个只读工具（§3.3）、项目绑定、`read_api` token、输出截断 | ✅ 代码完成：374 项测试全绿。真子进程 + 真 stdio 协议 + 真 HTTP 验证了 11 个工具、`get_job_log` 取到失败日志、跨项目参数被拒、写 token 在场即拒绝启动（见 §8.4）；**"真 GitLab + 真 codex 下 agent 主动调用"待内网机器补做** |
| **M5 运维加固** | pin 版本、镜像重写、删 glab、healthcheck、结构化日志、metrics、磁盘/保留策略（job 目录终态即清、codex 日志留 14 天、终态任务留 90 天）、`/tasks` 加鉴权（v1 不暴露） | ✅ 代码完成：417 项测试全绿。镜像/编排已重写并做静态校验；`/readyz`、`/metrics`、JSON 日志、Janitor 均有测试与真机烟测（§8.5）。**镜像首次 build 与 `/tasks` 的最终取舍待上线时确认**（v1 仍不暴露） |

**已明确推迟**：Renovate（F-1，不进本仓库）；GitHub 平台适配；非 Python 项目；仓库级配置 `.mini-gitlab-bot.yaml`（§12）；以及 `TODO.md` 里记录的两条需求（issue 自动回复并关闭、按 tag 自动补 release 文案）—— 都属于 v1 之后的功能扩充，不进 M1–M5。

## 10. 内网环境前提

1. **codex 只能访问内网，不能访问外网**（这是硬约束，也是我们的安全边界）
2. 内网自管基础设施：**GitLab**（自托管）、**kkRepo 内网源**；**kkRepo 可访问外网**
3. **模型端点支持 `/v1/responses`**（已确认内网长期正常使用 Codex）
4. 首期只支持 **Python 项目**，用 **uv** 管理环境；镜像预置 **CPython 3.13**，项目声明的其他版本由准备阶段从 kkRepo 补装

## 11. 参考资料

**可靠来源（本项目采信）**

- Codex CLI 仓库源码（核查基准 commit `6ae8b8aa`，≈ rust-v0.163.0）—— `exec` 参数、沙箱、MCP spawn、配置加载与信任模型
- Codex 官方讨论：`chat/completions` 弃用 → <https://github.com/openai/codex/discussions/7782>
- GitLab webhook 事件与接收要求 → <https://docs.gitlab.com/user/project/integrations/webhook_events/> 、<https://docs.gitlab.com/user/project/integrations/webhooks/>
- GitLab MR API（draft 前缀、错误码） → <https://docs.gitlab.com/api/merge_requests/>
- GitLab 服务端 MCP → <https://docs.gitlab.com/user/model_context_protocol/mcp_server/>
- glab 认证（自管实例环境变量） → <https://docs.gitlab.com/cli/authentication/>
- Renovate 自托管（一次性执行 + cron 建议） → <https://docs.renovatebot.com/getting-started/running/>
- 本项目核查归档：`docs/research/`（含 Codex 两份 + glab 一份）

**❌ 不可采信（已划掉）**

- `mintlify.wiki/openai/codex`：自动生成站，记载了仓库中**不存在**的 flag（如 `--progress-cursor`），v1 文档曾误引
- `about.gitlab.com` 的 glab MCP 博客：核查环境无法访问，全文未验证，本项目的结论不依赖它

## 12. 开放问题（2026-10-17 已全部定案）

| # | 原问题 | 结论 |
| --- | --- | --- |
| 1 | 只读 MCP 工具是否够用 | **扩到 11 个**（见 §3.3）；补上 `list_pipeline_jobs` 与 `get_pipeline_test_report`，否则"读 CI 失败日志"这条链路是断的 |
| 2 | 评论命令的权限下限 | **Developer (30) 及以上**；可叠加 `ALLOWED_TRIGGER_USERS` 白名单（AND）；**标签触发同闸门**；成员查询 `GET /projects/:id/members/all/:user_id`，404 拒绝；缓存 60s。详见 §7 加固基线 8 |
| 3 | MR 描述 / 状态评论文案 | 采用 **uv release-notes 风格的中文模板**（分类式小节 + 风险与未决分区 + 隐藏标记原地编辑的状态评论），全文见 [`docs/templates.md`](docs/templates.md) |
| 4 | 每仓库 `.mini-gitlab-bot.yaml` | **推迟**（属功能扩充，不进 v1）；v1 不做任何仓库级配置读取 |
| 5 | `TARGET_BRANCH` | **v1 取项目 `default_branch`**（零配置、与 GitLab 同步）；将来由 `.mini-gitlab-bot.yaml` 的 `target_branch` 覆盖 |
| 6 | Python 版本范围 | 镜像**只预置 3.13**；项目声明的其他版本在准备阶段从 kkRepo 补装，补装失败即 `ai-fix:failed` 并写明原因 |
| 7 | `/tasks` 鉴权与保留策略 | **v1 不暴露 `/tasks`**（只留无信息 `/healthz`）；job 目录**终态即清**；codex 日志留 **14 天**；终态任务行留 **90 天**或每项目最近 200 条；带 token 的状态页留到 M5 |

**新增推迟项**：仓库级配置文件 `.mini-gitlab-bot.yaml`（测试命令、`target_branch`、禁改路径、diff 上限）——见 #4、#5，作为 v1 之后的功能扩充。

---

## 附录 A：起点调研结论（保留 v1 的动机）

调研对象 [klboke/kkRepo](https://github.com/klboke/kkRepo) 的"为什么 PR/issue 处理这么快"：

| 环节 | 实际机制 |
| --- | --- |
| Issue 回复 | **作者本人**（非机器人） |
| PR 审查 | OpenAI Codex bot（`chatgpt-codex-connector[bot]`） |
| 依赖 PR | Dependabot + 平台原生 auto-merge |
| 快速手动合并 | 大量 CI 全绿支撑下的低风险合并 |
| 工程化基建 | `.github/agents/` 多个 agent、prompts、`agent_task.yml` |

**结论**：不是全自动，而是「**机器人干脏活 + 作者低风险快速拍板**」的组合 —— 这决定了本项目"永远 draft MR + 人工审批"的基调。

## 附录 B：命名

`glabot` 已被他人注册商标（Trademarkia #8103150），已排除；最终定为 `mini-gitlab-bot`。
