# 文案模板定案（v1）

> 对应 [PLAN.md](../PLAN.md) §12 第 3 条。风格参考 astral-sh/uv 的 release notes：
> 分类式小节、命令式短句、反引号标识符、开头一行元信息、**显式的"风险与未决"分区**。
> 模板中的 `{{...}}` 是网关渲染时的占位符，不是 GitLab 变量。

## 一、硬性规则

1. **MR 一律 draft**：标题必须是 `Draft: ` 前缀（GitLab 只认标题前缀，`WIP:` 已废弃，API 无 `draft` 参数）。
2. **MR 描述必须含 `Closes #{{issue_iid}}`**（首行"概要"里）。
3. 状态只用**单条评论原地编辑**，首行隐藏标记用于定位：
   `<!-- mini-gitlab-bot:status {{task_id}} -->`
4. 渲染前对 agent 原始输出做**长度截断**（日志尾部默认 80 行 / 8k 字符），并标注被截断。
5. 所有文本中**不得出现任何凭据**（token、带密码的 URL、`http.*://user:pass@`）。
6. agent 无网络装不了新依赖 → 一律走"需要新增依赖"条目，**不得**让 MR 里出现未安装却被引用的依赖。
7. agent 的结构化输出走文件而不是 stdout：agent 必须把 `report.json` 写进
   `{{workdir}}/.mini-gitlab-bot/report.json`（schema 见 §四）。缺字段就退化，
   但**不允许**因为报告缺失而判任务失败 —— 报告是给人看的，不是判据。

---

## 二、MR 描述模板

```markdown
## 概要
Closes #{{issue_iid}}

由 `mini-gitlab-bot` 自动生成（draft，需人工审批）。触发：@{{trigger_user}} 于 {{trigger_time}} 打上 `ai-fix` 标签。
任务：`{{task_id}}`

**摘要**：{{summary}}
（`summary` 为空时整行省略）

## 改动
### 修复
{{#each fixes}}- {{this}}
{{/each}}
### 重构 / 其他
{{#each others}}- {{this}}
{{/each}}
### 测试
{{#each tests}}- {{this}}
{{/each}}

## 验证
- 命令：`UV_OFFLINE=1 uv run --no-sync pytest -q`
- 结果：`{{test_summary}}`，耗时 {{test_duration}}
- 未运行：{{not_run}}

## 风险与未决
{{#each risks}}- ⚠️ {{this}}
{{/each}}
{{#each deps}}- ⚠️ 需要新增依赖：`{{this}}`（沙箱无网络，未安装、未写入 `pyproject.toml`）→ **请人工判断**
{{/each}}

## Agent 元信息
| 项 | 值 |
| --- | --- |
| 内核 | codex `{{codex_version}}` |
| 任务 | `{{task_id}}` |
| 分支 | `{{branch}}` |
| 提交 | `{{commit_sha}}` |
| 耗时 | {{elapsed}} |

<sub>自动生成 · 合并前请人工审查</sub>
```

**填写要求**

- "改动"每条必须带 `path:line` 定位（如 `` `gateway/config.py:42` ``），无定位的条目删掉。
- "风险与未决"**允许为空**，但不允许用"无"敷衍；确实没有风险时写 `- ✅ 未发现新增风险`。
- 只改代码不写测试时，必须在"测试"区写 `- （无新增测试）`，不要留空。
- 未运行项（如项目没配 `ruff`/`mypy`）要显式列出，避免读者以为跑过了。

---

## 三、状态评论模板（单条原地编辑）

### 3.1 运行中

```markdown
<!-- mini-gitlab-bot:status {{task_id}} -->
## 🤖 ai-fix · 运行中

| 阶段 | 状态 | 时间 |
| --- | --- | --- |
| 入队 | ✅ | {{t_enqueue}} |
| 准备环境（`uv sync`） | ✅ | {{t_prepare}} |
| Agent 修复 | 🔄 进行中 | {{t_agent}} |
| 校验 diff / push | ⏳ | — |
| 开 draft MR | ⏳ | — |

触发人 @{{trigger_user}} · 分支 `{{branch}}` · 任务 `{{task_id}}`
<sub>机器人原地更新本条评论，请勿手动编辑。</sub>
```

### 3.2 成功（终态）

```markdown
<!-- mini-gitlab-bot:status {{task_id}} -->
## ✅ ai-fix · 已完成

→ draft MR !{{mr_iid}}（Closes #{{issue_iid}}）：{{mr_title}}

| 阶段 | 状态 | 时间 |
| --- | --- | --- |
| 入队 | ✅ | {{t_enqueue}} |
| 准备环境（`uv sync`） | ✅ | {{t_prepare}} |
| Agent 修复 | ✅ | {{t_agent}} |
| 校验 diff / push | ✅ | {{t_push}} |
| 开 draft MR | ✅ | {{t_mr}} |

**改动**
{{#each fixes}}- {{this}}
{{/each}}

**验证**：`UV_OFFLINE=1 uv run --no-sync pytest -q` → `{{test_summary}}`

**风险与未决**
{{#each risks}}- ⚠️ {{this}}
{{/each}}

触发人 @{{trigger_user}} · 任务 `{{task_id}}` · 提交 `{{commit_sha}}` · 耗时 {{elapsed}}
<sub>机器人原地更新本条评论，请勿手动编辑。</sub>
```

### 3.3 失败（终态）

````markdown
<!-- mini-gitlab-bot:status {{task_id}} -->
## ❌ ai-fix · 失败

**阶段**：{{failed_stage}}
**原因**：{{failure_reason}}
**判定依据**：{{failure_evidence}}
**分支**：`{{branch}}`（{{push_state}}）
**工作目录**：{{workdir_state}}

<details><summary>日志尾部（{{log_lines}} 行，已截断）</summary>

```
{{log_tail}}
```

</details>

**如何重试**
- 重新打一次 `ai-fix` 标签，或评论 `/ai-fix retry`
- 若需人工接管：{{takeover_hint}}

触发人 @{{trigger_user}} · 任务 `{{task_id}}` · 耗时 {{elapsed}}
<sub>机器人原地更新本条评论，请勿手动编辑。</sub>
````

**失败文案的固定措辞**（不自由发挥，便于检索与统计）

| 场景 | `failure_reason` |
| --- | --- |
| codex 退出码非 0 | `codex 退出码 {{code}}，未产生 turn.completed 事件` |
| 退出 0 但无终止事件（Interrupted 不发事件） | `codex 已退出但未产生 turn.completed 事件（疑似被中断）` |
| 退出 0 但无改动 | `agent 未产生任何文件改动（diff 为空）` |
| diff 触达禁改路径 | `diff 校验未通过：改动触达禁改路径 {{paths}}` |
| 准备阶段失败 | `准备环境失败：{{cmd}} 退出码 {{code}}（详见日志）` |
| push 失败（非快进等） | `push 失败：{{git_error}}` |
| 超时 | `任务超时（{{elapsed}} > {{timeout}}）` |

失败时标签流转为 `ai-fix:failed`，**不移除** issue 上的原始上下文；重试会产生新 `task_id`，新评论用新标记，旧评论保留为历史。

---

## 四、agent 结构化报告 `report.json`

`report.json` 是 **agent → 网关** 的结构化交接协议，写在
`{{workdir}}/.mini-gitlab-bot/report.json`（该目录被 git 忽略，永不进提交）。
字段全部可选；缺失字段在文案里退化成占位文案，**不会**导致任务失败。

| 字段 | 类型 | 用途 | 缺失时的退化 |
| --- | --- | --- | --- |
| `summary` | string | MR 概要里的一句话摘要 | 整行省略 |
| `fixes` | string[] | "改动 → 修复"条目，**必须带 `path:line`** | `（未提供改动摘要）` |
| `others` | string[] | 重构 / 顺带改动 | `（无）` |
| `tests` | string[] | 新增或复用的测试 | `（无新增测试）` |
| `test_command` | string | 实际跑测试的命令 | `UV_OFFLINE=1 uv run --no-sync pytest -q` |
| `test_summary` | string | 测试结果（如 `12 passed`） | `（未运行）` |
| `test_duration` | string | 测试耗时 | 省略 |
| `not_run` | string[] | 明确没跑的检查（类型检查、lint…） | `（无）` |
| `risks` | string[] | 风险与未决 | `- ✅ 未发现新增风险` |
| `deps` | string[] | 需要的新依赖（**沙箱装不了，只能提示人工**） | 不出现依赖提示 |

网关另外注入两个字段（agent 不写）：`agent_version`（codex `--version` 输出）、
`agent_exit_code`。`report.json` 不是判据的一部分 —— 判据永远是
`exit 0` ∧ `turn.completed` ∧ `-o` 新写入 ∧ diff 非空（PLAN §7 基线 5）。
