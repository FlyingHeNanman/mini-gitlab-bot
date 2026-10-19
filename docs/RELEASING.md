# 发布与分支约定

## 分支模型

```
feature/*  ──PR──▶  dev  ──PR──▶  main  ──tag vX.Y.Z──▶  Release + GHCR 镜像
```

| 分支 | 用途 | 规则 |
| --- | --- | --- |
| `main` | 发布分支，只接受来自 `dev` 的合并 | 每次合并后在 `main` 上打 tag；不直接提交功能 |
| `dev` | 集成分支，日常开发都落在这里 | 功能分支 `feature/xxx` 提 PR 到 `dev` |
| `feature/*` | 单个改动 | 从 `dev` 切出，合并后删除 |

> 建议在 GitHub 上给 `main` 开启分支保护（禁止直接 push、要求 PR 与 CI 通过）。

## 发版步骤

```bash
# 1. 确认 dev 是绿的
git switch dev && git pull
uv run ruff check . && uv run pytest -q

# 2. 提升版本号（两处必须一致，测试会校验）
#    pyproject.toml 的 version 与 core/__init__.py 的 __version__
vim pyproject.toml core/__init__.py

# 3. 更新 CHANGELOG.md（新增一节，写清 Added/Changed/Fixed）

# 4. 合并到 main（走 PR 或本地 merge）
git switch main && git merge --no-ff dev
git push origin main

# 5. 打 tag 并推送 —— 这一步会触发 release.yml
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0

# 6. 把 main 合回 dev，避免版本号分叉
git switch dev && git merge --no-ff main && git push origin dev
```

## 打 tag 之后会发生什么

`.github/workflows/release.yml`：

1. 跑一遍 `ruff check` + `pytest`（**红了就不发**）
2. 构建镜像并推送到 `ghcr.io/flyinghenanman/mini-gitlab-bot`，标签为
   `v0.1.0`、`0.1`、`latest`
3. 用 GitHub 自动生成的 release notes 创建 Release

镜像拉取：

```bash
docker pull ghcr.io/flyinghenanman/mini-gitlab-bot:0.1.0
```

## 版本号约定

语义化版本 `MAJOR.MINOR.PATCH`：

- **MAJOR**：破坏兼容（配置项改名/删除、任务状态机语义变化）
- **MINOR**：向后兼容的功能（新工具、新端点、新配置项）
- **PATCH**：修复与文档

预发布用 `-rc.1` 后缀；`0.x` 阶段允许 MINOR 里带小破坏，但要在 CHANGELOG 写清迁移方式。

## 注意事项

- `release.yml` 里镜像名写死为小写 `flyinghenanman`：GHCR 不接受大写仓库名，
  不能用 `${{ github.repository }}`。
- 想改 codex 版本：在仓库 **Variables** 里设置 `CODEX_VERSION`（或改 workflow 默认值），
  同时同步 `.env` 的 `CODEX_EXPECTED_VERSION`。
- 发布前请确认 `Dockerfile` 能构建（本机：`docker build -t mini-gitlab-bot:test .`）。
