# 自动巡检（auto-triage）设计

> 状态：草案 v0.2（2026-11-02）
> 已定案：标签体系（全中文）、模型分档、小改动自动修默认开、**验证降级为「解释响应」**、三条评论、新 issue 全量 triage、旧标签硬切
> 待评审：§14 开放问题
> 相关文档：[PLAN.md](../../PLAN.md)（现有架构）、[templates.md](../templates.md)（文案风格）

---

## 1. 目标与非目标

**目标**

1. 新 issue 自动完成「排查·定位 → 受理·打标 → 修复 / 指派」，全过程在 issue 上有可读结论。
2. MR 合并后自动「**解释方案**」：告诉用户改了什么、为什么这么改（不做后台复跑验证，见 §10）。
3. 小改动由机器人直接出 draft MR；大改动交给人，并把「为什么找你」写清楚。
4. 机器人仍然**不合并**、agent 仍然**零凭据零网络**、GitLab 写操作仍然**网关独占**。

**非目标（本阶段不做）**

- 后台复跑复现、以执行结果判定「修好了没有」（列为升级项，见 §10.4）
- 跨项目去重（只做同项目内搜索）
- 自动合并、自动发布
- 非 Python 项目的环境准备（沿用现有约束）
- 全自动改大范围代码（大改动一律转人工）

---

## 2. 总体流程

```text
Issue open ──► triage 任务 ──► 打标签 + 巡检评论
                 │
                 ├─ 小改动（规则裁决通过）──► fix 任务（内部派生）──► draft MR ──► 状态：待审批
                 ├─ 大改动 / 低置信 ──────► 指派 owner + @ 开发者 ──► 状态：待人工处理
                 ├─ 重复（高置信）────────► 关联 + 关闭 ──► 状态：已关闭重复
                 ├─ 不成立 ───────────────► 状态：已关闭非缺陷
                 └─ 信息不足 ─────────────► 状态：待补充信息

MR merge（目标 = 默认分支，且 MR 关联了 issue）
    └─► explain 任务（一句话总结改了什么、为什么）──► 状态：已合并 + 解释评论
```

三条评论各自独立、各带 marker，原地更新：

```text
<!-- mini-gitlab-bot:triage #8 -->
<!-- mini-gitlab-bot:fix #8 -->
<!-- mini-gitlab-bot:explain #8 -->
```

marker 用 issue 号而不是 task id —— 重跑 triage 是「更新同一条」而不是「再来一条」。

---

## 3. 触发与过滤

| 事件 | 条件 | 派生任务 |
| --- | --- | --- |
| Issue Hook `action=open`（含 `reopen`） | 项目白名单、非 bot 作者、无 `机器人：跳过`、正文过短则跳过 | triage |
| Issue Hook 标签新增 `机器人：修复` | 同上 + 触发者 ≥ Developer（沿用现有闸门） | fix（直接修，跳过 triage） |
| Issue Hook 标签新增 `机器人：巡检` | 同上 | triage（重跑） |
| MR Hook `action=merge` | `target_branch == 默认分支`，且能关联到 issue | explain |
| 补偿扫描（可选，后置） | 每 N 小时扫「open 且无 `状态：` 标签」的 issue | triage（带配额） |

**不做的事**：不监听 issue `update`（否则改个描述就触发全仓分析）；重跑一律走显式指令。

补偿扫描的理由：GitLab webhook 失败 4 次临时禁用、40 次永久禁用（PLAN §6 #9），只靠事件会漏，必须有兜底。

---

## 4. 任务模型

### 4.1 三种 kind

| kind | 用途 | 状态图 | 是否装环境 |
| --- | --- | --- | --- |
| `triage` | 排查·定位 | `queued → running → finalizing → done \| failed` | 是（uv sync） |
| `fix` | 修复（沿用现有实现） | 现有 8 态，**本期不改** | 是 |
| `explain` | 合并后解释方案 | `queued → running → finalizing → done \| failed` | 否（只读 diff，一次调用） |

fix 不迁移的理由：它是唯一在真机跑通的闭环（41 秒、真 MR），重写收益只是概念统一，风险是 400+ 测试护着的代码。

### 4.2 状态机

`core/states.py` 从单表改为按 kind 查表：`STATE_GRAPHS[kind]`，`can_transition(kind, src, dst)`。triage/explain 的 5 态刻意粗：细粒度进度不值得进数据库。

### 4.3 表结构变更

```sql
ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'fix';
ALTER TABLE tasks ADD COLUMN parent_task_id TEXT;
ALTER TABLE tasks ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN subject_key TEXT;   -- 'issue:8' / 'mr:12:<merge_sha>'

-- 旧的「同一 issue 只能有一个活跃任务」按 kind 拆开：
DROP INDEX ux_tasks_active_issue;
CREATE UNIQUE INDEX ux_tasks_active_subject
    ON tasks(project_id, kind, subject_key)
    WHERE state NOT IN ('succeeded', 'failed');
```

用 `subject_key` 而不是给 `issue_iid`/`mr_iid` 加可空列，是为了让唯一索引覆盖三种 kind（explain 按 MR + merge sha 去重，重复投递只更新同一条评论）。

需要补一个**最小迁移机制**（现在只有 `CREATE TABLE IF NOT EXISTS`，加列在生产库上会静默无效）。

### 4.4 队列与并发

现有 `claim_next` 不区分 kind，`AGENT_CONCURRENCY=2` 时两个长 fix 会把 triage 饿死。

- 领取顺序：`triage > explain > fix`；
- 配额：`TRIAGE_CONCURRENCY=1`、`EXPLAIN_CONCURRENCY=1`、`FIX_CONCURRENCY=2`（合计仍受 `AGENT_CONCURRENCY` 约束）；
- 超时分开：triage 600s、explain 300s、fix 1800s。

---

## 5. 场景执行器

三种流程共用一条骨架，流程差异全部下沉到「场景包」：

```python
@dataclass(frozen=True)
class Scenario:
    name: str  # triage | fix | explain
    prompt: str  # prompts/<name>.md，改 prompt 不改 Python
    model_setting: str  # MODEL_TRIAGE / MODEL_FIX / MODEL_EXPLAIN
    tools: tuple[str, ...]  # triage/explain 为空：不给小模型 MCP
    timeout_sec: float
    needs_env: bool
    needs_clone: bool  # explain 为 False：只读 API 拿 diff
    result_schema: type  # pydantic，决定必需字段
    apply: Callable  # 受控副作用，唯一允许写 GitLab 的地方
```

```text
webhook → 场景路由 → SQLite 队列 → ScenarioRunner
                                     ├─（needs_clone）clone：fix 建分支；triage 只读检出
                                     ├─（needs_env）网关侧 uv sync
                                     ├─ codex exec（沙箱、零网络、指定模型）
                                     ├─ 解析并校验结论 JSON
                                     └─ apply()：标签 / 评论 / 关闭 / 指派 / 派生 fix
```

约束：**agent 只写结论文件，不直接写 GitLab**。副作用全部是确定性代码，可单测、可审计。

---

## 6. 模型路由

| 环境变量 | 用途 | 默认 |
| --- | --- | --- |
| `MODEL_TRIAGE` | 排查·定位 | 回落到 `MODEL_NAME` |
| `MODEL_EXPLAIN` | 合并后解释 | 回落到 `MODEL_NAME` |
| `MODEL_FIX` | 修复 | 回落到 `MODEL_NAME` |

triage/explain 用小模型（如 qwen3.8-27b），fix 用大模型（如 deepseek）。实现只需两处：`Settings` 加三个字段、`AgentRunner.run()` 加 `model` 参数，`CodexCli` 按参数下发 `-c model=...`（当前是 [adapters/codex_cli.py:216](../../adapters/codex_cli.py#L216) 的固定配置）。

**小模型的现实约束**：MCP 工具调用稳定性差。所以 triage **不给 MCP**，上下文全部由网关预取：

- issue 正文 + 最近 N 条人类评论（复用现有逻辑）
- 相似 issue 候选 top 10（只有 title / state / 链接，不带正文）
- 当前 issue 关联的 MR 列表
- 项目元信息、默认分支当前 CI 状态
- **仓库本身**（已 clone，agent 可自行 `git log` / `git blame` / grep）

少一轮工具调用就少一类失败模式。

---

## 7. 排查·定位（triage）

### 7.1 结论契约 `triage.json`

```json
{
  "verdict": "confirmed | not_reproducible | need_info | not_a_bug | duplicate",
  "confidence": 0.82,
  "summary": "一句话结论",
  "repro": {
    "command": "UV_OFFLINE=1 uv run --no-sync pytest tests/test_x.py -q",
    "result": "reproduced | failed | not_run",
    "evidence": "关键输出摘要（≤ 10 行）",
    "regression_test_patch": "可选：固化出的回归测试 diff"
  },
  "location": {
    "files": ["pkg/x.py:120"],
    "introduced_by": "sha 或空"
  },
  "impact": {
    "components": ["gateway"],
    "versions": "从 v1.2.0 起（网关按 git tag --contains 计算，不由模型填）",
    "users": "定性描述，标注「推测」"
  },
  "similar": [{"iid": 12, "relation": "duplicate", "reason": "同一空输入崩溃"}],
  "labels": {
    "type": "缺陷",
    "severity": "一般",
    "component": "gateway"
  },
  "size": {"files": 2, "lines": 40, "modules": 1, "risk": "low", "confidence": 0.8},
  "recommendation": "self_fix | assign | ask_reporter | close_duplicate | no_action"
}
```

网关只认这个文件；模型自由发挥的部分全部塞进 `summary` / `evidence`。

### 7.2 复现

- 两阶段沿用：网关联网 `uv sync` → agent 离线跑测试。
- triage 只读：不建 `ai-fix/*` 分支、不 push；临时脚本写在 workdir 内，任务结束随目录丢弃。
- 三种结果必须区分，否则会误伤 reporter：
  - 复现了 → `confirmed` + `证据：已复现`
  - 跑了但没复现 → `not_reproducible` + `证据：无法复现`（附命令与输出）
  - 环境不具备（要数据库/外部服务）→ `need_info` + `证据：环境不足`
- **复现固化成回归测试**（`regression_test_patch`）存进 task payload：既能指导 fix，也让「复现」可复用（升级真验证时直接拿它跑）。

### 7.3 定位与影响

| 项 | 谁算 | 方法 |
| --- | --- | --- |
| 代码位置 | agent | clone 内 grep + `git blame` |
| 可疑提交 | agent 提议，网关校验 sha 存在 | `git log -S<symbol>`（pickaxe）比模型猜准 |
| 影响版本 | **网关** | `introduced_by` 的 sha → `git tag --contains`，最早包含的 tag 即起点 |
| 影响模块 | **网关** | CODEOWNERS 段名 → 组件；无 CODEOWNERS 时用「命中文件的**公共顶层目录**」；跨顶层目录则 `组件：其他` |
| 影响用户 | agent（标注推测） | 只能定性推理，文案强制标「推测」 |

### 7.4 相似 issue / 去重

1. 网关先确定性召回：从标题提取错误类名 / 堆栈关键 token → GitLab search API，取同项目 top 10。
2. agent 只看候选的 title + state + 链接，判定 `duplicate | related`。
3. 命中重复：写入 issue links API（真的建立关联），不是评论里写一句「可能重复」。

---

## 8. 受理：打标与路由

### 8.1 自动修裁决（网关拍板，模型只给估计）

```text
AUTOFIX_ENABLED=true（默认开）且全部满足：
  size.files ≤ AUTOFIX_MAX_FILES（3）
  size.lines ≤ AUTOFIX_MAX_LINES（150）
  单一 component、无禁改路径、risk=low
  confidence ≥ AUTOFIX_MIN_CONFIDENCE（0.7）
  提问者 access_level ≥ AUTOFIX_MIN_ACCESS_LEVEL（默认 0 = 不设门槛）
```

`AUTOFIX_MIN_ACCESS_LEVEL=0` 的含义见 §12 的说明：内网公开仓库，**所有能提 issue 的账号都能触发自动修**，不区分角色。已知取舍：这等于「低权限账号也能借机器人之手提出改动」，兜底是 draft MR + 人工合并 + diff 禁改校验 + 每小时配额。

### 8.2 派生 fix 任务

**内部 enqueue**（`parent_task_id` 指向 triage），不靠 `机器人：修复` 标签二次触发：

- `BOT_USERNAME` 会把机器人自己的标签事件过滤掉，靠标签会静默不触发；
- 内部派生能记录「因为什么被自动修」，审计链完整；
- 标签照样打 `机器人：修复`（界面一致，且立刻被 fix 任务消费移除），但任务不依赖它。

### 8.3 指派（确定性解析）

```text
1. CODEOWNERS 命中改动路径的 owner
2. git log 这些文件的近期作者 → GitLab user
3. 项目 Maintainer+ 中最近活跃者
4. 兜底：不指派，评论 @ 维护者组
```

同时写一条 @ 评论说明「为什么找你」（命中的文件 / 组件），比静默 assignee 有用。

---

## 9. 标签体系（定案）

### 9.1 命名与可行性约束

- GitLab 标签名是普通 UTF-8 字符串，中文可用；**`add_labels` 是逗号拼接**，所以标签名禁含 `,`、引号、换行、emoji。
- **不使用 `key::value`**：scoped labels 是 Premium/Ultimate 功能（CE 用不了），改用中文全角冒号 `：` 做前缀，互斥由 bot 自己保证。
- 标签名集中到 `core/labels.py` 常量；模型输出必须落在枚举内，越界丢弃并记事件。
- 单个 issue 实际挂 ≤ 8 个标签（GitLab 上限 100，余量充足）。

### 9.2 A. 机器人指令（人工用，多值）

| 标签 | 含义 | 颜色 |
| --- | --- | --- |
| `机器人：修复` | 立即触发修复（旧 `ai-fix` 的角色），任务开始后由机器人移除 | `#1F6FEB` |
| `机器人：巡检` | 重跑排查·定位 | `#1F6FEB` |
| `机器人：跳过` | 不参与自动巡检，直到移除 | `#6E7781` |

### 9.3 B. 类型（单值，bot 写）

| 标签 | 含义 | 颜色 |
| --- | --- | --- |
| `类型：缺陷` | 现有行为与预期不符 | `#D73A4A` |
| `类型：新功能` | 新增能力 | `#A2EEEF` |
| `类型：优化` | 性能、可读性、重构 | `#0E8A16` |
| `类型：文档` | 文档 / 示例 / 注释 | `#0075CA` |
| `类型：疑问` | 不是 bug，需要答疑 | `#D876E3` |
| `类型：安全` | 漏洞、凭据、越权 | `#B60205` |

### 9.4 C. 严重度（单值，bot 写，全中文）

| 标签 | 描述（写进标签 description，UI 悬停可见） | 颜色 |
| --- | --- | --- |
| `严重度：致命` | 崩溃 / 数据丢失 / 安全 / 大范围不可用；当天响应 | `#B60205` |
| `严重度：严重` | 核心功能不可用，有绕过方案；本迭代内修复 | `#D93F0B` |
| `严重度：一般` | 局部功能异常或体验问题；正常排期 | `#FBCA04` |
| `严重度：轻微` | 文案、边缘场景、改进建议 | `#C2E0C6` |

### 9.5 D. 组件（单值，bot 写，按项目动态创建）

| 标签 | 含义 | 颜色 |
| --- | --- | --- |
| `组件：<标识符>` | 归属模块；值取 CODEOWNERS 段名或顶层目录名（如 `组件：gateway`） | `#5319E7` |
| `组件：其他` | 兜底：映射不到（含跨顶层目录）时使用 | `#5319E7` |

- 前缀中文、值用仓库真实标识符，避免维护易漂移的对照表；要中文映射等 `.mini-gitlab-bot.yaml` 落地后加 `component_map`。
- 只创建映射表里出现过的值，不做任意动态造词。

### 9.6 E. 状态（单值，bot 独占）

| 标签 | 何时出现 | 颜色 |
| --- | --- | --- |
| `状态：排队中` | 已接收，等待执行 | `#EDEDED` |
| `状态：排查中` | triage 运行中 | `#FBCA04` |
| `状态：待补充信息` | triage 判定 need_info | `#FEF2C0` |
| `状态：待人工处理` | 改动面大 / 低置信，已指派并 @ | `#E4E669` |
| `状态：自动修复中` | fix 运行中 | `#F9D0C4` |
| `状态：待审批` | draft MR 已开，等人工审批 | `#BFD4F2` |
| `状态：已合并` | MR 已合并，解释评论已发布 | `#0E8A16` |
| `状态：已关闭重复` | 高置信重复，已关联并关闭 | `#CFD3D7` |
| `状态：已关闭非缺陷` | 不成立 / 已实现 / 长期无法复现 | `#CFD3D7` |
| `状态：处理失败` | 机器人自身失败（环境/模型/GitLab 调用） | `#D93F0B` |

### 9.7 F. 证据（多值，bot 写）

| 标签 | 含义 | 颜色 |
| --- | --- | --- |
| `证据：已复现` | 按描述成功复现 | `#0E8A16` |
| `证据：无法复现` | 跑了但没复现（附命令与输出） | `#FBCA04` |
| `证据：有回归测试` | 已固化回归测试（payload 里带 patch） | `#C2E0C6` |
| `证据：环境不足` | 需要外部服务/数据库，triage 环境不具备 | `#CFD3D7` |

### 9.8 G. 风险（多值，bot 写）

| 标签 | 含义 | 颜色 |
| --- | --- | --- |
| `风险：改动面大` | 预估文件 / 行数超阈值 | `#F9D0C4` |
| `风险：跨模块` | 涉及多个 component | `#F9D0C4` |
| `风险：涉及依赖` | 需新增依赖（沙箱无网装不了） | `#FEF2C0` |
| `风险：需人工决策` | 方案有取舍，需人拍板 | `#D93F0B` |

共 **31 个固定标签** + 动态组件值。

### 9.9 标签演化示例

```text
#8 新建              → 状态：排队中
triage 运行           → 状态：排查中
triage 结论（小改动）  → 类型：缺陷 严重度：一般 组件：gateway
                       证据：已复现 证据：有回归测试 状态：自动修复中
fix 开出 draft MR     → 状态：待审批
MR 合并               → 状态：已合并（解释评论发布；issue 已被 Closes 自动关闭）

其他分支：
重复           → 状态：已关闭重复（关联到 #12）
不成立         → 状态：已关闭非缺陷
信息不足       → 状态：待补充信息
改动面大       → 状态：待人工处理 风险：改动面大 风险：跨模块
```

注意时序：`Closes #N` 会让 GitLab 在合并时立刻关闭 issue。本期不做后台验证，所以不存在「关早了」的问题；将来升级真验证时，才需要在失败时 reopen。

### 9.10 创建、写入与迁移规则

1. **启动时幂等创建**：拉项目标签，缺失的 `POST /labels`（带颜色 + 描述）；已存在的不动、不改色、不删。
2. **只碰自己的命名空间**：`机器人：/类型：/严重度：/组件：/状态：/证据：/风险：` 七个前缀；人手工加的标签一律不动。
3. **单值组互斥由 bot 完成**：一次 `PUT` 同时带 `add_labels` + `remove_labels`（先删同前缀旧值再加新值），不「读全量再整体写回」。
4. **模型不能造标签**：越界值丢弃 + `label_rejected` 事件。
5. **人工覆盖**：bot 独占组被人改动时，下次流转按自己的状态重新收敛，并在评论里说明。
6. **不改名**：标签一旦发布不改 title（历史引用会漂）。
7. **旧标签硬切（已定案）**：代码里不再识别 `ai-fix` / `ai-fix:running|done|failed`；随版本提供一次性脚本 `scripts/migrate_labels.py`：
   - 移除 issue 上的旧标签（只动这四个名字）；
   - 若项目里旧标签已无人使用，删除项目标签；
   - 不自动迁移旧状态（历史任务已经终态，无需回填）。

### 9.11 `core/labels.py` 结构（M6 直接照建）

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LabelDef:
    name: str
    color: str
    description: str
    group: str | None = None  # 互斥组名；None = 多值


BOT_PREFIXES = ("机器人：", "类型：", "严重度：", "组件：", "状态：", "证据：", "风险：")
SINGLE_VALUE_GROUPS = frozenset({"类型", "严重度", "组件", "状态"})
COMMAND_LABELS = frozenset({"机器人：修复", "机器人：巡检", "机器人：跳过"})
TRIAGE_ENUMS = {  # 校验模型输出
    "type": ("缺陷", "新功能", "优化", "文档", "疑问", "安全"),
    "severity": ("致命", "严重", "一般", "轻微"),
}

LABELS: tuple[LabelDef, ...] = (
    LabelDef("类型：缺陷", "#D73A4A", "现有行为与预期不符", group="类型"),
    LabelDef("严重度：严重", "#D93F0B", "核心功能不可用，有绕过方案；本迭代内修复", group="严重度"),
    LabelDef("状态：已合并", "#0E8A16", "MR 已合并，解释评论已发布", group="状态"),
    # …其余见 §9.2–§9.8
)


def group_of(name: str) -> str | None:
    """'严重度：一般' → '严重度'；非机器人标签返回 None。"""
```

组件标签不走 `LABELS` 常量，由 `ensure_component_labels(project, mapping)` 按需创建。

---

## 10. 合并后解释响应（原「验证」的降级版）

### 10.1 定位

后台复跑复现（clone + uv sync + 跑测试）在这个内网场景里太重：一次要几分钟、要独立环境、要两个提交的对照。**先做「解释响应」**，只回答用户最关心的问题：这次改动做了什么、为什么能解决这个 issue。

### 10.2 触发与输入

- 触发：MR merge 且目标为默认分支，且能关联到 issue（`closes_issues` API 优先，其次 `Closes #N` 正则）。
- 输入（全部由网关预取，**不 clone、不装环境**）：
  - MR 标题 / 描述 / 变更文件清单 / diff 摘要（单文件 diff 有上限）
  - issue 正文 + triage 结论（若有：定位到的文件、复现情况、根因推测）
  - 合并提交 message
- 执行：`MODEL_EXPLAIN`（27B）一次调用，产出 markdown 解释。

### 10.3 输出与动作

评论内容（原地更新 `<!-- mini-gitlab-bot:explain #8 -->`）：

```text
## 改动说明
- 改了哪些文件 / 哪个函数
- 为什么这样改（根因 → 方案，用大白话）
- 与 issue 描述对应关系：「你报的现象 X 由 Y 引起，现在 Z」
- 若 triage 有复现信息：说明「原复现路径现在走的是什么分支」
- 提醒：本说明由模型根据 diff 生成，未经二次复跑验证

## 关联
- MR !12 · 合并提交 abc123
```

动作：打 `状态：已合并`（若 issue 已被 Closes 关闭，标签照样打）。

### 10.4 明确不做 / 升级路径

- 不 clone、不装环境、不跑测试、不以执行结果判定成败；
- 不 reopen issue；不做 `证据：*` 的更新；
- 升级项（记入 M9+）：在合并提交上应用 triage 的 `regression_test_patch`（或 `repro.command`）复跑，通过 → `状态：验证通过`，失败 → reopen + `状态：验证失败`。代码接口预留 `explain` 任务的 payload 字段即可，不提前实现。

---

## 11. 安全与成本

- **提示注入**：issue 正文是不可信输入。agent 零凭据零网络不变，但它能影响「打什么标签、要不要自动修、关不关 issue」。防线 = §8 的网关裁决 + 自动动作开关。
- **误关 issue** 比误打标签严重：duplicate 自动关闭要求 `confidence ≥ 0.8` + 指向对象明确且仍 open；被人工 reopen 记一次误判指标。
- **自动修** 实质是「用机器人凭据推代码」：本期按需求不设权限门槛（`AUTOFIX_MIN_ACCESS_LEVEL=0`），兜底是 draft MR + 人工合并 + diff 禁改校验 + 配额。若之后要收紧，改一个配置即可。
- **成本**：一次全仓 triage 约 100k 输入 token（参考真机 fix 数据）；explain 只读 diff，约 10–20k。手段：项目级开关、每小时配额、正文过短跳过、triage 用 27B。
- **指标**：`triage_total{verdict}`、`triage_duration`、`autofix_decisions{size}`、`explain_total`、**误判率**（被人工 reopen / 改标签的比例）。

---

## 12. 配置

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MODEL_TRIAGE` / `MODEL_EXPLAIN` / `MODEL_FIX` | 分阶段模型 | 回落 `MODEL_NAME` |
| `TRIAGE_ENABLED` | 新 issue 自动巡检总开关；真机验证通过，默认开启（可显式关掉） | `true` |
| `AUTOFIX_ENABLED` | 小改动自动修总开关 | `true` |
| `AUTOFIX_MAX_FILES` / `AUTOFIX_MAX_LINES` | 自动修规模上限 | `3` / `150` |
| `AUTOFIX_MIN_CONFIDENCE` | 自动修置信下限 | `0.7` |
| `AUTOFIX_MIN_ACCESS_LEVEL` | 触发自动修的最低 GitLab 权限；**0 = 不检查，任何能提 issue 的账号都可** | `0` |
| `TRIAGE_QUOTA_PER_HOUR` | 每项目每小时最多创建多少个 triage；超限的 issue 直接忽略（不排队），`0` = 不限 | `20` |
| `TRIAGE_CONCURRENCY` / `EXPLAIN_CONCURRENCY` / `FIX_CONCURRENCY` | 分 kind 并发 | `1 / 1 / 2` |
| `DUPLICATE_AUTOCLOSE_ENABLED` | 重复自动关闭 | `true` |
| `DUPLICATE_MIN_CONFIDENCE` | 自动关闭置信下限 | `0.8` |

**关于 `AUTOFIX_MIN_ACCESS_LEVEL`（GitLab 角色等级）**

| 值 | 角色 | 效果 |
| --- | --- | --- |
| 0 | 不检查 | 任何能提 issue 的账号（含非成员/Guest）都可自动修 |
| 10 | Guest | 至少 Guest 及以上 |
| 20 | Reporter | 至少 Reporter（只读成员）及以上 |
| 30 | Developer | 至少能推代码的成员（默认「安全值」，创作者默认用它） |
| 40 | Maintainer | 仅维护者 |

本期按「内网公开仓库、所有人可自动响应」的需求取 **0**；它只影响「自动修」这一个动作，**不影响 triage**（triage 对所有新 issue 无条件运行）。

**关于 `TRIAGE_QUOTA_PER_HOUR`**：每个项目每小时最多跑多少次 triage（不是总量上限，是滑动/固定窗口限流）。目的是防刷 issue 把 27B 端点打满、把 fix 队列饿死；**当前实现**：超限的 issue 直接忽略（返回 `triage_quota_exceeded`，不排队），下一窗口需重开 issue 或手动 `机器人：巡检`；补跑需要「延后任务」能力，留作升级项。`0` = 完全不限，适合单机自用。

---

## 13. 里程碑

| 里程碑 | 内容 | 判据 |
| --- | --- | --- |
| **M6 Runner + triage** | 场景骨架、5 态、上下文预取、标签创建、巡检评论 | 假件 582 项 + 真 GitLab 19.5.3/真 codex 0.166 一个 issue 通过（含重开幂等、脏改告警） |
| **M7 自动修 + 指派** | 裁决、内部 enqueue、组件映射（CODEOWNERS / 顶层目录） | 自动修真机验证（draft MR）；CODEOWNERS 指派、重复关闭已落地（假件） |
| **M8 解释响应** | merge 事件、diff 读取、解释评论、标签流转 | 假件全链路 + 真 GitLab MR merge/真 codex 解释通过（含分阶段模型、配额） |
| **M9（可选）** | 真验证（复跑 repro / 回归测试）＋ fix 迁到 Runner | 446 项测试等价迁移；真实验证一次通过、一次失败 |

**M6 已落地的状态映射（临时）**：`need_info` → `状态：待补充信息`，其余成立/未复现/非缺陷/重复 →
`状态：待人工处理`（无法关闭 issue 的结论先不标「已关闭」）。`自动修复中` 在 M7 接线，
`已关闭重复` / `已关闭非缺陷` 在拿到 `close_issue` 端口能力后接线。

---

## 14. 开放问题

1. **旧标签清理的时机**：`scripts/migrate_labels.py` 是随版本手动跑一次，还是由 bot 启动时自动清理（风险：误删名同的标签）？建议手动跑。
2. **配额超限后的行为（已落地为直接忽略）**：如果要「下一窗口补跑」，需要给任务加 `not_before` 延后领取；否则重开/手动巡检。
3. **explain 的输入上限**：大 MR（几百文件）的 diff 摘要要不要截断到 N 文件 / N 行？建议默认 50 文件 / 2000 行，超出时在评论里注明「仅解释了主要改动」。
4. **组件映射缺失**：完全无 CODEOWNERS、且改动跨多个顶层目录时，是 `组件：其他`（本稿）还是取「改动最多的顶层目录」？后者更有信息量但会误标。
