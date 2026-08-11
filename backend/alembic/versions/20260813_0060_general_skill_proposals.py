"""
@Time       : 2026/08/13 08:20
@Author     : zhanglp8181
@File       : 20260813_0060_general_skill_proposals.py
@CallChain  : Alembic upgrade/downgrade → Agent Skill proposal → publication Attention
@Description: 增加 Agent 提案到修订、审核 Artifact、Attention 和绑定结果的持久状态机。

Revision ID: 20260813_0060
Revises: 20260813_0059
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0060"
down_revision: str | None = "20260813_0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 proposal 表；草稿正文和附件继续复用不可变 Revision 与 Artifact。"""

    op.create_table(
        "general_skill_proposals",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=512), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("initiator_user_id", sa.String(length=128), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("attention_id", sa.String(length=128), nullable=True),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=128), nullable=False),
        sa.Column("review_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("target_skill_id", sa.String(length=128), nullable=True),
        sa.Column("base_revision_id", sa.String(length=128), nullable=True),
        sa.Column("base_content_checksum", sa.String(length=128), nullable=True),
        sa.Column("proposal_checksum", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("published_binding_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("terminal_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('staged', 'awaiting_approval', 'publishing', 'published', "
            "'rejected', 'expired', 'failed')",
            name="ck_general_skill_proposal_status",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_general_skill_proposal_row_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", name="uq_general_skill_proposal_operation"
        ),
        sa.UniqueConstraint(
            "tenant_id", "revision_id", name="uq_general_skill_proposal_revision"
        ),
        sa.UniqueConstraint(
            "tenant_id", "review_artifact_id", name="uq_general_skill_proposal_artifact"
        ),
    )
    for name, columns in (
        ("ix_general_skill_proposals_tenant_id", ["tenant_id"]),
        ("ix_general_skill_proposals_execution_id", ["execution_id"]),
        ("ix_general_skill_proposals_session_id", ["session_id"]),
        ("ix_general_skill_proposals_agent_id", ["agent_id"]),
        ("ix_general_skill_proposals_initiator_user_id", ["initiator_user_id"]),
        ("ix_general_skill_proposals_operation_id", ["operation_id"]),
        ("ix_general_skill_proposals_attention_id", ["attention_id"]),
        ("ix_general_skill_proposals_skill_id", ["skill_id"]),
        ("ix_general_skill_proposals_revision_id", ["revision_id"]),
        ("ix_general_skill_proposals_review_artifact_id", ["review_artifact_id"]),
        ("ix_general_skill_proposals_target_skill_id", ["target_skill_id"]),
        ("ix_general_skill_proposals_base_revision_id", ["base_revision_id"]),
        ("ix_general_skill_proposals_proposal_checksum", ["proposal_checksum"]),
        ("ix_general_skill_proposals_status", ["status"]),
        ("ix_general_skill_proposals_error_code", ["error_code"]),
        ("ix_general_skill_proposals_published_binding_id", ["published_binding_id"]),
    ):
        op.create_index(name, "general_skill_proposals", columns, unique=False)
    op.create_index(
        "ix_general_skill_proposal_execution_status",
        "general_skill_proposals",
        ["tenant_id", "execution_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """删除仅承载提案编排的表，不改写已发布 Skill/Revision/Binding。"""

    op.drop_table("general_skill_proposals")
