"""端口（协议）层：定义网关与外界之间的**唯一**契约。

依赖方向：`core` ← `ports` ← `adapters`。
端口只声明协议，数据形状来自 `core.models`；适配器（真 GitLab / git / codex /
SQLite / 假实现）都在 `adapters/` 下。

这样 CI 里可以只用假 SCM + 假 agent 跑通全链路，不需要 GitLab、不需要 LLM（PLAN §8）。
"""

from ports.agent import AgentRunner
from ports.env import EnvPreparer
from ports.notifier import Notifier
from ports.scm import SCM
from ports.store import Store
from ports.vcs import VCS
from ports.workspace import Workspace

__all__ = ["SCM", "VCS", "AgentRunner", "EnvPreparer", "Notifier", "Store", "Workspace"]
