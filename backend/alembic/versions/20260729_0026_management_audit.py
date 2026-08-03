"""
@Time       : 2026/07/29 01:45
@Author     : zhanglp8181
@File       : 20260729_0026_management_audit.py
@CallChain  : Alembic upgrade/downgrade → M5-C 独立管理审计
@Description: 创建跨 SQLite/MySQL 的只追加脱敏管理审计表及常用查询索引。

Revision ID: 20260729_0026
Revises: 20260728_0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260729_0026"
down_revision: str | None = "20260728_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建独立管理审计表及租户、actor、动作、资源和组织范围索引。"""

    op.create_table(
        "management_audit_logs",
        sa.Column("id", sa.String(length=512), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=True),
        sa.Column("actor_type", sa.String(length=64), nullable=False),
        sa.Column("actor_display_name", sa.String(length=191), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("action_kind", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("target_org_unit_id", sa.String(length=128), nullable=True),
        sa.Column("permission_code", sa.String(length=128), nullable=True),
        sa.Column("permission_source", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "tenant_id",
        "actor_user_id",
        "actor_type",
        "action",
        "action_kind",
        "outcome",
        "resource_type",
        "resource_id",
        "target_org_unit_id",
        "permission_code",
        "request_id",
        "correlation_id",
        "created_at",
    ):
        op.create_index(
            f"ix_management_audit_logs_{column}",
            "management_audit_logs",
            [column],
        )
    for index_name, columns in (
        ("ix_management_audit_tenant_created", ["tenant_id", "created_at"]),
        (
            "ix_management_audit_tenant_actor_created",
            ["tenant_id", "actor_user_id", "created_at"],
        ),
        (
            "ix_management_audit_tenant_action_created",
            ["tenant_id", "action", "created_at"],
        ),
        (
            "ix_management_audit_tenant_resource_created",
            ["tenant_id", "resource_type", "resource_id", "created_at"],
        ),
        (
            "ix_management_audit_tenant_org_created",
            ["tenant_id", "target_org_unit_id", "created_at"],
        ),
    ):
        op.create_index(index_name, "management_audit_logs", columns)


def downgrade() -> None:
    """删除 M5-C 独立管理审计表。"""

    op.drop_table("management_audit_logs")
