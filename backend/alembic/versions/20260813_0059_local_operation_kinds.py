"""
@Time       : 2026/08/13 03:02
@Author     : zhanglp8181
@File       : 20260813_0059_local_operation_kinds.py
@CallChain  : Alembic upgrade/downgrade → SopOperation → 受管代码工作区动作
@Description: 为统一 Operation 账本增加本地写和隔离执行效果类别。

Revision ID: 20260813_0059
Revises: 20260813_0058
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0059"
down_revision: str | None = "20260813_0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_sop_operation_effect_kind"
_EXPANDED = (
    "effect_kind IN ('read', 'local_write', 'execute', 'external_write', "
    "'legacy_unknown')"
)
_LEGACY = "effect_kind IN ('read', 'external_write', 'legacy_unknown')"


def upgrade() -> None:
    """扩展效果类别约束；不改写任何既有 Operation。"""

    bind = op.get_bind()
    if "sop_operations" not in set(sa.inspect(bind).get_table_names()):
        return
    names = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints("sop_operations")
    }
    with op.batch_alter_table("sop_operations") as batch:
        if _NAME in names:
            batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _EXPANDED)


def downgrade() -> None:
    """仅在不存在新类别数据时恢复旧约束，避免静默丢失审计事实。"""

    bind = op.get_bind()
    if "sop_operations" not in set(sa.inspect(bind).get_table_names()):
        return
    count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM sop_operations "
            "WHERE effect_kind IN ('local_write', 'execute')"
        )
    ).scalar_one()
    if int(count) > 0:
        raise RuntimeError("cannot downgrade while local workspace operations exist")
    names = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints("sop_operations")
    }
    with op.batch_alter_table("sop_operations") as batch:
        if _NAME in names:
            batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _LEGACY)
