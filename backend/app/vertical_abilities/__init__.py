"""
@Time       : 2026/09/02 10:00
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : Chat API/AgentLoop → vertical_abilities catalog/resolver/executor
@Description: 提供平台内置垂直能力包的稳定目录、运行时解析和适配器边界。
"""

from app.vertical_abilities.catalog import (
    PLATFORM_VERTICAL_ABILITIES,
    VerticalAbilityCatalog,
    VerticalAbilityCatalogRead,
    VerticalAbilityResourceDefinition,
    VerticalAbilityRead,
)
from app.vertical_abilities.resolver import (
    VerticalAbilityResolution,
    VerticalAbilityResolver,
)

__all__ = [
    "PLATFORM_VERTICAL_ABILITIES",
    "VerticalAbilityCatalog",
    "VerticalAbilityCatalogRead",
    "VerticalAbilityResourceDefinition",
    "VerticalAbilityRead",
    "VerticalAbilityResolution",
    "VerticalAbilityResolver",
]
