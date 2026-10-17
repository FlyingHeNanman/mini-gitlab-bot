"""diff 策略硬校验 + agent 失败原因文案。

对应 PLAN §7 基线 5（四条件判定）与 §9 M2（diff 策略校验）。

禁改路径的**安全理由**：agent 若能改 `.gitlab-ci.yml`，它就能把 CI 改成恒绿 ——
而 CI 是本项目唯一的最终裁判（PLAN §5"验证环节"）。同理 `.env` / `*.pem` / `*.key`
是凭据文件，绝不允许 agent 触碰。
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from core.models import AgentResult, DiffSummary

DEFAULT_FORBIDDEN_PATHS: tuple[str, ...] = (
    ".gitlab-ci.yml",
    ".gitlab/",
    ".mini-gitlab-bot.yaml",
    ".env",
    "*.pem",
    "*.key",
    "id_rsa*",
)


@dataclass(frozen=True, slots=True)
class DiffViolation:
    kind: str  # empty_diff | forbidden_path | too_many_files | too_many_lines
    detail: str
    paths: tuple[str, ...] = ()


def _normalize(path: str) -> str:
    """去掉前后空白与 git 风格的 `./` 前缀。

    注意**不能**用 `lstrip("./")` —— 那会把 `.env`、`.gitlab-ci.yml` 这类
    dotfile 的前导点一起吃掉，导致禁改规则静默失效。
    """
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


@dataclass(frozen=True, slots=True)
class DiffPolicy:
    max_files: int = 50
    max_lines: int = 2000
    forbidden_paths: tuple[str, ...] = DEFAULT_FORBIDDEN_PATHS

    def check(self, diff: DiffSummary) -> DiffViolation | None:
        """返回第一个命中的违规；全部通过则返回 None。"""
        if diff.is_empty:
            return DiffViolation("empty_diff", "agent 未产生任何文件改动（diff 为空）")

        hits = tuple(path for path in diff.files if self.is_forbidden(path))
        if hits:
            return DiffViolation(
                "forbidden_path",
                f"diff 校验未通过：改动触达禁改路径 {', '.join(hits)}",
                hits,
            )

        if len(diff.files) > self.max_files:
            return DiffViolation(
                "too_many_files",
                f"diff 校验未通过：改动文件数 {len(diff.files)} 超过上限 {self.max_files}",
                tuple(diff.files[:10]),
            )

        if diff.changed_lines > self.max_lines:
            return DiffViolation(
                "too_many_lines",
                f"diff 校验未通过：改动行数 {diff.changed_lines} 超过上限 {self.max_lines}",
            )
        return None

    def is_forbidden(self, path: str) -> bool:
        cleaned = _normalize(path)
        name = cleaned.rsplit("/", 1)[-1]
        return any(self._matches(cleaned, name, pattern) for pattern in self.forbidden_paths)

    @staticmethod
    def _matches(path: str, name: str, pattern: str) -> bool:
        if pattern.endswith("/"):  # 目录前缀
            return path.startswith(pattern) or path == pattern.rstrip("/")
        if "/" not in pattern:  # 裸文件名/通配：同时匹配 basename 与完整路径
            return fnmatch(name, pattern) or fnmatch(path, pattern)
        return fnmatch(path, pattern)


def describe_agent_failure(result: AgentResult) -> str:
    """按 docs/templates.md 的**固定措辞**描述四条件判定失败，便于检索与统计。"""
    if result.timed_out:
        return "任务超时：codex 未在时限内结束，进程组已被终止"
    if result.exit_code != 0:
        return f"codex 退出码 {result.exit_code}，未产生 turn.completed 事件"
    if not result.completed_event:
        return "codex 已退出但未产生 turn.completed 事件（疑似被中断）"
    if not result.last_message_written:
        return "codex 未写入 --output-last-message 文件"
    return "agent 结果未通过四条件判定"
