"""
@Time       : 2026/09/01 12:00
@Author     : zhanglp8181
@File       : 20260901_0078_context_summary_budgets.py
@CallChain  : Alembic upgrade/downgrade → ui_configs → ConversationContextSettings
@Description: 增加长期/近期摘要预算，并把未来上下文预算默认值调整为 128K。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260901_0078"
down_revision: str | None = "20260830_0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LONG_SUMMARY = "long_summary_token_budget"
_MEDIUM_SUMMARY = "medium_summary_token_budget"
_CONTEXT_BUDGET = "context_token_budget"


def upgrade() -> None:
    """增加摘要预算并只改变未来缺省值，不更新已有租户的明确预算。"""

    bind = op.get_bind()
    if not _has_ui_config_table(bind):
        return
    existing = _column_names(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("ui_configs", recreate="always") as batch:
            if _LONG_SUMMARY not in existing:
                batch.add_column(
                    sa.Column(
                        _LONG_SUMMARY,
                        sa.Integer(),
                        nullable=False,
                        server_default=sa.text("4000"),
                    )
                )
            if _MEDIUM_SUMMARY not in existing:
                batch.add_column(
                    sa.Column(
                        _MEDIUM_SUMMARY,
                        sa.Integer(),
                        nullable=False,
                        server_default=sa.text("4000"),
                    )
                )
            if _CONTEXT_BUDGET in existing:
                batch.alter_column(
                    _CONTEXT_BUDGET,
                    existing_type=sa.Integer(),
                    server_default=sa.text("128000"),
                )
        return

    if _LONG_SUMMARY not in existing:
        op.add_column(
            "ui_configs",
            sa.Column(
                _LONG_SUMMARY,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("4000"),
            ),
        )
    if _MEDIUM_SUMMARY not in existing:
        op.add_column(
            "ui_configs",
            sa.Column(
                _MEDIUM_SUMMARY,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("4000"),
            ),
        )
    if _CONTEXT_BUDGET in existing:
        op.alter_column(
            "ui_configs",
            _CONTEXT_BUDGET,
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("128000"),
        )


def downgrade() -> None:
    """删除摘要预算并恢复旧默认值，不反向改写已有行。"""

    bind = op.get_bind()
    if not _has_ui_config_table(bind):
        return
    existing = _column_names(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("ui_configs", recreate="always") as batch:
            if _MEDIUM_SUMMARY in existing:
                batch.drop_column(_MEDIUM_SUMMARY)
            if _LONG_SUMMARY in existing:
                batch.drop_column(_LONG_SUMMARY)
            if _CONTEXT_BUDGET in existing:
                batch.alter_column(
                    _CONTEXT_BUDGET,
                    existing_type=sa.Integer(),
                    server_default=sa.text("32000"),
                )
        return

    if _MEDIUM_SUMMARY in existing:
        op.drop_column("ui_configs", _MEDIUM_SUMMARY)
    if _LONG_SUMMARY in existing:
        op.drop_column("ui_configs", _LONG_SUMMARY)
    if _CONTEXT_BUDGET in existing:
        op.alter_column(
            "ui_configs",
            _CONTEXT_BUDGET,
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("32000"),
        )


def _column_names(bind: sa.Connection) -> set[str]:
    """读取 ui_configs 现状，使不同历史库和非事务 DDL 都能安全执行。"""

    inspector = sa.inspect(bind)
    return {str(column["name"]) for column in inspector.get_columns("ui_configs")}


def _has_ui_config_table(bind: sa.Connection) -> bool:
    """判断当前局部历史库是否存在目标表，避免无关迁移被缺表阻断。"""

    return sa.inspect(bind).has_table("ui_configs")
