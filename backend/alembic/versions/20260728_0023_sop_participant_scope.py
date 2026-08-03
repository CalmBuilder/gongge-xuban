"""
@Time       : 2026/07/28 18:30
@Author     : zhanglp8181
@File       : 20260728_0023_sop_participant_scope.py
@CallChain  : Alembic upgrade/downgrade → M3-B 工作项候选范围快照
@Description: 为新结构化工作项保存组织参与范围快照，旧工作项以空对象保持兼容。

Revision ID: 20260728_0023
Revises: 20260728_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0023"
down_revision: str | None = "20260728_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加工作项级参与范围快照，不改写已有候选和状态。"""

    op.add_column(
        "sop_work_items",
        sa.Column(
            "participant_scope_snapshot_json",
            sa.JSON(),
            nullable=True,
        ),
    )
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            sa.text(
                "UPDATE sop_work_items "
                "SET participant_scope_snapshot_json = JSON_OBJECT() "
                "WHERE participant_scope_snapshot_json IS NULL"
            )
        )
        op.alter_column(
            "sop_work_items",
            "participant_scope_snapshot_json",
            existing_type=sa.JSON(),
            nullable=False,
        )
    else:
        op.execute(
            sa.text(
                "UPDATE sop_work_items "
                "SET participant_scope_snapshot_json = '{}' "
                "WHERE participant_scope_snapshot_json IS NULL"
            )
        )
        with op.batch_alter_table("sop_work_items") as batch_op:
            batch_op.alter_column(
                "participant_scope_snapshot_json",
                existing_type=sa.JSON(),
                nullable=False,
            )


def downgrade() -> None:
    """移除参与范围快照，保留工作项和候选历史。"""

    op.drop_column("sop_work_items", "participant_scope_snapshot_json")
