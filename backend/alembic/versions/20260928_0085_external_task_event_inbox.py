"""
@Time       : 2026/09/28 19:05
@Author     : zhanglp8181
@File       : 20260928_0085_external_task_event_inbox.py
@CallChain  : Alembic upgrade/downgrade → provider 回执 inbox → ExecutionSignal
@Description: 建立租户隔离、append-once、可租约消费的外部任务回执 inbox。

Revision ID: 20260928_0085
Revises: 20260928_0084
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260928_0085"
down_revision: str | None = "20260928_0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 provider 回执可信 inbox，保留早到、重复和乱序事实。"""

    inspector = sa.inspect(op.get_bind())
    if "sop_external_task_event_inbox" in inspector.get_table_names():
        return
    op.create_table(
        "sop_external_task_event_inbox",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("operation_id", sa.String(512), nullable=True),
        sa.Column("external_submission_id", sa.String(512), nullable=True),
        sa.Column("provider_task_id", sa.String(512), nullable=False),
        sa.Column("provider_event_id", sa.String(512), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("provider_status", sa.String(128), nullable=False),
        sa.Column("observation_revision", sa.Integer(), nullable=False),
        sa.Column("payload_checksum", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(512), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("signal_id", sa.String(512), nullable=True),
        sa.Column("last_error_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'applied', 'ignored', 'rejected')",
            name="ck_sop_external_event_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND observation_revision >= 0",
            name="ck_sop_external_event_bounds",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_event_id",
            name="uq_sop_external_event_provider_id",
        ),
    )
    for name, columns in (
        ("ix_sop_external_event_tenant", ("tenant_id",)),
        ("ix_sop_external_event_operation", ("operation_id",)),
        ("ix_sop_external_event_submission", ("external_submission_id",)),
        ("ix_sop_external_event_task", ("provider_task_id",)),
        ("ix_sop_external_event_event", ("provider_event_id",)),
        ("ix_sop_external_event_revision", ("observation_revision",)),
        ("ix_sop_external_event_status", ("status",)),
        ("ix_sop_external_event_available", ("available_at",)),
        ("ix_sop_external_event_lease_owner", ("lease_owner",)),
        ("ix_sop_external_event_lease_expires", ("lease_expires_at",)),
        ("ix_sop_external_event_signal", ("signal_id",)),
        ("ix_sop_external_event_received", ("received_at",)),
    ):
        op.create_index(name, "sop_external_task_event_inbox", list(columns))


def downgrade() -> None:
    """有回执事实时拒绝回退，避免丢失 provider 到 Operation 的审计链。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sop_external_task_event_inbox" not in inspector.get_table_names():
        return
    count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM sop_external_task_event_inbox")
        ).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade while external task event history exists")
    for name in (
        "ix_sop_external_event_tenant",
        "ix_sop_external_event_operation",
        "ix_sop_external_event_submission",
        "ix_sop_external_event_task",
        "ix_sop_external_event_event",
        "ix_sop_external_event_revision",
        "ix_sop_external_event_status",
        "ix_sop_external_event_available",
        "ix_sop_external_event_lease_owner",
        "ix_sop_external_event_lease_expires",
        "ix_sop_external_event_signal",
        "ix_sop_external_event_received",
    ):
        op.drop_index(name, table_name="sop_external_task_event_inbox")
    op.drop_table("sop_external_task_event_inbox")
