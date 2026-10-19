"""网关核心逻辑：状态机、领域模型、策略校验、文案渲染、编排器。

本包**不直接做 IO**：所有外部交互都通过 `ports/` 定义的协议注入，
因此可以在 CI 里用假 SCM + 假 agent 端到端跑通（见 PLAN §8）。
"""

#: 版本号的唯一来源。`pyproject.toml` 里的 version 必须与它一致
#: （`tests/unit/test_version.py` 会校验，防止两边漂移）。
__version__ = "0.1.0"
