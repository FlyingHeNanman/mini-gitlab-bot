#!/usr/bin/env python3
"""把 GitLab 项目从旧 `ai-fix*` 标签硬切到中文标签体系（默认 dry-run）。

做什么：

1. 幂等创建机器人自己的全部标签（31 个固定标签 + `组件：其他`，带颜色/描述）；
2. 删除旧的 `ai-fix` / `ai-fix:running` / `ai-fix:done` / `ai-fix:failed`
   （GitLab 会同时把它们从 issue 上抹掉）。

不做什么：不会把旧状态映射成新状态（`ai-fix:done` 不代表 `待审批`）；issue 上的
`状态：*` 由下一次任务写入，或人工补标。

用法：

    export GITLAB_URL=http://127.0.0.1:8929
    export GITLAB_TOKEN=glpat-xxx          # 需要 api scope、Maintainer/Owner 权限
    python scripts/migrate_labels.py --project group/project-a            # 预演
    python scripts/migrate_labels.py --project group/project-a --apply    # 真跑

可重复执行：已存在的标签不会重复创建，已删除的旧标签也不会报错。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import labels  # noqa: E402
from core.label_sync import FALLBACK_COMPONENT_LABEL  # noqa: E402


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"{method} {path} → HTTP {status}: {detail[:300]}")
        self.status = status


def api(
    method: str,
    base: str,
    token: str,
    path: str,
    payload: dict | None = None,
) -> object:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/v4{path}",
        data=body,
        method=method,
        headers={
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ApiError(method, path, exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise ApiError(method, path, 0, str(exc.reason)) from exc


def resolve_project_id(base: str, token: str, ref: str) -> int:
    if ref.isdigit():
        return int(ref)
    encoded = urllib.parse.quote(ref, safe="")
    project = api("GET", base, token, f"/projects/{encoded}")
    assert isinstance(project, dict)
    return int(project["id"])


def list_labels(base: str, token: str, project_id: int) -> list[str]:
    names: list[str] = []
    page = 1
    while True:
        batch = api(
            "GET",
            base,
            token,
            f"/projects/{project_id}/labels?per_page=100&page={page}",
        )
        assert isinstance(batch, list)
        names.extend(str(item["name"]) for item in batch)
        if len(batch) < 100:
            return names
        page += 1


def migration_plan(existing: list[str]) -> tuple[list[labels.LabelDef], list[str]]:
    wanted = (*labels.LABELS, FALLBACK_COMPONENT_LABEL)
    to_create = list(labels.missing_labels(existing, wanted))
    to_delete = list(labels.legacy_labels_present(existing))
    return to_create, to_delete


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--project",
        action="append",
        required=True,
        metavar="PATH_OR_ID",
        help="项目路径（group/project）或数字 ID；可重复",
    )
    parser.add_argument(
        "--gitlab-url",
        default=os.environ.get("GITLAB_URL", "http://127.0.0.1:8929"),
        help="GitLab 地址（默认取 GITLAB_URL）",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITLAB_TOKEN", ""),
        help="需要 api scope 的 token（默认取 GITLAB_TOKEN）",
    )
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只预演）")
    args = parser.parse_args(argv)

    if not args.token:
        parser.error("缺少 token：设置 GITLAB_TOKEN 或传 --token")

    base, token = args.gitlab_url, args.token
    failures = 0
    for ref in args.project:
        print(f"\n== {ref} ==")
        try:
            project_id = resolve_project_id(base, token, ref)
            existing = list_labels(base, token, project_id)
            to_create, to_delete = migration_plan(existing)
            print(f"  project_id={project_id} 现有标签 {len(existing)} 个")
            print(f"  待创建：{len(to_create)} 个")
            for definition in to_create:
                print(f"    + {definition.name}")
            print(f"  待删除：{len(to_delete)} 个")
            for name in to_delete:
                print(f"    - {name}")

            if not args.apply:
                print("  （dry-run；加 --apply 才会执行）")
                continue

            for definition in to_create:
                api(
                    "POST",
                    base,
                    token,
                    f"/projects/{project_id}/labels",
                    {
                        "name": definition.name,
                        "color": definition.color,
                        "description": definition.description,
                    },
                )
            for name in to_delete:
                encoded = urllib.parse.quote(name, safe="")
                try:
                    api(
                        "DELETE",
                        base,
                        token,
                        f"/projects/{project_id}/labels/{encoded}",
                    )
                except ApiError as exc:
                    if exc.status != 404:  # 并发删除按幂等处理
                        raise
            print(f"  已创建 {len(to_create)} 个、删除 {len(to_delete)} 个")
        except ApiError as exc:
            failures += 1
            print(f"  ✖ {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
