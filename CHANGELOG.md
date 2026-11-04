# Changelog

本项目的所有重要变更都记在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **免 clone 部署**：`docker run` / `docker compose up -d` 直接拉镜像启动，不再要求先复制 `.env`；首次打开 `/setup` 初始化，配置写入 `/data/config.json`（0600）。
- **初始化向导**：基础变量（GitLab、Webhook、项目白名单、模型、管理员）与高级变量（并发、配额、Sweep、Release、diff、保留策略）分层；保存后重启生效。
- **配置页**：登录后 `/settings` 可修改基础/高级变量；secret 不回显，留空保持；保存做 Origin 校验、原子写入。
- **登录页 + session**：`/login` 替代浏览器 HTTP Basic 弹框；HMAC 签名 HttpOnly cookie（SameSite=Strict，12h），改密码旧 session 失效。
- **Dashboard 重做**：按新版 `DESIGN.md` 的「云图柔板 / Dusty Canvas」重写总览、失败中心、项目列表、7 日趋势、任务详情、登录与配置页。
- **导航拆页**：最近任务/项目从页面内锚点改为 `/dashboard/recent`、`/dashboard/projects` 独立页，导航栏按当前页高亮。

### 测试

- 全量 **677 项**测试通过，`ruff check` / `ruff format --check` 干净。

## [0.3.0] - 2026-11-04

### 新增

- **只读 Dashboard**（`GET /dashboard`）：HTTP Basic 鉴权；总览健康/队列/24h 统计、最近任务、失败中心、项目聚合与 sweep/release 进程指标；手动刷新，不提供重试/取消/手动触发。当前实例只看自己的 SQLite。
- **Dashboard 视觉规范**：按 `DESIGN.md` 的 Apple 式产品画廊语言重做界面——深黑 global nav、parchment 磨砂 sub-nav、浅色/深色 tile 交替、Action Blue 单一交互强调色、SF Pro 字体层级、pill CTA 与无阴影 utility card。
- **任务详情**：`/dashboard/task/{id}` 展示任务档案、失败证据、事件时间线与日志尾部。
- **协作与预览**：新增 `AGENTS.md`（UI 必须遵循 `DESIGN.md`）与 `scripts/preview_dashboard.py`；本地可起带样例数据的预览，不依赖真 GitLab。

### 测试

- 全量 **655 项**测试通过，`ruff check` / `ruff format --check` 干净；覆盖 Basic Auth、只读边界、HTML 转义、SQLite 汇总与 DESIGN token 约束。

## [0.2.1] - 2026-11-04

### 修复

- **兼容 GitLab 默认 autoclose**：sweep 除 open issue 外，同时扫描近 `SWEEP_COMMIT_LOOKBACK_DAYS` 天更新过的 closed issue；GitLab 用 `Closes/Fixes #N` 先关闭时，机器人仍补回帖与 `状态：已解决`，不再调用 close。closed issue 通过评论 marker 幂等跳过，重启/重跑不重复计数或刷评论。
- **新项目标签缺失**：sweep 在 `add_labels` 前用 `LabelEnsurer` 幂等创建固定标签，避免新项目首次扫描 400。
- **Release compare 链接缺失**：真 GitLab tag 对象没有顶层 `web_url`，改为从 `commit.web_url` 推导 tag 链接。

### 测试

- 全量 **640 项**测试通过，`ruff check` 干净；真 GitLab CE 19.5.3 验证默认 autoclose 路径 9/9。

## [0.2.0] - 2026-11-03

在 v0.1.0 的事故式修复链路上，补齐两条「不需要人工盯着」的收尾能力：**周期扫描开放 issue 并用 commit 引用自动回帖/关闭**，以及 **tag push 后自动补 GitLab Release 文案**。两者都复用已有的持久队列、标签、评论与状态机，不引入新的运行时依赖。

### 新增

- **周期扫描（`SWEEP_*`）**：每 30 分钟（可配）扫白名单项目，做两件事：
  - **补偿巡检**：对「open 且没有任何 `状态：` 标签」的 issue 补建 triage 任务，按每项目每小时配额限流，解决 webhook 漏投（GitLab 连续失败会临时/永久禁用 hook）导致的漏单。
  - **commit 引用回收**：扫默认分支最近 N 天提交，识别 `Closes/Fixes/Resolves #N`（含 `#1, #2`、`#1 and #2`），对仍 open 的 issue 回帖「已由提交修复」、打 `状态：已解决` 并关闭；评论带 marker，重跑原地更新，不刷屏。
  - 新增 `TriggerKind.SWEEP`、`IssueSummary` / `CommitInfo` 模型，以及 `resolve_project` / `list_open_issues` / `list_recent_commits` 端口与 GitLab 实现。
- **自动 Release（`RELEASE_*`）**：监听 GitLab **Tag Push Hook** 的新建 tag（忽略分支 push、删 tag、移动/重推 tag），入队 `release` 任务：
  - 找到上一个 tag，用 compare API 取提交范围；首个 tag 退回最近提交；
  - 按 Conventional Commits（`feat` / `fix` / `perf` / `refactor` / `docs` / `test` / `build` / `ci` / `chore` / `style` / `revert`）分组，生成中文 Markdown 发布说明，附提交短链接、对比链接、破坏性变更标记；
  - 已有 Release 直接跳过，不覆盖人工改写的说明；重复投递/重试幂等。
  - 新增 `TriggerKind.TAG_PUSH`、`TaskKind.RELEASE`、`TagInfo` / `ReleaseInfo` 模型，以及 `list_tags` / `compare_commits` / `get_release` / `create_release` 端口与 GitLab 实现。
- **标签**：状态组新增 `状态：已解决`，固定标签从 31 个增至 32 个；triage/explain 的状态标签流转保持兼容。
- **运维指标**：`sweep_runs_total` / `sweep_triaged_total` / `sweep_resolved_total` / `sweep_errors_total`、`release_total` / `release_skipped_total` / `release_failures_total`。

### 修复

- **Janitor 未接入 Worker**：v0.1.0 装配了清理器却没有传给 worker，导致保留策略在生产中不生效；v0.2.0 已修复，Worker 的常驻维护循环同时启动 janitor 与 sweeper。

### 测试

- 全量 **633 项**测试通过，`ruff check` 干净；新增周期扫描、commit 引用解析、Release 说明生成、Tag Push 解析、Release 端口（真 HTTP 打桩 GitLab）与两条端到端流水线测试。

## [0.1.0] - 2026-10-19

首个可部署版本：给 GitLab issue 打上 `ai-fix` 标签，机器人自动跑完「准备环境 → Codex 修复 → 校验 → push → draft MR → 状态评论 → 标签流转」整条链路。

### 新增

- **网关与触发器**：GitLab Webhook（secret 校验、项目白名单、标签触发、`/ai-fix` 评论命令、幂等去重）、SQLite 持久队列、状态机（含终态不可变与恢复边）、租约 + 心跳 + 看门狗、崩溃重启恢复；任何情况都不返回 5xx。
- **权限闸门**：`GET /projects/:id/members/all/:user_id` 校验 `access_level >= 30`（Developer），可叠加 `ALLOWED_TRIGGER_USERS` 白名单；404 一律拒绝，成员关系缓存 60s。
- **两阶段执行**：网关侧（联网）`uv sync` 准备依赖 + 离线自检；agent 侧（`workspace-write`，无网络无凭据）跑 Codex。
- **Codex 适配器**：`codex exec --json`，安全关键配置全部用 `-c` 钉死（sandbox / network / approval），启动前删除 `.codex/`，四条件判定（exit 0 + `turn.completed` + `-o` 新写入 + diff 非空），超时/取消杀进程组，JSONL 落盘保留。
- **git 适配器**：clone / 建分支 / 兜底提交 / diff / push；凭据只经 `GIT_ASKPASS` + 环境变量，绝不进 argv；`.git/info/exclude` 防本地产物入库，并在 `git add` 后做后置校验。
- **GitLab 适配器**：draft MR（`Draft:` 前缀 + 先查后建 + 409 兜底）、状态评论按 marker 原地编辑、标签增量更新（`add_labels`/`remove_labels`）、issue 上下文预取（丢弃 system note）。
- **只读迷你 MCP**：11 个只读工具（issue / 搜索 / 关联 MR / MR 详情 / pipeline / job / CI 日志 / 测试报告 / commit diff / 最近提交 / 项目信息），项目与 issue 由网关经环境变量绑定（工具 schema 里没有 `project` 参数），只认 `read_api` token，输出默认截断 20k 字符；环境里出现写 token 则拒绝启动。
- **运维**：JSON 结构化日志（`task_id` 上下文）、`/healthz` `/readyz` `/metrics`、保留策略（日志 14 天 / 终态任务 90 天 / 孤儿 job 目录 1 天）、可选只读任务页 `/tasks`（默认开启，token 自动生成并持久化）。
- **部署**：多阶段 Dockerfile（Python 3.13 + uv + codex，全部 pin 版本，非 root，HEALTHCHECK，`PORT` 可覆盖）、docker-compose（seccomp/apparmor 放行 codex 沙箱、双卷、`init`）、GitHub Actions（CI + tag 触发发布 GHCR 镜像与 Release）。
- **测试**：444 项（单元 + 集成），包含真子进程的 codex/MCP 测试、真 HTTP 的 GitLab 桩、真 git 仓库；另有 `scripts/e2e/` 可在真 GitLab CE 上跑完整闭环。

### 已知限制

- 只支持 Python 项目（uv 管理）。
- agent 阶段的网络与凭据是硬隔离；需要 MCP 时只给只读 token。
- 真机验证过 GitLab CE 19.5 与 codex 0.157.1；更老/更新版本未逐一验证。

[0.2.0]: https://github.com/FlyingHeNanman/mini-gitlab-bot/releases/tag/v0.2.0
[0.1.0]: https://github.com/FlyingHeNanman/mini-gitlab-bot/releases/tag/v0.1.0
