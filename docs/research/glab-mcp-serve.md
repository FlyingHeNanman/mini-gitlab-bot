# 核查：`glab mcp serve`（GitLab CLI 自带 MCP server）

> 核查方式：读 glab 源码（浅克隆）+ **实际下载并运行 glab v1.123.0 Linux 二进制**，走 stdio JSON-RPC 完成 `initialize` + `tools/list`。
> 标注：【验】= 源码/二进制验证；【档】= 官方文档；【未证】= 未能证实。
> 核查日期：2026-10-16。

## 1. 存在性与状态

| 项 | 结论 |
| --- | --- |
| 存在 | 【验】是，**v1.69.0（2025-09-15）** 引入 |
| 状态 | 【验】**experimental**（v1.72.0 起标注），且 `main` 上已 **DEPRECATED**（尚未进入任何 release） |
| 传输 | 【验】**仅 stdio**；无 HTTP/SSE |
| 参数 | 【验】基本没有：`Args: cobra.NoArgs`，只有 `-h/--help`。实测 `--read-only`、`--hostname` 均报 `Unknown flag` |

- `main` 的 deprecation 文案：`use the GitLab MCP Server (https://docs.gitlab.com/user/model_context_protocol/mcp_server/) instead.`（commit `9d587016`，2026-10-13）
- 最新 release `v1.123.0`（2026-10-13）**还没有**这个字段 → 弃用已合并但未发布。
- 移除计划：「GitLab MCP Server 达到 GA 后的下一个 milestone」；反馈 issue 称 GA 在 **GitLab 19.5**。
- 文档现状：`docs.gitlab.com/cli/mcp/serve/` 已变成**重定向占位页**（`remove_date: 2027-10-07`），glab 不再自己文档化该命令。

## 2. 工具集

**机制【验】**：一个 glab 命令 = 一个工具，名字为 `glab_` + 命令路径（`_` 连接），schema 为 `{args:[...], flags:{...}}`。**实测 v1.123.0 共 217 个工具。**

我们关心能力的对应工具：

| 能力 | 工具 |
| --- | --- |
| 读 issue | `glab_issue_view`、`glab_issue_list` |
| 发 issue 评论 | `glab_issue_note` |
| 开 MR | `glab_mr_create`（30 个 flag） |
| 开 **draft** MR | `glab_mr_create` + `flags.draft`（或 `wip`） |
| 看 MR diff / 评论 | `glab_mr_diff`、`glab_mr_view`、`glab_mr_note_create` 等 |

**写操作完全支持，且没有只读默认【验】**：实测 217 个工具里 **142 个带 `destructiveHint: true`，0 个带 `readOnlyHint: true`**；75 个"非破坏性"工具**完全不带标注**。
（源码注释解释了这个刻意选择：`mcp:safe` 只表示"非破坏性"，该集合里仍包含会重配置 runner 或写磁盘的命令，"声称只读会让客户端自动批准它们"。）

**glab 侧没有 allowlist【验】**：server 不读任何环境变量、无参数，只有编译期源码标注（`mcp:safe`/`mcp:destructive`/`mcp:exclude`/`mcp:interactive`）。只读模式与 `--include`/`--exclude` 都还只是需求 issue（#8353 / #8058）。
> 注意：**客户端侧**（Codex 的 `enabled_tools`/`disabled_tools`）可以过滤，但等于要手工维护 217 个名字的黑/白名单。

**上下文成本【验/档】**：固定约 **113,633 字节**的工具目录，**每个 session 都要付**。官方 MR !3932 原话：*"the catalog is fixed context cost in every session that loads this server"*（该 MR 已把 150,434 → 113,633 字节，−24.5%）。

## 3. 认证与配置

- 【验】server 每次工具调用都 `exec.Command(currentBinary, ...)` 重新起一个自己，`cmd.Env = append(os.Environ(), …)` → **完整复用 glab 的认证**（环境变量 + keyring/配置文件）。
- 令牌优先级【档】：`GITLAB_TOKEN` → `GITLAB_ACCESS_TOKEN` → `OAUTH_TOKEN` → 已存凭据 → CI job token。**环境变量优先。**
- 自管实例推荐纯环境变量方案（无需 `glab auth login`）：`GITLAB_HOST` / `GITLAB_API_HOST` / `GITLAB_SSH_HOST` / `GLAB_API_PROTOCOL=https` / `GITLAB_TOKEN=<PAT>`，且必须存在于 **MCP client spawn 出来的那个 glab 进程**的环境里。
- 写操作需要 PAT 具备 **`api`**（`read_api` 只够读子集，约 113 个写命令会 401/403）。
- 这是普通 PAT + REST v4，**与 GitLab 服务端 MCP 不同**（后者拒绝传统 PAT，需要 OAuth `mcp` scope 或细粒度 PAT）。

## 4. 与 GitLab 服务端 MCP（`/api/v4/mcp`）的区别

- 【验/档】`glab mcp serve` **是客户端实现、与 Duo 无关**，只是本地 stdio server 去调普通 REST API v4。issue #8583 明说它是用来绕开"服务端 MCP 仅 Premium/Ultimate"的。
- 【档】服务端 MCP 现状（**PLAN.md 的说法已过时**）：**已 GA**，Free/Premium/Ultimate 都有；需要管理员/群组开关 "Allow access to the MCP server"；认证要 OAuth `mcp` scope 或 MCP 权限的细粒度 PAT（**传统/项目/群组 token → 403**）；可用 `X-Gitlab-Enabled-Mcp-Server-Toolsets` 限工具集；`duo_agent_platform` 工具集才依赖 Duo Agent Platform。

## 5. 已知限制（多条为实测）

1. **glab 进程必须出网**【验】：`GITLAB_HOST=127.0.0.1:1` 时 `glab issue view 1 --repo foo/bar` 报 `dial tcp 127.0.0.1:1: i/o timeout`。stdio 这一跳不需要网络，但 glab 子进程要真实 HTTPS。
2. **cwd 必须是 git 仓库（或用 `--repo`）**【验】：非仓库目录下 `glab issue view 1` 报 `Could not determine base repository: not a git repository`。cwd 继承自 MCP client。
   - ⚠️ **对我们是硬伤**：issue #8529 记录过 schema 缺 `--repo` 导致**静默写错项目**的 bug，**v1.123.0 才修好**。我们 Dockerfile 里 pin 的 **1.76.2 早于该修复**。
3. 无需 TTY；`glab auth login` 网页流需要浏览器 → 容器里用 `--device` 或直接给 `GITLAB_TOKEN`。凭据默认进 OS keyring，没有 keyring（或 `--insecure-storage`）时**明文写进配置文件**。
4. **无缓存，每次调用起一个子进程**【验】；输出上限 **50,000 字符**，被截断的 JSON 会变成不可解析的碎片（`limit`/`offset` 可用但**刻意不写进 schema**）。
5. 并发隐患（OAuth 刷新相关：#8512、#8390、#8214，均已关闭）——用 PAT 时不涉及。

## 6. 对架构的结论（含一处重要更正）

**更正**：本次核查的收尾结论写了「PLAN.md 的事实 #2 不成立」——**这个判断是错的**，因为它没有检查 glab 进程到底在不在沙箱里。

准确的事实链是：

| 跳 | 是否受 Codex shell 沙箱约束 | 是否出网 |
| --- | --- | --- |
| agent 的 shell 命令 | **是**（Landlock + seccomp，`network_access` 默认 false） | 否 |
| Codex → `glab mcp serve`（stdio） | **否**（`rmcp-client` 用裸 `Command::new`，无任何沙箱包装） | 本地管道 |
| `glab mcp serve` → `glab <cmd>` → GitLab | **否**（沙箱子进程的后代，同样无沙箱） | **是，HTTPS** |

所以 **PLAN.md 事实 #2「MCP 进程不受 shell 沙箱网络限制」成立** —— 收尾结论里列的"方案 1（MCP server 在受限沙箱之外启动）"恰好就是 Codex 的默认行为，不是假设。

**但结论仍然是：放弃 `glab mcp serve`。** 理由与网络无关，且比网络问题更硬：

1. **官方已弃用**（main 上已有 `Deprecated`，计划在 GitLab 19.5 后移除），且一直是 experimental。
2. **217 个工具 / ~114 KB 固定上下文**，每个 session 都付；对 coder 模型是实打实的工具选择干扰与 token 成本。
3. **无只读模式**：142 个破坏性工具默认暴露；glab 侧无法收口。
4. **我们 pin 的 1.76.2 早于"静默写错项目"修复（v1.123.0）**。
5. 每次调用起进程 + 50k 截断 → 长输出不可靠。
6. 若采纳「网关独占 GitLab 写、agent 零凭据」，这套组件**根本不需要存在**。

## 7. 来源可靠性

- ✅ 采用：glab 源码（gitlab.com raw，含 pinned tag）、真实二进制实测、docs.gitlab.com、docs.renovatebot.com。
- ❌ 未采信：`about.gitlab.com` 博客（本环境超时无法访问，全文未验证）。
- 证据文件（临时）：`/tmp/glabcli`（克隆）、`/tmp/bin/glab`（v1.123.0）、`/tmp/tools_list.json`、`/tmp/toolnames.txt`。
