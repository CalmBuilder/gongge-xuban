"""
@Time       : 2026/09/27
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : app.core import AgentLoop → agent_loop；子模块导入 → core 子模块
@Description: 延迟暴露 AgentLoop，避免核心时间上下文与动态运行时之间形成初始化环路。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.agent_loop import AgentLoop


def __getattr__(name: str) -> object:
    """按需加载公开 AgentLoop，保持历史导入入口并允许子模块独立初始化。"""

    if name == "AgentLoop":
        from app.core.agent_loop import AgentLoop as agent_loop

        return agent_loop
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["AgentLoop"]
