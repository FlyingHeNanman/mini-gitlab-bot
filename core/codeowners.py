"""最小可用的 CODEOWNERS 解析（只做确定性指派，不做组件映射）。

支持 GitLab 官方位置：`.gitlab/CODEOWNERS`、`CODEOWNERS`、`docs/CODEOWNERS`。
规则按「最后一条匹配的规则生效」；支持 `*`、`?`、`**`、前导 `/` 与目录尾 `/`。
owner 只取 `@username`；team（`@group/team`）与邮箱暂不指派（无法映射到单个用户）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

CODEOWNERS_PATHS: tuple[str, ...] = (".gitlab/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/")
    body = pattern[1:] if anchored else pattern
    if body.endswith("/"):
        body += "**"

    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "*":
            if body[index : index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1

    prefix = "^" if anchored else r"(^|.*/)"
    return re.compile(prefix + "".join(out) + "$")


def parse_codeowners(text: str) -> tuple[tuple[re.Pattern[str], tuple[str, ...]], ...]:
    """解析成 `(pattern, owners)` 列表；注释、空行、无 owner 行忽略。"""
    rules: list[tuple[re.Pattern[str], tuple[str, ...]]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, owners = parts[0], parts[1:]
        usernames = tuple(
            owner[1:]
            for owner in owners
            if owner.startswith("@") and "/" not in owner and "@" not in owner[1:]
        )
        if usernames:
            rules.append((_glob_to_regex(pattern), usernames))
    return tuple(rules)


def owners_for_files(files: Iterable[str], text: str) -> tuple[str, ...]:
    """按 CODEOWNERS 解析改动文件的 owner（去重、保持出现顺序）。"""
    rules = parse_codeowners(text)
    found: list[str] = []
    for path in files:
        clean = path.split(":", 1)[0].strip().lstrip("./")
        if not clean:
            continue
        matched: tuple[str, ...] = ()
        for regex, owners in rules:
            if regex.match(clean):
                matched = owners  # 最后一条匹配生效
        for owner in matched:
            if owner not in found:
                found.append(owner)
    return tuple(found)


def first_assignable(owners: Sequence[str], *, known: Iterable[str]) -> str | None:
    """在已知用户里挑第一个可指派的 owner（测试与网关共用）。"""
    known_set = set(known)
    for owner in owners:
        if owner in known_set:
            return owner
    return None
