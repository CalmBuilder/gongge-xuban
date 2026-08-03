"""
@Time       : 2026/07/22 16:20
@Author     : zhanglp8181
@File       : 20260722_0005_employee_profiles.py
@CallChain  : Alembic upgrade/downgrade → 员工档案 → SOP 身份上下文
@Description: 创建账号与业务员工身份的一对一映射表，支持本人办理和授权代办。

Revision ID: 20260722_0005
Revises: 20260722_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0005"
down_revision: str | None = "20260722_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建租户隔离且账号、工号双唯一的员工档案表。"""

    op.create_table(
        "employee_profiles",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("employee_id", sa.String(128), nullable=False),
        sa.Column("employee_name", sa.String(191), nullable=True),
        sa.Column("department_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", name="uq_employee_profile_tenant_user"
        ),
        sa.UniqueConstraint(
            "tenant_id", "employee_id", name="uq_employee_profile_tenant_employee"
        ),
    )
    for column_name in (
        "tenant_id",
        "user_id",
        "employee_id",
        "department_id",
        "status",
    ):
        op.create_index(
            f"ix_employee_profiles_{column_name}",
            "employee_profiles",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    """移除员工档案表。"""

    op.drop_table("employee_profiles")
