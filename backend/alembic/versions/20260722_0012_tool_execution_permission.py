"""
@Time       : 2026/07/22 22:24
@Author     : zhanglp8181
@File       : 20260722_0012_tool_execution_permission.py
@CallChain  : Alembic upgrade/downgrade → tools.required_permission_code → ToolExecutor 受控授权
@Description: 为工具增加可选的统一业务权限边界引用。

Revision ID: 20260722_0012
Revises: 20260722_0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0012"
down_revision: str | None = "20260722_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加工具权限引用列和查询索引，旧工具默认不受新契约影响。"""

    op.add_column(
        "tools",
        sa.Column("required_permission_code", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_tools_required_permission_code",
        "tools",
        ["required_permission_code"],
        unique=False,
    )


def downgrade() -> None:
    """移除工具权限引用，不改动统一权限目录与角色映射。"""

    op.drop_index("ix_tools_required_permission_code", table_name="tools")
    op.drop_column("tools", "required_permission_code")
