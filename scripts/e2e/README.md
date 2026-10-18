# 本机端到端（真 GitLab CE）

用**真的 GitLab** 把「打标签 → 分支 → 提交 → push → draft MR → 状态评论 → 标签流转」
这条链路跑一遍。agent 默认是**桩**（`stub-codex.sh`），因为本机通常没有可用的模型端点；
换成真 codex 只需要一组环境变量。

## 快速开始

```bash
# 首次：拉镜像 + 起 GitLab（3–6 分钟，4–6 GB 内存）
./scripts/e2e/run.sh --keep

# 之后：复用已在跑的 GitLab
./scripts/e2e/run.sh --reuse

# 换真 codex（模型端点必须支持 /v1/responses）
E2E_STUB_AGENT=0 \
MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3 \
MODEL_NAME=deepseek-v4-1-flash-260910 \
LLM_API_KEY=sk-... \
./scripts/e2e/run.sh --reuse --keep
```

跑完会打印 GitLab 地址、root 密码、bot 日志路径与通过/失败汇总。

## 它做了什么

1. `docker compose up` 起 GitLab CE（端口 8929，密码见 `E2E_ROOT_PASSWORD`）
2. 用 `gitlab-rails runner` 造一个带 `api`+`sudo` 的 root token
3. 建 `alice`（Developer=30）、`mallory`（Reporter=20）、`bot` 三个用户
4. 建 group `bot-e2e` + project `demo`，播种 `pyproject.toml`/`uv.lock`/`app.py`/`.gitlab-ci.yml`
5. 打开 `allow_local_requests_from_web_hooks_and_services`，把 webhook 指到宿主机
6. 起网关（`uv run uvicorn`，**生产装配**：真 GitLab + 真 git + 桩/真 codex）
7. alice 建 issue 并打 `ai-fix` 标签（走 Sudo，触发真实 webhook）
8. 断言 15 项：任务成功、分支已推、draft MR、`Closes #N`、report 摘要进描述、
   MR 幂等、状态评论只有 1 条且被原地编辑、标签流转到 `ai-fix:done`、
   `ai-fix:running` 已移除、日志留存、工作目录清理
9. 负面用例：mallory（Reporter < Developer）评论 `/ai-fix` → 必须被闸门拒绝且不产生任务

`--with-runner` 会额外注册 runner 并触发 pipeline（验 M4 的 CI 工具；默认关）。

## 已知的坑（都踩过了）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 容器起来就退出，日志 `Password must not contain commonly used combinations` | GitLab 弱口令库 | 默认密码已改 `Kx7mQ2vT9pL4nR8` |
| 建 webhook 返回 422 `Invalid url given` | 默认禁止 webhook 打本地/内网 | 先 `PUT /application/settings?allow_local_requests_from_web_hooks_and_services=true` |
| 建 issue 返回 `insufficient_scope: sudo` | GitLab 19 要求 token 带 `sudo` scope | root token 用 `api`+`sudo` |
| 建用户 400 `password must not contain...` | 同上弱口令 | 用 `force_random_password: true` |
| 分支删除 404 | 分支名里的 `/` 要 URL 编码 | `ai-fix%2F...` |
| 二次运行播种 400 | `create` 撞已存在文件 | 播种按已存在文件改用 `update` |
| `/-/readiness` 404 | 版本差异 | 用 `/users/sign_in` 200 当就绪判据 |

## 清理

```bash
docker compose -p mgb-e2e -f scripts/e2e/docker-compose.yml down -v
rm -rf .e2e
```
