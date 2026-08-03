"""
@Time       : 2026/08/01 15:20
@Author     : zhanglp8181
@File       : 20260801_0031_memory_agent_pagination.py
@CallChain  : Alembic upgrade → memories/sessions → 员工记忆分页查询
@Description: 为长期记忆增加可索引员工归属，并从 metadata 或历史会话完成兼容回填。

Revision ID: 20260801_0031
Revises: 20260729_0030
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0031"
down_revision: str | None = "20260729_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_memories_tenant_agent_user_updated"


def upgrade() -> None:
    """增加员工归属列并回填，且允许 MySQL 非事务 DDL 中断后安全续跑。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    memory_columns = {column["name"] for column in inspector.get_columns("memories")}
    if "agent_id" not in memory_columns:
        with op.batch_alter_table("memories") as batch_op:
            batch_op.add_column(sa.Column("agent_id", sa.String(length=128), nullable=True))

    session_agents = {
        row.id: row.agent_id
        for row in bind.execute(sa.text("SELECT id, agent_id FROM sessions"))
        if row.agent_id
    }
    memories = bind.execute(
        sa.text("SELECT id, session_id, metadata_json FROM memories WHERE agent_id IS NULL")
    )
    for row in memories:
        metadata = _metadata_object(row.metadata_json)
        metadata_agent_id = metadata.get("agent_id")
        agent_id = (
            metadata_agent_id.strip()
            if isinstance(metadata_agent_id, str) and metadata_agent_id.strip()
            else session_agents.get(row.session_id)
        )
        if agent_id:
            bind.execute(
                sa.text("UPDATE memories SET agent_id = :agent_id WHERE id = :memory_id"),
                {"agent_id": agent_id, "memory_id": row.id},
            )

    inspector = sa.inspect(bind)
    memory_indexes = {index["name"] for index in inspector.get_indexes("memories")}
    if _INDEX_NAME not in memory_indexes:
        with op.batch_alter_table("memories") as batch_op:
            batch_op.create_index(
                _INDEX_NAME,
                ["tenant_id", "agent_id", "user_id", "updated_at"],
                unique=False,
            )


def downgrade() -> None:
    """移除员工归属索引和列，兼容 metadata 中保留的原有归属信息。"""

    with op.batch_alter_table("memories") as batch_op:
        batch_op.drop_index(_INDEX_NAME)
        batch_op.drop_column("agent_id")


def _metadata_object(value: Any) -> dict[str, Any]:
    """把 SQLite 文本或 MySQL JSON 返回值统一解析为字典，非法值按空对象处理。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
