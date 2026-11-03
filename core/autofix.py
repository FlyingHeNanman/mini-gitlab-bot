"""自动修裁决：模型只给估计，**是否真的派 fix 由网关按确定性规则拍板**。

规则（设计文档 §8.1）：

* `AUTOFIX_ENABLED=true`；
* triage 结论为 `confirmed` 且建议 `self_fix`；
* 文件数 / 行数不超阈值、单一组件、无禁改路径、`risk=low`、无新增依赖；
* 置信度 ≥ `AUTOFIX_MIN_CONFIDENCE`；
* 提问者权限 ≥ `AUTOFIX_MIN_ACCESS_LEVEL`（默认 0 = 不检查）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from core import labels
from core.triage import TriageResult, TriageVerdict


@dataclass(frozen=True, slots=True)
class AutofixConfig:
    enabled: bool = True
    max_files: int = 3
    max_lines: int = 150
    min_confidence: float = 0.7
    min_access_level: int = 0
    forbidden_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutofixDecision:
    allowed: bool
    reason: str
    #: 拒绝时附加的风险标签（改动面大 / 跨模块 / 涉及依赖 / 需人工决策）
    risk_labels: tuple[str, ...] = ()


def _reject(reason: str, *risk_labels: str) -> AutofixDecision:
    return AutofixDecision(False, reason, tuple(dict.fromkeys(risk_labels)))


def _forbidden_hit(files: tuple[str, ...], patterns: tuple[str, ...]) -> str | None:
    for path in files:
        for pattern in patterns:
            if not pattern:
                continue
            if fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern):
                return path
            if pattern.endswith("/") and path.startswith(pattern):
                return path
    return None


def adjudicate(
    result: TriageResult,
    config: AutofixConfig,
    *,
    access_level: int | None = None,
) -> AutofixDecision:
    if not config.enabled:
        return _reject("autofix_disabled")
    if result.verdict is not TriageVerdict.CONFIRMED:
        return _reject(f"verdict_{result.verdict.value}")
    if result.recommendation != "self_fix":
        return _reject(f"recommendation_{result.recommendation}")
    if result.confidence < config.min_confidence:
        return _reject("low_confidence")
    if not result.files:
        return _reject("no_location")
    if len(result.files) > config.max_files or result.size_files > config.max_files:
        return _reject("too_many_files", labels.RISK_LARGE_DIFF)
    if result.size_lines > config.max_lines:
        return _reject("too_many_lines", labels.RISK_LARGE_DIFF)
    if result.deps:
        return _reject("needs_dependencies", labels.RISK_NEEDS_DEP)

    forbidden = _forbidden_hit(result.files, config.forbidden_paths)
    if forbidden is not None:
        return _reject(f"forbidden_path:{forbidden}")

    # 单组件 = 命中文件同属一个顶层目录；根目录单文件也视为单组件
    parents = {path.split("/", 1)[0] if "/" in path else "" for path in result.files}
    if len(parents) != 1:
        return _reject("cross_component", labels.RISK_CROSS_MODULE)

    if result.risk != "low":
        return _reject("risk_not_low", labels.RISK_NEEDS_HUMAN)
    if config.min_access_level > 0 and (
        access_level is None or access_level < config.min_access_level
    ):
        return _reject("insufficient_access_level")
    return AutofixDecision(True, "eligible")
