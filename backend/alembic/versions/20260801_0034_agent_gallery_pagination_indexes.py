"""
@Time       : 2026/08/01 23:05
@Author     : zhanglp8181
@File       : 20260801_0034_agent_gallery_pagination_indexes.py
@CallChain  : Alembic upgrade → agent_profiles → 数字员工广场关系视图分页
@Description: 为拥有、广场发布和专家分类分页增加状态与更新时间组合索引。

Revision ID: 20260801_0034
Revises: 20260801_0033
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260801_0034"
down_revision: str | None = "20260801_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    (
        "ix_agent_profiles_tenant_owner_status_updated",
        ["tenant_id", "owner_user_id", "status", "updated_at"],
    ),
    (
        "ix_agent_profiles_tenant_gallery_status_category_updated",
        ["tenant_id", "published_to_gallery", "status", "agent_category_code", "updated_at"],
    ),
    (
        "ix_agent_profiles_tenant_category_status_updated",
        ["tenant_id", "agent_category_code", "status", "updated_at"],
    ),
)


def upgrade() -> None:
    """创建三个数字员工关系视图分页组合索引。"""

    for name, columns in _INDEXES:
        op.create_index(name, "agent_profiles", columns, unique=False)


def downgrade() -> None:
    """移除广场分页组合索引，不修改数字员工或使用关系。"""

    for name, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name="agent_profiles")
