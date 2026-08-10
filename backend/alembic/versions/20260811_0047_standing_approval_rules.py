"""
@Time       : 2026/08/11 22:05
@Author     : zhanglp8181
@File       : 20260811_0047_standing_approval_rules.py
@CallChain  : Alembic upgrade/downgrade → StandingApprovalRule → 动态外部写授权
@Description: 增加受管长期批准、命令幂等回执及 Operation 授权来源字段。

Revision ID: 20260811_0047
Revises: 20260811_0046
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0047"
down_revision: str | None = "20260811_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入创建规则/命令表，并为历史 Operation 回填明确授权来源。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required = {
        "scheduled_tasks",
        "connection_profiles",
        "agent_connection_bindings",
        "sop_operations",
    }
    missing = required - set(inspector.get_table_names())
    if missing:
        raise RuntimeError(f"standing approval rules require tables: {sorted(missing)}")
    if not inspector.has_table("standing_approval_rules"):
        op.create_table(
            "standing_approval_rules",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.String(128), nullable=False),
            sa.Column("source_schedule_id", sa.String(128), nullable=False),
            sa.Column("source_schedule_checksum", sa.String(128), nullable=False),
            sa.Column("profile_id", sa.String(128), nullable=False),
            sa.Column("binding_id", sa.String(128), nullable=False),
            sa.Column("tool_id", sa.String(255), nullable=False),
            sa.Column("tool_snapshot_checksum", sa.String(128), nullable=False),
            sa.Column("risk_class", sa.String(64), nullable=False),
            sa.Column("target_type", sa.String(64), nullable=False),
            sa.Column("canonical_target", sa.String(512), nullable=False),
            sa.Column("target_hash", sa.String(128), nullable=False),
            sa.Column("argument_constraints_json", sa.JSON(), nullable=False),
            sa.Column("active_scope_key", sa.String(128), nullable=True),
            sa.Column("valid_from", sa.DateTime(), nullable=False),
            sa.Column("valid_to", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_by_user_id", sa.String(128), nullable=False),
            sa.Column("revoked_by_user_id", sa.String(128), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("risk_class = 'external_write'", name="ck_standing_rule_risk"),
            sa.CheckConstraint(
                "status IN ('active', 'revoked')",
                name="ck_standing_rule_status",
            ),
            sa.CheckConstraint("revision >= 1", name="ck_standing_rule_revision"),
            sa.UniqueConstraint(
                "tenant_id",
                "active_scope_key",
                name="uq_standing_rule_active_scope",
            ),
        )
    if not sa.inspect(bind).has_table("standing_approval_command_receipts"):
        op.create_table(
            "standing_approval_command_receipts",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("command_id", sa.String(128), nullable=False),
            sa.Column("command_type", sa.String(64), nullable=False),
            sa.Column("actor_user_id", sa.String(128), nullable=False),
            sa.Column("payload_checksum", sa.String(128), nullable=False),
            sa.Column("rule_id", sa.String(128), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "command_id",
                name="uq_standing_approval_command_receipt",
            ),
        )
    _ensure_operation_columns(bind)
    _ensure_rule_column_width(bind)
    _ensure_indexes(bind)


def downgrade() -> None:
    """存在规则或长期授权派发事实时拒绝丢弃不可恢复的治理证据。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("standing_approval_rules"):
        rule_count = int(
            bind.execute(sa.text("SELECT COUNT(*) FROM standing_approval_rules")).scalar_one()
        )
        if rule_count:
            raise RuntimeError("cannot downgrade standing approval rules with rule facts")
    operation_columns = {
        str(column["name"]) for column in inspector.get_columns("sop_operations")
    }
    if "authorization_source_type" in operation_columns:
        standing_count = int(
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM sop_operations "
                    "WHERE authorization_source_type = 'standing_rule'"
                )
            ).scalar_one()
        )
        if standing_count:
            raise RuntimeError("cannot downgrade standing approval rules with dispatch facts")
        operation_indexes = {
            str(item["name"])
            for item in sa.inspect(bind).get_indexes("sop_operations")
        }
        for index_name in (
            "ix_sop_operations_authorization_source_ref",
            "ix_sop_operations_authorization_source_type",
        ):
            if index_name in operation_indexes:
                op.drop_index(index_name, table_name="sop_operations")
        with op.batch_alter_table("sop_operations") as batch:
            batch.drop_column("authorization_source_ref")
            batch.drop_column("authorization_source_type")
    if inspector.has_table("standing_approval_command_receipts"):
        op.drop_table("standing_approval_command_receipts")
    if inspector.has_table("standing_approval_rules"):
        op.drop_table("standing_approval_rules")


def _ensure_operation_columns(bind: sa.Connection) -> None:
    """先以可空列回填历史授权来源，再收紧为非空，兼容 MySQL DDL 中断。"""

    columns = {
        str(column["name"]): column
        for column in sa.inspect(bind).get_columns("sop_operations")
    }
    if "authorization_source_type" not in columns:
        op.add_column(
            "sop_operations",
            sa.Column("authorization_source_type", sa.String(64), nullable=True),
        )
    if "authorization_source_ref" not in columns:
        op.add_column(
            "sop_operations",
            sa.Column("authorization_source_ref", sa.String(512), nullable=True),
        )
    bind.execute(
        sa.text(
            "UPDATE sop_operations SET authorization_source_type = "
            "CASE WHEN approval_work_item_id IS NOT NULL THEN 'attention' ELSE 'legacy' END "
            "WHERE authorization_source_type IS NULL"
        )
    )
    refreshed = {
        str(column["name"]): column
        for column in sa.inspect(bind).get_columns("sop_operations")
    }
    if bool(refreshed["authorization_source_type"].get("nullable")):
        with op.batch_alter_table("sop_operations") as batch:
            batch.alter_column(
                "authorization_source_type",
                existing_type=sa.String(64),
                nullable=False,
            )


def _ensure_rule_column_width(bind: sa.Connection) -> None:
    """修复 MySQL 非事务 DDL 中断后可能遗留的超宽 tool_id，避免复合索引越界。"""

    if bind.dialect.name != "mysql":
        return
    tool_column = next(
        column
        for column in sa.inspect(bind).get_columns("standing_approval_rules")
        if str(column["name"]) == "tool_id"
    )
    current_type = tool_column["type"]
    if int(getattr(current_type, "length", 0) or 0) <= 255:
        return
    op.alter_column(
        "standing_approval_rules",
        "tool_id",
        existing_type=current_type,
        type_=sa.String(255),
        existing_nullable=False,
    )


def _ensure_indexes(bind: sa.Connection) -> None:
    """幂等创建运行时匹配和审计查询需要的索引。"""

    definitions = {
        "standing_approval_rules": {
            "ix_standing_approval_rules_active_scope_key": ["active_scope_key"],
            "ix_standing_rules_active_lookup": [
                "tenant_id",
                "source_schedule_id",
                "agent_id",
                "status",
                "valid_from",
                "valid_to",
            ],
            "ix_standing_rules_target_lookup": [
                "tenant_id",
                "tool_id",
                "target_hash",
                "status",
            ],
        },
        "standing_approval_command_receipts": {
            "ix_standing_approval_command_receipts_tenant_id": ["tenant_id"],
            "ix_standing_approval_command_receipts_command_id": ["command_id"],
            "ix_standing_approval_command_receipts_rule_id": ["rule_id"],
        },
        "sop_operations": {
            "ix_sop_operations_authorization_source_type": ["authorization_source_type"],
            "ix_sop_operations_authorization_source_ref": ["authorization_source_ref"],
        },
    }
    for table, indexes in definitions.items():
        existing = {str(item["name"]) for item in sa.inspect(bind).get_indexes(table)}
        for name, columns in indexes.items():
            if name not in existing:
                op.create_index(name, table, columns)
