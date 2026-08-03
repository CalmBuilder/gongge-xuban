"""
@Time       : 2026/07/22 12:30
@Author     : zhanglp8181
@File       : 20260722_0004_sop_runtime_execution.py
@CallChain  : Alembic upgrade/downgrade → SOP Runtime 实例/节点/操作聚合
@Description: 创建可恢复 SOP 执行所需的实例、节点 attempt 和幂等操作回执表。

Revision ID: 20260722_0004
Revises: 20260722_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0004"
down_revision: str | None = "20260722_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建统一 SOP Runtime 的三个首批执行聚合。"""

    op.create_table(
        "sop_instances",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("skill_version_id", sa.String(128), nullable=False),
        sa.Column("skill_version", sa.String(64), nullable=False),
        sa.Column("definition_checksum", sa.String(64), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("current_node_id", sa.String(128), nullable=True),
        sa.Column("slots_json", sa.JSON(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "skill_version_id",
            "run_number",
            name="uq_sop_instance_session_version_run",
        ),
    )
    _create_indexes(
        "sop_instances",
        ("tenant_id", "session_id", "skill_id", "skill_version_id", "status", "current_node_id"),
    )

    op.create_table(
        "sop_node_executions",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "instance_id",
            "node_id",
            "attempt",
            name="uq_sop_node_execution_attempt",
        ),
    )
    _create_indexes(
        "sop_node_executions", ("tenant_id", "instance_id", "node_id", "status")
    )

    op.create_table(
        "sop_operations",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("node_execution_id", sa.String(128), nullable=False),
        sa.Column("operation_name", sa.String(191), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=False),
        sa.Column("external_reference", sa.String(128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_sop_operation_tenant_idempotency"
        ),
    )
    _create_indexes(
        "sop_operations",
        (
            "tenant_id",
            "instance_id",
            "node_execution_id",
            "operation_name",
            "status",
            "external_reference",
        ),
    )


def downgrade() -> None:
    """按依赖反序移除 SOP Runtime 执行聚合。"""

    op.drop_table("sop_operations")
    op.drop_table("sop_node_executions")
    op.drop_table("sop_instances")


def _create_indexes(table_name: str, column_names: tuple[str, ...]) -> None:
    """按 SQLModel 的默认命名规则创建非唯一单列索引。"""

    for column_name in column_names:
        op.create_index(
            f"ix_{table_name}_{column_name}", table_name, [column_name], unique=False
        )
