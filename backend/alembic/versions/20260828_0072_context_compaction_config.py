"""
@Time       : 2026/08/28 15:00
@Author     : zhanglp8181
@File       : 20260828_0072_context_compaction_config.py
@CallChain  : Alembic upgrade/downgrade → ui_configs → AgentLoop 上下文压缩配置
@Description: 为租户保存安全边界内的会话上下文预算、压缩阈值和近期轮数。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260828_0072"
down_revision: str | None = "20260820_0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """向租户 UI 配置表增加上下文压缩参数。"""

    bind = op.get_bind()
    additions = (
        sa.Column("context_token_budget", sa.Integer(), nullable=False, server_default="32000"),
        sa.Column(
            "context_compaction_trigger_ratio",
            sa.Float(),
            nullable=False,
            server_default="0.7",
        ),
        sa.Column("context_recent_round_limit", sa.Integer(), nullable=False, server_default="6"),
    )
    existing = _column_names(bind, "ui_configs")
    for column in additions:
        if column.name not in existing:
            op.add_column("ui_configs", column)
            existing.add(str(column.name))


def downgrade() -> None:
    """移除租户上下文压缩参数。"""

    bind = op.get_bind()
    existing = _column_names(bind, "ui_configs")
    for name in (
        "context_recent_round_limit",
        "context_compaction_trigger_ratio",
        "context_token_budget",
    ):
        if name in existing:
            op.drop_column("ui_configs", name)
            existing.remove(name)


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """读取当前列集合，使 MySQL 非事务 DDL 中断后可从已完成列继续升级。"""

    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        raise RuntimeError(f"context compaction migration requires table: {table_name}")
    return {str(column["name"]) for column in inspector.get_columns(table_name)}
