"""
@Time       : 2026/08/13 01:10
@Author     : zhanglp8181
@File       : 20260813_0057_general_skill_runtime.py
@CallChain  : Alembic upgrade/downgrade → Skill runtime → session override/use ledger
@Description: 增加会话收窄状态与不可重复的 Skill 加载使用账本。

Revision ID: 20260813_0057
Revises: 20260813_0056
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0057"
down_revision: str | None = "20260813_0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以 expand 方式创建会话 override 与使用账本，不切换旧运行读路径。"""

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_general_skill_overrides" not in tables:
        op.create_table(
            "session_general_skill_overrides",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("session_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.String(128), nullable=False),
            sa.Column("skill_id", sa.String(128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "session_id",
                "user_id",
                "agent_id",
                "skill_id",
                name="uq_session_general_skill_override_scope",
            ),
            sa.CheckConstraint(
                "row_version >= 1", name="ck_session_general_skill_override_version"
            ),
        )
        for column in ("tenant_id", "session_id", "user_id", "agent_id", "skill_id", "enabled"):
            op.create_index(
                f"ix_session_general_skill_overrides_{column}",
                "session_general_skill_overrides",
                [column],
            )
        op.create_index(
            "ix_session_general_skill_override_lookup",
            "session_general_skill_overrides",
            ["tenant_id", "session_id", "user_id", "agent_id", "enabled"],
        )
    if "general_skill_uses" not in tables:
        op.create_table(
            "general_skill_uses",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("session_id", sa.String(128), nullable=False),
            sa.Column("turn_id", sa.String(128), nullable=False),
            sa.Column("execution_id", sa.String(128), nullable=True),
            sa.Column("agent_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("skill_id", sa.String(128), nullable=False),
            sa.Column("revision_id", sa.String(128), nullable=False),
            sa.Column("content_checksum", sa.String(128), nullable=False),
            sa.Column("selection_mode", sa.String(128), nullable=False),
            sa.Column("status", sa.String(128), nullable=False, server_default="loading"),
            sa.Column("parent_skill_use_id", sa.String(128), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("loaded_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("invalidation_reason", sa.String(128), nullable=True),
            sa.Column("result_summary_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "idempotency_key", name="uq_general_skill_use_idempotency"
            ),
            sa.CheckConstraint(
                "selection_mode IN ('auto', 'forced', 'dependency')",
                name="ck_general_skill_use_selection_mode",
            ),
            sa.CheckConstraint(
                "status IN ('loading', 'active', 'completed', 'invalidated', 'failed', 'cancelled')",
                name="ck_general_skill_use_status",
            ),
        )
        for column in (
            "tenant_id",
            "session_id",
            "turn_id",
            "execution_id",
            "agent_id",
            "user_id",
            "skill_id",
            "revision_id",
            "content_checksum",
            "selection_mode",
            "status",
            "parent_skill_use_id",
            "idempotency_key",
        ):
            op.create_index(f"ix_general_skill_uses_{column}", "general_skill_uses", [column])
        op.create_index(
            "ix_general_skill_use_session_status",
            "general_skill_uses",
            ["tenant_id", "session_id", "status", "created_at"],
        )
    if "sop_operations" in tables:
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns("sop_operations")}
        if "caused_by_skill_use_id" not in columns:
            with op.batch_alter_table("sop_operations") as batch:
                batch.add_column(
                    sa.Column("caused_by_skill_use_id", sa.String(128), nullable=True)
                )
                batch.create_index(
                    "ix_sop_operations_caused_by_skill_use_id",
                    ["caused_by_skill_use_id"],
                )


def downgrade() -> None:
    """仅在无引用时移除 S3 expand 表；生产回滚优先关闭开关。"""

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "sop_operations" in tables:
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns("sop_operations")}
        if "caused_by_skill_use_id" in columns:
            with op.batch_alter_table("sop_operations") as batch:
                batch.drop_index("ix_sop_operations_caused_by_skill_use_id")
                batch.drop_column("caused_by_skill_use_id")
    if "general_skill_uses" in tables:
        op.drop_table("general_skill_uses")
    if "session_general_skill_overrides" in tables:
        op.drop_table("session_general_skill_overrides")
