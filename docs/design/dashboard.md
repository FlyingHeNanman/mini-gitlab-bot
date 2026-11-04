# Dashboard（只读观测台）设计

> 状态：v0.3.x 只读观测台 + 初始化/配置；视觉按根目录 `DESIGN.md` 的「云图柔板 / Dusty Canvas」。
> 部署：镜像直跑或 compose，不再要求先 clone / 复制 `.env`；首次打开 `/setup` 初始化。

---

## 1. 目标与非目标

**目标**

1. 值班/运维和开发者都能在一个页面看到机器人当前是否正常。
2. 把「队列积压 / 最近失败 / 7 日趋势 / 项目状态 / sweep 与 release 结果」集中展示。
3. 首次部署不用编辑 `.env`：在浏览器里配置基础变量，高级变量有默认值、按需展开。
4. 复用现有 FastAPI、SQLite、Metrics，不引入前端工程链与新服务。
5. 页面只读：所有数据来自数据库或进程内指标，不写库、不调 GitLab 写接口。

**非目标（当前）**

- 重试 / 取消任务 / 手动触发 triage、sweep、release；
- 多实例聚合、跨实例告警、实时推送（SSE/WebSocket）；
- 面向公网；Dashboard 仍应只在内网或反向代理 TLS 后暴露。

---

## 2. 鉴权、初始化与配置

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `DASHBOARD_ENABLED` | 总开关；`false` 时 `/dashboard*` 一律 404 | `true` |
| `DASHBOARD_USERNAME` | 登录用户名 | `admin` |
| `DASHBOARD_PASSWORD` | 登录密码；**空 = 关闭**（fail closed） | 空 |
| `CONFIG_FILE` | 初始化/配置页写入的 JSON；容器默认 `/data/config.json` | `/data/config.json` |

**首次部署流程**

1. 容器直接启动（不需要 `.env`）；`/setup` 在未初始化时开放。
2. `/setup` 把基础变量 + 高级变量写入 `CONFIG_FILE`，文件权限 0600；页面不回显已保存的 secret。
3. 保存后配置只在下次启动读取，因此页面提供「重启服务」按钮；容器 `restart: unless-stopped` 时自动拉起。
4. 重启后 `/setup` 自动关闭，打开 `/login` 登录。

**登录**

- `POST /login` 校验用户名/密码，设置 HMAC 签名的 HttpOnly session cookie（`SameSite=Strict`，12 小时）。
- session 只含用户名与过期时间；签名 key 由 Dashboard 密码派生，改密码即让旧 session 全部失效。
- 未登录访问 `/dashboard*` 或 `/settings` 返回 303 到 `/login?next=…`；不会弹浏览器 Basic 框。
- 密码为空时 `/login` 与 `/dashboard*` 都是 404（或未初始化时 303 到 `/setup`）；不生成默认密码。
- 与 `/tasks` 的 `ADMIN_TOKEN` 完全独立：Dashboard 不接受 `X-Admin-Token`、`?token=`。

**配置页**

- `/settings` 需登录；把变量分「基础变量」和「高级变量」两块，secret 留空表示保持不变。
- 保存后同样需要重启；POST 校验 `Origin` 与 `SameSite=Strict`，避免跨站提交。
- 配置文件保存时用临时文件 + `os.replace` 原子写入，失败不会留下半份配置。

> 生产环境应通过反向代理启用 HTTPS；当前 session cookie 未强制 `Secure`，以便本地 HTTP 预览。

---

## 3. 数据范围：单实例、多项目、多实例分别是什么

- **单实例（当前范围）**：一个机器人进程 + 一个 SQLite 数据库。Dashboard 只读这个库。
- **多项目**：同一实例可在 `REPO_WHITELIST` 配多个项目，任务都在同一 SQLite；Dashboard 的「项目」列表按 `project_id + path` 聚合，所以**当前就能看到多个项目**。
- **多实例聚合**：多个机器人进程/多个 SQLite 再做一个上层汇总；当前**不做**，每个实例看自己的 `/dashboard`。

---

## 4. 视觉规范

Dashboard / 登录 / 初始化 / 配置页共用 `core/ui_theme.py`，所有页面都以 `DESIGN.md` 为唯一依据：

- 页面外框 `#B0B7CE`；应用容器 `#F0F1F4`、28px 圆角、三档冷色阴影；白卡片 22px 圆角 + 软阴影。
- 标题/大数字衬线（Source Serif 4 / Georgia / Songti SC）；正文无衬线（Inter / PingFang SC）。
- 珊瑚红 `#E24D33` 只做品牌块/单一关键数字；失败用 `#C2503A` + 浅红底，警告用 `#B9822A` + 浅黄底。
- 卡片不描边，靠阴影分层；按钮/胶囊全圆角，主按钮深海军蓝；列表行优先于高密度表格。
- 响应式覆盖 1280 / 1024 / 768 断点；触控目标至少 44px。

---

## 5. 页面结构

导航拆成四个独立页，当前页高亮；不再用页面内锚点冒充“切换”。

- `GET /dashboard`：总览，`总览` 高亮。
- `GET /dashboard/recent`：最近任务独立页，展示最多 50 条，`最近任务` 高亮。
- `GET /dashboard/projects`：项目独立页，展示最多 50 个项目，`项目` 高亮。
- `GET /settings`：配置页，`配置` 高亮。

`GET /dashboard` 内容：

1. **顶部导航**：品牌、总览/最近任务/项目/配置、健康 pill、账号与退出。
2. **Hero**：健康结论、刷新时间、版本、刷新按钮与配置入口。
3. **主栅格 7/5**：左侧 7 日任务趋势（柱线 + 今日数字 + 24h 成功率），右侧失败中心。
4. **底部 3 列**：运行概况、最近任务（前 8 条 + “查看全部”）、项目（前 6 个 + “全部 N 个”）。
5. **进程指标**：`sweep_*` / `release_*` 计数器，注明重启归零。
6. **页脚**：只读声明与 UTC 时间。

`GET /dashboard/task/{id}`：任务档案、失败证据、事件时间线、日志尾部、issue/MR 链接；顶栏高亮 `最近任务`，返回链接回到最近任务页。

`GET /login`：登录卡；`GET /setup`：初始化表单；`GET /settings`：基础/高级配置。

时间统一 UTC；刷新是普通链接 `/dashboard`，不使用 meta refresh。

---

## 6. 数据来源

| 数据 | 来源 |
| --- | --- |
| 状态分布 / 24h 计数 | `SqliteStore.dashboard_stats(since)` |
| 7 日趋势 | `SqliteStore.daily_counts(since)`（按 UTC `created_at` 日期） |
| 项目聚合 | `SqliteStore.project_summaries(limit)` |
| 最近任务 / 失败任务 | `SqliteStore.list_tasks(...)` |
| 任务详情事件 | `SqliteStore.events(task_id)` |
| Worker / 卡死 | `Runtime.worker` + `_has_stalled_task` |
| sweep/release 指标 | `Metrics.value(...)`（进程内累计） |

**边界**

- Dashboard 不直接调 GitLab API；issue/MR 链接只做 URL 拼接。
- 24h 窗口按 `created_at`；成功/失败按任务**当前终态**统计。
- 指标重启归零，长期趋势看 Prometheus `/metrics` 或数据库。
- 失败原因是外部输入（模型/命令输出），渲染时统一 HTML 转义。

---

## 7. 安全边界

- 只读 GET；写方法只存在于 `/login`、`/logout`、`/setup`、`/settings`，且都做 Origin 校验（登录除外）。
- `/setup` 仅在未初始化时开放；初始化后一律 303 到 `/login`。
- 所有外部文本 HTML 转义，防止项目路径、失败原因、事件 detail 注入。
- 配置页不回显 GitLab token、模型 key、Dashboard 密码；只显示“已配置，留空保持不变”。
- 配置文件 0600，原子写入；保存失败不覆盖旧配置。
- 数据读取失败返回 503 文本，不把异常堆栈带回页面。
- session 不写日志；登录失败只记录被截断的用户名，不记录密码。

---

## 8. 第二期候选（未实现）

- 失败任务「重试」、非终态任务「取消」；
- 手动触发 triage / sweep / release；
- 操作审计（谁、何时、对哪个任务）；
- 多实例聚合与失败告警；
- 实时刷新（SSE）。

这些都需要先解决权限（Dashboard 用户与 GitLab 用户的关系）、CSRF、幂等与审计，不能直接加按钮。
