# 事实核查与架构评审汇总（草案）

> 用途：把「内网 GitLab issue 修复机器人」的关键外部事实与代码缺陷固定下来，供架构决策与后续接手使用。
> 状态：**待用户拍板**——本文只记录事实与待决项，不代表已定架构。日期 2026-10-16。
> 核查基准：Codex CLI repo commit `6ae8b8aa`（≈ rust-v0.163.0）、glab **v1.123.0**（真实二进制实测）。

## 本目录

| 文件 | 内容 |
| --- | --- |
| [codex-cli-research-brief.md](codex-cli-research-brief.md) | Codex CLI：`exec` 参数、沙箱语义、MCP 与沙箱的关系、`mcp_servers` 字段、`model_providers`、配置层与优先级、headless 认证 |
| [codex-cli-followup-trust-and-exit-codes.md](codex-cli-followup-trust-and-exit-codes.md) | cwd 信任模型、`-c` 覆盖能力与合并语义、`--json` 结束判定、退出码 |
| [glab-mcp-serve.md](glab-mcp-serve.md) | `glab mcp serve`：存在性/状态/工具集/认证/限制，以及对架构的结论 |

来源可靠性：`mintlify.wiki/openai/codex` **不可靠**（自动生成站，记载了仓库中不存在的 flag），不得引用；`developers.openai.com` 本次返回 403，故以仓库源码为准；`about.gitlab.com` 博客无法访问，未采信。

---

## A. 会决定架构的关键事实

| # | 事实 | 对项目的影响 |
| --- | --- | --- |
| 1 | **`wire_api = "chat"` 已是硬错误**，只接受 `"responses"`（官方讨论 [#7782](https://github.com/openai/codex/discussions/7782)，2026-02 前后移除） | 🔴 **当前 `codex/config.toml` 直接起不来**；内网端点必须实现 OpenAI **Responses API**（`/v1/responses`），仅支持 chat/completions 的 vLLM/网关不可用 → 可能触发"是否继续用 Codex CLI"的路线变更 |
| 2 | **MCP stdio server 在沙箱外 spawn**（`rmcp-client` 用裸 `Command::new`，无 Landlock/seccomp；远程路径显式 `sandbox: None`） | ✅ PLAN 事实 #2 成立：agent shell 无网、但 MCP 通道可出网。这是**官方有意留的逃生口**，不是巧合 |
| 3 | **`workspace-write` 只限写与网络，读是全盘可读**（`path_beneath_rules(&["/"], access_ro)`），`network_access` 默认 `false` | ⚠️ agent 可读 `/root/.codex/config.toml`、`/proc/*/environ` → 只要 agent 手里有凭据，**注入即可外泄**（读 + MCP 出网 = 链路闭合） |
| 4 | **项目层配置被 trust 门控，默认 untrusted**（`[projects."/path"] trust_level='trusted'` 才生效，且仓库无法自我授权） | ✅ 恶意仓库自带的 `.codex/config.toml` 默认不生效；但**trust 是这条链上唯一的闸门**——trusted 仓库可注入 `[mcp_servers.evil]` → **无沙箱任意命令执行**（denylist 不含 `mcp_servers`/`sandbox_mode`/`approval_policy`/`network_access`） |
| 5 | **`-c` 优先级 30 > 项目层 25 > 用户 20**；合并语义为「表深合并、标量覆盖、数组整体替换」 | 🔧 安全关键配置必须全部用 `-c` 下发，才压得住仓库；这是唯一不受仓库影响的加固面 |
| 6 | `turn.completed` 只看 turn 状态、**与有无文件改动无关**；`Interrupted` **不发任何终止事件**但 exit 1；`-o/--output-last-message` **只在成功时写入**（失败不写也不删） | 🔧 成功判定必须四者与运算：`exit 0` ∧ 见到 `turn.completed` ∧ `-o` 文件**被新写入** ∧ 自己查出的 diff 非空 |
| 7 | `[mcp_servers.*]` 支持 `env_vars`（按名字从 Codex 自身环境透传，**值不落 config**）与 `enabled_tools`/`disabled_tools` | 🔧 可消除"token 进仓库/进镜像层"；工具集可在**客户端**收窄 |
| 8 | `requires_openai_auth` 默认 `false` + `env_key` | ✅ headless 无需 `codex login`，注入环境变量即可 |
| 9 | **GitLab webhook 无自动重试，但会自我禁用**：连续失败 4 次 → 临时禁用（最长 24h）；**40 次 → 永久禁用**（[文档](https://docs.gitlab.com/user/project/integrations/webhooks/#auto-disabled-webhooks)） | 🔴 任何 500 都在把机器人自己的入口推向永久禁用；必须"永远尽快 2xx + 用 `webhook-id` 幂等" |
| 10 | 标签变更**没有独立事件**：是 issue hook 的 `action: "update"` + `changes.labels.{previous,current}`（元素是对象，取 `title`） | 🔴 现有代码只认 `open`/`reopen` → **"给已有 issue 打标签"永远不会触发** |
| 11 | Draft MR = **标题前缀**（`Draft:`/`[Draft]`/`(Draft)`），API 无 `draft` 参数；`WIP:` 已废弃 | ✅ 现有代码写法正确；但源分支无新 commit 时 API 行为**未证实**（可能不是报错而是建出空 MR） |
| 12 | **Renovate OSS 镜像是"跑完即退出"**，官方推荐 cron / 每小时（[running docs](https://docs.renovatebot.com/getting-started/running/)）；无常驻/调度模式 | 🔴 `restart: unless-stopped` + 一次性镜像 = 无限重启扫描循环；`RENOVATE_AUTODISCOVER=true` 且无 `RENOVATE_REPOSITORIES` = 扫全部可见项目 |
| 13 | `glab mcp serve` **已弃用**、217 个工具 / ~114 KB 固定上下文、无只读模式；pin 的 1.76.2 早于"静默写错项目"修复（v1.123.0） | 🔴 不建议继续依赖（详见 [glab-mcp-serve.md](glab-mcp-serve.md)） |
| 14 | GitLab **服务端 MCP 已 GA**（Free/Premium/Ultimate），但需管理员开关 + OAuth `mcp` scope / 细粒度 PAT，**拒绝传统 PAT** | PLAN 事实 #4 已过时；即便如此对本项目仍不适用（我们是 PAT + 自管实例） |

---

## B. 对 `PLAN.md`「关键技术事实」的逐条修正

| PLAN 原文 | 状态 | 修正 |
| --- | --- | --- |
| #1 `codex exec --full-auto` / `--sandbox workspace-write` 是官方 headless 模式 | ⚠️ 半过时 | `--full-auto` 在 v0.147.0 已移除（替代品 `--approve-for-me`）；`--sandbox workspace-write` 仍正确。`--ask-for-approval` 从来不是 `exec` 的参数（`exec` 硬编码 `approval_policy = never`） |
| #2 MCP 进程不受 shell 沙箱网络限制 | ✅ **成立** | 机制补充：MCP 在沙箱外 spawn，且其子进程（如 glab）同样无沙箱 |
| #3 `glab mcp serve` 是客户端 MCP、与服务器版本无关 | ✅ 成立 | 但**已弃用且 experimental**，工具数 217，无只读模式 |
| #4 官方 MCP（`/api/v4/mcp`）内网走不通（需 Duo + 云 AI Gateway） | ❌ **过时** | 服务端 MCP 已 GA、非 Duo 专属；门槛变成"管理员开关 + OAuth `mcp` scope/细粒度 PAT（拒传统 PAT）"。结论（不用它）不变，理由要换 |
| #5 Codex 可接任意 OpenAI 兼容端点 | ⚠️ **有硬前提** | 必须是 **Responses API**；`wire_api="chat"` 已硬报错 |
| #6 glab 动态内省全部命令为 MCP 工具 | ✅ 成立 | 精确数字：208 个带标注命令块 → 实测 **217 个工具**、~114 KB |
| #7 Renovate 自托管 docker 一行起 | ⚠️ 需修正 | 需 cron/调度；`automerge` 默认走平台原生（`platformAutomerge` 默认 true），MWPS 已更名 `auto_merge` |
| #8 `glabot` 商标雷区 | ➖ | 与本次核查无关 |
| 参考资料里的 `mintlify.wiki` | ❌ 不可靠 | 删除或替换为仓库源码/官方文档 |

---

## C. 代码级缺陷（已实测确认，非推测）

| # | 位置 | 问题 | 后果 |
| --- | --- | --- | --- |
| 1 | [worker.py:92-95](../../gateway/worker.py#L92-L95) | `_has_commits` 用 `git log -n 1`，而 fresh clone 本来就有 commit（实测：`.git log` 有 base 提交，`origin/HEAD..HEAD` 为 0） | **重试逻辑完全失效**；agent 什么都没改也会往下走 → push 空分支 → 可能开出无改动的 MR |
| 2 | [worker.py:22](../../gateway/worker.py#L22) + [db.py:33-49](../../gateway/db.py#L33-L49) | 任务只在内存线程池里；重投 webhook 因唯一约束被静默丢弃（返回 `queued:false`） | **静默丢单**：重启后 pending/running 任务永不复跑，且 issue 上无任何提示 |
| 3 | [app.py:33](../../gateway/app.py#L33) | 只接受 `action in ("open","reopen")` | **打标签这一主要用法永远不触发**（应为 `update` + `changes.labels`） |
| 4 | [app.py:36](../../gateway/app.py#L36) | `payload["project"][...]` 无保护；handler 异常 → 500 | 累积失败 → **webhook 被 GitLab 永久禁用** |
| 5 | [worker.py:34](../../gateway/worker.py#L34) | `job-{iid}-{pid}`：同进程内 pid 相同 | 不同项目的同号 issue **共用一个目录并互相 rmtree** |
| 6 | Dockerfile | 无 `git config user.name/email` | agent **无法 commit**（`Author identity unknown`），而全流程押在 commit 上 |
| 7 | Dockerfile | 只有 python/node，无 jdk/go/maven | 提示词要求"跑测试"实际跑不了 → 闭环质量无从谈起 |
| 8 | [worker.py:70-77](../../gateway/worker.py#L70-L77) + [worker.py:66-67](../../gateway/worker.py#L66-L67) | 不检查 returncode、丢弃 stdout/stderr、无 `--json`；超时后 `finally` 把 workdir 一起删掉 | 线上不可排障；**30 分钟成果连同日志一起丢** |
| 9 | [codex/config.toml:13/22](../../codex/config.toml#L13) + [Dockerfile:29](../../Dockerfile#L29) | `wire_api="chat"`（现为硬错误）；PAT 硬编码并被 COPY 进镜像 | 起不来 + **token 进仓库与镜像层**；且 agent 可读该文件 |
| 10 | [docker-compose.yml](../../docker-compose.yml) | 未传 `LLM_API_KEY`；`restart: unless-stopped` 配一次性 Renovate 镜像；`AUTODISCOVER=true` 无收口 | codex 起不来；Renovate 无限重启扫描全部项目 |
| 11 | [worker.py:87](../../gateway/worker.py#L87) | 提示词该用 f-string 却用了普通字符串 | agent 看到字面量 `{iid}` |
| 12 | 全局 | 版本不 pin（`npm install -g @openai/codex`）；无 `.dockerignore`/healthcheck/测试/结构化日志/资源上限；`/tasks` 无鉴权；token 进 git argv | 构建不可复现、无回归网、运维与安全面缺失 |

---

## D. 待决策清单

| 代号 | 议题 | 选项与倾向 |
| --- | --- | --- |
| **阻塞项** | 内网模型端点是否支持 `/v1/responses` | 支持 → 继续用 Codex CLI；不支持 → 自研 agent loop（`wire_api` 自由）。**pin 到删 `chat` 之前的版本是被时间锁死，不推荐** |
| A | GitLab 写权限归谁 | **A1（推荐）网关独占**：agent 零凭据，MCP/glab 整套可删；A2 保留 MCP 但客户端收窄为只读；A3 全自主（不推荐） |
| B | 任务模型 | 持久队列 + 状态机 + 租约心跳 + 看门狗；webhook 永远尽快 2xx + `webhook-id` 幂等；per-project 串行。不上 Celery/Redis |
| C | 验证环节 | diff 策略（文件数/行数上限、禁改 CI/锁文件/密钥）+ agent 自跑测试留日志；最终裁判仍是 GitLab CI |
| D | agent 运行环境 | 胖镜像 / 复用 `.gitlab-ci.yml` 的 `image:`（需 DinD，安全税高）/ 不跑测试交给 CI |
| E | 触发协议 | 标签状态机（`ai-fix` → `ai-fix:running` → `:done`/`:failed`）+ 可选评论命令；状态用**单条评论原地编辑** |
| F | Renovate | 拆出本项目（仅共用 token），改用 cron / GitLab pipeline schedule |

### 无论选哪条路，都应立即落实的加固基线

1. codex 启动前**删除 workdir 里的 `.codex/`**（纵深防御，不依赖 trust 语义）
2. 安全关键配置全部走 **`-c`**（压得过项目层）：`sandbox_mode`、`sandbox_workspace_write.network_access`、`approval_policy`
3. **永不**把克隆目录写进 `[projects.*] trust_level='trusted'`
4. **每个 job 独立 `TMPDIR`**（`workspace-write` 默认可写 `/tmp`，并发任务可经 `/tmp` 交换文件）
5. 成功判定四者与运算（见 A 表 #6），`--json` 事件流按 job 落盘
6. **pin 版本**（codex 精确版本 + glab 明确版本）
7. webhook handler **绝不返回 5xx**；入队即 2xx，异常只落库
