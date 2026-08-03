"""
@Time       : 2026/07/22 23:05
@Author     : zhanglp8181
@File       : 20260722_0013_tool_authorization_mode.py
@CallChain  : Alembic upgrade/downgrade → tools.permission_authorization_mode → AgentExecutionAuthorizer
@Description: 区分调用人代理与已发布 SOP 流程委托两种工具授权来源。

Revision ID: 20260722_0013
Revises: 20260722_0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0013"
down_revision: str | None = "20260722_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加非空授权来源字段，旧受保护工具继续使用调用人权限上限。"""

    op.add_column(
        "tools",
        sa.Column(
            "permission_authorization_mode",
            sa.String(length=64),
            nullable=False,
            server_default="caller_and_agent",
        ),
    )
    op.create_index(
        "ix_tools_permission_authorization_mode",
        "tools",
        ["permission_authorization_mode"],
        unique=False,
    )


def downgrade() -> None:
    """移除授权来源字段，保留工具引用的权限编码。"""

    op.drop_index("ix_tools_permission_authorization_mode", table_name="tools")
    op.drop_column("tools", "permission_authorization_mode")
