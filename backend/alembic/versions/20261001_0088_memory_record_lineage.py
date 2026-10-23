"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : 20261001_0088_memory_record_lineage.py
@CallChain  : Alembic upgrade → MemoryRecord → 记忆键状态原子结算与来源审计
@Description: 为 SD-6B 记忆正文补齐来源序号、任务和失效代数，保持旧记录可读。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261001_0088"
down_revision: str | None = "20261001_0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    """读取历史表字段，允许 SQLite/MySQL 重复升级。"""

    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {item["name"] for item in inspector.get_columns(table_name)}


def _add_column_if_missing(column: sa.Column) -> None:
    """只为 memories 增加不存在的 lineage 字段。"""

    if "memories" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if column.name not in _columns("memories"):
        op.add_column("memories", column)


def upgrade() -> None:
    """补齐旧 MemoryRecord 的可验证来源和失效代数。"""

    if "memories" not in sa.inspect(op.get_bind()).get_table_names():
        # 部分历史迁移单元只构造迁移自身所需的旧表，再把临时库升级到 head；
        # 记忆表尚未存在时保留其最小 fixture 语义，待正式记忆迁移创建后再执行本变更。
        return

    _add_column_if_missing(
        sa.Column("agent_scope_key", sa.String(128), nullable=False, server_default="__user__")
    )
    _add_column_if_missing(
        sa.Column("source_sequence", sa.BigInteger(), nullable=False, server_default="0")
    )
    _add_column_if_missing(sa.Column("capture_job_id", sa.String(128), nullable=True))
    _add_column_if_missing(sa.Column("source_hash", sa.String(64), nullable=True))
    for name in (
        "user_generation",
        "agent_generation",
        "user_permission_generation",
        "agent_permission_generation",
    ):
        _add_column_if_missing(sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"))

    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_indexes("memories")}
    if "ix_memories_capture_job_id" not in existing:
        op.create_index("ix_memories_capture_job_id", "memories", ["capture_job_id"])
    if "ix_memories_source_hash" not in existing:
        op.create_index("ix_memories_source_hash", "memories", ["source_hash"])


def downgrade() -> None:
    """回退记忆 lineage 字段，保留原有正文表和有效归属数据。"""

    inspector = sa.inspect(op.get_bind())
    if "memories" not in inspector.get_table_names():
        return
    for name in ("ix_memories_source_hash", "ix_memories_capture_job_id"):
        if name in {item["name"] for item in inspector.get_indexes("memories")}:
            op.drop_index(name, table_name="memories")
    with op.batch_alter_table("memories") as batch:
        for name in (
            "agent_scope_key",
            "source_sequence",
            "capture_job_id",
            "source_hash",
            "user_generation",
            "agent_generation",
            "user_permission_generation",
            "agent_permission_generation",
        ):
            if name in _columns("memories"):
                batch.drop_column(name)
