"""
@Time       : 2026/09/02 10:00
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : Chat API/AgentLoop → vertical_abilities catalog/resolver/executor
@Description: 提供平台内置垂直能力包的稳定导出，并延迟加载目录与解析器以隔离轻量连接器。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.vertical_abilities.catalog import (
        PLATFORM_VERTICAL_ABILITIES,
        VerticalAbilityCatalog,
        VerticalAbilityCatalogRead,
        VerticalAbilityResourceDefinition,
        VerticalAbilityRead,
    )
    from app.vertical_abilities.resolver import VerticalAbilityResolution, VerticalAbilityResolver

__all__ = [
    "PLATFORM_VERTICAL_ABILITIES",
    "VerticalAbilityCatalog",
    "VerticalAbilityCatalogRead",
    "VerticalAbilityResourceDefinition",
    "VerticalAbilityRead",
    "VerticalAbilityResolution",
    "VerticalAbilityResolver",
]

_CATALOG_EXPORTS = frozenset(
    {
        "PLATFORM_VERTICAL_ABILITIES",
        "VerticalAbilityCatalog",
        "VerticalAbilityCatalogRead",
        "VerticalAbilityResourceDefinition",
        "VerticalAbilityRead",
    }
)
_RESOLVER_EXPORTS = frozenset({"VerticalAbilityResolution", "VerticalAbilityResolver"})


def __getattr__(name: str) -> Any:
    """按需加载目录/解析器导出，避免宿主机 Relay 连接器启动整个业务依赖树。"""

    if name in _CATALOG_EXPORTS:
        from app.vertical_abilities import catalog

        value = getattr(catalog, name)
    elif name in _RESOLVER_EXPORTS:
        from app.vertical_abilities import resolver

        value = getattr(resolver, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
