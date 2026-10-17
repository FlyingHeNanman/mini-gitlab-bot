"""子进程层：脱敏、尾部截断、最小环境（绝不整体继承网关环境）。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from adapters.proc import (
    AsyncProcessRunner,
    CompletedProcess,
    Scrubber,
    _decode_tail,
    minimal_env,
)

SECRET = "glpat-abcdefghijklmnop1234"


def test_scrubber_removes_known_secret() -> None:
    scrub = Scrubber((SECRET,))
    assert SECRET not in scrub(f"clone {SECRET} done")


def test_scrubber_removes_unknown_glpat_token() -> None:
    scrub = Scrubber()
    assert "glpat" not in scrub("remote: https://oauth2:glpat-ZZZZZZZZZZZZ@host/x.git")


def test_scrubber_removes_authorization_header_and_url_credentials() -> None:
    scrub = Scrubber()
    text = "Authorization: Bearer abcdef\nurl=https://user:pa55word@gitlab/x.git"
    cleaned = scrub(text)
    assert "abcdef" not in cleaned
    assert "pa55word" not in cleaned


def test_scrubber_ignores_very_short_secrets() -> None:
    """太短的值到处都会误伤（例如 "abc"），只脱敏长度 ≥ 6 的。"""
    scrub = Scrubber(("abc",))
    assert scrub("abc def") == "abc def"


def test_scrubber_handles_argv_and_env() -> None:
    scrub = Scrubber((SECRET,))
    # 整段选项被抹掉（比只抹值更保险）
    assert scrub.argv(["git", f"--token={SECRET}"]) == ("git", "--***")
    assert scrub.env({"GITLAB_TOKEN": SECRET}) == {"GITLAB_TOKEN": "***"}


def test_scrubber_prefers_longest_secret_first() -> None:
    """先替换长密钥，否则短前缀被替换后长密钥会残留尾巴。"""
    scrub = Scrubber(("glpat-abc", "glpat-abcdefgh"))
    assert scrub("glpat-abcdefgh") == "***"


def test_decode_tail_keeps_the_end() -> None:
    text, truncated = _decode_tail(b"x" * 100, 10)
    assert truncated is True
    assert text == "x" * 10


def test_decode_tail_marks_no_truncation() -> None:
    text, truncated = _decode_tail("你好".encode(), 100)
    assert truncated is False
    assert text == "你好"


def test_decode_tail_survives_invalid_utf8() -> None:
    text, _ = _decode_tail(b"\xff\xfe ok", 100)
    assert "ok" in text


def test_minimal_env_is_explicit_allowlist_only() -> None:
    env = minimal_env(path="/usr/bin", home="/root", extra={"UV_OFFLINE": "1", "EMPTY": ""})
    assert env["PATH"] == "/usr/bin"
    assert env["UV_OFFLINE"] == "1"
    assert "EMPTY" not in env  # 空值不写入
    assert "GITLAB_TOKEN" not in env
    assert "WEBHOOK_SECRET" not in env


def test_completed_process_tail_and_line_count() -> None:
    result = CompletedProcess(argv=("x",), exit_code=1, stdout="a\nb\n", stderr="c\n")
    assert result.tail(2) == "b\nc"
    assert result.line_count == 3
    assert not result.ok


async def test_async_runner_captures_output_and_env() -> None:
    runner = AsyncProcessRunner()
    result = await runner.run(
        [
            sys.executable,
            "-c",
            "import os,sys; print('out'); print(os.environ['ONLY'], file=sys.stderr)",
        ],
        env={"PATH": "/usr/bin", "ONLY": "yes", "GITLAB_TOKEN": SECRET},
        timeout_sec=30,
        label="test",
    )
    assert result.ok
    assert "out" in result.stdout
    assert "yes" in result.stderr
    # 记录里已脱敏，但子进程确实只拿到我们显式给的环境
    assert runner.calls[0]["env"]["ONLY"] == "yes"
    assert "GITLAB_TOKEN" in runner.calls[0]["env"]  # 这是测试显式给的，不是继承来的


async def test_async_runner_does_not_inherit_parent_env(monkeypatch) -> None:
    monkeypatch.setenv("LEAKY_GATEWAY_SECRET", "must-not-leak")
    runner = AsyncProcessRunner()
    result = await runner.run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('LEAKY_GATEWAY_SECRET', 'absent'))",
        ],
        env={"PATH": "/usr/bin"},
        timeout_sec=30,
    )
    assert "absent" in result.stdout


async def test_async_runner_kills_process_group_on_timeout() -> None:
    runner = AsyncProcessRunner()
    script = "import time; time.sleep(30)"
    result = await runner.run(
        [sys.executable, "-c", script], env={"PATH": "/usr/bin"}, timeout_sec=0.3
    )
    assert result.timed_out
    assert result.duration_sec < 10


async def test_async_runner_reports_nonzero_exit() -> None:
    runner = AsyncProcessRunner()
    result = await runner.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        env={"PATH": "/usr/bin"},
        timeout_sec=30,
    )
    assert result.exit_code == 3
    assert "boom" in result.stderr


@pytest.mark.parametrize("timeout", [0.2])
async def test_async_runner_timeout_is_not_an_exception(timeout: float) -> None:
    """超时是"结果"而不是异常：调用方（agent 适配器）要把它变成四条件判定的一部分。"""
    runner = AsyncProcessRunner()
    result = await runner.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        env={"PATH": "/usr/bin"},
        timeout_sec=timeout,
    )
    assert result.exit_code == -1
    assert result.timed_out


async def test_async_runner_records_scrubbed_argv() -> None:
    runner = AsyncProcessRunner(scrubber=Scrubber((SECRET,)))
    await runner.run(
        [sys.executable, "-c", "pass", SECRET], env={"PATH": "/usr/bin"}, timeout_sec=30
    )
    assert SECRET not in runner.calls[0]["argv"]


async def test_async_runner_raises_on_missing_binary() -> None:
    runner = AsyncProcessRunner()
    with pytest.raises(FileNotFoundError):
        await runner.run(["/nonexistent/binary-xyz"], env={"PATH": "/usr/bin"}, timeout_sec=5)


async def test_cancellation_kills_the_child(monkeypatch) -> None:
    """编排器的硬超时会取消适配器：子进程必须被连带清掉，不能留孤儿。"""
    runner = AsyncProcessRunner()
    task = asyncio.create_task(
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env={"PATH": "/usr/bin"},
            timeout_sec=60,
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
