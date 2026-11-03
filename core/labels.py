"""标签体系契约：全中文标签、互斥组、颜色与描述（纯数据，无 IO）。

设计要点（详见 `docs/design/auto-triage.md` §9）：

* GitLab 标签名是普通 UTF-8 字符串，中文可用；但 `add_labels` / `remove_labels`
  是**逗号拼接**的，所以标签名里不能有逗号、引号、换行和 emoji。
* GitLab CE 没有 scoped labels（`key::value` 是 Premium/Ultimate），因此这里用
  **中文全角冒号** `：` 做分组前缀，同类互斥由网关自己保证（见 `exclusive_removals`）。
* 所有标签名只在此处出现，业务代码不要写裸字符串；模型输出的值必须落在
  `TRIAGE_ENUMS` 里，越界即丢弃。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: 分组分隔符。故意用全角冒号，避免和 GitLab 的 scoped label 语法（`::`）混淆。
SEPARATOR = "："

#: 机器人使用的命名空间前缀。网关只增删这些前缀下的标签，人工标签一律不动。
BOT_PREFIXES: tuple[str, ...] = (
    "机器人",
    "类型",
    "严重度",
    "组件",
    "状态",
    "证据",
    "风险",
)

#: 单值组：同一 issue 上只能有一个。组件组虽然也是单值，但值是动态的。
SINGLE_VALUE_GROUPS: frozenset[str] = frozenset({"类型", "严重度", "组件", "状态"})

#: 人工指令标签（多值，人工写入、机器人消费）。
COMMAND_GROUP = "机器人"
TRIGGER_LABEL = f"{COMMAND_GROUP}{SEPARATOR}修复"
TRIAGE_LABEL = f"{COMMAND_GROUP}{SEPARATOR}巡检"
SKIP_LABEL = f"{COMMAND_GROUP}{SEPARATOR}跳过"
COMMAND_LABELS: frozenset[str] = frozenset({TRIGGER_LABEL, TRIAGE_LABEL, SKIP_LABEL})

#: 组件标签：前缀固定，值取 CODEOWNERS 段名 / 顶层目录名。
COMPONENT_GROUP = "组件"
COMPONENT_PREFIX = f"{COMPONENT_GROUP}{SEPARATOR}"
COMPONENT_FALLBACK = f"{COMPONENT_PREFIX}其他"

#: 状态标签的取值（机器人独占，按流转写入）。
STATUS_GROUP = "状态"
STATUS_QUEUED = f"{STATUS_GROUP}{SEPARATOR}排队中"
STATUS_TRIAGING = f"{STATUS_GROUP}{SEPARATOR}排查中"
STATUS_NEEDS_INFO = f"{STATUS_GROUP}{SEPARATOR}待补充信息"
STATUS_NEEDS_HUMAN = f"{STATUS_GROUP}{SEPARATOR}待人工处理"
STATUS_FIXING = f"{STATUS_GROUP}{SEPARATOR}自动修复中"
STATUS_REVIEW = f"{STATUS_GROUP}{SEPARATOR}待审批"
STATUS_MERGED = f"{STATUS_GROUP}{SEPARATOR}已合并"
#: commit 引用修复后由周期扫描自动关闭（TODO #1）
STATUS_RESOLVED = f"{STATUS_GROUP}{SEPARATOR}已解决"
STATUS_DUPLICATE = f"{STATUS_GROUP}{SEPARATOR}已关闭重复"
STATUS_NOT_A_BUG = f"{STATUS_GROUP}{SEPARATOR}已关闭非缺陷"
STATUS_FAILED = f"{STATUS_GROUP}{SEPARATOR}处理失败"

#: 三分类枚举：triage 结论里类型/严重度只能是这些值。
TRIAGE_ENUMS: dict[str, tuple[str, ...]] = {
    "type": ("缺陷", "新功能", "优化", "文档", "疑问", "安全"),
    "severity": ("致命", "严重", "一般", "轻微"),
}

#: 证据 / 风险（多值，正交）。
EVIDENCE_REPRODUCED = f"证据{SEPARATOR}已复现"
EVIDENCE_NOT_REPRODUCED = f"证据{SEPARATOR}无法复现"
EVIDENCE_REGRESSION_TEST = f"证据{SEPARATOR}有回归测试"
EVIDENCE_NO_ENV = f"证据{SEPARATOR}环境不足"

RISK_LARGE_DIFF = f"风险{SEPARATOR}改动面大"
RISK_CROSS_MODULE = f"风险{SEPARATOR}跨模块"
RISK_NEEDS_DEP = f"风险{SEPARATOR}涉及依赖"
RISK_NEEDS_HUMAN = f"风险{SEPARATOR}需人工决策"


@dataclass(frozen=True, slots=True)
class LabelDef:
    """一个标签的完整定义。`group=None` 表示多值（不互斥）。"""

    name: str
    color: str
    description: str
    group: str | None = None


#: 32 个固定标签（组件值是动态的，不列在这里；兜底值见 `COMPONENT_FALLBACK`）。
LABELS: tuple[LabelDef, ...] = (
    # ---- 机器人指令（多值，人工写入） ----
    LabelDef(TRIGGER_LABEL, "#1F6FEB", "人工指令：立即触发修复任务；任务开始后由机器人移除"),
    LabelDef(TRIAGE_LABEL, "#1F6FEB", "人工指令：重跑排查·定位（补跑失败的 triage）"),
    LabelDef(SKIP_LABEL, "#6E7781", "人工指令：该 issue 不参与自动巡检，直到移除本标签"),
    # ---- 类型（单值） ----
    LabelDef(f"类型{SEPARATOR}缺陷", "#D73A4A", "现有行为与预期不符", group="类型"),
    LabelDef(f"类型{SEPARATOR}新功能", "#A2EEEF", "新增能力", group="类型"),
    LabelDef(f"类型{SEPARATOR}优化", "#0E8A16", "性能、可读性、重构", group="类型"),
    LabelDef(f"类型{SEPARATOR}文档", "#0075CA", "文档 / 示例 / 注释", group="类型"),
    LabelDef(f"类型{SEPARATOR}疑问", "#D876E3", "不是缺陷，需要答疑", group="类型"),
    LabelDef(f"类型{SEPARATOR}安全", "#B60205", "漏洞、凭据、越权", group="类型"),
    # ---- 严重度（单值，全中文） ----
    LabelDef(
        f"严重度{SEPARATOR}致命",
        "#B60205",
        "崩溃 / 数据丢失 / 安全 / 大范围不可用；当天响应",
        group="严重度",
    ),
    LabelDef(
        f"严重度{SEPARATOR}严重",
        "#D93F0B",
        "核心功能不可用，有绕过方案；本迭代内修复",
        group="严重度",
    ),
    LabelDef(
        f"严重度{SEPARATOR}一般", "#FBCA04", "局部功能异常或体验问题；正常排期", group="严重度"
    ),
    LabelDef(f"严重度{SEPARATOR}轻微", "#C2E0C6", "文案、边缘场景、改进建议", group="严重度"),
    # ---- 状态（单值，机器人独占） ----
    LabelDef(STATUS_QUEUED, "#EDEDED", "已接收，等待执行", group="状态"),
    LabelDef(STATUS_TRIAGING, "#FBCA04", "排查·定位任务运行中", group="状态"),
    LabelDef(STATUS_NEEDS_INFO, "#FEF2C0", "环境或复现信息不足，等待反馈者补充", group="状态"),
    LabelDef(STATUS_NEEDS_HUMAN, "#E4E669", "改动面大或置信不足，已指派相关开发者", group="状态"),
    LabelDef(STATUS_FIXING, "#F9D0C4", "修复任务运行中", group="状态"),
    LabelDef(STATUS_REVIEW, "#BFD4F2", "draft MR 已开，等待人工审批合并", group="状态"),
    LabelDef(STATUS_MERGED, "#0E8A16", "MR 已合并，解释评论已发布", group="状态"),
    LabelDef(STATUS_RESOLVED, "#1A7F37", "提交已引用修复；机器人已回帖并关闭", group="状态"),
    LabelDef(STATUS_DUPLICATE, "#CFD3D7", "高置信重复，已关联并关闭", group="状态"),
    LabelDef(STATUS_NOT_A_BUG, "#CFD3D7", "判定不成立 / 已实现 / 长期无法复现", group="状态"),
    LabelDef(STATUS_FAILED, "#D93F0B", "机器人自身失败（环境、模型或 GitLab 调用）", group="状态"),
    # ---- 证据（多值） ----
    LabelDef(EVIDENCE_REPRODUCED, "#0E8A16", "按描述成功复现"),
    LabelDef(EVIDENCE_NOT_REPRODUCED, "#FBCA04", "跑了但没复现，附命令与输出"),
    LabelDef(EVIDENCE_REGRESSION_TEST, "#C2E0C6", "已固化回归测试（payload 内含 patch）"),
    LabelDef(EVIDENCE_NO_ENV, "#CFD3D7", "需要外部服务或数据库，当前环境不具备"),
    # ---- 风险（多值） ----
    LabelDef(RISK_LARGE_DIFF, "#F9D0C4", "预估文件或行数超出自动修阈值"),
    LabelDef(RISK_CROSS_MODULE, "#F9D0C4", "涉及多个组件"),
    LabelDef(RISK_NEEDS_DEP, "#FEF2C0", "需要新增依赖，离线沙箱装不了"),
    LabelDef(RISK_NEEDS_HUMAN, "#D93F0B", "方案存在取舍，需要人拍板"),
)

LABEL_NAMES: frozenset[str] = frozenset(label.name for label in LABELS)


def is_bot_label(name: str) -> bool:
    """是否是机器人命名空间下的标签。"""
    return any(name.startswith(f"{prefix}{SEPARATOR}") for prefix in BOT_PREFIXES)


def group_of(name: str) -> str | None:
    """`'严重度：一般'` → `'严重度'`；非机器人标签返回 `None`。

    组件标签的值是动态的，同样归入 `'组件'` 组。
    """
    if not is_bot_label(name):
        return None
    prefix, _, _value = name.partition(SEPARATOR)
    return prefix


def labels_in_group(group: str) -> tuple[LabelDef, ...]:
    return tuple(label for label in LABELS if label.group == group)


def component_label(component: str) -> str:
    return f"{COMPONENT_PREFIX}{component}"


def exclusive_removals(current: Iterable[str], *, keep: str) -> tuple[str, ...]:
    """同一单值组里，除 `keep` 之外应当移除的标签。"""
    group = group_of(keep)
    if group is None or group not in SINGLE_VALUE_GROUPS:
        return ()
    return tuple(name for name in current if name != keep and group_of(name) == group)


def missing_labels(
    existing: Iterable[str], wanted: Sequence[LabelDef] = LABELS
) -> tuple[LabelDef, ...]:
    """启动时幂等创建用：返回项目里还不存在的标签定义。"""
    present = set(existing)
    return tuple(label for label in wanted if label.name not in present)


def validate_triage_labels(*, type_value: str, severity: str) -> tuple[str, str]:
    """校验模型给出的类型/严重度，越界抛 `ValueError`（由网关丢弃并记事件）。"""
    if type_value not in TRIAGE_ENUMS["type"]:
        raise ValueError(f"非法类型标签值：{type_value!r}")
    if severity not in TRIAGE_ENUMS["severity"]:
        raise ValueError(f"非法严重度标签值：{severity!r}")
    return type_value, severity


#: 旧 fix 链路的四个标签（硬切后不再产生，迁移脚本负责从项目里清掉）。
LEGACY_FIX_LABELS: tuple[str, ...] = (
    "ai-fix",
    "ai-fix:running",
    "ai-fix:done",
    "ai-fix:failed",
)


def legacy_labels_present(existing: Iterable[str]) -> tuple[str, ...]:
    """返回项目里仍然存在的旧标签（迁移脚本用）。"""
    present = set(existing)
    return tuple(name for name in LEGACY_FIX_LABELS if name in present)
