"""
@Time       : 2026/09/06
@Author     : zhanglp8181
@File       : 20260906_0083_agent_event_time_precision.py
@CallChain  : Alembic upgrade/downgrade → agent_events.created_at → replay/keyset cursor
@Description: 将 MySQL AgentEvent 排序时间提升到微秒精度，保证同一秒内事件的稳定游标顺序。

Revision ID: 20260906_0083
Revises: 20260905_0082
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260906_0083"
down_revision: str | None = "20260905_0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _alter_created_at_precision(*, fsp: int) -> None:
    """仅在 MySQL 修改 AgentEvent 的排序时间精度，SQLite 保持原生 datetime 语义。"""

    if op.get_bind().dialect.name != "mysql":
        return
    op.alter_column(
        "agent_events",
        "created_at",
        existing_type=mysql.DATETIME(fsp=0 if fsp else 6),
        type_=mysql.DATETIME(fsp=fsp),
        existing_nullable=False,
    )


def upgrade() -> None:
    """把 MySQL 事件时间改为 DATETIME(6)，保留已有时间值并支持稳定 keyset 游标。"""

    _alter_created_at_precision(fsp=6)


def downgrade() -> None:
    """将 MySQL 事件时间恢复到秒级，明确接受微秒部分被截断的回退代价。"""

    _alter_created_at_precision(fsp=0)
