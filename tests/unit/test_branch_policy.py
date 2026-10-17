"""分支命名 + diff 策略 + agent 失败文案。"""

from __future__ import annotations

import pytest

from core.branch import branch_name, slugify
from core.models import AgentResult, DiffSummary
from core.policy import DiffPolicy, describe_agent_failure


def test_slugify_ascii_title() -> None:
    assert slugify("Fix parse_labels When Empty!") == "fix-parse-labels-when-empty"


def test_slugify_chinese_title_falls_back_to_empty() -> None:
    # 中文标题 slug 出空串，由 branch_name 兜底成 "fix"
    assert slugify("修复空标签解析") == ""


def test_slugify_truncates() -> None:
    assert len(slugify("a" * 100)) <= 40


def test_branch_name_first_attempt_is_stable_and_readable() -> None:
    name = branch_name(12, "Fix Empty Labels", attempt=1, task_id="task-abc123")
    assert name == "ai-fix/12-fix-empty-labels"


def test_branch_name_retry_is_distinct() -> None:
    first = branch_name(12, "Fix Empty Labels", attempt=1, task_id="task-abc123")
    retry = branch_name(12, "Fix Empty Labels", attempt=2, task_id="task-abc123")
    assert retry != first
    assert retry.startswith(first)
    assert retry.endswith("abc123")


def test_branch_name_chinese_title_uses_fix_fallback() -> None:
    assert branch_name(7, "修复问题") == "ai-fix/7-fix"


@pytest.mark.parametrize(
    "path",
    [
        ".gitlab-ci.yml",
        ".gitlab/deploy.yml",
        "config/.env",
        "certs/server.pem",
        "deploy/id_rsa",
        ".mini-gitlab-bot.yaml",
    ],
)
def test_forbidden_paths_are_detected(path: str) -> None:
    assert DiffPolicy().is_forbidden(path), f"{path} 应当被禁止"


@pytest.mark.parametrize("path", ["gateway/config.py", "tests/unit/test_x.py", "src/env.py"])
def test_normal_source_paths_are_allowed(path: str) -> None:
    assert not DiffPolicy().is_forbidden(path)


@pytest.mark.parametrize(
    "path", [".env", "./.env", "  .env  ", ".gitlab-ci.yml", "./.gitlab-ci.yml"]
)
def test_normalization_does_not_swallow_leading_dots(path: str) -> None:
    """回归：`lstrip("./")` 会把 dotfile 的前导点吃掉，导致禁改规则静默失效。"""
    assert DiffPolicy().is_forbidden(path)


def test_empty_diff_is_a_violation() -> None:
    violation = DiffPolicy().check(DiffSummary())
    assert violation is not None
    assert violation.kind == "empty_diff"
    assert violation.detail == "agent 未产生任何文件改动（diff 为空）"


def test_forbidden_path_violation_lists_hits() -> None:
    diff = DiffSummary(files=("gateway/config.py", ".gitlab-ci.yml"), insertions=5, deletions=1)
    violation = DiffPolicy().check(diff)
    assert violation is not None
    assert violation.kind == "forbidden_path"
    assert violation.paths == (".gitlab-ci.yml",)
    assert "禁改路径" in violation.detail


def test_too_many_files_violation() -> None:
    diff = DiffSummary(files=tuple(f"pkg/f{i}.py" for i in range(5)), insertions=5, deletions=0)
    violation = DiffPolicy(max_files=3).check(diff)
    assert violation is not None
    assert violation.kind == "too_many_files"


def test_too_many_lines_violation() -> None:
    diff = DiffSummary(files=("pkg/f.py",), insertions=1900, deletions=200)
    violation = DiffPolicy(max_lines=2000).check(diff)
    assert violation is not None
    assert violation.kind == "too_many_lines"


def test_reasonable_diff_passes() -> None:
    diff = DiffSummary(
        files=("gateway/config.py", "tests/unit/test_config.py"), insertions=30, deletions=4
    )
    assert DiffPolicy().check(diff) is None


def test_describe_agent_failure_uses_fixed_wording() -> None:
    assert (
        describe_agent_failure(
            AgentResult(exit_code=1, completed_event=False, last_message_written=False)
        )
        == "codex 退出码 1，未产生 turn.completed 事件"
    )
    assert (
        describe_agent_failure(
            AgentResult(exit_code=0, completed_event=False, last_message_written=True)
        )
        == "codex 已退出但未产生 turn.completed 事件（疑似被中断）"
    )
    assert (
        describe_agent_failure(
            AgentResult(exit_code=0, completed_event=True, last_message_written=False)
        )
        == "codex 未写入 --output-last-message 文件"
    )


def test_agent_result_ok_requires_first_three_conditions() -> None:
    assert AgentResult(exit_code=0, completed_event=True, last_message_written=True).ok
    assert not AgentResult(exit_code=0, completed_event=True, last_message_written=False).ok
    assert not AgentResult(exit_code=0, completed_event=False, last_message_written=True).ok
    assert not AgentResult(exit_code=2, completed_event=True, last_message_written=True).ok
