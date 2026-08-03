"""
@Time       : 2026/07/27 19:45
@Author     : zhanglp8181
@File       : 20260727_0015_approval_requests.py
@CallChain  : Alembic upgrade/downgrade → 通用审批申请台账 → SOP 工作项结果回写
@Description: 创建审批申请主表和按步骤追加的权威决定表。

Revision ID: 20260727_0015
Revises: 20260727_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260727_0015"
down_revision: str | None = "20260727_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建通用审批申请和逐步决定表，并建立查询与幂等索引。"""

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("request_number", sa.String(128), nullable=False),
        sa.Column("request_type", sa.String(64), nullable=False),
        sa.Column("policy_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("initiator_user_id", sa.String(128), nullable=False),
        sa.Column("subject_employee_profile_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=True),
        sa.Column("skill_version_id", sa.String(128), nullable=True),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("total_steps", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_number",
            name="uq_approval_request_tenant_number",
        ),
    )
    op.create_table(
        "approval_request_decisions",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.String(128), nullable=False),
        sa.Column("actor_user_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "step_number",
            name="uq_approval_request_decision_step",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "work_item_id",
            name="uq_approval_request_decision_work_item",
        ),
    )
    _create_indexes()


def downgrade() -> None:
    """按依赖逆序移除审批决定和申请主表。"""

    op.drop_table("approval_request_decisions")
    op.drop_table("approval_requests")


def _create_indexes() -> None:
    """创建租户查询、状态、流程关联和决定审计索引。"""

    index_columns = {
        "approval_requests": (
            "tenant_id",
            "request_number",
            "request_type",
            "policy_key",
            "status",
            "initiator_user_id",
            "subject_employee_profile_id",
            "instance_id",
            "skill_version_id",
        ),
        "approval_request_decisions": (
            "tenant_id",
            "request_id",
            "step_number",
            "work_item_id",
            "actor_user_id",
            "outcome",
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
