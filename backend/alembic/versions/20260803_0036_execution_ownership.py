"""
@Time       : 2026/08/03 19:25
@Author     : zhanglp8181
@File       : 20260803_0036_execution_ownership.py
@CallChain  : Alembic upgrade/downgrade → sop_instances → 统一 Execution 所有权与租约
@Description: 为执行实例增加 kind、活动槽、来源、取消请求、数据库租约和 fencing 字段及约束。

Revision ID: 20260803_0036
Revises: 20260802_0035
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0036"
down_revision: str | None = "20260802_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_STATUSES = frozenset({"created", "running", "waiting"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
CHECK_CONSTRAINTS = {
    "ck_execution_kind": "kind IN ('sop', 'dynamic_task')",
    "ck_execution_active_slot": (
        "((status IN ('created', 'running', 'waiting') AND active_slot_key IS NOT NULL) OR "
        "(status IN ('succeeded', 'failed', 'cancelled', 'timed_out') "
        "AND active_slot_key IS NULL))"
    ),
    "ck_execution_sop_identity": (
        "kind <> 'sop' OR (skill_id IS NOT NULL AND skill_version_id IS NOT NULL "
        "AND skill_version IS NOT NULL AND definition_checksum IS NOT NULL)"
    ),
    "ck_execution_lease_pair": (
        "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
        "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))"
    ),
    "ck_execution_fencing_nonnegative": "fencing_token >= 0",
    "ck_execution_cancellation_request": (
        "((cancellation_requested_at IS NULL AND cancellation_disposition = 'none') OR "
        "(cancellation_requested_at IS NOT NULL AND cancellation_disposition <> 'none'))"
    ),
}


def upgrade() -> None:
    """预检历史数据后，以可恢复的 expand/backfill/constraint 顺序扩展执行实例。"""

    bind = op.get_bind()
    _preflight_legacy_instances(bind)
    _add_missing_columns(bind)
    _backfill_execution_fields(bind)
    _make_required_columns_not_null(bind)
    _create_constraints(bind)
    _create_indexes(bind)
    _create_rejection_audit_table(bind)


def downgrade() -> None:
    """移除 B0.1 所有权字段和约束，同时保留原 SOP 实例数据。"""

    bind = op.get_bind()
    _preflight_downgrade_instances(bind)
    if sa.inspect(bind).has_table("execution_mutation_rejections"):
        op.drop_table("execution_mutation_rejections")
    index_names = _index_names(bind)
    for index_name in (
        "ix_sop_instances_tenant_lease_expiry",
        "ix_sop_instances_lease_owner",
    ):
        if index_name in index_names:
            op.drop_index(index_name, table_name="sop_instances")

    constraint_names = _constraint_names(bind)
    with op.batch_alter_table("sop_instances") as batch:
        if "uq_execution_tenant_active_slot" in constraint_names:
            batch.drop_constraint("uq_execution_tenant_active_slot", type_="unique")
        for name in CHECK_CONSTRAINTS:
            if name in constraint_names:
                batch.drop_constraint(name, type_="check")
        existing_columns = _column_names(bind)
        for column_name in (
            "fencing_token",
            "lease_heartbeat_at",
            "lease_acquired_at",
            "lease_expires_at",
            "lease_owner",
            "cancellation_disposition",
            "cancellation_reason",
            "cancellation_requested_by",
            "cancellation_requested_at",
            "source_ref",
            "source_kind",
            "initiator_user_id",
            "active_slot_key",
            "kind",
        ):
            if column_name in existing_columns:
                batch.drop_column(column_name)
        for column_name, column_type in (
            ("skill_id", sa.String(length=128)),
            ("skill_version_id", sa.String(length=128)),
            ("skill_version", sa.String(length=64)),
            ("definition_checksum", sa.String(length=64)),
        ):
            batch.alter_column(
                column_name,
                existing_type=column_type,
                nullable=False,
            )


def _preflight_legacy_instances(bind: sa.Connection) -> None:
    """拒绝未知状态、缺失 SOP 身份和同 tenant/session 的多活动历史行。"""

    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, session_id, status, skill_id, skill_version_id, "
            "skill_version, definition_checksum FROM sop_instances ORDER BY tenant_id, session_id, id"
        )
    ).mappings().all()
    unknown = [
        str(row["id"])
        for row in rows
        if str(row["status"]) not in ACTIVE_STATUSES | TERMINAL_STATUSES
    ]
    if unknown:
        raise RuntimeError(f"unknown execution statuses: {','.join(unknown)}")
    missing_identity = [
        str(row["id"])
        for row in rows
        if any(
            not str(row[field] or "").strip()
            for field in ("skill_id", "skill_version_id", "skill_version", "definition_checksum")
        )
    ]
    if missing_identity:
        raise RuntimeError(f"invalid SOP execution identities: {','.join(missing_identity)}")
    active_by_slot: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        status = str(row["status"])
        if status in ACTIVE_STATUSES:
            active_by_slot[(str(row["tenant_id"]), str(row["session_id"]))].append(
                (str(row["id"]), status)
            )
    conflicts = [entries for entries in active_by_slot.values() if len(entries) > 1]
    if conflicts:
        summary = ";".join(
            ",".join(f"{instance_id}:{status}" for instance_id, status in entries)
            for entries in conflicts
        )
        raise RuntimeError(f"duplicate active executions: {summary}")


def _add_missing_columns(bind: sa.Connection) -> None:
    """只增加当前 schema 尚不存在的列，支持 MySQL 非事务 DDL 中断后续跑。"""

    existing = _column_names(bind)
    columns = (
        sa.Column("kind", sa.String(length=64), nullable=True, server_default="sop"),
        sa.Column("active_slot_key", sa.String(length=512), nullable=True),
        sa.Column("initiator_user_id", sa.String(length=128), nullable=True),
        sa.Column("source_kind", sa.String(length=64), nullable=True, server_default="chat"),
        sa.Column("source_ref", sa.String(length=512), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(), nullable=True),
        sa.Column("cancellation_requested_by", sa.String(length=128), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=2000), nullable=True),
        sa.Column(
            "cancellation_disposition",
            sa.String(length=64),
            nullable=True,
            server_default="none",
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("lease_acquired_at", sa.DateTime(), nullable=True),
        sa.Column("lease_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True, server_default="0"),
    )
    missing = [column for column in columns if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table("sop_instances") as batch:
        for column in missing:
            batch.add_column(column)


def _backfill_execution_fields(bind: sa.Connection) -> None:
    """确定性回填历史 SOP，并按状态生成唯一活动槽，不推测历史发起人。"""

    rows = bind.execute(
        sa.text("SELECT id, session_id, status FROM sop_instances ORDER BY id")
    ).mappings().all()
    statement = sa.text(
        "UPDATE sop_instances SET kind = 'sop', source_kind = 'legacy', "
        "source_ref = COALESCE(source_ref, :source_ref), "
        "cancellation_disposition = COALESCE(cancellation_disposition, 'none'), "
        "fencing_token = COALESCE(fencing_token, 0), active_slot_key = :active_slot_key "
        "WHERE id = :id"
    )
    for row in rows:
        session_id = str(row["session_id"])
        bind.execute(
            statement,
            {
                "id": str(row["id"]),
                "source_ref": session_id,
                "active_slot_key": (
                    f"foreground:{session_id}"
                    if str(row["status"]) in ACTIVE_STATUSES
                    else None
                ),
            },
        )


def _make_required_columns_not_null(bind: sa.Connection) -> None:
    """按 kind 条件放宽身份列，并收紧所有执行共享的服务端必需字段。"""

    with op.batch_alter_table("sop_instances") as batch:
        for column_name, column_type in (
            ("skill_id", sa.String(length=128)),
            ("skill_version_id", sa.String(length=128)),
            ("skill_version", sa.String(length=64)),
            ("definition_checksum", sa.String(length=64)),
        ):
            batch.alter_column(
                column_name,
                existing_type=column_type,
                nullable=True,
            )
        batch.alter_column(
            "kind",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default="sop",
        )
        batch.alter_column(
            "source_kind",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default="chat",
        )
        batch.alter_column(
            "cancellation_disposition",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default="none",
        )
        batch.alter_column(
            "fencing_token",
            existing_type=sa.BigInteger(),
            nullable=False,
            server_default="0",
        )


def _create_constraints(bind: sa.Connection) -> None:
    """创建活动槽、kind、租约和取消请求的数据库级不变式。"""

    existing = _constraint_names(bind)
    with op.batch_alter_table("sop_instances") as batch:
        if "uq_execution_tenant_active_slot" not in existing:
            batch.create_unique_constraint(
                "uq_execution_tenant_active_slot",
                ["tenant_id", "active_slot_key"],
            )
        for name, condition in CHECK_CONSTRAINTS.items():
            if name not in existing:
                batch.create_check_constraint(name, condition)


def _create_indexes(bind: sa.Connection) -> None:
    """增加租约过期扫描和 owner 运维查询索引。"""

    existing = _index_names(bind)
    if "ix_sop_instances_tenant_lease_expiry" not in existing:
        op.create_index(
            "ix_sop_instances_tenant_lease_expiry",
            "sop_instances",
            ["tenant_id", "lease_expires_at"],
            unique=False,
        )
    if "ix_sop_instances_lease_owner" not in existing:
        op.create_index(
            "ix_sop_instances_lease_owner",
            "sop_instances",
            ["lease_owner"],
            unique=False,
        )


def _create_rejection_audit_table(bind: sa.Connection) -> None:
    """创建不含业务载荷的 fencing 拒绝隔离审计表。"""

    if sa.inspect(bind).has_table("execution_mutation_rejections"):
        return
    op.create_table(
        "execution_mutation_rejections",
        sa.Column("id", sa.String(length=512), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("rejected_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("current_lease_owner", sa.String(length=128), nullable=True),
        sa.Column("current_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column(
            "reason",
            sa.String(length=64),
            nullable=False,
            server_default="lease_or_fence_mismatch",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_mutation_rejection_lookup",
        "execution_mutation_rejections",
        ["tenant_id", "instance_id", "created_at"],
        unique=False,
    )


def _preflight_downgrade_instances(bind: sa.Connection) -> None:
    """拒绝把无法由 0035 表达的动态或无 SOP 身份执行静默降级。"""

    rows = bind.execute(
        sa.text(
            "SELECT id FROM sop_instances WHERE kind <> 'sop' OR skill_id IS NULL "
            "OR skill_version_id IS NULL OR skill_version IS NULL "
            "OR definition_checksum IS NULL ORDER BY id"
        )
    ).scalars().all()
    if rows:
        raise RuntimeError(
            "execution ownership downgrade requires SOP-only rows: "
            + ",".join(str(row_id) for row_id in rows)
        )


def _column_names(bind: sa.Connection) -> set[str]:
    """读取 sop_instances 当前列名。"""

    return {column["name"] for column in sa.inspect(bind).get_columns("sop_instances")}


def _index_names(bind: sa.Connection) -> set[str]:
    """读取 sop_instances 当前普通和唯一索引名。"""

    inspector = sa.inspect(bind)
    return {
        str(index["name"])
        for index in inspector.get_indexes("sop_instances")
        if index.get("name")
    }


def _constraint_names(bind: sa.Connection) -> set[str]:
    """读取 sop_instances 当前唯一与 CHECK 约束名。"""

    inspector = sa.inspect(bind)
    names = {
        str(item["name"])
        for item in inspector.get_unique_constraints("sop_instances")
        if item.get("name")
    }
    names.update(
        str(item["name"])
        for item in inspector.get_check_constraints("sop_instances")
        if item.get("name")
    )
    return names
