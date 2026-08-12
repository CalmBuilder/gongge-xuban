"""
@Time       : 2026/08/03 23:42
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : DynamicTaskAgent/management API → dynamic_tasks package
@Description: 对外暴露动态任务能力目录的稳定入口。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.dynamic_tasks.capability_catalog import (
        CapabilityAccessDenied,
        DynamicCapabilityCatalog,
        ToolReliabilityContract,
    )

__all__ = [
    "CapabilityAccessDenied",
    "DynamicCapabilityCatalog",
    "ToolReliabilityContract",
]


def __getattr__(name: str) -> object:
    """按需暴露能力目录类型，避免包初始化阶段与 Skill 提案形成循环导入。"""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.dynamic_tasks import capability_catalog

    return getattr(capability_catalog, name)
