# Changelog

本项目的所有重要变更都记在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[0.1.0]: https://github.com/FlyingHeNanman/mini-gitlab-bot/releases/tag/v0.1.0
