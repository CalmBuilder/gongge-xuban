"""
@Time       : 2026/08/30 16:10
@Author     : zhanglp8181
@File       : 20260830_0077_destructive_operation_kind.py
@CallChain  : Alembic upgrade/downgrade → SopOperation → destructive gray dispatch
@Description: 为统一 Operation 账本增加独立 destructive 效果类别，不改变既有操作数据。

Revision ID: 20260830_0077
Revises: 20260830_0076
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_0077"
down_revision: str | None = "20260830_0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_sop_operation_effect_kind"
_EXPANDED = (
    "effect_kind IN ('read', 'local_write', 'execute', 'external_write', 'destructive', "
    "'legacy_unknown')"
)
_WITHOUT_DESTRUCTIVE = (
    "effect_kind IN ('read', 'local_write', 'execute', 'external_write', 'legacy_unknown')"
)


def upgrade() -> None:
    """扩展 Operation 效果类别约束，使 destructive 可以独立持久化和审计。"""

    bind = op.get_bind()
    if "sop_operations" not in set(sa.inspect(bind).get_table_names()):
        return
    effect_constraint_names = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints("sop_operations")
        if item.get("name")
        and (
            str(item["name"]) == _NAME
            or "effect_kind" in str(item.get("sqltext") or "")
        )
    }
    with op.batch_alter_table("sop_operations") as batch:
        for name in effect_constraint_names:
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(_NAME, _EXPANDED)


def downgrade() -> None:
    """仅在不存在 destructive Operation 时恢复旧约束，避免删除历史效果语义。"""

    bind = op.get_bind()
    if "sop_operations" not in set(sa.inspect(bind).get_table_names()):
        return
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM sop_operations WHERE effect_kind = 'destructive'")
    ).scalar_one()
    if int(count) > 0:
        raise RuntimeError("cannot downgrade while destructive operations exist")
    effect_constraint_names = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints("sop_operations")
        if item.get("name")
        and (
            str(item["name"]) == _NAME
            or "effect_kind" in str(item.get("sqltext") or "")
        )
    }
    with op.batch_alter_table("sop_operations") as batch:
        for name in effect_constraint_names:
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(_NAME, _WITHOUT_DESTRUCTIVE)
