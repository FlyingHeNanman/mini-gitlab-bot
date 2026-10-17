"""`context.md`：agent 唯一的上下文入口，所以它的内容值得被逐条钉住。"""

from __future__ import annotations

from datetime import UTC, datetime

from core.context import render_context
from core.models import IssueContext, ProjectRef, Task, TriggerKind
from core.policy import DiffPolicy

START = datetime(2026, 10, 17, 10, 31, 0, tzinfo=UTC)
TOOLS = ("get_issue", "get_job_log", "list_pipelines")


def make_task() -> Task:
    return Task(
        id="task-ctx",
        delivery_key="d",
        project=ProjectRef(project_id=42, path="group/project-a"),
        issue_iid=12,
        trigger_kind=TriggerKind.LABEL,
        trigger_actor="alice",
        created_at=START,
        updated_at=START,
    )


def make_issue(**overrides) -> IssueContext:
    base = {
        "iid": 12,
        "title": "parse_labels 在空标签时返回 None",
        "description": "复现：给一个没有任何标签的 issue 调用 parse_labels()",
        "author": "reporter",
        "web_url": "https://gitlab.example.com/group/project-a/-/issues/12",
        "labels": ("ai-fix", "bug"),
        "comments": ("我也遇到了这个问题", "试了 3.12 也一样"),
    }
    base.update(overrides)
    return IssueContext(**base)


def render(**kwargs) -> str:
    return render_context(make_task(), make_issue(), policy=DiffPolicy(), now=START, **kwargs)


def test_contains_the_issue_facts() -> None:
    body = render()
    assert "# 任务：修复 GitLab issue #12" in body
    assert "parse_labels 在空标签时返回 None" in body
    assert "@reporter" in body
    assert "group/project-a" in body
    assert "`ai-fix`" in body
    assert "我也遇到了这个问题" in body


def test_hard_constraints_are_spelled_out() -> None:
    body = render()
    # agent 无网络 / 不许 push / 禁改路径 / 必须 commit / 不许改 pyproject
    assert "你**没有网络**" in body
    assert "`git push`" in body
    assert "**不要**" in body
    assert ".gitlab-ci.yml" in body
    assert "`git commit`" in body
    assert "不要**改 `pyproject.toml`" in body


def test_report_json_contract_is_documented() -> None:
    body = render()
    assert "## 结构化报告（必需）" in body
    for field in ("summary", "fixes", "tests", "test_summary", "not_run", "risks", "deps"):
        assert f'"{field}"' in body


def test_mcp_section_only_when_tools_are_available() -> None:
    with_tools = render(mcp_tools=TOOLS)
    assert "## 只读上下文工具（MCP）" in with_tools
    for name in TOOLS:
        assert f"`{name}`" in with_tools
    assert "只读" in with_tools
    assert "list_pipeline_jobs" in with_tools  # 提示里给出了"拿 job_id"的用法

    without_tools = render()
    assert "只读上下文工具" not in without_tools


def test_forbidden_paths_are_rendered_from_policy() -> None:
    policy = DiffPolicy(forbidden_paths=("secrets/", "Makefile"))
    body = render_context(make_task(), make_issue(), policy=policy, now=START)
    assert "`secrets/`" in body
    assert "`Makefile`" in body
    assert ".gitlab-ci.yml" not in body


def test_long_issue_body_and_comments_are_clipped() -> None:
    issue = make_issue(description="x" * 20_000, comments=("y" * 20_000,))
    body = render_context(make_task(), issue, policy=DiffPolicy(), now=START)
    assert "已截断" in body
    assert len(body) < 20_000


def test_extra_sections_are_appended() -> None:
    body = render(extra_sections=["上一次失败的日志：boom"])
    assert "## 补充上下文" in body
    assert "上一次失败的日志：boom" in body
