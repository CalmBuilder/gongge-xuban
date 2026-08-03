"""
@Time       : 2026/07/28 19:10
@Author     : zhanglp8181
@File       : 20260728_0022_governance_role_scopes.py
@CallChain  : Alembic upgrade/downgrade → M3-A 治理角色与结构化授权
@Description: 区分业务/治理角色，并为员工角色授权补充组织后代和授予人事实。

Revision ID: 20260728_0022
Revises: 20260728_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0022"
down_revision: str | None = "20260728_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """补充治理角色类型和可校验的组织范围授权字段，并回填既有业务语义。"""

    op.add_column(
        "business_roles",
        sa.Column("role_kind", sa.String(length=64), nullable=False, server_default="business"),
    )
    op.create_index("ix_business_roles_role_kind", "business_roles", ["role_kind"], unique=False)
    op.add_column(
        "employee_role_assignments",
        sa.Column(
            "include_descendants",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "employee_role_assignments",
        sa.Column("granted_by_user_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_employee_role_assignments_include_descendants",
        "employee_role_assignments",
        ["include_descendants"],
        unique=False,
    )
    op.create_index(
        "ix_employee_role_assignments_granted_by_user_id",
        "employee_role_assignments",
        ["granted_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_employee_role_governance_resolution",
        "employee_role_assignments",
        [
            "tenant_id",
            "employee_profile_id",
            "status",
            "effective_until",
            "business_role_id",
        ],
        unique=False,
    )


def downgrade() -> None:
    """移除 M3-A 新字段，保留既有角色和任职关系数据。"""

    op.drop_index(
        "ix_employee_role_governance_resolution",
        table_name="employee_role_assignments",
    )
    op.drop_index(
        "ix_employee_role_assignments_granted_by_user_id",
        table_name="employee_role_assignments",
    )
    op.drop_index(
        "ix_employee_role_assignments_include_descendants",
        table_name="employee_role_assignments",
    )
    op.drop_column("employee_role_assignments", "granted_by_user_id")
    op.drop_column("employee_role_assignments", "include_descendants")
    op.drop_index("ix_business_roles_role_kind", table_name="business_roles")
    op.drop_column("business_roles", "role_kind")
