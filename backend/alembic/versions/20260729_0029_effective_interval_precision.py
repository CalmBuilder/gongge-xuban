"""
@Time       : 2026/07/29 22:10
@Author     : zhanglp8181
@File       : 20260729_0029_effective_interval_precision.py
@CallChain  : Alembic upgrade/downgrade → 任期有效区间列 → 组织与 SOP 候选查询
@Description: 将 MySQL 任期边界统一为微秒精度，避免立即写入后被舍入成短暂未来时间。

Revision ID: 20260729_0029
Revises: 20260729_0028
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260729_0029"
down_revision: str | None = "20260729_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EFFECTIVE_INTERVAL_COLUMNS = {
    "member_org_assignments": (False, True),
    "position_assignments": (False, True),
    "organization_leader_assignments": (False, True),
    "employee_role_assignments": (True, True),
}


def upgrade() -> None:
    """将 MySQL 当前有效性判断涉及的起止时间列提升为微秒精度。"""

    _alter_mysql_effective_interval_precision(fsp=6)


def downgrade() -> None:
    """将 MySQL 任期边界恢复为秒级，业务行保持不变但微秒部分会丢失。"""

    _alter_mysql_effective_interval_precision(fsp=0)


def _alter_mysql_effective_interval_precision(*, fsp: int) -> None:
    """仅在 MySQL 修改四类任期列精度，SQLite 沿用原生 datetime 存储。"""

    if op.get_bind().dialect.name != "mysql":
        return
    target_type = mysql.DATETIME(fsp=fsp)
    existing_type = mysql.DATETIME(fsp=0 if fsp else 6)
    for table_name, (from_nullable, until_nullable) in _EFFECTIVE_INTERVAL_COLUMNS.items():
        op.alter_column(
            table_name,
            "effective_from",
            existing_type=existing_type,
            type_=target_type,
            existing_nullable=from_nullable,
        )
        op.alter_column(
            table_name,
            "effective_until",
            existing_type=existing_type,
            type_=target_type,
            existing_nullable=until_nullable,
        )
