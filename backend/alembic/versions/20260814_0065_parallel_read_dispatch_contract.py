"""
@Time       : 2026/08/12 23:12
@Author     : zhanglp8181
@File       : 20260814_0065_parallel_read_dispatch_contract.py
@CallChain  : Alembic upgrade/downgrade → Operation/Attempt parallel dispatch contract
@Description: 扩展纯读重试状态和逐物理派发 attempt 身份，为有界并发恢复提供基础。

Revision ID: 20260814_0065
Revises: 20260814_0064
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260814_0065"
down_revision: str | None = "20260814_0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OP_STATUS = "ck_sop_operation_status"
_ATTEMPT_STATUS = "ck_sop_operation_attempt_status"
_OLD_ATTEMPT_UNIQUE = "uq_sop_operation_attempt_execution"
_DISPATCH_UNIQUE = "uq_sop_operation_attempt_dispatch_token"


def upgrade() -> None:
    """增加 retry_wait、派发 token/deadline，并解除每节点只能一个 attempt 的旧约束。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("sop_operation_attempts"):
        raise RuntimeError("parallel read dispatch requires sop_operation_attempts")
    columns = {str(item["name"]) for item in sa.inspect(bind).get_columns("sop_operation_attempts")}
    with op.batch_alter_table("sop_operation_attempts") as batch:
        if "dispatch_token" not in columns:
            batch.add_column(sa.Column("dispatch_token", sa.String(128), nullable=True))
        if "deadline_at" not in columns:
            batch.add_column(sa.Column("deadline_at", sa.DateTime(), nullable=True))
        if "retry_at" not in columns:
            batch.add_column(sa.Column("retry_at", sa.DateTime(), nullable=True))
    _replace_check(
        bind,
        "sop_operations",
        _OP_STATUS,
        "status IN ('prepared', 'running', 'retry_wait', 'succeeded', 'failed', "
        "'unknown', 'cancelled')",
    )
    _replace_check(
        bind,
        "sop_operation_attempts",
        _ATTEMPT_STATUS,
        "status IN ('prepared', 'running', 'retry_wait', 'succeeded', 'failed', "
        "'unknown', 'cancelled', 'reused')",
    )
    constraints = _constraint_names(bind, "sop_operation_attempts")
    with op.batch_alter_table("sop_operation_attempts") as batch:
        if _OLD_ATTEMPT_UNIQUE in constraints:
            batch.drop_constraint(_OLD_ATTEMPT_UNIQUE, type_="unique")
        if _DISPATCH_UNIQUE not in constraints:
            batch.create_unique_constraint(
                _DISPATCH_UNIQUE,
                ["tenant_id", "dispatch_token"],
            )
    indexes = {str(item["name"]) for item in sa.inspect(bind).get_indexes("sop_operation_attempts")}
    for column in ("dispatch_token", "deadline_at", "retry_at"):
        name = f"ix_sop_operation_attempts_{column}"
        if name not in indexes:
            op.create_index(name, "sop_operation_attempts", [column])
    _create_dispatch_tables(bind)


def downgrade() -> None:
    """存在并发/重试派发事实时拒绝退回单 attempt 语义。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("sop_operation_attempts"):
        raise RuntimeError("parallel read downgrade requires sop_operation_attempts")
    active = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM sop_operations WHERE status = 'retry_wait'"
            )
        ).scalar_one()
    )
    expanded = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM sop_operation_attempts "
                "WHERE dispatch_token IS NOT NULL OR deadline_at IS NOT NULL OR retry_at IS NOT NULL"
            )
        ).scalar_one()
    )
    duplicate = bind.execute(
        sa.text(
            "SELECT 1 FROM sop_operation_attempts GROUP BY tenant_id, operation_id, "
            "node_execution_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if active or expanded or duplicate is not None:
        raise RuntimeError("cannot downgrade with parallel dispatch facts")
    for table in (
        "dynamic_read_dispatch_results",
        "dynamic_read_dispatch_items",
        "dynamic_read_dispatch_batches",
    ):
        if sa.inspect(bind).has_table(table):
            count = int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            if count:
                raise RuntimeError("cannot downgrade with parallel dispatch facts")
            op.drop_table(table)
    _replace_check(
        bind,
        "sop_operations",
        _OP_STATUS,
        "status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')",
    )
    _replace_check(
        bind,
        "sop_operation_attempts",
        _ATTEMPT_STATUS,
        "status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown', 'cancelled', "
        "'reused')",
    )
    constraints = _constraint_names(bind, "sop_operation_attempts")
    with op.batch_alter_table("sop_operation_attempts") as batch:
        if _DISPATCH_UNIQUE in constraints:
            batch.drop_constraint(_DISPATCH_UNIQUE, type_="unique")
        if _OLD_ATTEMPT_UNIQUE not in constraints:
            batch.create_unique_constraint(
                _OLD_ATTEMPT_UNIQUE,
                ["tenant_id", "operation_id", "node_execution_id"],
            )
    indexes = {str(item["name"]) for item in sa.inspect(bind).get_indexes("sop_operation_attempts")}
    for column in ("dispatch_token", "deadline_at", "retry_at"):
        name = f"ix_sop_operation_attempts_{column}"
        if name in indexes:
            op.drop_index(name, table_name="sop_operation_attempts")
    with op.batch_alter_table("sop_operation_attempts") as batch:
        for column in ("retry_at", "deadline_at", "dispatch_token"):
            batch.drop_column(column)


def _replace_check(
    bind: sa.Connection,
    table: str,
    name: str,
    condition: str,
) -> None:
    """跨 SQLite/MySQL 替换命名检查约束。"""

    names = _constraint_names(bind, table)
    with op.batch_alter_table(table) as batch:
        if name in names:
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, condition)


def _constraint_names(bind: sa.Connection, table: str) -> set[str]:
    """返回表上检查和唯一约束名称。"""

    inspector = sa.inspect(bind)
    return {
        str(item["name"])
        for item in (
            *inspector.get_check_constraints(table),
            *inspector.get_unique_constraints(table),
        )
        if item.get("name")
    }


def _create_dispatch_tables(bind: sa.Connection) -> None:
    """创建波次、顺序项与 append-only 结果 inbox。"""

    if not sa.inspect(bind).has_table("dynamic_read_dispatch_batches"):
        op.create_table(
            "dynamic_read_dispatch_batches",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("execution_id", sa.String(128), nullable=False),
            sa.Column("plan_revision_id", sa.String(128), nullable=False),
            sa.Column("wave_checksum", sa.String(128), nullable=False),
            sa.Column("ordered_step_keys_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(128), nullable=False),
            sa.Column("parallelism", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("deadline_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "wave_checksum", name="uq_dynamic_read_wave"),
            sa.CheckConstraint(
                "status IN ('ready', 'dispatched', 'settling', 'succeeded', 'failed', "
                "'cancelled', 'superseded')",
                name="ck_dynamic_read_batch_status",
            ),
            sa.CheckConstraint(
                "parallelism >= 1 AND revision >= 0", name="ck_dynamic_read_batch_bounds"
            ),
        )
    if not sa.inspect(bind).has_table("dynamic_read_dispatch_items"):
        op.create_table(
            "dynamic_read_dispatch_items",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("batch_id", sa.String(128), nullable=False),
            sa.Column("execution_id", sa.String(128), nullable=False),
            sa.Column("plan_revision_id", sa.String(128), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("step_key", sa.String(128), nullable=False),
            sa.Column("node_execution_id", sa.String(128), nullable=False),
            sa.Column("operation_id", sa.String(128), nullable=False),
            sa.Column("operation_revision_at_start", sa.Integer(), nullable=False),
            sa.Column("dispatch_token", sa.String(128), nullable=False),
            sa.Column("capability_checksum", sa.String(128), nullable=False),
            sa.Column("request_fingerprint", sa.String(128), nullable=False),
            sa.Column("status", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "dispatch_token", name="uq_dynamic_read_dispatch_token"),
            sa.UniqueConstraint("tenant_id", "batch_id", "position", name="uq_dynamic_read_position"),
            sa.CheckConstraint("position >= 0", name="ck_dynamic_read_item_position"),
            sa.CheckConstraint(
                "status IN ('dispatch_pending', 'dispatched', 'result_ready', 'settled', "
                "'discarded')",
                name="ck_dynamic_read_item_status",
            ),
        )
    if not sa.inspect(bind).has_table("dynamic_read_dispatch_results"):
        op.create_table(
            "dynamic_read_dispatch_results",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("dispatch_token", sa.String(128), nullable=False),
            sa.Column("status", sa.String(128), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("error_json", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "dispatch_token", name="uq_dynamic_read_result_token"),
            sa.CheckConstraint(
                "status IN ('succeeded', 'failed')", name="ck_dynamic_read_result_status"
            ),
        )
