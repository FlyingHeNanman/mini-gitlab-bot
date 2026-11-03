"""Release 说明生成（TODO #2）：纯函数，不依赖模型。

用户在 main 上打 tag 后，机器人把「上一个 tag → 当前 tag」之间的提交按
Conventional Commits 前缀分组，生成一份确定性的发布说明。分类规则与 GitHub
release notes 的默认分类接近，方便直接阅读；无法归类的提交进「其他变更」。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import quote

from core.models import CommitInfo, TagInfo

#: 小节顺序（也是 GitHub 常见顺序：功能 → 修复 → 性能 → 重构 → 文档 → 测试 → 维护）
SECTION_ORDER: tuple[str, ...] = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "docs",
    "test",
    "build",
    "ci",
    "chore",
    "style",
    "revert",
    "other",
)

SECTION_TITLES: dict[str, str] = {
    "feat": "新功能",
    "fix": "修复",
    "perf": "性能",
    "refactor": "重构",
    "docs": "文档",
    "test": "测试",
    "build": "构建",
    "ci": "CI",
    "chore": "杂项",
    "style": "代码风格",
    "revert": "回滚",
    "other": "其他变更",
}

_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<subject>.+)"
)


def classify_commit(commit: CommitInfo) -> tuple[str, bool]:
    """返回 (小节 key, 是否破坏性变更)。非 conventional 提交归入 other。"""
    title = (commit.title or commit.message.splitlines()[0] or "").strip()
    match = _CONVENTIONAL_RE.match(title)
    if match is None:
        return "other", False
    type_ = match.group("type").lower()
    breaking = bool(match.group("breaking"))
    # `feat!:` / `fix!:` 已经是破坏性变更；正文里出现 BREAKING CHANGE 也标注
    if not breaking and "BREAKING CHANGE" in (commit.message or "").upper():
        breaking = True
    return (type_ if type_ in SECTION_TITLES else "other"), breaking


def find_previous_tag(tags: Sequence[TagInfo], current: TagInfo) -> TagInfo | None:
    """在按时间倒序的 tag 列表里找当前 tag 的上一个 tag。"""
    for index, tag in enumerate(tags):
        if tag.name == current.name:
            return tags[index + 1] if index + 1 < len(tags) else None
    # tag 列表可能因分页/缓存没包含当前 tag：退化为「比当前更旧的最新 tag」
    if current.committed_at is None:
        return None
    older = [
        tag
        for tag in tags
        if tag.name != current.name
        and tag.committed_at is not None
        and tag.committed_at < current.committed_at
    ]
    return older[0] if older else None


def build_compare_url(tag: TagInfo, previous: TagInfo | None) -> str:
    """从 tag 的 web_url 推出 GitLab 的 compare 链接（推不出就返回空）。"""
    if previous is None or "/-/tags/" not in (tag.web_url or ""):
        return ""
    base = tag.web_url.split("/-/tags/", 1)[0]
    return f"{base}/-/compare/{quote(previous.name, safe='')}...{quote(tag.name, safe='')}"


def display_title(commit: CommitInfo) -> str:
    """小节里只显示提交描述，去掉冗余的 `feat:` / `fix(scope):` 前缀。"""
    title = (commit.title or commit.message.splitlines()[0] or "").strip()
    match = _CONVENTIONAL_RE.match(title)
    return match.group("subject").strip() if match else title


def _bullet(commit: CommitInfo, breaking: bool) -> str:
    short = (commit.sha or "")[:8] or "unknown"
    sha = f"[`{short}`]({commit.web_url})" if commit.web_url else f"`{short}`"
    prefix = "**破坏性变更** " if breaking else ""
    return f"- {prefix}{display_title(commit)}（{sha}）"


def render_release_notes(
    *,
    tag: TagInfo,
    previous: TagInfo | None,
    commits: Sequence[CommitInfo],
    now: datetime | None = None,
) -> str:
    """生成 GitLab Release 的 description（Markdown）。"""
    now = now or datetime.now(UTC)
    compare_url = build_compare_url(tag, previous)
    header = [f"## {tag.name} 发布说明", ""]
    if previous is not None:
        diff = f"`{previous.name}` → `{tag.name}`"
        if compare_url:
            diff = f"[{diff}]({compare_url})"
        header.append(f"对比：{diff}（{len(commits)} 个提交）")
    else:
        header.append(f"首个 Release（{len(commits)} 个提交）")
    header.append("")

    if not commits:
        return "\n".join(
            header
            + [
                "本次发布没有可列出的提交变更。",
                "",
                "<sub>由 mini-gitlab-bot 自动生成</sub>",
            ]
        )

    grouped: dict[str, list[tuple[CommitInfo, bool]]] = {key: [] for key in SECTION_ORDER}
    for commit in commits:
        section, breaking = classify_commit(commit)
        grouped.setdefault(section, []).append((commit, breaking))

    body: list[str] = []
    for section in SECTION_ORDER:
        items = grouped.get(section) or []
        if not items:
            continue
        body.extend([f"### {SECTION_TITLES[section]}", ""])
        body.extend(_bullet(commit, breaking) for commit, breaking in items)
        body.append("")

    footer = [
        f"<sub>发布于 {now.astimezone(UTC).strftime('%Y-%m-%d')} · "
        "由 mini-gitlab-bot 自动生成</sub>"
    ]
    return "\n".join(header + body + footer)


def release_name(tag: TagInfo) -> str:
    """Release 标题默认与 tag 同名（GitLab 也允许单独命名，这里保持最省心）。"""
    return tag.name
