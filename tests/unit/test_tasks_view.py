"""`/tasks` 的渲染纯函数：转义、过滤显示、JSON 形状。

HTML 转义是这里最重要的一条 —— issue 标题/项目路径来自外部输入，拼错就是 XSS。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.models import ProjectRef, Task, TriggerKind
from core.states import TaskState
from core.tasks_view import TaskFilters, render_task_detail, render_tasks_page, task_to_dict

START = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
TOKEN = "admin-token-0123456789"


def make_task(**overrides) -> Task:
    base = {
        "id": "task-abc123def456",
        "delivery_key": "d-1",
        "project": ProjectRef(project_id=42, path="group/project-a"),
        "issue_iid": 12,
        "trigger_kind": TriggerKind.LABEL,
        "trigger_actor": "alice",
        "created_at": START,
        "updated_at": START,
    }
    base.update(overrides)
    return Task(**base)


def test_json_view_has_expected_fields_and_no_secrets() -> None:
    task = make_task(
        state=TaskState.SUCCEEDED,
        finished_at=START + timedelta(seconds=41),
        branch="ai-fix/12-x",
        mr_iid=3,
        mr_url="http://gitlab/mr/3",
        report={"summary": "内部报告不应出现在列表里"},
        workdir="/work/task-abc123def456",
    )
    data = task_to_dict(task)
    assert data["short_id"] == "abc123def456"
    assert data["state"] == "succeeded"
    assert data["duration_sec"] == 41
    assert data["mr_iid"] == 3
    assert "workdir" not in data  # 宿主路径不外泄
    assert "report" not in data


def test_page_lists_tasks_and_shows_failure_reason() -> None:
    task = make_task(
        state=TaskState.FAILED,
        failure_stage="validating",
        failure_reason="agent 未产生任何文件改动（diff 为空）",
        finished_at=START + timedelta(seconds=9),
    )
    page = render_tasks_page([task], filters=TaskFilters(), token=TOKEN, now=START)
    assert "group/project-a" in page
    assert "failed" in page
    assert "validating" in page
    assert "diff 为空" in page
    assert f'href="/tasks/{task.id}?token={TOKEN}"' in page


def test_page_escapes_html_from_external_input() -> None:
    """项目路径/标题里带 HTML 时必须转义（XSS 回归）。"""
    task = make_task(
        project=ProjectRef(project_id=42, path="<img src=x onerror=alert(1)>"),
        failure_reason='<script>alert("x")</script>',
        branch="ai-fix/<b>",
    )
    page = render_tasks_page([task], filters=TaskFilters(), token=TOKEN, now=START)
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "<img src=x" not in page


def test_empty_state_renders_hint() -> None:
    page = render_tasks_page([], filters=TaskFilters(state="failed"), token=TOKEN, now=START)
    assert "没有符合条件的任务" in page
    assert 'value="failed" selected' in page


def test_page_does_not_render_auto_refresh_when_disabled() -> None:
    page = render_tasks_page([], filters=TaskFilters(), token=TOKEN, refresh_sec=None)
    assert "http-equiv" not in page


def test_detail_page_shows_evidence_and_log_tail() -> None:
    task = make_task(
        state=TaskState.FAILED,
        failure_stage="running",
        failure_reason="codex 退出码 1",
        failure_evidence="未通过：exit_code=1",
    )
    page = render_task_detail(task, token=TOKEN, log_tail='{"type":"turn.failed"}', log_lines=7)
    assert "codex 退出码 1" in page
    assert "未通过：exit_code=1" in page
    assert "turn.failed" in page
    assert "日志尾部（7 行）" in page


def test_detail_page_says_no_log_when_missing() -> None:
    page = render_task_detail(make_task(), token=TOKEN)
    assert "没有可读的日志文件" in page


def test_detail_page_escapes_log_content() -> None:
    page = render_task_detail(make_task(), token=TOKEN, log_tail="<script>x</script>", log_lines=1)
    assert "<script>x" not in page
    assert "&lt;script&gt;" in page


def test_dark_mode_block_comes_after_base_rules() -> None:
    """深色模式必须最后出现：同优先级下靠后者胜出。

    这条测试是"白底浅色字"那个真 bug 的回归 —— 之前 @media 块写在基础规则前面，
    button/th 的基础背景色把它覆盖了。
    """
    from core.tasks_view import _CSS

    dark = _CSS.index("@media (prefers-color-scheme: dark)")
    for selector in ("button {", "th {", "table {", "tr:nth-child(even) td {", "pre {"):
        assert _CSS.index(selector) < dark, selector


def test_dark_mode_overrides_every_colored_surface() -> None:
    """深色块要覆盖所有带底色的元素，漏一个就会出现"看不见"。"""
    from core.tasks_view import _CSS

    block = _CSS[_CSS.index("@media (prefers-color-scheme: dark)") :]
    for selector in ("body {", "th {", "td", "input, select, button", "pre {", ".badge-succeeded"):
        assert selector in block, selector
    assert "color: #e6edf3" in block


def test_page_has_brand_header_and_no_meta_notice() -> None:
    page = render_tasks_page([make_task()], filters=TaskFilters(), token=TOKEN, now=START)
    assert "mini-gitlab-bot" in page
    assert 'class="brand"' in page
    assert 'class="chip"' in page
    # 之前那句"不提供 retry/cancel / 不要分享链接"已按需求删除
    assert "不提供 retry/cancel" not in page
    assert "不要把那条链接分享出去" not in page


def test_detail_page_reuses_the_same_header() -> None:
    page = render_task_detail(make_task(), token=TOKEN)
    assert 'class="brand"' in page
    assert "← 返回列表" in page
