# AGENTS.md

本文件是给在本仓库工作的编码 agent 的约束。它与 `PLAN.md`、`README.md` 一起使用；
发生冲突时，**用户的最新明确要求 > 本文件 > 其他文档**。

## 0. UI 的硬性前置：先读 `DESIGN.md`

**任何界面（HTML、CSS、模板、预览页、空态、按钮、表格、表单）的设计与实现，
必须先完整阅读根目录的 [DESIGN.md](DESIGN.md)，并以它作为唯一视觉规范。**

具体要求：

- 颜色只用 `DESIGN.md` 的 token：Action Blue `#0066cc` 是**唯一**交互强调色；
  不要引入第二个品牌色/强调色（状态可以用 ink / muted / tile 的明暗来表达）。
- 字体：Display 用 `SF Pro Display, system-ui, -apple-system, ...`；
  Body 用 `SF Pro Text, system-ui, -apple-system, ...`。正文 17px / 400 / 1.47，
  display 标题 600 且带负字距；不要用 500 字重。
- 间距按 8px 基准；section 80px、卡片 24px、圆角只用 `sm(8)` / `lg(18)` / `pill`。
- 不要加装饰性渐变；不要给卡片/按钮/文字加阴影。唯一允许的 drop-shadow 是
  产品摄影图落在表面时使用，本项目的 Dashboard 没有产品图，因此不要用阴影。
- 主 CTA 用 Action Blue 全圆角 pill；次 CTA 用透明底 + Action Blue 描边 pill；
  press 状态统一 `transform: scale(0.95)`；focus 用 `#0071e3` 的 2px outline。
- 版面遵循 DESIGN.md 的脉冲：浅色/ parchment hero → 深色 tile → 浅色 utility →
  深色 tile → parchment footer；全 bleed tile 不要圆角，靠表面色变化做分隔。
- 响应式至少覆盖 1440 / 1068 / 833 / 640 / 419 断点；触控目标不小于 44px。
- **不要为了 UI 引入新的 Web 框架、构建链或外部字体/图片依赖**；继续用
  FastAPI 服务端渲染 + 纯 CSS。若必须偏离 DESIGN.md，先在回复里说明 token 缺口，
  得到用户确认后再做。
- 不要修改 `DESIGN.md`，除非用户明确要求更新设计规范。

UI 改动完成前至少验证：

1. `uv run --frozen ruff check .`
2. `uv run --frozen ruff format --check .`
3. `uv run --frozen pytest -q`
4. 本地起预览并给出可访问 URL（用户明确要求“看效果”时）。

本地 Dashboard 预览：

```bash
uv run python scripts/preview_dashboard.py
# http://127.0.0.1:8099/dashboard
# Basic Auth: admin / preview2026
```

## 1. 项目结构与边界

- `core/`：纯领域逻辑与纯渲染函数，**不做 IO**，可单测。
- `ports/`：协议/数据契约。
- `adapters/`：GitLab、SQLite、git、codex、文件系统等实现。
- `gateway/`：FastAPI 入口、配置、运行时装配、鉴权与路由。
- 依赖方向：`gateway → adapters → ports → core`，`core` 不反向依赖。
- 不要绕过端口直接在 `core/` 里发 HTTP、读数据库或调子进程。

## 2. 安全红线

- 不提交、不回显、不写日志：GitLab token、模型 key、admin token、Dashboard 密码。
- Dashboard 使用 HTTP Basic；密码为空时**关闭页面**（fail closed），不要生成默认密码。
- 所有外部文本（项目路径、issue 标题、失败原因、事件 detail）进 HTML 前必须转义。
- `/healthz` 保持信息最少；任务详情只走受保护的 `/tasks` 或 `/dashboard`。
- 只读页面不要偷偷加写操作；操作能力必须先设计权限、CSRF、审计和幂等。
- 不把 Basic 凭据放进 URL、Cookie、localStorage 或日志。

## 3. 开发与测试

```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
```

- Python 3.13；类型注解优先；公开函数写中文 docstring，标识符用英文。
- 新行为必须有测试：单元测试放 `tests/unit/`，HTTP/适配器集成放 `tests/integration/`。
- 修 bug 先写能复现的测试；不要只改实现。
- 测试不依赖真实 GitLab / LLM / 外网；真机验证放 `docs/verification/`，不要把 secrets 写进去。
- 文档面向中文读者；配置项、端点、状态值保持代码里的英文原名。

## 4. 分支、提交与发布

- 分支模型：`feature/*` → `dev` → `main` → tag `vX.Y.Z`。
- 未经用户明确要求，**不要 push、不要打 tag、不要发 Release**。
- 提交信息用 Conventional Commits（如 `feat(dashboard): ...` / `fix(sweep): ...`），
  一个提交只做一件事；测试与文档随功能提交。
- 版本号只在发版提交里改，四处保持一致：
  `pyproject.toml` / `core/__init__.py` / `core/mcp_spec.py` / `uv.lock`。
- 不要在用户没要求时升级依赖、重写无关模块或“顺手重构”旧代码。

## 5. 当前已知的 UI 上下文

- `/dashboard` 是只读观测台：HTTP Basic、单实例 SQLite、手动刷新、无重试/取消/手动触发。
- `/tasks` 是 token 保护的只读任务页；两者的鉴权不要混用。
- Dashboard 的视觉规范以 `DESIGN.md` 为准；`core/dashboard_view.py` 中的 CSS
  变量应从 DESIGN.md 的 token 映射而来。
