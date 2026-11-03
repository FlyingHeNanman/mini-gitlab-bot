# 待办

两条需求均已在 v0.2.0（M6）落地，保留原文作为验收依据：

1. ✅ 自动回复 issue：周期检查开放的 issue，如果某个 commit 已经引用（`Closes/Fixes/Resolves #N`），
   则回帖「已由提交修复」并关闭 issue。实现见 `core/sweep.py`，配置见 `SWEEP_*`。
2. ✅ 自动发布 Release：用户在 main 上打 tag 后，机器人根据 tag 补上 GitLab Release，
   并按提交类型生成类似 GitHub Release 的中文发布说明。实现见 `core/release.py` /
   `core/release_orchestrator.py`，配置见 `RELEASE_*`。

验收：`uv run pytest -q`（633 项）、`uv run ruff check .`、`uv run ruff format --check .`。
