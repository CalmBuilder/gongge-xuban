"""
@Time       : 2026/07/22 10:25
@Author     : zhanglp8181
@File       : 20260722_0008_work_item_timeout.py
@CallChain  : Alembic upgrade/downgrade → 工作项超时扫描 → SOP 超时终态
@Description: 为人工工作项保存发布版本冻结的超时动作和实际过期时间。

Revision ID: 20260722_0008
Revises: 20260722_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0008"
down_revision: str | None = "20260722_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加超时动作和过期事实，并为后台到期扫描建立索引。"""

    op.add_column(
        "sop_work_items",
        sa.Column("timeout_action", sa.String(64), nullable=False, server_default="fail"),
    )
    op.add_column(
        "sop_work_items",
        sa.Column("expired_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_sop_work_items_timeout_action",
        "sop_work_items",
        ["timeout_action"],
        unique=False,
    )


def downgrade() -> None:
    """移除工作项超时动作和过期事实字段。"""

    op.drop_index("ix_sop_work_items_timeout_action", table_name="sop_work_items")
    op.drop_column("sop_work_items", "expired_at")
    op.drop_column("sop_work_items", "timeout_action")
