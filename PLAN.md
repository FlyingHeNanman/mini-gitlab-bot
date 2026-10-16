# mini-gitlab-bot 交接文档（v2 · 架构定案）

> 用途：记录「内网 GitLab 仓库维护机器人」的最终架构、决策依据、已验证技术事实与实施计划，供后续接手者直接开工。
> 更新日期：2026-10-16　状态：**架构已定案，代码 v0 骨架待按本文重写**（本轮不写代码）
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
| `get_job_log` | 读当前项目某 CI job 的失败日志 | `job_id`、`tail_lines` |
| `get_recent_commits` | 最近提交（可按路径过滤） | `limit`、`path` |

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

## 8. 代码现状

**v0 骨架 13 个文件已在仓库，存在 12 项已实测缺陷**（完整清单见 [`docs/research/README.md`](docs/research/README.md) §C）。其中 4 项是"静默失效"级：

- `_has_commits` 对 fresh clone 恒为真 → **重试逻辑完全失效**（可能开出无改动的 MR）
- 任务只在内存线程池 + 重投被唯一约束吞掉 → **重启即静默丢单**
- 触发只认 `open`/`reopen` → **打标签这个主要用法永远不触发**
- handler 可能抛 500 → **累积到 GitLab 永久禁用 webhook**

**结论**：v0 作为参考实现保留（`config.py` / `gitlab_client.py` / `db.py` 的部分逻辑可借鉴），主干按**端口 / 适配器**重写：

```
core/     TaskOrchestrator + 状态机        ← 纯逻辑，无 IO，可单测
ports/    SCM · VCS · AgentRunner · Store · Notifier
adapters/ GitLabClient · GitCli · CodexCli（CODEX_BIN 可换成假 agent）· SqliteStore · MiniMcp
```

这样 CI 里能用 **假 SCM + 假 agent** 把整条链路端到端跑通，不需要 LLM、不需要真 GitLab。

## 9. 里程碑与待办

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| **M0 文档定案** | 本文 + `docs/research/` 归档 | ✅ 本轮完成 |
| **M1 主干与触发器** | 端口/适配器骨架、SQLite 持久队列 + 状态机 + 心跳、webhook（secret/白名单/标签 update/评论命令/权限校验）、幂等、假 SCM + 假 agent 集成测试 | 单测 + 集成测试全绿；重复投递不重复处理、重启后任务恢复 |
| **M2 Agent 链路** | job 工作目录与隔离、`context.md` 预取、uv 两阶段、分支创建、codex 调用（`-c` 钉死 + 删 `.codex/` + `--json` 落盘 + 四条件判定）、diff 策略校验 | 用假 agent 能跑通"改代码 → commit → 校验通过"；真 agent 至少一次端到端成功 |
| **M3 收尾与状态** | push（凭据不进 argv）、draft MR、单条状态评论原地编辑、标签状态机流转、失败升级 | 真 GitLab 上一次完整闭环 |
| **M4 只读迷你 MCP** | 4 个只读工具、项目绑定、`read_api` token、输出截断 | agent 能按需取到 CI 日志；跨项目调用不可能 |
| **M5 运维加固** | pin 版本、镜像瘦身（删 glab）、healthcheck、结构化日志、metrics、磁盘/保留策略、`/tasks` 加鉴权 | —— |

**已明确推迟**：Renovate（F-1，不进本仓库）；GitHub 平台适配；非 Python 项目。

## 10. 内网环境前提

1. **codex 只能访问内网，不能访问外网**（这是硬约束，也是我们的安全边界）
2. 内网自管基础设施：**GitLab**（自托管）、**kkRepo 内网源**；**kkRepo 可访问外网**
3. **模型端点支持 `/v1/responses`**（已确认内网长期正常使用 Codex）
4. 首期只支持 **Python 项目**，用 **uv** 管理环境

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

## 12. 开放问题（开工前需确认）

1. 只读 MCP 的 4 个工具是否够用？是否要加"按文件路径查最近提交者/历史"？
2. 评论命令的权限下限：项目 Developer 及以上？是否还要限定到指定用户白名单？
3. MR 描述模板、状态评论模板的最终文案
4. 多项目配置：是否要支持每仓库的 `.mini-gitlab-bot.yml`（测试命令、目标分支、禁改路径、diff 上限）？
5. `TARGET_BRANCH` 是全局一个还是按项目取默认分支？
6. Python 版本范围（3.10–3.13？）与镜像里预置哪些版本
7. 状态页 `/tasks` 是否需要鉴权 + 是否需要历史保留策略

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
