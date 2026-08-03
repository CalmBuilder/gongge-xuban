"""
@Time       : 2026/08/03 22:10
@Author     : zhanglp8181
@File       : 20260803_0037_operation_reliability.py
@CallChain  : Alembic upgrade/downgrade → SOP Operation → attempt/effect 可靠执行账本
@Description: 扩展逻辑动作幂等、严格请求指纹、远端幂等键、未知效果、对账与补偿历史。

Revision ID: 20260803_0037
Revises: 20260803_0036
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0037"
down_revision: str | None = "20260803_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPERATION_CHECKS = {
    "ck_sop_operation_status": (
        "status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')"
    ),
    "ck_sop_operation_effect_kind": (
        "effect_kind IN ('read', 'external_write', 'legacy_unknown')"
    ),
    "ck_sop_operation_effect_state": (
        "effect_state IN ('none', 'complete', 'unknown', 'compensated')"
    ),
}
INSTANCE_EFFECT_CHECK = (
    "ck_execution_effect_state",
    "effect_state IN ('none', 'partial', 'complete', 'unknown')",
)


def upgrade() -> None:
    """以 expand/backfill/constraint 顺序建立跨 attempt 的可靠 Operation 账本。"""

    bind = op.get_bind()
    _preflight_legacy_operations(bind)
    _add_missing_columns(bind)
    _backfill_operations(bind)
    _make_columns_required(bind)
    _create_constraints_and_indexes(bind)
    _create_attempt_table(bind)
    _create_effect_table(bind)
    _backfill_ledgers(bind)
    _backfill_instance_effect_state(bind)


def downgrade() -> None:
    """仅在账本仍是纯迁移生成数据时回退，拒绝丢弃真实 attempt、对账或补偿历史。"""

    bind = op.get_bind()
    _preflight_downgrade(bind)
    if sa.inspect(bind).has_table("sop_operation_effects"):
        op.drop_table("sop_operation_effects")
    if sa.inspect(bind).has_table("sop_operation_attempts"):
        op.drop_table("sop_operation_attempts")

    operation_indexes = _index_names(bind, "sop_operations")
    for name in (
        "ix_sop_operations_logical_action_id",
        "ix_sop_operations_remote_idempotency_key",
        "ix_sop_operations_compensates_operation_id",
    ):
        if name in operation_indexes:
            op.drop_index(name, table_name="sop_operations")
    operation_constraints = _constraint_names(bind, "sop_operations")
    with op.batch_alter_table("sop_operations") as batch:
        if "uq_sop_operation_tenant_logical_action" in operation_constraints:
            batch.drop_constraint("uq_sop_operation_tenant_logical_action", type_="unique")
        for name in OPERATION_CHECKS:
            if name in operation_constraints:
                batch.drop_constraint(name, type_="check")
        existing = _column_names(bind, "sop_operations")
        for name in (
            "reconciled_at",
            "compensates_operation_id",
            "cancellation_disposition",
            "effect_state",
            "effect_kind",
            "idempotency_key_fields_json",
            "idempotency_scope",
            "idempotency_required",
            "remote_idempotency_key",
            "request_fingerprint",
            "logical_action_id",
        ):
            if name in existing:
                batch.drop_column(name)

    instance_constraints = _constraint_names(bind, "sop_instances")
    with op.batch_alter_table("sop_instances") as batch:
        if INSTANCE_EFFECT_CHECK[0] in instance_constraints:
            batch.drop_constraint(INSTANCE_EFFECT_CHECK[0], type_="check")
        if "effect_state" in _column_names(bind, "sop_instances"):
            batch.drop_column("effect_state")


def _preflight_legacy_operations(bind: sa.Connection) -> None:
    """验证旧状态和请求都是可无损规范化的 JSON，禁止迁移时猜测损坏数据。"""

    if not sa.inspect(bind).has_table("sop_operations"):
        raise RuntimeError("sop_operations table is required for operation reliability migration")
    rows = bind.execute(
        sa.text("SELECT id, status, request_json FROM sop_operations ORDER BY id")
    ).mappings()
    allowed = {"prepared", "running", "succeeded", "failed", "unknown"}
    invalid: list[str] = []
    for row in rows:
        try:
            request = _decode_json(row["request_json"])
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
            _canonical_hash(request)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid.append(str(row["id"]))
            continue
        if str(row["status"]) not in allowed:
            invalid.append(str(row["id"]))
    if invalid:
        raise RuntimeError("unmappable legacy operations: " + ",".join(invalid))


def _add_missing_columns(bind: sa.Connection) -> None:
    """逐列扩展实例与操作，允许 MySQL 在非事务 DDL 中断后安全续跑。"""

    instance_columns = _column_names(bind, "sop_instances")
    if "effect_state" not in instance_columns:
        with op.batch_alter_table("sop_instances") as batch:
            batch.add_column(
                sa.Column("effect_state", sa.String(64), nullable=True, server_default="none")
            )

    operation_columns = _column_names(bind, "sop_operations")
    additions = (
        sa.Column("logical_action_id", sa.String(128), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("remote_idempotency_key", sa.String(128), nullable=True),
        sa.Column("idempotency_required", sa.Boolean(), nullable=True),
        sa.Column("idempotency_scope", sa.String(64), nullable=True),
        sa.Column("idempotency_key_fields_json", sa.JSON(), nullable=True),
        sa.Column("effect_kind", sa.String(64), nullable=True),
        sa.Column("effect_state", sa.String(64), nullable=True),
        sa.Column("cancellation_disposition", sa.String(64), nullable=True),
        sa.Column("compensates_operation_id", sa.String(512), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
    )
    missing = [column for column in additions if column.name not in operation_columns]
    if missing:
        with op.batch_alter_table("sop_operations") as batch:
            for column in missing:
                batch.add_column(column)


def _backfill_operations(bind: sa.Connection) -> None:
    """为旧操作生成保守逻辑身份、严格请求指纹和效果分类，不伪造远端幂等事实。"""

    metadata = sa.MetaData()
    operations = sa.Table("sop_operations", metadata, autoload_with=bind)
    rows = bind.execute(sa.select(operations).order_by(operations.c.id)).mappings().all()
    for row in rows:
        request = _decode_json(row["request_json"])
        status = str(row["status"])
        effect_kind = _legacy_effect_kind(str(row["operation_name"]), request)
        effect_state = _legacy_effect_state(effect_kind, status)
        bind.execute(
            operations.update()
            .where(operations.c.id == row["id"])
            .values(
                logical_action_id=f"legacy:{_sha256(str(row['id']))}",
                request_fingerprint=_canonical_hash(request),
                remote_idempotency_key=None,
                idempotency_required=True,
                idempotency_scope="instance",
                idempotency_key_fields_json=[],
                effect_kind=effect_kind,
                effect_state=effect_state,
                cancellation_disposition="none",
            )
        )
    bind.execute(
        sa.text("UPDATE sop_instances SET effect_state = COALESCE(effect_state, 'none')")
    )


def _make_columns_required(bind: sa.Connection) -> None:
    """完成回填后收紧服务端必需列，避免新写入绕过可靠执行契约。"""

    with op.batch_alter_table("sop_instances") as batch:
        batch.alter_column(
            "effect_state",
            existing_type=sa.String(64),
            nullable=False,
            server_default="none",
        )
    with op.batch_alter_table("sop_operations") as batch:
        for name, column_type, default in (
            ("logical_action_id", sa.String(128), None),
            ("request_fingerprint", sa.String(64), None),
            ("idempotency_required", sa.Boolean(), "1"),
            ("idempotency_scope", sa.String(64), "instance"),
            ("idempotency_key_fields_json", sa.JSON(), None),
            ("effect_kind", sa.String(64), "read"),
            ("effect_state", sa.String(64), "none"),
            ("cancellation_disposition", sa.String(64), "none"),
        ):
            batch.alter_column(
                name,
                existing_type=column_type,
                nullable=False,
                server_default=default,
            )


def _create_constraints_and_indexes(bind: sa.Connection) -> None:
    """建立逻辑动作唯一性、状态不变式和查询索引。"""

    instance_constraints = _constraint_names(bind, "sop_instances")
    if INSTANCE_EFFECT_CHECK[0] not in instance_constraints:
        with op.batch_alter_table("sop_instances") as batch:
            batch.create_check_constraint(*INSTANCE_EFFECT_CHECK)

    operation_constraints = _constraint_names(bind, "sop_operations")
    with op.batch_alter_table("sop_operations") as batch:
        if "uq_sop_operation_tenant_logical_action" not in operation_constraints:
            batch.create_unique_constraint(
                "uq_sop_operation_tenant_logical_action",
                ["tenant_id", "logical_action_id"],
            )
        for name, condition in OPERATION_CHECKS.items():
            if name not in operation_constraints:
                batch.create_check_constraint(name, condition)
    indexes = _index_names(bind, "sop_operations")
    for name, columns in (
        ("ix_sop_operations_logical_action_id", ["logical_action_id"]),
        ("ix_sop_operations_remote_idempotency_key", ["remote_idempotency_key"]),
        ("ix_sop_operations_compensates_operation_id", ["compensates_operation_id"]),
    ):
        if name not in indexes:
            op.create_index(name, "sop_operations", columns, unique=False)


def _create_attempt_table(bind: sa.Connection) -> None:
    """创建逻辑动作的追加式本地 dispatch attempt 表。"""

    if sa.inspect(bind).has_table("sop_operation_attempts"):
        return
    op.create_table(
        "sop_operation_attempts",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("operation_id", sa.String(512), nullable=False),
        sa.Column("node_execution_id", sa.String(128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", "node_execution_id",
            name="uq_sop_operation_attempt_execution",
        ),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", "attempt_number",
            name="uq_sop_operation_attempt_number",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown', "
            "'cancelled', 'reused')",
            name="ck_sop_operation_attempt_status",
        ),
    )
    _create_single_column_indexes(
        "sop_operation_attempts",
        ("tenant_id", "instance_id", "operation_id", "node_execution_id", "status"),
    )


def _create_effect_table(bind: sa.Connection) -> None:
    """创建追加式外部效果事实和补偿 lineage 表。"""

    if sa.inspect(bind).has_table("sop_operation_effects"):
        return
    op.create_table(
        "sop_operation_effects",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("operation_id", sa.String(512), nullable=False),
        sa.Column("logical_action_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("effect_state", sa.String(64), nullable=False),
        sa.Column("external_reference", sa.String(128), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("compensation_operation_id", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", "sequence",
            name="uq_sop_operation_effect_sequence",
        ),
        sa.CheckConstraint(
            "effect_state IN ('none', 'complete', 'unknown', 'compensated')",
            name="ck_sop_operation_effect_record_state",
        ),
    )
    _create_single_column_indexes(
        "sop_operation_effects",
        ("tenant_id", "instance_id", "operation_id", "logical_action_id", "event_type"),
    )


def _backfill_ledgers(bind: sa.Connection) -> None:
    """为每条旧操作建立一个 legacy attempt，并仅对确定或未知外部效果补历史事实。"""

    metadata = sa.MetaData()
    operations = sa.Table("sop_operations", metadata, autoload_with=bind)
    attempts = sa.Table("sop_operation_attempts", metadata, autoload_with=bind)
    effects = sa.Table("sop_operation_effects", metadata, autoload_with=bind)
    existing_attempts = set(bind.execute(sa.select(attempts.c.operation_id)).scalars())
    existing_effects = set(bind.execute(sa.select(effects.c.operation_id)).scalars())
    for row in bind.execute(sa.select(operations).order_by(operations.c.id)).mappings():
        operation_id = str(row["id"])
        if operation_id not in existing_attempts:
            bind.execute(
                attempts.insert().values(
                    id=f"legacyattempt:{_sha256(operation_id)}",
                    tenant_id=row["tenant_id"],
                    instance_id=row["instance_id"],
                    operation_id=operation_id,
                    node_execution_id=row["node_execution_id"],
                    attempt_number=1,
                    status=row["status"],
                    error_json=_decode_json(row["error_json"]),
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        if operation_id not in existing_effects and row["effect_state"] in {"complete", "unknown"}:
            bind.execute(
                effects.insert().values(
                    id=f"legacyeffect:{_sha256(operation_id)}",
                    tenant_id=row["tenant_id"],
                    instance_id=row["instance_id"],
                    operation_id=operation_id,
                    logical_action_id=row["logical_action_id"],
                    sequence=1,
                    event_type=(
                        "legacy_effect_confirmed"
                        if row["effect_state"] == "complete"
                        else "legacy_effect_unknown"
                    ),
                    effect_state=row["effect_state"],
                    external_reference=row["external_reference"],
                    evidence_json={"migration_revision": revision},
                    compensation_operation_id=None,
                    created_at=row["updated_at"],
                )
            )


def _backfill_instance_effect_state(bind: sa.Connection) -> None:
    """从旧外部写操作汇总实例效果，不把读取成功误当作业务副作用。"""

    metadata = sa.MetaData()
    instances = sa.Table("sop_instances", metadata, autoload_with=bind)
    operations = sa.Table("sop_operations", metadata, autoload_with=bind)
    instance_ids = bind.execute(sa.select(instances.c.id)).scalars().all()
    for instance_id in instance_ids:
        states = bind.execute(
            sa.select(operations.c.effect_state).where(
                operations.c.instance_id == instance_id,
                operations.c.effect_kind == "external_write",
            )
        ).scalars().all()
        if "unknown" in states:
            aggregate = "unknown"
        else:
            completed = sum(state in {"complete", "compensated"} for state in states)
            aggregate = (
                "none" if completed == 0
                else "complete" if completed == len(states)
                else "partial"
            )
        bind.execute(
            instances.update().where(instances.c.id == instance_id).values(effect_state=aggregate)
        )


def _preflight_downgrade(bind: sa.Connection) -> None:
    """拒绝丢弃 0037 后产生的真实重试、取消、对账、补偿和效果证据。"""

    if not sa.inspect(bind).has_table("sop_operation_attempts"):
        return
    unsafe_attempts = bind.execute(
        sa.text(
            "SELECT id FROM sop_operation_attempts WHERE id NOT LIKE 'legacyattempt:%' "
            "OR attempt_number <> 1 ORDER BY id"
        )
    ).scalars().all()
    unsafe_effects: list[Any] = []
    if sa.inspect(bind).has_table("sop_operation_effects"):
        unsafe_effects = bind.execute(
            sa.text(
                "SELECT id FROM sop_operation_effects WHERE id NOT LIKE 'legacyeffect:%' "
                "OR event_type NOT IN ('legacy_effect_confirmed', 'legacy_effect_unknown') "
                "ORDER BY id"
            )
        ).scalars().all()
    unsafe_operations = bind.execute(
        sa.text(
            "SELECT id FROM sop_operations WHERE logical_action_id NOT LIKE 'legacy:%' "
            "OR status = 'cancelled' OR reconciled_at IS NOT NULL "
            "OR compensates_operation_id IS NOT NULL ORDER BY id"
        )
    ).scalars().all()
    unsafe = [*unsafe_attempts, *unsafe_effects, *unsafe_operations]
    if unsafe:
        raise RuntimeError(
            "operation reliability downgrade would discard managed history: "
            + ",".join(str(item) for item in unsafe)
        )


def _legacy_effect_kind(operation_name: str, request: Mapping[str, object]) -> str:
    """保守识别已知读取；其余旧工具按外部写处理，避免漏报潜在副作用。"""

    method = str(request.get("method", "")).upper()
    if operation_name == "knowledge.search" or method == "GET":
        return "read"
    return "external_write"


def _legacy_effect_state(effect_kind: str, status: str) -> str:
    """从旧生命周期保守映射效果事实，运行中外部写一律视为 unknown。"""

    if effect_kind != "external_write":
        return "none"
    if status == "succeeded":
        return "complete"
    if status in {"running", "unknown"}:
        return "unknown"
    return "none"


def _canonical_hash(value: object) -> str:
    """验证并规范化严格 JSON，然后生成可跨方言复算的 SHA-256。"""

    _validate_json(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_json(value: object) -> None:
    """递归拒绝 RFC 8259 数据模型之外的键、容器和值。"""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("non-finite JSON number")
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("non-string JSON key")
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("non-JSON value")


def _decode_json(value: object) -> object:
    """兼容方言驱动已解码对象和 SQLite 原始 JSON 文本。"""

    if isinstance(value, str):
        return json.loads(value)
    return value


def _sha256(value: str) -> str:
    """对稳定文本生成小写十六进制摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_single_column_indexes(table_name: str, column_names: tuple[str, ...]) -> None:
    """按 SQLModel 约定为账本检索字段创建普通单列索引。"""

    for column_name in column_names:
        op.create_index(
            f"ix_{table_name}_{column_name}",
            table_name,
            [column_name],
            unique=False,
        )


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """读取指定表的当前列名。"""

    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    """读取指定表的普通和唯一索引名。"""

    return {
        str(index["name"])
        for index in sa.inspect(bind).get_indexes(table_name)
        if index.get("name")
    }


def _constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """读取指定表的唯一约束与 CHECK 约束名。"""

    inspector = sa.inspect(bind)
    names = {
        str(item["name"])
        for item in inspector.get_unique_constraints(table_name)
        if item.get("name")
    }
    names.update(
        str(item["name"])
        for item in inspector.get_check_constraints(table_name)
        if item.get("name")
    )
    return names
