"""
@Time       : 2026/09/01
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : AgentLoop/ResponseGenerator → app.llm → LLMClient/错误识别
@Description: 暴露统一模型客户端及上下文溢出识别契约。
"""

from app.llm.client import LLMClient, LLMError, is_context_overflow_error

__all__ = ["LLMClient", "LLMError", "is_context_overflow_error"]
