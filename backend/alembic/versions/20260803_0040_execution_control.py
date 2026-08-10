"""
@Time       : 2026/08/03 20:05
@Author     : zhanglp8181
@File       : 20260803_0040_execution_control.py
@CallChain  : Alembic upgrade/downgrade → Attention/Command/Signal/Result/Publication/Outbox
@Description: 扩展统一控制平面、结果发布和版本化领域事件的持久契约。

Revision ID: 20260803_0040
Revises: 20260803_0039
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0040"
down_revision: str | None = "20260803_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_TABLES = (
    "execution_commands",
    "execution_signals",
    "execution_results",
    "execution_publications",
    "event_outbox",
)
WORK_ITEM_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("attention_kind", sa.String(64), nullable=True),
    sa.Column("attention_key", sa.String(512), nullable=True),
    sa.Column("attention_identity", sa.String(64), nullable=True),
    sa.Column("title", sa.String(191), nullable=True),
    sa.Column("source_type", sa.String(64), nullable=True),
    sa.Column("source_ref", sa.String(512), nullable=True),
    sa.Column("payload_json", sa.JSON(), nullable=True),
    sa.Column("allowed_commands_json", sa.JSON(), nullable=True),
    sa.Column("resolution_json", sa.JSON(), nullable=True),
    sa.Column("required", sa.Boolean(), nullable=True),
)
EVENT_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("schema_version", sa.Integer(), nullable=True),
    sa.Column("aggregate_type", sa.String(64), nullable=True),
    sa.Column("aggregate_id", sa.String(512), nullable=True),
    sa.Column("aggregate_revision", sa.Integer(), nullable=True),
    sa.Column("correlation_id", sa.String(512), nullable=True),
    sa.Column("causation_id", sa.String(512), nullable=True),
    sa.Column("payload_checksum", sa.String(64), nullable=True),
)


def upgrade() -> None:
    """按 expand、确定性回填、约束收紧、新表和索引顺序执行可续跑升级。"""

    bind = op.get_bind()
    _require_tables(bind)
    _add_columns(bind, "sop_instances", (sa.Column("current_result_id", sa.String(512)),))
    _add_columns(bind, "sop_work_items", WORK_ITEM_COLUMNS)
    _add_columns(bind, "agent_events", EVENT_COLUMNS)
    _backfill(bind)
    _alter_existing_tables(bind)
    _create_tables(bind)
    _create_indexes(bind)


def downgrade() -> None:
    """仅在没有新控制、Attention 和结果事实时允许回退到 0039。"""

    bind = op.get_bind()
    _preflight_downgrade(bind)
    for table_name in reversed(NEW_TABLES):
        if sa.inspect(bind).has_table(table_name):
            op.drop_table(table_name)
    _drop_indexes(bind)
    constraints = _constraint_names(bind, "sop_work_items")
    for name, kind in (
        ("uq_attention_execution_identity", "unique"),
        ("ck_attention_sop_identity", "check"),
        ("ck_attention_kind", "check"),
    ):
        if name in constraints:
            with op.batch_alter_table("sop_work_items") as batch:
                batch.drop_constraint(name, type_=kind)
    event_constraints = _constraint_names(bind, "agent_events")
    for name in (
        "ck_agent_event_aggregate_revision",
        "ck_agent_event_schema_version",
    ):
        if name in event_constraints:
            with op.batch_alter_table("agent_events") as batch:
                batch.drop_constraint(name, type_="check")
    with op.batch_alter_table("sop_work_items") as batch:
        batch.alter_column("node_execution_id", existing_type=sa.String(128), nullable=False)
        batch.alter_column("skill_version_id", existing_type=sa.String(128), nullable=False)
        batch.alter_column("node_id", existing_type=sa.String(128), nullable=False)
    _drop_columns(bind, "agent_events", EVENT_COLUMNS)
    _drop_columns(bind, "sop_work_items", WORK_ITEM_COLUMNS)
    _drop_columns(
        bind,
        "sop_instances",
        (sa.Column("current_result_id", sa.String(512)),),
    )


def _require_tables(bind: sa.Connection) -> None:
    """在任何 DDL 前拒绝缺少 0039 核心表的错误基线。"""

    required = {"sop_instances", "sop_work_items", "agent_events"}
    missing = required - set(sa.inspect(bind).get_table_names())
    if missing:
        raise RuntimeError(f"execution control migration requires tables: {sorted(missing)}")


def _add_columns(
    bind: sa.Connection,
    table_name: str,
    columns: tuple[sa.Column[object], ...],
) -> None:
    """只扩展缺失列，允许 MySQL 非事务 DDL 中断后安全续跑。"""

    existing = _column_names(bind, table_name)
    missing = [column for column in columns if column.name not in existing]
    if missing:
        with op.batch_alter_table(table_name) as batch:
            for column in missing:
                batch.add_column(column)


def _backfill(bind: sa.Connection) -> None:
    """为历史 SOP 工作项和事件写入可验证默认值及稳定 Attention 身份。"""

    work_items = sa.Table("sop_work_items", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.select(
            work_items.c.id,
            work_items.c.tenant_id,
            work_items.c.instance_id,
            work_items.c.node_execution_id,
            work_items.c.attention_identity,
        )
    ).mappings()
    for row in rows:
        identity = row["attention_identity"]
        if not identity:
            identity = _attention_identity(
                tenant_id=str(row["tenant_id"]),
                execution_id=str(row["instance_id"]),
                attention_key=f'sop-node:{row["node_execution_id"]}',
            )
        bind.execute(
            work_items.update()
            .where(work_items.c.id == row["id"])
            .values(
                attention_kind="sop_human_task",
                attention_key=f'sop-node:{row["node_execution_id"]}',
                attention_identity=identity,
                source_type="formal_sop",
                payload_json={},
                allowed_commands_json=["claim", "unclaim", "complete"],
                resolution_json={},
                required=True,
            )
        )
    events = sa.Table("agent_events", sa.MetaData(), autoload_with=bind)
    bind.execute(events.update().where(events.c.schema_version.is_(None)).values(schema_version=1))


def _alter_existing_tables(bind: sa.Connection) -> None:
    """收紧已回填列并允许通用 Attention 不携带正式 SOP 节点身份。"""

    with op.batch_alter_table("sop_work_items") as batch:
        batch.alter_column("node_execution_id", existing_type=sa.String(128), nullable=True)
        batch.alter_column("skill_version_id", existing_type=sa.String(128), nullable=True)
        batch.alter_column("node_id", existing_type=sa.String(128), nullable=True)
        batch.alter_column(
            "attention_kind",
            existing_type=sa.String(64),
            nullable=False,
            server_default="sop_human_task",
        )
        batch.alter_column("attention_identity", existing_type=sa.String(64), nullable=False)
        batch.alter_column(
            "source_type",
            existing_type=sa.String(64),
            nullable=False,
            server_default="runtime",
        )
        for column_name in ("payload_json", "allowed_commands_json", "resolution_json"):
            batch.alter_column(column_name, existing_type=sa.JSON(), nullable=False)
        batch.alter_column(
            "required",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        )
    constraints = _constraint_names(bind, "sop_work_items")
    definitions = (
        (
            "ck_attention_kind",
            "attention_kind IN ('sop_human_task', 'clarification', 'plan_approval', "
            "'tool_approval', 'reauth', 'exception', 'publication', 'result_review')",
        ),
        (
            "ck_attention_sop_identity",
            "attention_kind <> 'sop_human_task' OR (node_execution_id IS NOT NULL AND "
            "skill_version_id IS NOT NULL AND node_id IS NOT NULL)",
        ),
    )
    for name, condition in definitions:
        if name not in constraints:
            with op.batch_alter_table("sop_work_items") as batch:
                batch.create_check_constraint(name, condition)
    if "uq_attention_execution_identity" not in constraints:
        with op.batch_alter_table("sop_work_items") as batch:
            batch.create_unique_constraint(
                "uq_attention_execution_identity",
                ["tenant_id", "instance_id", "attention_identity"],
            )
    with op.batch_alter_table("agent_events") as batch:
        batch.alter_column(
            "schema_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
        if "ck_agent_event_schema_version" not in _constraint_names(bind, "agent_events"):
            batch.create_check_constraint(
                "ck_agent_event_schema_version",
                "schema_version >= 1",
            )
        if "ck_agent_event_aggregate_revision" not in _constraint_names(
            bind, "agent_events"
        ):
            batch.create_check_constraint(
                "ck_agent_event_aggregate_revision",
                "aggregate_revision IS NULL OR aggregate_revision >= 0",
            )


def _create_tables(bind: sa.Connection) -> None:
    """创建五张控制事实表，并拒绝静默接受缺列或缺约束的半成品表。"""

    for table_name, columns, constraints in _table_definitions():
        if sa.inspect(bind).has_table(table_name):
            missing = {str(column.name) for column in columns} - _column_names(bind, table_name)
            if missing:
                raise RuntimeError(f"partial {table_name} table is missing: {sorted(missing)}")
            expected_constraints = {
                str(constraint.name)
                for constraint in constraints
                if getattr(constraint, "name", None)
            }
            missing_constraints = expected_constraints - _constraint_names(bind, table_name)
            if missing_constraints:
                raise RuntimeError(
                    f"partial {table_name} table is missing constraints: "
                    f"{sorted(missing_constraints)}"
                )
            continue
        op.create_table(table_name, *columns, *constraints)


def _table_definitions() -> tuple[
    tuple[str, tuple[sa.Column[object], ...], tuple[sa.SchemaItem, ...]], ...
]:
    """返回与 SQLModel 契约一致且兼容 SQLite/MySQL 的控制表定义。"""

    dt = sa.DateTime()
    json_type = sa.JSON()
    return (
        (
            "execution_commands",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("command_id", sa.String(128), nullable=False),
                sa.Column("command_type", sa.String(64), nullable=False),
                sa.Column("actor_user_id", sa.String(512)),
                sa.Column("source_type", sa.String(64), nullable=False),
                sa.Column("source_message_id", sa.String(512)),
                sa.Column("expected_execution_revision", sa.Integer(), nullable=False),
                sa.Column("payload_json", json_type, nullable=False),
                sa.Column("payload_checksum", sa.String(64), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("result_plan_revision_id", sa.String(512)),
                sa.Column("result_json", json_type, nullable=False),
                sa.Column("reason_code", sa.String(128)),
                sa.Column("claimed_by", sa.String(128)),
                sa.Column("claimed_fencing_token", sa.Integer()),
                sa.Column("issued_at", dt, nullable=False),
                sa.Column("claimed_at", dt),
                sa.Column("consumed_at", dt),
                sa.Column("created_at", dt, nullable=False),
                sa.Column("updated_at", dt, nullable=False),
            ),
            (
                sa.UniqueConstraint("tenant_id", "command_id", name="uq_execution_command_id"),
                sa.CheckConstraint("command_type IN ('cancel', 'steer')", name="ck_execution_command_type"),
                sa.CheckConstraint(
                    "status IN ('pending', 'claimed', 'applied', 'conflicted', 'rejected')",
                    name="ck_execution_command_status",
                ),
                sa.CheckConstraint(
                    "expected_execution_revision >= 0 AND "
                    "(claimed_fencing_token IS NULL OR claimed_fencing_token >= 0)",
                    name="ck_execution_command_revisions",
                ),
            ),
        ),
        (
            "execution_signals",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("signal_type", sa.String(64), nullable=False),
                sa.Column("dedupe_key", sa.String(64), nullable=False),
                sa.Column("causation_type", sa.String(64), nullable=False),
                sa.Column("causation_id", sa.String(512), nullable=False),
                sa.Column("payload_json", json_type, nullable=False),
                sa.Column("payload_checksum", sa.String(64), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("priority", sa.Integer(), nullable=False),
                sa.Column("attempt_count", sa.Integer(), nullable=False),
                sa.Column("max_attempts", sa.Integer(), nullable=False),
                sa.Column("available_at", dt, nullable=False),
                sa.Column("lease_owner", sa.String(128)),
                sa.Column("lease_expires_at", dt),
                sa.Column("claimed_at", dt),
                sa.Column("consumed_at", dt),
                sa.Column("last_error_json", json_type, nullable=False),
                sa.Column("created_at", dt, nullable=False),
                sa.Column("updated_at", dt, nullable=False),
            ),
            (
                sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_execution_signal_dedupe"),
                sa.CheckConstraint(
                    "signal_type IN ('command', 'attention_decided', 'timer', "
                    "'operation_settled', 'external_event', 'publication_retry')",
                    name="ck_execution_signal_type",
                ),
                sa.CheckConstraint(
                    "status IN ('pending', 'claimed', 'consumed', 'dead_letter', 'discarded')",
                    name="ck_execution_signal_status",
                ),
                sa.CheckConstraint(
                    "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
                    "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
                    name="ck_execution_signal_lease_pair",
                ),
                sa.CheckConstraint(
                    "attempt_count >= 0 AND max_attempts >= 1",
                    name="ck_execution_signal_attempts",
                ),
            ),
        ),
        (
            "execution_results",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("result_revision", sa.Integer(), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("result_json", json_type, nullable=False),
                sa.Column("verification_json", json_type, nullable=False),
                sa.Column("checksum", sa.String(64), nullable=False),
                sa.Column("created_by_step_key", sa.String(128)),
                sa.Column("created_at", dt, nullable=False),
            ),
            (
                sa.UniqueConstraint(
                    "tenant_id", "execution_id", "result_revision",
                    name="uq_execution_result_revision",
                ),
                sa.UniqueConstraint(
                    "tenant_id", "execution_id", "checksum",
                    name="uq_execution_result_checksum",
                ),
                sa.CheckConstraint("status IN ('verified', 'rejected')", name="ck_execution_result_status"),
                sa.CheckConstraint(
                    "result_revision >= 1",
                    name="ck_execution_result_revision",
                ),
            ),
        ),
        (
            "execution_publications",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("result_id", sa.String(512), nullable=False),
                sa.Column("publication_key", sa.String(64), nullable=False),
                sa.Column("target_type", sa.String(64), nullable=False),
                sa.Column("target_ref", sa.String(512)),
                sa.Column("required", sa.Boolean(), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("operation_id", sa.String(512)),
                sa.Column("outbox_id", sa.String(512)),
                sa.Column("attempt_count", sa.Integer(), nullable=False),
                sa.Column("receipt_json", json_type, nullable=False),
                sa.Column("error_json", json_type, nullable=False),
                sa.Column("settled_at", dt),
                sa.Column("created_at", dt, nullable=False),
                sa.Column("updated_at", dt, nullable=False),
            ),
            (
                sa.UniqueConstraint("tenant_id", "publication_key", name="uq_execution_publication_key"),
                sa.CheckConstraint(
                    "target_type IN ('application', 'external_thread', 'webhook')",
                    name="ck_execution_publication_target",
                ),
                sa.CheckConstraint(
                    "status IN ('pending', 'delivering', 'settled', 'unknown', "
                    "'dead_letter', 'skipped')",
                    name="ck_execution_publication_status",
                ),
                sa.CheckConstraint(
                    "attempt_count >= 0",
                    name="ck_execution_publication_attempts",
                ),
            ),
        ),
        (
            "event_outbox",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("event_id", sa.String(512), nullable=False),
                sa.Column("publication_key", sa.String(64), nullable=False),
                sa.Column("destination", sa.String(64), nullable=False),
                sa.Column("payload_json", json_type, nullable=False),
                sa.Column("payload_checksum", sa.String(64), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("attempt_count", sa.Integer(), nullable=False),
                sa.Column("max_attempts", sa.Integer(), nullable=False),
                sa.Column("available_at", dt, nullable=False),
                sa.Column("lease_owner", sa.String(128)),
                sa.Column("lease_expires_at", dt),
                sa.Column("last_error_json", json_type, nullable=False),
                sa.Column("delivered_at", dt),
                sa.Column("created_at", dt, nullable=False),
                sa.Column("updated_at", dt, nullable=False),
            ),
            (
                sa.UniqueConstraint("tenant_id", "publication_key", name="uq_event_outbox_key"),
                sa.CheckConstraint(
                    "status IN ('pending', 'delivering', 'delivered', 'dead_letter')",
                    name="ck_event_outbox_status",
                ),
                sa.CheckConstraint(
                    "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
                    "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
                    name="ck_event_outbox_lease_pair",
                ),
                sa.CheckConstraint(
                    "attempt_count >= 0 AND max_attempts >= 1",
                    name="ck_event_outbox_attempts",
                ),
            ),
        ),
    )


def _create_indexes(bind: sa.Connection) -> None:
    """为租户查询、worker 扫描和结果追踪建立稳定索引。"""

    definitions = (
        ("sop_instances", "ix_sop_instances_current_result_id", ("current_result_id",)),
        ("sop_work_items", "ix_sop_work_items_attention_kind", ("attention_kind",)),
        ("sop_work_items", "ix_sop_work_items_attention_identity", ("attention_identity",)),
        ("agent_events", "ix_agent_events_aggregate", ("tenant_id", "aggregate_type", "aggregate_id")),
        ("execution_commands", "ix_execution_commands_execution_status", ("tenant_id", "execution_id", "status")),
        ("execution_signals", "ix_execution_signals_available", ("status", "available_at", "priority")),
        ("execution_signals", "ix_execution_signals_execution", ("tenant_id", "execution_id", "status")),
        ("execution_results", "ix_execution_results_execution", ("tenant_id", "execution_id")),
        ("execution_publications", "ix_execution_publications_execution", ("tenant_id", "execution_id", "status")),
        ("event_outbox", "ix_event_outbox_available", ("status", "available_at")),
    )
    for table_name, index_name, columns in definitions:
        if index_name not in _index_names(bind, table_name):
            op.create_index(index_name, table_name, list(columns), unique=False)


def _drop_indexes(bind: sa.Connection) -> None:
    """删除既有表上的 B0.5 索引；新表索引随表一并删除。"""

    for table_name, index_name in (
        ("agent_events", "ix_agent_events_aggregate"),
        ("sop_work_items", "ix_sop_work_items_attention_identity"),
        ("sop_work_items", "ix_sop_work_items_attention_kind"),
        ("sop_instances", "ix_sop_instances_current_result_id"),
    ):
        if index_name in _index_names(bind, table_name):
            op.drop_index(index_name, table_name=table_name)


def _preflight_downgrade(bind: sa.Connection) -> None:
    """拒绝丢弃任何控制事实或非正式 SOP Attention。"""

    for table_name in NEW_TABLES:
        if sa.inspect(bind).has_table(table_name):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            if count:
                raise RuntimeError(f"cannot downgrade with {table_name} facts")
    if "attention_kind" in _column_names(bind, "sop_work_items"):
        count = bind.execute(
            sa.text("SELECT COUNT(*) FROM sop_work_items WHERE attention_kind <> 'sop_human_task'")
        ).scalar_one()
        if count:
            raise RuntimeError("cannot downgrade with generic attention facts")


def _drop_columns(
    bind: sa.Connection,
    table_name: str,
    columns: tuple[sa.Column[object], ...],
) -> None:
    """以 batch 模式删除仍存在的列，兼容 SQLite 表重建。"""

    existing = _column_names(bind, table_name)
    present = [str(column.name) for column in columns if column.name in existing]
    if present:
        with op.batch_alter_table(table_name) as batch:
            for column_name in present:
                batch.drop_column(column_name)


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """读取指定表的当前列名。"""

    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


def _attention_identity(*, tenant_id: str, execution_id: str, attention_key: str) -> str:
    """按 Runtime 同一规范为历史工作项派生 Attention identity。"""

    encoded = json.dumps(
        {
            "tenant_id": tenant_id,
            "execution_id": execution_id,
            "attention_key": attention_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """合并 unique 与 check 约束名，供可续跑判断。"""

    inspector = sa.inspect(bind)
    return {
        str(item["name"])
        for item in (
            inspector.get_unique_constraints(table_name)
            + inspector.get_check_constraints(table_name)
        )
        if item.get("name")
    }


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    """读取当前普通索引名。"""

    if not sa.inspect(bind).has_table(table_name):
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes(table_name)
        if item.get("name")
    }
