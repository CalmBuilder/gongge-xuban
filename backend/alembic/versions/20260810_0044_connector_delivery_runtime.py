"""
@Time       : 2026/08/10 22:20
@Author     : zhanglp8181
@File       : 20260810_0044_connector_delivery_runtime.py
@CallChain  : Alembic upgrade/downgrade → Connector 入站身份、路由、线程与出站 outbox
@Description: 建立企业 Connector 从持久 inbox 到平台会话及外部回发的生产级运行事实。

Revision ID: 20260810_0044
Revises: 20260810_0043
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0044"
down_revision: str | None = "20260810_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建入站授权/路由/线程/outbox，并为 inbox 增加租约与处理关联。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("connector_inbound_events"):
        raise RuntimeError("connector delivery runtime requires connector inbox baseline")
    inbound_columns = {column["name"] for column in inspector.get_columns("connector_inbound_events")}
    additions = (
        ("lease_owner", sa.String(128)),
        ("lease_until", sa.DateTime()),
        ("thread_binding_id", sa.String(128)),
        ("session_id", sa.String(128)),
        ("message_id", sa.String(128)),
        ("execution_id", sa.String(128)),
    )
    for name, type_ in additions:
        if name not in inbound_columns:
            op.add_column("connector_inbound_events", sa.Column(name, type_, nullable=True))
            op.create_index(f"ix_connector_inbound_events_{name}", "connector_inbound_events", [name])
    _create_principal_bindings(inspector)
    _create_inbound_routes(inspector)
    _create_thread_bindings(inspector)
    _create_outbound_deliveries(inspector)


def _create_principal_bindings(inspector: sa.Inspector) -> None:
    """创建外部发送者摘要到平台用户的显式授权绑定。"""

    if inspector.has_table("connector_principal_bindings"):
        return
    op.create_table(
        "connector_principal_bindings",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("sender_ref_hash", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.String(128), nullable=False),
        sa.Column("updated_by_user_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "profile_id", "sender_ref_hash",
            name="uq_connector_principal_sender",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_connector_principal_revision"),
    )
    _indexes("connector_principal_bindings", (
        "tenant_id", "provider", "profile_id", "sender_ref_hash", "user_id", "enabled"
    ))


def _create_inbound_routes(inspector: sa.Inspector) -> None:
    """创建每个连接档案唯一的入站 Agent 路由。"""

    if inspector.has_table("connector_inbound_routes"):
        return
    op.create_table(
        "connector_inbound_routes",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.String(128), nullable=False),
        sa.Column("updated_by_user_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "profile_id", name="uq_connector_inbound_route_profile"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_connector_inbound_route_revision"),
    )
    _indexes("connector_inbound_routes", (
        "tenant_id", "provider", "profile_id", "agent_id", "enabled"
    ))


def _create_thread_bindings(inspector: sa.Inspector) -> None:
    """创建外部线程与平台 ChatSession 的持久关联。"""

    if inspector.has_table("connector_thread_bindings"):
        return
    op.create_table(
        "connector_thread_bindings",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("sender_ref_hash", sa.String(128), nullable=False),
        sa.Column("encrypted_recipient_ref", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False, server_default="active"),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "profile_id", "sender_ref_hash", "agent_id",
            name="uq_connector_thread_sender_agent",
        ),
        sa.UniqueConstraint("tenant_id", "session_id", name="uq_connector_thread_session"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_connector_thread_status"
        ),
    )
    _indexes("connector_thread_bindings", (
        "tenant_id", "provider", "profile_id", "sender_ref_hash", "user_id", "agent_id",
        "session_id", "status", "lease_owner", "lease_until"
    ))


def _create_outbound_deliveries(inspector: sa.Inspector) -> None:
    """创建可恢复的外部回发 outbox。"""

    if inspector.has_table("connector_outbound_deliveries"):
        return
    op.create_table(
        "connector_outbound_deliveries",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("thread_binding_id", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("payload_checksum", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "source_type", "source_ref", name="uq_connector_outbound_source"
        ),
        sa.CheckConstraint(
            "source_type IN ('assistant_message', 'execution_publication')",
            name="ck_connector_outbound_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'settled', 'unknown', 'dead_letter')",
            name="ck_connector_outbound_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_connector_outbound_attempts"),
    )
    _indexes("connector_outbound_deliveries", (
        "tenant_id", "provider", "profile_id", "thread_binding_id", "source_type", "source_ref",
        "payload_checksum", "status", "available_at", "lease_owner", "lease_until"
    ))
    op.create_index(
        "ix_connector_outbound_dispatch",
        "connector_outbound_deliveries",
        ["status", "available_at", "created_at"],
    )


def _indexes(table: str, columns: Sequence[str]) -> None:
    """按 SQLModel 默认命名为运行时常用列创建普通索引。"""

    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    """仅在没有投递事实时移除本批表和 inbox 关联列。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "connector_outbound_deliveries",
        "connector_thread_bindings",
        "connector_inbound_routes",
        "connector_principal_bindings",
    ):
        if inspector.has_table(table):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}" )).scalar_one()
            if count:
                raise RuntimeError(f"cannot downgrade connector runtime with persisted {table}")
            op.drop_table(table)
    inbound_columns = {column["name"] for column in inspector.get_columns("connector_inbound_events")}
    for name in (
        "execution_id", "message_id", "session_id", "thread_binding_id", "lease_until", "lease_owner"
    ):
        if name in inbound_columns:
            op.drop_index(f"ix_connector_inbound_events_{name}", table_name="connector_inbound_events")
            op.drop_column("connector_inbound_events", name)
