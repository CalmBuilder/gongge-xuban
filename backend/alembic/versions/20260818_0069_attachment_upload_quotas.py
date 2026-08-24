"""
@Time       : 2026/08/14 23:58
@Author     : zhanglp8181
@File       : 20260818_0069_attachment_upload_quotas.py
@CallChain  : Alembic upgrade/downgrade → 附件上传数据库配额
@Description: 建立上传body前跨进程并发slot、TTL reservation和tenant/user每日字节bucket。

Revision ID: 20260818_0069
Revises: 20260817_0068
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260818_0069"
down_revision: str | None = "20260817_0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建上传配额reservation、唯一并发slot和UTC日用量表。"""

    precise_time = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.create_table(
        "attachment_upload_quota_reservations",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("owner_user_id", sa.String(length=128), nullable=False),
        sa.Column("binding_id", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.String(length=128), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("expires_at", precise_time, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'released', 'expired')",
            name="ck_attachment_upload_reservation_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "binding_id", name="uq_attachment_upload_reservation_binding"
        ),
    )
    for column in ("tenant_id", "owner_user_id", "binding_id", "status", "expires_at"):
        op.create_index(
            op.f(f"ix_attachment_upload_quota_reservations_{column}"),
            "attachment_upload_quota_reservations",
            [column],
            unique=False,
        )

    op.create_table(
        "attachment_upload_quota_leases",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("reservation_id", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("scope_ref", sa.String(length=128), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("expires_at", precise_time, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('tenant', 'user')",
            name="ck_attachment_upload_quota_scope_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "reservation_id",
            "scope_type",
            name="uq_attachment_upload_quota_reservation_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_ref",
            "slot_number",
            name="uq_attachment_upload_quota_scope_slot",
        ),
    )
    for column in ("tenant_id", "reservation_id", "scope_type", "scope_ref", "expires_at"):
        op.create_index(
            op.f(f"ix_attachment_upload_quota_leases_{column}"),
            "attachment_upload_quota_leases",
            [column],
            unique=False,
        )

    op.create_table(
        "attachment_upload_daily_usage",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("scope_type", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("scope_ref", sa.String(length=128), nullable=False),
        sa.Column("day_key", sa.String(length=10), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("consumed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('tenant', 'user')",
            name="ck_attachment_upload_daily_scope_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_ref",
            "day_key",
            name="uq_attachment_upload_daily_scope",
        ),
    )
    for column in ("tenant_id", "scope_type", "scope_ref", "day_key"):
        op.create_index(
            op.f(f"ix_attachment_upload_daily_usage_{column}"),
            "attachment_upload_daily_usage",
            [column],
            unique=False,
        )

    op.create_table(
        "attachment_upload_cleanup_jobs",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("owner_user_id", sa.String(length=128), nullable=False),
        sa.Column("upload_binding_id", sa.String(length=128), nullable=False),
        sa.Column("resource_manifest_json", sa.JSON(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", precise_time, nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'purging', 'succeeded', 'failed')",
            name="ck_attachment_upload_cleanup_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "upload_binding_id",
            name="uq_attachment_upload_cleanup_binding",
        ),
    )
    for column in ("tenant_id", "owner_user_id", "upload_binding_id", "status"):
        op.create_index(
            op.f(f"ix_attachment_upload_cleanup_jobs_{column}"),
            "attachment_upload_cleanup_jobs",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """按引用反序删除上传配额表。"""

    op.drop_table("attachment_upload_cleanup_jobs")
    op.drop_table("attachment_upload_daily_usage")
    op.drop_table("attachment_upload_quota_leases")
    op.drop_table("attachment_upload_quota_reservations")
