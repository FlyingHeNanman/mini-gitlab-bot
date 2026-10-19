"""版本号单一来源：`core.__version__` 必须与 `pyproject.toml` 一致。"""

from __future__ import annotations

import tomllib
from pathlib import Path

from core import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_core_version_matches_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__


def test_mcp_server_reports_the_package_version() -> None:
    from core.mcp_spec import SERVER_VERSION

    assert __version__ == SERVER_VERSION
