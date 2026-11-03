"""CODEOWNERS 解析：最后匹配生效、通配符、只认 @username。"""

from __future__ import annotations

from core.codeowners import owners_for_files, parse_codeowners

SAMPLE = """
# 注释
* @fallback
/gateway/ @backend
gateway/*.py @alice
**/models.py @bob
docs/ @group/docs @carol
"""


def test_parse_skips_comments_and_team_owners() -> None:
    rules = parse_codeowners(SAMPLE)
    owners = {owner for _, group in rules for owner in group}
    assert "fallback" in owners
    assert "alice" in owners
    assert "docs" not in owners  # team 无法映射到单个用户
    assert "carol" in owners


def test_last_matching_rule_wins() -> None:
    text = "gateway/*.py @alice\ngateway/config.py @bob\n"
    assert owners_for_files(["gateway/config.py"], text) == ("bob",)
    assert owners_for_files(["gateway/other.py"], text) == ("alice",)


def test_double_star_and_root_anchor() -> None:
    text = "**/models.py @bob\n/docs/index.md @carol\n"
    assert owners_for_files(["core/models.py"], text) == ("bob",)
    assert owners_for_files(["docs/index.md"], text) == ("carol",)
    assert owners_for_files(["gateway/index.md"], text) == ()


def test_unknown_files_fall_back_to_global_rule() -> None:
    assert owners_for_files(["README.md"], SAMPLE) == ("fallback",)


def test_no_codeowners_text_returns_empty() -> None:
    assert owners_for_files(["app.py"], "") == ()
