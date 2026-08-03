"""
@Time       : 2026/07/28 17:20
@Author     : zhanglp8181
@File       : 20260728_0019_position_role_bindings.py
@CallChain  : Alembic upgrade/downgrade → 岗位默认业务角色绑定
@Description: 创建岗位默认角色关系，为统一角色解析和任务候选来源提供事实。

Revision ID: 20260728_0019
Revises: 20260728_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0019"
down_revision: str | None = "20260728_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建岗位与业务角色的可停用默认绑定。"""

    op.create_table(
        "position_role_bindings",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("business_role_id", sa.String(128), nullable=False),
        sa.Column("scope_mode", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "position_id",
            "business_role_id",
            name="uq_position_role_binding",
        ),
    )
    for column_name in (
        "tenant_id",
        "position_id",
        "business_role_id",
        "scope_mode",
        "status",
    ):
        op.create_index(
            f"ix_position_role_bindings_{column_name}",
            "position_role_bindings",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    """移除岗位默认角色绑定，不修改岗位、角色或历史任职。"""

    op.drop_table("position_role_bindings")
