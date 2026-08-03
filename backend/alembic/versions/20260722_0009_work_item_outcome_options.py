"""
@Time       : 2026/07/22 13:10
@Author     : zhanglp8181
@File       : 20260722_0009_work_item_outcome_options.py
@CallChain  : Alembic upgrade/downgrade → 工作项结果快照 → 任务箱/异步通知
@Description: 为通用人工工作项保存版本化结果按钮、意见要求和完成通知契约。

Revision ID: 20260722_0009
Revises: 20260722_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0009"
down_revision: str | None = "20260722_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加可空 JSON 快照列，旧工作项继续按 allowed_outcomes 兼容展示。"""

    op.add_column(
        "sop_work_items",
        sa.Column("outcome_options_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """移除工作项结果展示与通知快照列。"""

    op.drop_column("sop_work_items", "outcome_options_json")
