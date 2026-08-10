"""
@Time       : 2026/08/10 23:40
@Author     : zhanglp8181
@File       : 20260810_0045_controlled_external_writes.py
@CallChain  : Alembic upgrade/downgrade → Agent 连接动作授权与 Operation 审批派发证据
@Description: 为一次性审批外部写增加绑定动作白名单和不可变派发授权事实。

Revision ID: 20260810_0045
Revises: 20260810_0044
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0045"
down_revision: str | None = "20260810_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展既有绑定与 Operation；先回填再收紧 JSON 非空约束。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required = {"agent_connection_bindings", "sop_operations", "sop_work_items"}
    missing = required - set(inspector.get_table_names())
    if missing:
        raise RuntimeError(f"controlled external write requires tables: {sorted(missing)}")

    binding_columns = {
        str(column["name"])
        for column in inspector.get_columns("agent_connection_bindings")
    }
    if "allowed_actions_json" not in binding_columns:
        op.add_column(
            "agent_connection_bindings",
            sa.Column("allowed_actions_json", sa.JSON(), nullable=True),
        )
    bind.execute(
        sa.text(
            "UPDATE agent_connection_bindings "
            "SET allowed_actions_json = :empty WHERE allowed_actions_json IS NULL"
        ),
        {"empty": json.dumps([])},
    )
    binding_info = {
        str(column["name"]): column
        for column in sa.inspect(bind).get_columns("agent_connection_bindings")
    }
    if bool(binding_info["allowed_actions_json"].get("nullable")):
        with op.batch_alter_table("agent_connection_bindings") as batch:
            batch.alter_column(
                "allowed_actions_json",
                existing_type=sa.JSON(),
                nullable=False,
            )

    operation_columns = {
        str(column["name"])
        for column in sa.inspect(bind).get_columns("sop_operations")
    }
    additions: tuple[tuple[str, sa.types.TypeEngine, bool], ...] = (
        ("approval_work_item_id", sa.String(128), True),
        ("approval_fingerprint", sa.String(128), True),
        ("approved_by_user_id", sa.String(128), True),
        ("approved_at", sa.DateTime(), True),
        ("authorization_evidence_json", sa.JSON(), False),
        ("dispatched_at", sa.DateTime(), True),
    )
    for name, type_, nullable in additions:
        if name not in operation_columns:
            op.add_column(
                "sop_operations",
                sa.Column(name, type_, nullable=True),
            )
        if not nullable:
            bind.execute(
                sa.text(
                    "UPDATE sop_operations "
                    "SET authorization_evidence_json = :empty "
                    "WHERE authorization_evidence_json IS NULL"
                ),
                {"empty": json.dumps({})},
            )
            operation_info = {
                str(column["name"]): column
                for column in sa.inspect(bind).get_columns("sop_operations")
            }
            if bool(operation_info[name].get("nullable")):
                with op.batch_alter_table("sop_operations") as batch:
                    batch.alter_column(
                        name,
                        existing_type=type_,
                        nullable=False,
                    )

    _create_index_if_missing(
        bind,
        "sop_operations",
        "ix_sop_operations_approval_work_item_id",
        ["approval_work_item_id"],
    )
    _create_index_if_missing(
        bind,
        "sop_operations",
        "ix_sop_operations_approved_by_user_id",
        ["approved_by_user_id"],
    )


def downgrade() -> None:
    """仅在没有审批派发事实和动作授权时允许移除本批字段。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    operation_columns = {
        str(column["name"])
        for column in inspector.get_columns("sop_operations")
    }
    if "approval_work_item_id" in operation_columns:
        count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM sop_operations "
                "WHERE approval_work_item_id IS NOT NULL OR dispatched_at IS NOT NULL"
            )
        ).scalar_one()
        if int(count) > 0:
            raise RuntimeError("cannot downgrade controlled writes with approval or dispatch facts")

    binding_columns = {
        str(column["name"])
        for column in inspector.get_columns("agent_connection_bindings")
    }
    if "allowed_actions_json" in binding_columns:
        values = bind.execute(
            sa.text("SELECT allowed_actions_json FROM agent_connection_bindings")
        ).scalars()
        if any(_json_list(value) for value in values):
            raise RuntimeError("cannot downgrade controlled writes with action grants")
        with op.batch_alter_table("agent_connection_bindings") as batch:
            batch.drop_column("allowed_actions_json")

    if "approval_work_item_id" in operation_columns:
        with op.batch_alter_table("sop_operations") as batch:
            for name in (
                "dispatched_at",
                "authorization_evidence_json",
                "approved_at",
                "approved_by_user_id",
                "approval_fingerprint",
                "approval_work_item_id",
            ):
                batch.drop_column(name)


def _create_index_if_missing(
    bind: sa.Connection,
    table: str,
    name: str,
    columns: list[str],
) -> None:
    """以可重入方式创建普通索引，兼容 MySQL 非事务 DDL 恢复。"""

    existing = {str(item["name"]) for item in sa.inspect(bind).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def _json_list(value: object) -> list[object]:
    """把 SQLite 文本或 MySQL JSON 统一解析为列表，异常值按有数据拒绝降级。"""

    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return [] if value is None else [value]
