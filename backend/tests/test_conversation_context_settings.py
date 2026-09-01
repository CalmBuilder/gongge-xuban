"""
@Time       : 2026/08/28 15:05
@Author     : zhanglp8181
@File       : test_conversation_context_settings.py
@CallChain  : UIConfig/ConversationContextSettings → build_conversation_context → AgentLoop 上下文元数据
@Description: 验证租户上下文配置的边界、覆盖优先级和压缩行为。
"""

from collections.abc import Iterator
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app.api.ui_config import (
    UIConfigUpdateRequest,
    get_or_create_ui_config,
    update_enterprise_ui_config,
)
from app.core.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)
from app.db.models import Tenant, UIConfig, User


@pytest.fixture
def ui_config_admin_session() -> Iterator[tuple[Session, User]]:
    """建立只包含 UIConfig 更新所需表的 SQLite 管理员测试会话。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(
        engine,
        tables=[Tenant.__table__, User.__table__, UIConfig.__table__],
    )
    db = Session(engine)
    admin = User(
        id="user_context_settings_admin",
        tenant_id="tenant_context_settings",
        username="context-settings-admin",
        role="admin",
        password_hash="test-password-hash",
    )
    db.add(Tenant(id=admin.tenant_id, name="上下文配置测试租户"))
    db.add(admin)
    db.commit()
    try:
        yield db, admin
    finally:
        db.close()
        engine.dispose()


def test_ui_config_values_are_clamped_before_runtime_use() -> None:
    """验证管理端配置越界时会收敛到安全范围，而不会扩大模型输入预算。"""

    settings = ConversationContextSettings.from_ui_config(
        SimpleNamespace(
            context_token_budget=999_999,
            context_compaction_trigger_ratio=0.01,
            context_recent_round_limit=999,
        )
    )

    assert settings.token_budget == 262_144
    assert settings.compaction_trigger_ratio == 0.10
    assert settings.recent_round_limit == 50
    assert settings.long_summary_token_budget == 4_000


def test_context_settings_accept_the_new_default_and_management_ceiling() -> None:
    """验证 128K 产品默认值、262K 配置上限和新的正向边界。"""

    default = ConversationContextSettings().normalized()
    maximum = ConversationContextSettings(
        token_budget=262_144,
        compaction_trigger_ratio=0.10,
        recent_round_limit=50,
        long_summary_token_budget=32_768,
        medium_summary_token_budget=32_768,
    ).normalized()

    assert default.token_budget == 128_000
    assert maximum.token_budget == 262_144
    assert maximum.compaction_trigger_ratio == 0.10
    assert maximum.recent_round_limit == 50
    assert maximum.long_summary_token_budget == 32_768
    assert maximum.medium_summary_token_budget == 32_768
    assert (
        maximum.long_summary_token_budget + maximum.medium_summary_token_budget
        <= maximum.token_budget
    )


def test_context_settings_rebalance_summary_budgets_in_a_fixed_order() -> None:
    """摘要预算超出总预算时先缩减近期摘要，再缩减长期摘要，结果必须确定。"""

    normalized = ConversationContextSettings(
        token_budget=300,
        long_summary_token_budget=300,
        medium_summary_token_budget=300,
    ).normalized()

    assert normalized.medium_summary_token_budget == 128
    assert normalized.long_summary_token_budget == 172
    assert (
        normalized.long_summary_token_budget + normalized.medium_summary_token_budget
        <= normalized.token_budget
    )


def test_context_metadata_restoration_uses_the_same_normalization_contract() -> None:
    """失效或过期的持久化 metadata 也必须得到与设置对象一致的确定性归一化。"""

    restored = ConversationContextSettings.from_metadata(
        {
            "token_budget": 300,
            "compaction_trigger_ratio": 0.01,
            "recent_round_limit": 999,
            "long_summary_token_budget": 300,
            "medium_summary_token_budget": 300,
        }
    )

    assert restored.token_budget == 300
    assert restored.compaction_trigger_ratio == 0.10
    assert restored.recent_round_limit == 50
    assert restored.medium_summary_token_budget == 128
    assert restored.long_summary_token_budget == 172


def test_context_settings_reject_unsafe_new_api_bounds() -> None:
    """反向验证摘要预算、比例、轮数和上下文上限的边界拒绝。"""

    base = {
        "tenant_id": "tenant_demo",
        "context_token_budget": 262_144,
        "context_compaction_trigger_ratio": 0.10,
        "context_recent_round_limit": 50,
        "long_summary_token_budget": 32_768,
        "medium_summary_token_budget": 32_768,
    }
    valid = UIConfigUpdateRequest(**base)
    assert valid.context_token_budget == 262_144
    assert valid.context_compaction_trigger_ratio == 0.10
    assert valid.context_recent_round_limit == 50
    assert valid.long_summary_token_budget == 32_768
    assert valid.medium_summary_token_budget == 32_768

    invalid_cases = (
        {"context_token_budget": 262_145},
        {"context_compaction_trigger_ratio": 0.09},
        {"context_recent_round_limit": 51},
        {"long_summary_token_budget": 127},
        {"medium_summary_token_budget": 127},
        {
            "context_token_budget": 4_096,
            "long_summary_token_budget": 3_000,
            "medium_summary_token_budget": 2_000,
        },
    )
    for override in invalid_cases:
        with pytest.raises(ValidationError):
            UIConfigUpdateRequest(**{**base, **override})


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
        "context_compaction_trigger_ratio": 0.10,
        "context_recent_round_limit": 1,
    }
    assert UIConfigUpdateRequest(**base).context_token_budget == 4_096

    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(**{**base, "context_token_budget": 4_095})
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(**{**base, "context_compaction_trigger_ratio": 0.09})
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(**{**base, "context_recent_round_limit": 51})


def test_legacy_ui_config_update_does_not_reset_context_settings() -> None:
    """旧客户端省略新增上下文字段时，Pydantic 请求不应把数据库配置降回默认值。"""

    request = UIConfigUpdateRequest(tenant_id="tenant_demo")

    assert request.context_token_budget is None
    assert request.context_compaction_trigger_ratio is None
    assert request.context_recent_round_limit is None
    assert request.long_summary_token_budget is None
    assert request.medium_summary_token_budget is None


def test_ui_config_partial_update_preserves_a_valid_summary_budget_contract(
    ui_config_admin_session: tuple[Session, User],
) -> None:
    """正向验证局部更新会合并数据库旧值，并保存不超过上下文总预算的结果。"""

    db, admin = ui_config_admin_session
    tenant_id = admin.tenant_id
    get_or_create_ui_config(db, tenant_id)

    updated = update_enterprise_ui_config(
        UIConfigUpdateRequest(
            tenant_id=tenant_id,
            context_token_budget=8_192,
            long_summary_token_budget=4_096,
        ),
        db,
        admin,
    )

    assert updated.context_token_budget == 8_192
    assert updated.long_summary_token_budget == 4_096
    assert updated.medium_summary_token_budget == 4_000
    assert (
        updated.long_summary_token_budget + updated.medium_summary_token_budget
        <= updated.context_token_budget
    )

    update_enterprise_ui_config(
        UIConfigUpdateRequest(
            tenant_id=tenant_id,
            context_token_budget=4_096,
            long_summary_token_budget=128,
            medium_summary_token_budget=3_968,
        ),
        db,
        admin,
    )
    partial = update_enterprise_ui_config(
        UIConfigUpdateRequest(
            tenant_id=tenant_id,
            context_token_budget=4_096,
            long_summary_token_budget=128,
        ),
        db,
        admin,
    )

    assert partial.context_token_budget == 4_096
    assert partial.long_summary_token_budget == 128
    assert partial.medium_summary_token_budget == 3_968


@pytest.mark.parametrize(
    "invalid_update",
    (
        {"context_token_budget": 4_096},
        {"long_summary_token_budget": 8_192},
    ),
)
def test_ui_config_partial_update_rejects_an_invalid_effective_budget(
    ui_config_admin_session: tuple[Session, User],
    invalid_update: dict[str, int],
) -> None:
    """反向验证只提交一个字段也不能绕过最终上下文与摘要预算的总量约束。"""

    db, admin = ui_config_admin_session
    tenant_id = admin.tenant_id
    baseline = update_enterprise_ui_config(
        UIConfigUpdateRequest(
            tenant_id=tenant_id,
            context_token_budget=8_192,
            long_summary_token_budget=4_096,
            medium_summary_token_budget=4_096,
        ),
        db,
        admin,
    )

    with pytest.raises(HTTPException) as error:
        update_enterprise_ui_config(
            UIConfigUpdateRequest(tenant_id=tenant_id, **invalid_update),
            db,
            admin,
        )

    assert error.value.status_code == 422
    stored = db.get(UIConfig, tenant_id)
    assert stored is not None
    assert stored.context_token_budget == baseline.context_token_budget == 8_192
    assert stored.long_summary_token_budget == baseline.long_summary_token_budget == 4_096
    assert stored.medium_summary_token_budget == baseline.medium_summary_token_budget == 4_096
