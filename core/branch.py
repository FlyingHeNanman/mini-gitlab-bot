"""分支命名：确定性、可读、重试不撞车。

* 首次尝试用稳定名字 `ai-fix/<iid>-<slug>`（issue 上好看，MR URL 可预测）；
* 重试追加任务短 id 后缀 —— 上一次尝试可能已经把同名分支推上去了，
  复用同名分支会变成 non-fast-forward（PLAN §9 M3 的 push 冲突问题）。
"""

from __future__ import annotations

import re
import unicodedata

BRANCH_PREFIX = "ai-fix"
_MAX_SLUG = 40
_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = _MAX_SLUG) -> str:
    """把标题变成本 ASCII slug；中文标题会退化成空串，由调用方兜底。"""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _UNSAFE.sub("-", normalized.lower()).strip("-")
    return slug[:max_length].strip("-")


def branch_name(issue_iid: int, title: str, *, attempt: int = 1, task_id: str = "") -> str:
    slug = slugify(title) or "fix"
    base = f"{BRANCH_PREFIX}/{issue_iid}-{slug}"
    if attempt <= 1:
        return base
    suffix = task_id.removeprefix("task-")[:6] or f"r{attempt}"
    return f"{base}-{suffix}"
