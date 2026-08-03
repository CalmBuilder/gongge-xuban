"""
@Time       : 2026/07/22 18:40
@Author     : zhanglp8181
@File       : 20260722_0006_business_roles.py
@CallChain  : Alembic upgrade/downgrade → 公司业务角色/任职/数字员工映射 → SOP 授权
@Description: 创建独立于平台管理员角色的公司业务角色及员工、数字员工映射表。

Revision ID: 20260722_0006
Revises: 20260722_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0006"
down_revision: str | None = "20260722_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建业务角色、员工任职和数字员工角色映射表。"""

    op.create_table(
        "business_roles",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("role_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("permissions_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "role_code", name="uq_business_role_tenant_code"),
    )
    op.create_table(
        "employee_role_assignments",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("employee_profile_id", sa.String(128), nullable=False),
        sa.Column("business_role_id", sa.String(128), nullable=False),
        sa.Column("scope_type", sa.String(64), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_until", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "employee_profile_id",
            "business_role_id",
            "scope_type",
            "scope_id",
            name="uq_employee_role_assignment_scope",
        ),
    )
    op.create_table(
        "agent_role_bindings",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("business_role_id", sa.String(128), nullable=False),
        sa.Column("assignment_mode", sa.String(64), nullable=False),
        sa.Column("supervisor_employee_profile_id", sa.String(128), nullable=True),
        sa.Column("scope_type", sa.String(64), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "business_role_id",
            "scope_type",
            "scope_id",
            name="uq_agent_role_binding_scope",
        ),
    )
    _create_indexes()


def downgrade() -> None:
    """按依赖逆序移除数字员工映射、员工任职和业务角色表。"""

    op.drop_table("agent_role_bindings")
    op.drop_table("employee_role_assignments")
    op.drop_table("business_roles")


def _create_indexes() -> None:
    """为租户过滤、动态角色解析和有效期校验创建索引。"""

    index_columns = {
        "business_roles": ("tenant_id", "role_code", "category", "status"),
        "employee_role_assignments": (
            "tenant_id",
            "employee_profile_id",
            "business_role_id",
            "scope_type",
            "scope_id",
            "status",
            "effective_from",
            "effective_until",
        ),
        "agent_role_bindings": (
            "tenant_id",
            "agent_id",
            "business_role_id",
            "assignment_mode",
            "supervisor_employee_profile_id",
            "scope_type",
            "scope_id",
            "status",
        ),
    }
    for table_name, columns in index_columns.items():
        for column_name in columns:
            op.create_index(
                f"ix_{table_name}_{column_name}", table_name, [column_name], unique=False
            )
