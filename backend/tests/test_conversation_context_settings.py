"""
@Time       : 2026/08/28 15:05
@Author     : zhanglp8181
@File       : test_conversation_context_settings.py
@CallChain  : UIConfig/ConversationContextSettings → build_conversation_context → AgentLoop 上下文元数据
@Description: 验证租户上下文配置的边界、覆盖优先级和压缩行为。
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.ui_config import UIConfigUpdateRequest
from app.core.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)


def test_ui_config_values_are_clamped_before_runtime_use() -> None:
    """验证管理端配置越界时会收敛到安全范围，而不会扩大模型输入预算。"""

    settings = ConversationContextSettings.from_ui_config(
        SimpleNamespace(
            context_token_budget=999_999,
            context_compaction_trigger_ratio=0.01,
            context_recent_round_limit=999,
        )
    )

    assert settings.token_budget == 32_000
    assert settings.compaction_trigger_ratio == 0.45
    assert settings.recent_round_limit == 20
    assert settings.long_summary_token_budget == 4_000


def test_context_settings_drive_metadata_and_recent_round_projection() -> None:
    """验证自定义预算、触发比例和近期轮数确实进入压缩决策与输出元数据。"""

    settings = ConversationContextSettings(
        token_budget=220,
        compaction_trigger_ratio=0.5,
        recent_round_limit=2,
        long_summary_token_budget=30,
        medium_summary_token_budget=30,
    )
    messages = [
        {
            "id": f"message_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"round {index} " + ("x" * 90),
        }
        for index in range(12)
    ]

    context = build_conversation_context(messages, settings=settings)

    assert context["metadata"]["token_budget"] == 220
    assert context["metadata"]["compaction_trigger_ratio"] == 0.5
    assert context["metadata"]["recent_round_limit"] == 2
    assert context["metadata"]["compacted"] is True
    assert context["messages"][-1]["content"] == messages[-1]["content"]
    assert context["metadata"]["estimated_tokens"] <= 220


def test_explicit_legacy_budget_overrides_tenant_settings() -> None:
    """验证内部回放显式预算仍可收紧租户配置，保持既有调用兼容性。"""

    context = build_conversation_context(
        [{"role": "user", "content": "简短问题"}],
        token_budget=120,
        settings=ConversationContextSettings(token_budget=8_000),
    )

    assert context["metadata"]["token_budget"] == 120


def test_ui_config_api_rejects_unsafe_context_bounds() -> None:
    """验证 HTTP 配置契约拒绝低于安全下限或高于平台上限的上下文参数。"""

    base = {
        "tenant_id": "tenant_demo",
        "context_token_budget": 4_096,
        "context_compaction_trigger_ratio": 0.45,
        "context_recent_round_limit": 1,
    }
    assert UIConfigUpdateRequest(**base).context_token_budget == 4_096

    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(**{**base, "context_token_budget": 4_095})
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(**{**base, "context_compaction_trigger_ratio": 0.4})
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(**{**base, "context_recent_round_limit": 21})


def test_legacy_ui_config_update_does_not_reset_context_settings() -> None:
    """旧客户端省略新增上下文字段时，Pydantic 请求不应把数据库配置降回默认值。"""

    request = UIConfigUpdateRequest(tenant_id="tenant_demo")

    assert request.context_token_budget is None
    assert request.context_compaction_trigger_ratio is None
    assert request.context_recent_round_limit is None
