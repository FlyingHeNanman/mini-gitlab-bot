"""Dockerfile / compose 的静态约束：这些错了，镜像会在部署时而不是测试时炸。

不能在这里真 build（CI 无网络、无 docker），但"必须 pin 版本、非 root、拷对目录、
不带 glab"这类事实是静态可查的，值得锁住 —— 它们正是 v0 镜像踩过的坑。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"


def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_no_stale_warning_header() -> None:
    """v0 那版镜像带着"请勿直接部署"的警告头，M5 后不该再出现。"""
    text = dockerfile()
    assert "请勿直接部署" not in text
    assert "⚠️" not in text


def test_base_image_is_pinned_python_313() -> None:
    text = dockerfile()
    assert re.search(r"^ARG PYTHON_VERSION=3\.13", text, re.MULTILINE)
    assert "FROM python:${PYTHON_VERSION}-slim" in text
    assert "python:3.12" not in text


def test_every_toolchain_is_pinned() -> None:
    text = dockerfile()
    for pattern in (
        r"^ARG UV_VERSION=\d+\.\d+\.\d+$",
        r"^ARG CODEX_VERSION=\d+\.\d+\.\d+$",
        r"^ARG NODE_VERSION=\d+$",
    ):
        assert re.search(pattern, text, re.MULTILINE), pattern
    # uv 来自 pin 版本镜像；codex 来自 pin 版本的 npm 包
    assert "ghcr.io/astral-sh/uv:${UV_VERSION}" in text
    assert "@openai/codex@${CODEX_VERSION}" in text


def test_installs_git_and_uv_but_not_glab() -> None:
    text = dockerfile()
    assert "git ca-certificates" in text
    # 注释里可以提"不装 glab"，但安装命令里绝不能出现它
    install_lines = [
        line for line in text.splitlines() if "apt-get install" in line or "npm install" in line
    ]
    assert install_lines
    assert all("glab" not in line for line in install_lines)


def test_runs_as_non_root() -> None:
    text = dockerfile()
    assert re.search(r"^USER bot$", text, re.MULTILINE)
    assert "--uid 10001" in text
    assert re.search(r"^USER root$", text, re.MULTILINE) is None


def test_copies_every_package_and_declares_volumes() -> None:
    text = dockerfile()
    for package in ("core", "ports", "adapters", "gateway"):
        assert re.search(rf"^COPY {package} \./{package}$", text, re.MULTILINE), package
    assert "mkdir -p /app /data/logs /work" in text
    assert "LOG_DIR=/data/logs" in text
    assert "UV_CACHE_DIR=/work/.uv-cache" in text


def test_has_healthcheck_and_python_module_entrypoint() -> None:
    text = dockerfile()
    assert "HEALTHCHECK" in text
    assert "/healthz" in text
    assert 'CMD ["python", "-m", "uvicorn", "gateway.app:app"' in text


def test_compose_has_one_service_with_healthcheck_and_volumes() -> None:
    text = compose()
    assert text.count("\n  bot:") == 1
    # 只允许注释里出现 renovate（说明为什么删掉），不允许真的定义这个服务
    assert not re.search(r"^\s{2}renovate:", text, re.MULTILINE)
    assert "TARGET_BRANCH" not in text
    assert "MAX_ITERATIONS" not in text
    assert "healthcheck:" in text
    assert "/readyz" in text
    assert "bot-data:/data" in text
    assert "bot-work:/work" in text
    assert "env_file:" in text


def test_compose_hardens_the_container() -> None:
    text = compose()
    assert "no-new-privileges:true" in text
    assert "cap_drop:" in text
    assert "init: true" in text
    assert "stop_grace_period" in text


def test_compose_pins_codex_version_arg() -> None:
    assert re.search(r"CODEX_VERSION: \$\{CODEX_VERSION:-", compose())


@pytest.mark.parametrize("name", ["core", "ports", "adapters", "gateway"])
def test_package_dirs_exist(name: str) -> None:
    assert (ROOT / name).is_dir()
