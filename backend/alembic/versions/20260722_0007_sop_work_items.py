"""
@Time       : 2026/07/22 09:34
@Author     : zhanglp8181
@File       : 20260722_0007_sop_work_items.py
@CallChain  : Alembic upgrade/downgrade → SOP 人工工作项聚合 → Runtime/任务箱
@Description: 创建人工工作项、候选快照、结构化决定和幂等命令回执表。

Revision ID: 20260722_0007
Revises: 20260722_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0007"
down_revision: str | None = "20260722_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建工作项主表及候选、决定和命令回执关系表。"""

    op.create_table(
        "sop_work_items",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("node_execution_id", sa.String(128), nullable=False),
        sa.Column("skill_version_id", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=True),
        sa.Column("assignee_user_id", sa.String(128), nullable=True),
        sa.Column("initiator_user_id", sa.String(128), nullable=True),
        sa.Column("subject_employee_profile_id", sa.String(128), nullable=True),
        sa.Column("completion_mode", sa.String(64), nullable=False),
        sa.Column("claim_required", sa.Boolean(), nullable=False),
        sa.Column("required_count", sa.Integer(), nullable=True),
        sa.Column("exclude_initiator", sa.Boolean(), nullable=False),
        sa.Column("allowed_outcomes_json", sa.JSON(), nullable=False),
        sa.Column("candidate_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "node_execution_id",
            name="uq_sop_work_item_node_execution",
        ),
    )
    op.create_table(
        "sop_work_item_candidates",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("work_item_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("employee_profile_id", sa.String(128), nullable=True),
        sa.Column("source_role_codes_json", sa.JSON(), nullable=False),
        sa.Column("source_types_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "work_item_id",
            "user_id",
            name="uq_sop_work_item_candidate_user",
        ),
    )
    op.create_table(
        "sop_work_item_decisions",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("work_item_id", sa.String(128), nullable=False),
        sa.Column("actor_user_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "work_item_id",
            "actor_user_id",
            name="uq_sop_work_item_decision_actor",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_sop_work_item_decision_idempotency",
        ),
    )
    op.create_table(
        "sop_work_item_command_receipts",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("work_item_id", sa.String(128), nullable=False),
        sa.Column("command_id", sa.String(128), nullable=False),
        sa.Column("command_type", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.String(128), nullable=False),
        sa.Column("aggregate_revision", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "command_id",
            name="uq_sop_work_item_command_receipt",
        ),
    )
    _create_indexes()


def downgrade() -> None:
    """按依赖逆序移除工作项命令、决定、候选和主表。"""

    op.drop_table("sop_work_item_command_receipts")
    op.drop_table("sop_work_item_decisions")
    op.drop_table("sop_work_item_candidates")
    op.drop_table("sop_work_items")


def _create_indexes() -> None:
    """创建任务箱、候选资格、恢复和幂等命令所需的单列索引。"""

    index_columns = {
        "sop_work_items": (
            "tenant_id",
            "instance_id",
            "node_execution_id",
            "skill_version_id",
            "node_id",
            "status",
            "owner_user_id",
            "assignee_user_id",
            "initiator_user_id",
            "subject_employee_profile_id",
            "completion_mode",
            "outcome",
            "expires_at",
        ),
        "sop_work_item_candidates": (
            "tenant_id",
            "work_item_id",
            "user_id",
            "employee_profile_id",
        ),
        "sop_work_item_decisions": (
            "tenant_id",
            "work_item_id",
            "actor_user_id",
        ),
        "sop_work_item_command_receipts": (
            "tenant_id",
            "work_item_id",
            "command_id",
            "command_type",
            "actor_user_id",
        ),
    }
    for table_name, columns in index_columns.items():
        for column_name in columns:
            op.create_index(
                f"ix_{table_name}_{column_name}",
                table_name,
                [column_name],
                unique=False,
            )
