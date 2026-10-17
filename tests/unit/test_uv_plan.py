"""uv 准备阶段的纯逻辑：Python 版本规划（镜像只预置 3.13，其余按需补装）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.uv_env import (
    PRESET_PYTHON,
    _detect_requires_python,
    _python_plan,
    _satisfies,
)


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [
        (">=3.11", True),
        (">=3.13", True),
        (">=3.14", False),
        ("==3.13", True),
        ("==3.13.*", True),
        ("==3.12.*", False),
        (">=3.11,<3.14", True),
        (">=3.11,<3.13", False),
        ("~=3.12", True),  # ~=3.12 ⇔ >=3.12, ==3.*
        ("~=3.13.1", False),
        ("~=3.13", True),
        (">3.13", False),
        ("<=3.13", True),
        (">=3.9,!=3.13.*", False),
        (">= 3.12", True),  # 允许操作符后有空格
        ("bogus", True),  # 认不出的约束不否决预置版本
    ],
)
def test_satisfies(specifier: str, expected: bool) -> None:
    assert _satisfies(PRESET_PYTHON, specifier) is expected


def test_no_declaration_uses_preset_without_installing() -> None:
    assert _python_plan("") == ("", PRESET_PYTHON)


def test_preset_satisfies_means_no_network_install() -> None:
    assert _python_plan(">=3.11") == ("", PRESET_PYTHON)


def test_lower_bound_is_installed_when_preset_is_too_new() -> None:
    assert _python_plan(">=3.9,<3.13") == ("3.9", "3.9")


def test_exact_pin_is_installed() -> None:
    assert _python_plan("==3.12.4") == ("3.12.4", "3.12.4")


def test_detect_prefers_python_version_file(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.12.4\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    assert _detect_requires_python(tmp_path) == "3.12.4"


def test_detect_reads_requires_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.12,<3.13"\n', encoding="utf-8"
    )
    assert _detect_requires_python(tmp_path) == ">=3.12,<3.13"


def test_detect_handles_single_quotes_and_missing_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nrequires-python = '>=3.11'\n", encoding="utf-8"
    )
    assert _detect_requires_python(tmp_path) == ">=3.11"
    assert _detect_requires_python(tmp_path / "nope") == ""


def test_detect_ignores_unrelated_requires_python_like_text(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "requires-python-fake"\n', encoding="utf-8"
    )
    assert _detect_requires_python(tmp_path) == ""
