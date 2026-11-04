# Dashboard（只读观测台）设计

> 状态：v0.3 第一期（只读 MVP）
> 决策：HTTP Basic（用户名 + 密码），挂在现有 FastAPI 服务，只看当前 SQLite，手动刷新，第一期不提供操作能力。

---

## 1. 目标与非目标

**目标**

1. 值班/运维和开发者都能在一个页面看到机器人当前是否正常。
2. 把「队列积压 / 最近失败 / 项目状态 / sweep 与 release 结果」集中展示。
3. 复用现有 FastAPI、SQLite、Metrics，不引入前端工程链与新服务。
4. 页面只读：所有数据来自数据库或进程内指标，不写库、不调 GitLab 写接口。

**非目标（第一期）**

- 重试 / 取消任务 / 手动触发 triage、sweep、release；
- 修改配置、编辑标签、关闭 issue；
- 多实例聚合、跨实例告警、实时推送（SSE/WebSocket）；
- 面向公网；Dashboard 仍应只在内网或反向代理 TLS 后暴露。

---

## 2. 鉴权与开关

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `DASHBOARD_ENABLED` | 总开关；`false` 时 `/dashboard*` 一律 404 | `true` |
| `DASHBOARD_USERNAME` | HTTP Basic 用户名 | `admin` |
| `DASHBOARD_PASSWORD` | HTTP Basic 密码；**空 = 关闭**（fail closed） | 空 |

- 浏览器访问 `/dashboard` 会收到 `401 + WWW-Authenticate: Basic`，由浏览器弹框；登录后同一浏览器会话内自动携带凭据。
- 使用 `hmac.compare_digest` 做常量时间比较，用户名和密码都校验，不把凭据写进 URL、Cookie 或日志。
- 密码为空时**不会**生成默认密码或放行，页面直接 404；启动日志会给出警告。
- 与 `/tasks` 的 `ADMIN_TOKEN` 完全独立：Dashboard 不接受 `X-Admin-Token`、`?token=`，避免两种鉴权混用。

> HTTP Basic 是明文传输（除非外层 TLS）。生产环境应通过反向代理启用 HTTPS，或只绑定内网地址。

---

## 3. 数据范围：单实例、多项目、多实例分别是什么

这三个概念容易混：

- **单实例（当前范围）**：一个机器人进程 + 一个 SQLite 数据库。Dashboard 只读这个库。
- **多项目**：同一个实例可以在 `REPO_WHITELIST` 里配置多个项目；所有项目的任务都在同一个 SQLite 里。Dashboard 的「项目」表就是按 `project_id + path` 聚合，所以**当前就能看到多个项目**，这不是多实例。
- **多实例聚合**：部署了多个机器人（例如每个 GitLab 实例/每个团队一个进程，各自有独立 SQLite），再做一个上层页面把多个实例的数据汇总。第一期**不做**：没有跨实例数据源、没有统一 ID、也没有聚合查询；每个实例看自己的 `/dashboard`。

换句话说：当前 Dashboard 会展示「这个实例看过的所有项目」，但不会去另一个实例的数据库取数。

---

## 4. 页面结构（MVP）

`GET /dashboard` 单页，锚点分区：

1. **健康条**：Worker 是否启用、是否有租约过期、24h 失败、队列积压、当前执行中。
2. **运行概况**：总任务、排队、执行中、已完成、失败、24h 新增/成功/失败。
3. **最近任务**：最近 20 条，含类型、项目/issue、状态、耗时、失败原因；点击进入任务详情。
4. **需要关注**：最近 8 条失败任务，突出失败阶段与原因。
5. **项目**：按最后活动时间倒序，最多 50 个项目；显示总数/进行中/成功/失败。
6. **运维指标**：`sweep_*` 与 `release_*` 计数器；明确标注「进程启动以来累计，重启归零」。
7. **任务详情** `/dashboard/task/{id}`：基本信息、失败证据、事件时间线、日志尾部、GitLab issue/MR 链接。

时间统一 UTC；页面顶部显示上次刷新时间，刷新按钮是普通链接 `/dashboard`，不使用 meta refresh。

---

## 5. 数据来源

| 数据 | 来源 |
| --- | --- |
| 状态分布 / 24h 计数 | `SqliteStore.dashboard_stats(since)`（`tasks` 表） |
| 项目聚合 | `SqliteStore.project_summaries(limit)`（`GROUP BY project_id, path`） |
| 最近任务 / 失败任务 | `SqliteStore.list_tasks(...)` |
| 任务详情事件 | `SqliteStore.events(task_id)` |
| Worker / 卡死 | `Runtime.worker` + `_has_stalled_task`（租约过期阈值 `READY_STALE_SEC`） |
| sweep/release 指标 | `Metrics.value(...)`（进程内累计） |
| issue / MR 链接 | `GITLAB_URL` + 项目 `path` + issue iid / 任务记录的 `mr_url` |

**边界**

- Dashboard 不直接调用 GitLab API；链接只是拼接 URL，拿不到实时 issue 状态。
- 24h 窗口按 `created_at`；「失败/成功」按任务**当前终态**统计，不是按完成时间。
- 指标重启归零，不能当作长期审计数据；长期趋势仍应看 Prometheus `/metrics` 或数据库。
- 失败原因是外部输入（模型/命令输出），渲染时统一 HTML 转义。

---

## 6. 安全边界

- 只读 GET；写方法不存在（第二期再加操作时，必须单独设计 CSRF、审计与权限）。
- 所有外部文本 HTML 转义，防止项目路径、失败原因、事件 detail 注入。
- 不回显 `ADMIN_TOKEN`、GitLab token、模型 key、完整 workdir。
- 数据读取失败返回 503 文本，不把异常堆栈带回页面。
- Basic 凭据不写日志；启动日志只说明 Dashboard 是否启用。

---

## 7. 第二期候选（未实现）

- 失败任务「重试」、非终态任务「取消」；
- 手动触发 triage / sweep / release；
- 操作审计（谁、何时、对哪个任务）；
- 多实例聚合与失败告警；
- 实时刷新（SSE），但仍保持服务端渲染的简单性。

这些都需要先解决权限（Dashboard 用户与 GitLab 用户的关系）、CSRF、幂等与审计，不能直接加按钮。
