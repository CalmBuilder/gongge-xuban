"""
@Time       : 2026/07/28 17:25
@Author     : zhanglp8181
@File       : 20260728_0021_organization_query_indexes.py
@CallChain  : Alembic upgrade/downgrade → 大组织按层、分页和当前任期查询索引
@Description: 为组织直接子级、成员归属、岗位任职和负责人上下文查询增加双方言组合索引。

Revision ID: 20260728_0021
Revises: 20260728_0020
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260728_0021"
down_revision: str | None = "20260728_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = (
    (
        "ix_org_unit_tenant_parent_status_sort",
        "organization_units",
        ["tenant_id", "parent_id", "status", "sort_order"],
    ),
    (
        "ix_member_org_tenant_org_current",
        "member_org_assignments",
        [
            "tenant_id",
            "org_unit_id",
            "status",
            "effective_until",
            "employee_profile_id",
        ],
    ),
    (
        "ix_member_org_tenant_member_current",
        "member_org_assignments",
        ["tenant_id", "employee_profile_id", "status", "effective_until"],
    ),
    (
        "ix_position_tenant_org_status_code",
        "positions",
        ["tenant_id", "org_unit_id", "status", "code"],
    ),
    (
        "ix_pos_assign_tenant_position_current",
        "position_assignments",
        [
            "tenant_id",
            "position_id",
            "status",
            "effective_until",
            "employee_profile_id",
        ],
    ),
    (
        "ix_pos_assign_tenant_member_current",
        "position_assignments",
        ["tenant_id", "employee_profile_id", "status", "effective_until"],
    ),
    (
        "ix_org_leader_tenant_org_current",
        "organization_leader_assignments",
        ["tenant_id", "org_unit_id", "status", "effective_until"],
    ),
    (
        "ix_org_leader_tenant_member_current",
        "organization_leader_assignments",
        ["tenant_id", "employee_profile_id", "status", "effective_until"],
    ),
)


def upgrade() -> None:
    """创建 M2.5-B 常用上下文查询的组合索引。"""

    for index_name, table_name, columns in INDEXES:
        op.create_index(index_name, table_name, columns, unique=False)


def downgrade() -> None:
    """按逆序移除 M2.5-B 组合索引，不改写任何组织历史。"""

    for index_name, table_name, _columns in reversed(INDEXES):
        op.drop_index(index_name, table_name=table_name)
