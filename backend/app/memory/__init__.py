"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : 领域调用方 → memory公共服务；LLMClient → 独立投影模块
@Description: 延迟导出记忆服务，避免纯投影导入触发LLM/Runtime循环初始化。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.memory.service import MemoryService


def __getattr__(name: str):
    """保持原MemoryService导出合同，同时允许低层请求只导入投影类型。"""
    if name == "MemoryService":
        from app.memory.service import MemoryService

        return MemoryService
    raise AttributeError(name)

__all__ = ["MemoryService"]
