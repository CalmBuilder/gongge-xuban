"""
@Time       : 2026/09/01 10:00
@Author     : zhanglp8181
@File       : context_limits.py
@CallChain  : UIConfig/ConversationContext/LLMClient → context_limits
@Description: 提供会话历史预算的统一默认值和安全上限，避免上下文与请求裁剪各自定义边界。
"""

from __future__ import annotations


DEFAULT_CONTEXT_TOKEN_BUDGET = 128_000
MAX_CONTEXT_TOKEN_BUDGET = 262_144
