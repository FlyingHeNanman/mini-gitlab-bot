"""领域异常：全部继承 `BotError`，便于在编排器里统一兜底。"""

from __future__ import annotations


class BotError(Exception):
    """本项目所有自定义异常的基类。"""


class InvalidTransition(BotError):
    """非法状态流转（违反 `core.states.ALLOWED_TRANSITIONS`）。"""

    def __init__(self, src: object, dst: object) -> None:
        super().__init__(f"非法状态流转：{src} → {dst}")
        self.src = src
        self.dst = dst


class AgentFailure(BotError):
    """agent 阶段失败（四条件判定未通过 / 超时）。"""


class PolicyViolation(BotError):
    """diff 策略校验未通过。"""
