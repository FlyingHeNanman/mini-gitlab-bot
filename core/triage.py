"""triage 结论契约：解析 agent 返回的 JSON、推导组件标签、渲染巡检评论。

设计原则是 **LLM 提议、网关裁决**：模型只给结论与估计，标签落在哪个组件、状态怎么流转、
是否可信，都由这里的确定性代码决定。所以解析失败的结论**不是"尽力而为"地猜**，
而是让任务明确失败（评论里说清原因），避免把没校验的东西打到 issue 上。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from core import labels
from core.models import IssueContext, Task
from core.render import BOT_SIGNATURE

TRIAGE_MARKER_PREFIX = "mini-gitlab-bot:triage"

#: 评论里最多列出的文件数（模型偶尔会把整个仓库列一遍）
MAX_FILES_IN_COMMENT = 10
MAX_SIMILAR_IN_COMMENT = 5


class TriageVerdict(StrEnum):
    CONFIRMED = "confirmed"
    NOT_REPRODUCIBLE = "not_reproducible"
    NEED_INFO = "need_info"
    NOT_A_BUG = "not_a_bug"
    DUPLICATE = "duplicate"


VERDICT_LABELS: dict[TriageVerdict, str] = {
    TriageVerdict.CONFIRMED: "确认成立",
    TriageVerdict.NOT_REPRODUCIBLE: "未能复现",
    TriageVerdict.NEED_INFO: "信息不足",
    TriageVerdict.NOT_A_BUG: "不是缺陷",
    TriageVerdict.DUPLICATE: "疑似重复",
}

RECOMMENDATIONS = frozenset({"self_fix", "assign", "ask_reporter", "close_duplicate", "no_action"})
REPRO_RESULTS = frozenset({"reproduced", "failed", "not_run"})


class TriageResultError(ValueError):
    """结论 JSON 不符合契约（缺少必填字段 / 枚举越界 / 置信度越界）。"""


@dataclass(frozen=True, slots=True)
class TriageResult:
    verdict: TriageVerdict
    confidence: float
    summary: str
    type_value: str
    severity: str
    files: tuple[str, ...] = ()
    repro_command: str = ""
    repro_result: str = "not_run"
    repro_evidence: str = ""
    regression_test_patch: str = ""
    similar: tuple[tuple[int, str, str], ...] = ()
    recommendation: str = "assign"
    size_files: int = 0
    size_lines: int = 0
    risk: str = ""
    deps: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def component(self) -> str:
        return common_component(self.files)

    @property
    def duplicate_target(self) -> int | None:
        """模型给出的重复目标（只认 relation=duplicate 的第一条）。"""
        for iid, relation, _ in self.similar:
            if relation == "duplicate":
                return iid
        return None

    @property
    def status_label(self) -> str:
        return (
            labels.STATUS_NEEDS_INFO
            if self.verdict is TriageVerdict.NEED_INFO
            else labels.STATUS_NEEDS_HUMAN
        )

    @property
    def evidence_builders(self) -> tuple[str, ...]:
        """证据标签：只有真跑过才打，避免"无法复现"变成一句空话。"""
        evidence: list[str] = []
        if self.repro_result == "reproduced":
            evidence.append(labels.EVIDENCE_REPRODUCED)
        elif self.repro_result == "failed":
            evidence.append(labels.EVIDENCE_NOT_REPRODUCED)
        elif self.verdict is TriageVerdict.NEED_INFO:
            evidence.append(labels.EVIDENCE_NO_ENV)
        if self.regression_test_patch.strip():
            evidence.append(labels.EVIDENCE_REGRESSION_TEST)
        return tuple(evidence)


def triage_marker(issue_iid: int) -> str:
    """巡检评论的 marker（按 issue 号，而不是 task id：重跑要原地更新同一条）。"""
    return f"<!-- {TRIAGE_MARKER_PREFIX} #{issue_iid} -->"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _clean_path(path: str) -> str:
    # 允许 "gateway/config.py:42" 这种写法；组件只看文件路径本身
    return path.strip().split(":", 1)[0].strip()


def common_component(files: Iterable[str]) -> str:
    """命中文件的**公共顶层目录**；跨顶层目录 / 没有文件时回落 `组件：其他`。

    仓库根下的单文件（`README.md`）不算组件 —— 它们不属于任何顶层目录。
    """
    cleaned = [_clean_path(f) for f in files if _clean_path(f)]
    tops = {path.split("/", 1)[0] for path in cleaned if "/" in path}
    if len(tops) == 1:
        return tops.pop()
    return "其他"


def parse_triage_result(report: Mapping[str, Any]) -> TriageResult:
    """把 agent 的 report.json 解析成 `TriageResult`；不合契约抛 `TriageResultError`。"""
    verdict_raw = str(report.get("verdict") or "")
    try:
        verdict = TriageVerdict(verdict_raw)
    except ValueError as exc:
        raise TriageResultError(f"verdict 非法：{verdict_raw!r}") from exc

    try:
        confidence = float(report.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise TriageResultError("confidence 缺失或不是数字") from exc
    if not 0.0 <= confidence <= 1.0:
        raise TriageResultError(f"confidence 越界：{confidence}")

    summary = str(report.get("summary") or "").strip()
    if not summary:
        raise TriageResultError("summary 为空")

    label_values = _as_mapping(report.get("labels"))
    try:
        type_value, severity = labels.validate_triage_labels(
            type_value=str(label_values.get("type") or ""),
            severity=str(label_values.get("severity") or ""),
        )
    except ValueError as exc:
        raise TriageResultError(str(exc)) from exc

    location = _as_mapping(report.get("location"))
    raw_files = location.get("files") or report.get("files") or ()
    if isinstance(raw_files, str):
        raw_files = (raw_files,)
    files = tuple(dict.fromkeys(_clean_path(str(item)) for item in raw_files if str(item).strip()))

    repro = _as_mapping(report.get("repro"))
    repro_result = str(repro.get("result") or "not_run")
    if repro_result not in REPRO_RESULTS:
        repro_result = "not_run"

    similar: list[tuple[int, str, str]] = []
    raw_similar = report.get("similar") or ()
    if isinstance(raw_similar, Mapping):
        raw_similar = (raw_similar,)
    for item in raw_similar:
        entry = _as_mapping(item)
        iid = entry.get("iid")
        if not isinstance(iid, int):
            continue
        similar.append(
            (iid, str(entry.get("relation") or "related"), str(entry.get("reason") or ""))
        )

    recommendation = str(report.get("recommendation") or "assign")
    if recommendation not in RECOMMENDATIONS:
        recommendation = "assign"

    size = _as_mapping(report.get("size"))
    raw_deps = report.get("deps") or size.get("deps") or ()
    if isinstance(raw_deps, str):
        raw_deps = (raw_deps,)
    deps = tuple(dict.fromkeys(str(item).strip() for item in raw_deps if str(item).strip()))

    return TriageResult(
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        type_value=type_value,
        severity=severity,
        files=files,
        repro_command=str(repro.get("command") or "").strip(),
        repro_result=repro_result,
        repro_evidence=str(repro.get("evidence") or "").strip(),
        regression_test_patch=str(repro.get("regression_test_patch") or "").strip(),
        similar=tuple(similar),
        recommendation=recommendation,
        size_files=_as_int(size.get("files"), len(files)),
        size_lines=_as_int(size.get("lines"), 0),
        risk=str(size.get("risk") or "").strip().lower(),
        deps=deps,
        raw=dict(report),
    )


def _ymd_hm(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if moment else "—"


def _bullets(items: Sequence[str], *, empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def label_plan(
    current_labels: Iterable[str],
    result: TriageResult,
    *,
    status: str | None = None,
    extra_add: Iterable[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """计算要加的标签与要移除的同组旧标签（互斥由网关保证）。"""
    current = tuple(current_labels)
    add = [
        f"类型{labels.SEPARATOR}{result.type_value}",
        f"严重度{labels.SEPARATOR}{result.severity}",
        labels.component_label(result.component),
        status or result.status_label,
        *result.evidence_builders,
        *extra_add,
    ]
    remove: list[str] = []
    for name in add:
        remove.extend(labels.exclusive_removals(current, keep=name))
    return tuple(dict.fromkeys(add)), tuple(dict.fromkeys(remove))


def render_triage_comment(
    task: Task,
    issue: IssueContext,
    result: TriageResult,
    *,
    worktree_dirty: bool = False,
    autofix: bool = False,
    duplicate_of: int | None = None,
    assigned_to: str | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    files = list(result.files[:MAX_FILES_IN_COMMENT])
    file_lines = _bullets([f"`{path}`" for path in files], empty="未定位到具体文件")

    repro_bits = []
    if result.repro_command:
        repro_bits.append(f"命令：`{result.repro_command}`")
    repro_bits.append(f"结果：`{result.repro_result}`")
    if result.repro_evidence:
        repro_bits.append(f"证据：{result.repro_evidence}")
    repro_block = "\n".join(repro_bits)

    similar_lines = [
        f"#{iid}（{relation}）{reason}".strip()
        for iid, relation, reason in result.similar[:MAX_SIMILAR_IN_COMMENT]
    ]

    parts = [
        triage_marker(issue.iid),
        f"## 🔎 自动巡检结论：{VERDICT_LABELS[result.verdict]}",
        "",
        f"**{result.summary}**",
        "",
        f"- 置信度：{result.confidence:.0%}",
        f"- 分类：`类型：{result.type_value}` / `严重度：{result.severity}` / "
        f"`组件：{result.component}`",
        "",
        "### 复现",
        "",
        repro_block or "（未尝试复现）",
        "",
        "### 定位",
        "",
        file_lines,
        "",
        "### 相似 issue",
        "",
        _bullets(similar_lines, empty="未发现明显重复"),
        "",
        "### 处理建议",
        "",
        f"`{result.recommendation}`",
    ]
    if autofix:
        parts += [
            "",
            "> ✅ 已自动排入修复任务（改动小、置信度高）；产出 draft MR，合并仍需人工审批。",
        ]
    if duplicate_of is not None:
        parts += [
            "",
            f"> 🔗 已关联为 #{duplicate_of} 的重复并关闭本 issue。",
        ]
    if assigned_to is not None:
        parts += ["", f"> 👤 已按 CODEOWNERS 指派给 @{assigned_to}。"]
    if worktree_dirty:
        parts += [
            "",
            "> ⚠️ 巡检期间工作区出现过未提交的修改，已随工作目录丢弃。",
            "> 若上面的结论提到「已修复」，不代表仓库现状；仓库仍以默认分支为准。",
        ]
    parts += [
        "",
        f"<sub>任务 `{task.id}` · 生成于 {_ymd_hm(now)} UTC · 结论由模型生成，未经人工确认</sub>",
        BOT_SIGNATURE,
    ]
    return "\n".join(parts)


def render_triage_failure(
    task: Task,
    issue: IssueContext,
    *,
    stage: str,
    reason: str,
    evidence: str = "",
    log_tail: str = "",
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    tail = (log_tail or "").strip()
    body = [
        triage_marker(issue.iid),
        "## ⚠️ 自动巡检失败",
        "",
        f"- 阶段：`{stage}`",
        f"- 原因：{reason}",
    ]
    if evidence:
        body += [f"- 证据：{evidence}"]
    body += [f"- 任务：`{task.id}` · {_ymd_hm(now)} UTC"]
    if tail:
        body += ["", "```text", tail[-1200:], "```"]
    body += ["", "维护者可重新打开 issue 触发重跑（手动重跑命令尚未接线）。", BOT_SIGNATURE]
    return "\n".join(body)
