"""
@Time       : 2026/08/13 17:10
@Author     : zhanglp8181
@File       : 20260813_0062_general_skill_install_intents.py
@CallChain  : Alembic upgrade/downgrade → G1-B conversation install intents
@Description: 创建对话显式安装 Skill 的持久卡状态、幂等键和 ImportJob 关联。

Revision ID: 20260813_0062
Revises: 20260813_0061
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0062"
down_revision: str | None = "20260813_0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建对话安装 intent 表及其查询索引。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("general_skill_install_intents"):
        return
    op.create_table(
        "general_skill_install_intents",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("import_job_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("installed_revision_ids_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("terminal_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('preparing', 'awaiting_owner_confirmation', 'installing', "
            "'installed', 'failed', 'cancelled', 'expired', 'stale')",
            name="ck_general_skill_install_intent_status",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_general_skill_install_intent_row_version"),
        sa.UniqueConstraint("tenant_id", "import_job_id", name="uq_general_skill_install_intent_job"),
        sa.UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "idempotency_key",
            name="uq_general_skill_install_intent_idempotency",
        ),
    )
    for column in (
        "tenant_id",
        "session_id",
        "agent_id",
        "owner_user_id",
        "import_job_id",
        "source_kind",
        "status",
        "error_code",
    ):
        op.create_index(
            f"ix_general_skill_install_intents_{column}",
            "general_skill_install_intents",
            [column],
        )
    op.create_index(
        "ix_general_skill_install_intent_session_created",
        "general_skill_install_intents",
        ["tenant_id", "session_id", "created_at"],
    )


def downgrade() -> None:
    """存在安装授权事实时拒绝降级，避免删除审计链。"""

    bind = op.get_bind()
    count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM general_skill_install_intents")).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with general skill install intents")
    op.drop_table("general_skill_install_intents")
