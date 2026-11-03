"""机器人标签的幂等创建（fix / triage 共用）。

第一次在某个项目上干活时，把 31 个固定标签 + `组件：其他` 按定义的颜色/描述建出来。
标签是机器人自己的命名空间（`机器人：` / `类型：` / `严重度：` / `组件：` / `状态：` /
`证据：` / `风险：`），不依赖项目里已有的标签体系。
"""

from __future__ import annotations

import logging

from core import labels

logger = logging.getLogger(__name__)

#: 兜底组件标签：不在 31 个固定标签里（值是动态的），但需要颜色与描述
FALLBACK_COMPONENT_LABEL = labels.LabelDef(
    labels.COMPONENT_FALLBACK, "#C2E0C6", "未匹配到具体组件（跨顶层目录 / 根目录文件）"
)


class LabelEnsurer:
    """按项目缓存"已确保"状态；失败不缓存，下次任务会重试。"""

    def __init__(self, scm) -> None:
        self._scm = scm
        self._ready: set[int] = set()

    async def ensure(self, project_id: int) -> None:
        if project_id in self._ready:
            return
        existing = await self._scm.list_labels(project_id)
        wanted = (*labels.LABELS, FALLBACK_COMPONENT_LABEL)
        for label in labels.missing_labels(existing, wanted):
            await self._scm.create_label(
                project_id,
                name=label.name,
                color=label.color,
                description=label.description,
            )
        self._ready.add(project_id)
