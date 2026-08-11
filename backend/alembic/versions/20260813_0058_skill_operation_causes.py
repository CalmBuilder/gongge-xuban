"""
@Time       : 2026/08/13 03:20
@Author     : zhanglp8181
@File       : 20260813_0058_skill_operation_causes.py
@CallChain  : Alembic upgrade/downgrade → dynamic operation → Skill authorization guard
@Description: 为 Operation 增加完整 Skill Use 因果集合并保留旧单值字段兼容。

Revision ID: 20260813_0058
Revises: 20260813_0057
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0058"
down_revision: str | None = "20260813_0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以 expand 方式加入 JSON 因果集合，旧行继续回退读取单值字段。"""

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "sop_operations" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sop_operations")}
    if "caused_by_skill_use_ids_json" not in columns:
        with op.batch_alter_table("sop_operations") as batch:
            batch.add_column(
                sa.Column(
                    "caused_by_skill_use_ids_json",
                    sa.JSON(),
                    nullable=False,
                    server_default=(
                        sa.text("(JSON_ARRAY())")
                        if bind.dialect.name == "mysql"
                        else sa.text("'[]'")
                    ),
                )
            )


def downgrade() -> None:
    """移除集合字段；主 Use 仍由兼容单值字段保留。"""

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "sop_operations" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sop_operations")}
    if "caused_by_skill_use_ids_json" in columns:
        with op.batch_alter_table("sop_operations") as batch:
            batch.drop_column("caused_by_skill_use_ids_json")
