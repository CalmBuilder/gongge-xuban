"""
@Time       : 2026/08/02 16:35
@Author     : zhanglp8181
@File       : 20260802_0035_role_binding_effective_intervals.py
@CallChain  : Alembic upgrade → 岗位/数字员工角色绑定 → 统一时点与组织作用域解析
@Description: 为两类角色绑定补齐有效期、授予人和数字员工组织子树契约，并回填历史起点。

Revision ID: 20260802_0035
Revises: 20260801_0034
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260802_0035"
down_revision: str | None = "20260801_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加绑定治理字段、回填历史生效时间并创建当前解析组合索引。"""

    datetime_type = mysql.DATETIME(fsp=6) if op.get_bind().dialect.name == "mysql" else sa.DateTime()
    op.add_column(
        "position_role_bindings",
        sa.Column("granted_by_user_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "position_role_bindings",
        sa.Column("effective_from", datetime_type, nullable=True),
    )
    op.add_column(
        "position_role_bindings",
        sa.Column("effective_until", datetime_type, nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE position_role_bindings SET effective_from = created_at "
            "WHERE effective_from IS NULL"
        )
    )
    op.add_column(
        "agent_role_bindings",
        sa.Column(
            "include_descendants",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "agent_role_bindings",
        sa.Column("granted_by_user_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "agent_role_bindings",
        sa.Column("effective_from", datetime_type, nullable=True),
    )
    op.add_column(
        "agent_role_bindings",
        sa.Column("effective_until", datetime_type, nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_role_bindings SET effective_from = created_at "
            "WHERE effective_from IS NULL"
        )
    )
    op.create_index(
        "ix_position_role_effective_resolution",
        "position_role_bindings",
        ["tenant_id", "position_id", "status", "effective_until", "business_role_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_role_effective_resolution",
        "agent_role_bindings",
        ["tenant_id", "agent_id", "status", "effective_until", "business_role_id"],
        unique=False,
    )


def downgrade() -> None:
    """移除新增治理字段和解析索引，保留原有角色绑定关系。"""

    op.drop_index("ix_agent_role_effective_resolution", table_name="agent_role_bindings")
    op.drop_index(
        "ix_position_role_effective_resolution",
        table_name="position_role_bindings",
    )
    op.drop_column("agent_role_bindings", "effective_until")
    op.drop_column("agent_role_bindings", "effective_from")
    op.drop_column("agent_role_bindings", "granted_by_user_id")
    op.drop_column("agent_role_bindings", "include_descendants")
    op.drop_column("position_role_bindings", "effective_until")
    op.drop_column("position_role_bindings", "effective_from")
    op.drop_column("position_role_bindings", "granted_by_user_id")
