"""
@Time       : 2026/08/10 14:35
@Author     : zhanglp8181
@File       : 20260810_0043_connector_inbound_events.py
@CallChain  : Alembic upgrade/downgrade → connector inbound inbox
@Description: 创建租户隔离、幂等且载荷加密的 Connector 入站事件收件箱。

Revision ID: 20260810_0043
Revises: 20260810_0042
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0043"
down_revision: str | None = "20260810_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建入站 inbox；已存在时严格验证列、约束和调度索引。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("connection_profiles"):
        raise RuntimeError("connector inbox requires connection_profiles baseline")
    profile_columns = {column["name"] for column in inspector.get_columns("connection_profiles")}
    if "callback_configured" not in profile_columns:
        op.add_column(
            "connection_profiles",
            sa.Column("callback_configured", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index(
            "ix_connection_profiles_callback_configured",
            "connection_profiles",
            ["callback_configured"],
        )
    elif "ix_connection_profiles_callback_configured" not in {
        item["name"] for item in inspector.get_indexes("connection_profiles")
    }:
        raise RuntimeError("connection_profiles missing callback configuration index")
    if not inspector.has_table("connector_inbound_events"):
        op.create_table(
            "connector_inbound_events",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("profile_id", sa.String(128), nullable=False),
            sa.Column("external_event_id", sa.String(255), nullable=False),
            sa.Column("payload_checksum", sa.String(128), nullable=False),
            sa.Column("encrypted_payload", sa.Text(), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("sender_ref_hash", sa.String(128), nullable=True),
            sa.Column("status", sa.String(64), nullable=False, server_default="pending"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("available_at", sa.DateTime(), nullable=False),
            sa.Column("last_error_code", sa.String(128), nullable=True),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "provider",
                "profile_id",
                "external_event_id",
                name="uq_connector_inbound_external_event",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'processing', 'processed', 'failed', 'dead_letter')",
                name="ck_connector_inbound_status",
            ),
            sa.CheckConstraint("attempt_count >= 0", name="ck_connector_inbound_attempts"),
        )
        for column in (
            "tenant_id",
            "provider",
            "profile_id",
            "external_event_id",
            "payload_checksum",
            "event_type",
            "sender_ref_hash",
            "status",
            "available_at",
        ):
            op.create_index(f"ix_connector_inbound_events_{column}", "connector_inbound_events", [column])
        op.create_index(
            "ix_connector_inbound_dispatch",
            "connector_inbound_events",
            ["status", "available_at", "created_at"],
        )
        return
    columns = {column["name"] for column in inspector.get_columns("connector_inbound_events")}
    required = {
        "id", "tenant_id", "provider", "profile_id", "external_event_id",
        "payload_checksum", "encrypted_payload", "event_type", "sender_ref_hash",
        "status", "attempt_count", "available_at", "last_error_code", "processed_at",
        "created_at", "updated_at",
    }
    if not required <= columns:
        raise RuntimeError("connector_inbound_events exists with incompatible columns")
    indexes = {index["name"] for index in inspector.get_indexes("connector_inbound_events")}
    if "ix_connector_inbound_dispatch" not in indexes:
        raise RuntimeError("connector_inbound_events missing dispatch index")
    unique_constraints = {
        item["name"] for item in inspector.get_unique_constraints("connector_inbound_events")
    }
    if "uq_connector_inbound_external_event" not in unique_constraints:
        raise RuntimeError("connector_inbound_events missing idempotency constraint")
    check_constraints = {
        item["name"] for item in inspector.get_check_constraints("connector_inbound_events")
    }
    if not {"ck_connector_inbound_status", "ck_connector_inbound_attempts"} <= check_constraints:
        raise RuntimeError("connector_inbound_events missing state constraints")


def downgrade() -> None:
    """仅在不存在已接收外部事实时删除本修订创建的入站事件表。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("connector_inbound_events"):
        count = bind.execute(sa.text("SELECT COUNT(*) FROM connector_inbound_events")).scalar_one()
        if count:
            raise RuntimeError("cannot downgrade connector inbox with persisted inbound events")
    op.drop_table("connector_inbound_events")
    op.drop_index("ix_connection_profiles_callback_configured", table_name="connection_profiles")
    op.drop_column("connection_profiles", "callback_configured")
