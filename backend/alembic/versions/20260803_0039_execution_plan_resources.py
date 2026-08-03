"""
@Time       : 2026/08/03 23:52
@Author     : zhanglp8181
@File       : 20260803_0039_execution_plan_resources.py
@CallChain  : Alembic upgrade/downgrade → unified Execution/Plan/Step/Proposal/InputResource
@Description: 建立动态任务统一包络、追加计划/提案账本和受管输入资源契约。

Revision ID: 20260803_0039
Revises: 20260803_0038
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260803_0039"
down_revision: str | None = "20260803_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INSTANCE_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("agent_id", sa.String(512), nullable=True),
    sa.Column("goal_snapshot_json", sa.JSON(), nullable=True),
    sa.Column("current_plan_revision_id", sa.String(512), nullable=True),
    sa.Column("current_plan_checksum", sa.String(64), nullable=True),
    sa.Column("capability_snapshot_json", sa.JSON(), nullable=True),
    sa.Column("capability_checksum", sa.String(64), nullable=True),
    sa.Column("budget_snapshot_json", sa.JSON(), nullable=True),
    sa.Column("terminal_reason_json", sa.JSON(), nullable=True),
)
STEP_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("step_key", sa.String(128), nullable=True),
    sa.Column("plan_revision_id", sa.String(512), nullable=True),
    sa.Column("step_kind", sa.String(64), nullable=True),
    sa.Column("title", sa.String(191), nullable=True),
    sa.Column("required", sa.Boolean(), nullable=True),
    sa.Column("superseded_by_step_key", sa.String(128), nullable=True),
)
NEW_TABLES = (
    "execution_plan_revisions",
    "action_proposal_records",
    "managed_input_resources",
    "input_resource_snapshots",
)


def upgrade() -> None:
    """按 preflight、expand、回填、收紧和新账本顺序实施可续跑迁移。"""

    bind = op.get_bind()
    _preflight(bind)
    _add_columns(bind, "sop_instances", INSTANCE_COLUMNS)
    _add_columns(bind, "sop_node_executions", STEP_COLUMNS)
    _backfill_existing_rows(bind)
    _make_step_columns_required(bind)
    _create_execution_constraints(bind)
    _create_tables(bind)
    _create_indexes(bind)


def downgrade() -> None:
    """仅在没有动态 Execution、新计划/提案/输入事实时允许回退到 0038。"""

    bind = op.get_bind()
    _preflight_downgrade(bind)
    for table_name in reversed(NEW_TABLES):
        if sa.inspect(bind).has_table(table_name):
            op.drop_table(table_name)
    step_constraints = _constraint_names(bind, "sop_node_executions")
    if "uq_execution_step_attempt" in step_constraints:
        with op.batch_alter_table("sop_node_executions") as batch:
            batch.drop_constraint("uq_execution_step_attempt", type_="unique")
    instance_constraints = _constraint_names(bind, "sop_instances")
    for name in (
        "ck_execution_sop_without_dynamic_plan",
        "ck_execution_dynamic_identity",
    ):
        if name in instance_constraints:
            with op.batch_alter_table("sop_instances") as batch:
                batch.drop_constraint(name, type_="check")
    _drop_indexes_for_columns(bind, "sop_node_executions", STEP_COLUMNS)
    _drop_indexes_for_columns(bind, "sop_instances", INSTANCE_COLUMNS)
    _drop_columns(bind, "sop_node_executions", STEP_COLUMNS)
    _drop_columns(bind, "sop_instances", INSTANCE_COLUMNS)


def _preflight(bind: sa.Connection) -> None:
    """拒绝缺少依赖表或已经存在无法安全推断动态身份的历史行。"""

    for table_name in ("sop_instances", "sop_node_executions"):
        if not sa.inspect(bind).has_table(table_name):
            raise RuntimeError(f"execution plan migration requires table: {table_name}")
    instance_columns = _column_names(bind, "sop_instances")
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM sop_instances WHERE kind = 'dynamic_task'")
    ).scalar_one()
    if not count:
        return
    required = {
        "agent_id",
        "goal_snapshot_json",
        "current_plan_revision_id",
        "current_plan_checksum",
        "capability_snapshot_json",
    }
    if not required <= instance_columns:
        raise RuntimeError("cannot infer identity for legacy dynamic executions")
    missing_identity = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM sop_instances WHERE kind = 'dynamic_task' AND "
            "(agent_id IS NULL OR initiator_user_id IS NULL OR goal_snapshot_json IS NULL OR "
            "current_plan_revision_id IS NULL OR current_plan_checksum IS NULL OR "
            "capability_snapshot_json IS NULL)"
        )
    ).scalar_one()
    if missing_identity:
        raise RuntimeError("cannot infer identity for legacy dynamic executions")


def _add_columns(
    bind: sa.Connection,
    table_name: str,
    columns: tuple[sa.Column[object], ...],
) -> None:
    """逐列扩展既有表，使 MySQL 非事务 DDL 中断后能够安全续跑。"""

    existing = _column_names(bind, table_name)
    missing = [column for column in columns if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table(table_name) as batch:
        for column in missing:
            batch.add_column(column)


def _backfill_existing_rows(bind: sa.Connection) -> None:
    """为全部正式 SOP 节点补稳定 step key，并给 JSON 字段写入保守默认。"""

    bind.execute(
        sa.text(
            "UPDATE sop_node_executions SET step_key = node_id "
            "WHERE step_key IS NULL OR step_key = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE sop_node_executions SET step_kind = 'sop_node' "
            "WHERE step_kind IS NULL OR step_kind = ''"
        )
    )
    bind.execute(
        sa.text("UPDATE sop_node_executions SET required = 1 WHERE required IS NULL")
    )
    instances = sa.Table("sop_instances", sa.MetaData(), autoload_with=bind)
    bind.execute(
        instances.update()
        .where(instances.c.budget_snapshot_json.is_(None))
        .values(budget_snapshot_json={})
    )
    bind.execute(
        instances.update()
        .where(instances.c.terminal_reason_json.is_(None))
        .values(terminal_reason_json={})
    )


def _make_step_columns_required(bind: sa.Connection) -> None:
    """收紧回填后的步骤身份及 Execution JSON 默认字段。"""

    with op.batch_alter_table("sop_node_executions") as batch:
        batch.alter_column("step_key", existing_type=sa.String(128), nullable=False)
        batch.alter_column(
            "step_kind",
            existing_type=sa.String(64),
            nullable=False,
            server_default="sop_node",
        )
        batch.alter_column(
            "required",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        )
    with op.batch_alter_table("sop_instances") as batch:
        batch.alter_column(
            "budget_snapshot_json",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch.alter_column(
            "terminal_reason_json",
            existing_type=sa.JSON(),
            nullable=False,
        )


def _create_execution_constraints(bind: sa.Connection) -> None:
    """建立 SOP/动态身份互斥和 execution 内 step attempt 唯一约束。"""

    instance_constraints = _constraint_names(bind, "sop_instances")
    definitions = (
        (
            "ck_execution_sop_without_dynamic_plan",
            "kind <> 'sop' OR current_plan_revision_id IS NULL",
        ),
        (
            "ck_execution_dynamic_identity",
            "kind <> 'dynamic_task' OR (agent_id IS NOT NULL AND initiator_user_id IS NOT NULL "
            "AND goal_snapshot_json IS NOT NULL AND current_plan_revision_id IS NOT NULL "
            "AND current_plan_checksum IS NOT NULL AND capability_snapshot_json IS NOT NULL)",
        ),
    )
    for name, condition in definitions:
        if name not in instance_constraints:
            with op.batch_alter_table("sop_instances") as batch:
                batch.create_check_constraint(name, condition)
    if "uq_execution_step_attempt" not in _constraint_names(bind, "sop_node_executions"):
        with op.batch_alter_table("sop_node_executions") as batch:
            batch.create_unique_constraint(
                "uq_execution_step_attempt",
                ["tenant_id", "instance_id", "step_key", "attempt"],
            )


def _create_tables(bind: sa.Connection) -> None:
    """创建四张追加事实表；已存在表必须具有完整列集合。"""

    definitions = _table_definitions()
    for table_name, columns, constraints in definitions:
        if sa.inspect(bind).has_table(table_name):
            expected = {str(column.name) for column in columns}
            missing = expected - _column_names(bind, table_name)
            if missing:
                raise RuntimeError(f"partial {table_name} table is missing: {sorted(missing)}")
            expected_constraints = {
                str(item.name) for item in constraints if getattr(item, "name", None)
            }
            missing_constraints = expected_constraints - _constraint_names(bind, table_name)
            if missing_constraints:
                raise RuntimeError(
                    f"partial {table_name} table constraints are missing: "
                    f"{sorted(missing_constraints)}"
                )
            continue
        op.create_table(table_name, *columns, *constraints)


def _table_definitions() -> tuple[
    tuple[str, tuple[sa.Column[object], ...], tuple[sa.SchemaItem, ...]],
    ...,
]:
    """返回与 SQLModel 一致且兼容 SQLite/MySQL 的新表定义。"""

    timestamps = lambda: sa.Column("created_at", sa.DateTime(), nullable=False)  # noqa: E731
    long_text = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")
    return (
        (
            "execution_plan_revisions",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("revision_number", sa.Integer(), nullable=False),
                sa.Column("parent_revision_id", sa.String(512), nullable=True),
                sa.Column("reason", sa.String(64), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("plan_json", sa.JSON(), nullable=False),
                sa.Column("checksum", sa.String(64), nullable=False),
                sa.Column("capability_snapshot_json", sa.JSON(), nullable=False),
                sa.Column("capability_checksum", sa.String(64), nullable=False),
                sa.Column("created_by_proposal_id", sa.String(512), nullable=True),
                sa.Column("activated_at", sa.DateTime(), nullable=True),
                sa.Column("superseded_at", sa.DateTime(), nullable=True),
                timestamps(),
            ),
            (
                sa.UniqueConstraint(
                    "tenant_id",
                    "execution_id",
                    "revision_number",
                    name="uq_execution_plan_revision_number",
                ),
                sa.CheckConstraint(
                    "status IN ('validated', 'active', 'superseded', 'rejected')",
                    name="ck_execution_plan_revision_status",
                ),
            ),
        ),
        (
            "action_proposal_records",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("plan_revision_id", sa.String(512), nullable=False),
                sa.Column("step_key", sa.String(128), nullable=False),
                sa.Column("step_attempt", sa.Integer(), nullable=False),
                sa.Column("provider", sa.String(64), nullable=False),
                sa.Column("model", sa.String(191), nullable=False),
                sa.Column("provider_response_id", sa.String(512), nullable=False),
                sa.Column("provider_response_identity", sa.String(64), nullable=False),
                sa.Column("finish_reason", sa.String(64), nullable=False),
                sa.Column("model_capability_snapshot_json", sa.JSON(), nullable=False),
                sa.Column("normalized_proposal_json", sa.JSON(), nullable=False),
                sa.Column("validation_json", sa.JSON(), nullable=False),
                sa.Column("proposal_checksum", sa.String(64), nullable=False),
                sa.Column("usage_json", sa.JSON(), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("consumed_operation_id", sa.String(512), nullable=True),
                sa.Column("consumed_plan_revision_id", sa.String(512), nullable=True),
                sa.Column("consumed_at", sa.DateTime(), nullable=True),
                sa.Column("superseded_at", sa.DateTime(), nullable=True),
                sa.Column("causation_id", sa.String(512), nullable=True),
                timestamps(),
            ),
            (
                sa.UniqueConstraint(
                    "tenant_id",
                    "execution_id",
                    "proposal_checksum",
                    name="uq_action_proposal_checksum",
                ),
                sa.UniqueConstraint(
                    "tenant_id",
                    "execution_id",
                    "provider_response_identity",
                    name="uq_action_proposal_provider_response",
                ),
                sa.CheckConstraint(
                    "status IN ('validated', 'consumed', 'superseded')",
                    name="ck_action_proposal_status",
                ),
                sa.CheckConstraint(
                    "NOT (consumed_operation_id IS NOT NULL AND "
                    "consumed_plan_revision_id IS NOT NULL)",
                    name="ck_action_proposal_single_consumption_target",
                ),
            ),
        ),
        (
            "managed_input_resources",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("owner_user_id", sa.String(512), nullable=False),
                sa.Column("agent_id", sa.String(512), nullable=True),
                sa.Column("source_type", sa.String(64), nullable=False),
                sa.Column("source_message_id", sa.String(512), nullable=True),
                sa.Column("version", sa.String(64), nullable=False),
                sa.Column("filename", sa.String(191), nullable=False),
                sa.Column("mime_type", sa.String(191), nullable=False),
                sa.Column("size_bytes", sa.Integer(), nullable=False),
                sa.Column("content_checksum", sa.String(64), nullable=False),
                sa.Column("extraction_checksum", sa.String(64), nullable=True),
                sa.Column("ingestion_status", sa.String(64), nullable=False),
                sa.Column("storage_locator", sa.String(1000), nullable=False),
                sa.Column("extracted_text", long_text, nullable=True),
                sa.Column("extraction_metadata_json", sa.JSON(), nullable=False),
                sa.Column("acl_revision", sa.Integer(), nullable=False),
                sa.Column("revoked_at", sa.DateTime(), nullable=True),
                timestamps(),
                sa.Column("updated_at", sa.DateTime(), nullable=False),
            ),
            (
                sa.UniqueConstraint(
                    "tenant_id",
                    "id",
                    "version",
                    name="uq_managed_input_resource_version",
                ),
                sa.CheckConstraint(
                    "ingestion_status IN ('uploaded', 'scanning', 'extracting', 'ready', "
                    "'quarantined', 'failed', 'revoked')",
                    name="ck_managed_input_resource_status",
                ),
            ),
        ),
        (
            "input_resource_snapshots",
            (
                sa.Column("id", sa.String(512), primary_key=True),
                sa.Column("tenant_id", sa.String(128), nullable=False),
                sa.Column("execution_id", sa.String(512), nullable=False),
                sa.Column("source_type", sa.String(64), nullable=False),
                sa.Column("source_resource_id", sa.String(512), nullable=False),
                sa.Column("source_version", sa.String(64), nullable=False),
                sa.Column("source_message_id", sa.String(512), nullable=True),
                sa.Column("filename", sa.String(191), nullable=False),
                sa.Column("mime_type", sa.String(191), nullable=False),
                sa.Column("size_bytes", sa.Integer(), nullable=False),
                sa.Column("content_checksum", sa.String(64), nullable=False),
                sa.Column("extraction_checksum", sa.String(64), nullable=True),
                sa.Column("ingestion_status", sa.String(64), nullable=False),
                sa.Column("identity_checksum", sa.String(64), nullable=False),
                sa.Column("storage_locator_digest", sa.String(64), nullable=False),
                sa.Column("captured_acl_json", sa.JSON(), nullable=False),
                timestamps(),
            ),
            (
                sa.UniqueConstraint(
                    "tenant_id",
                    "execution_id",
                    "identity_checksum",
                    name="uq_execution_input_resource_identity",
                ),
                sa.CheckConstraint(
                    "ingestion_status IN ('ready', 'quarantined', 'failed', 'revoked')",
                    name="ck_input_resource_snapshot_status",
                ),
            ),
        ),
    )


def _create_indexes(bind: sa.Connection) -> None:
    """为所有按 tenant/execution、状态、checksum 和当前计划读取的列建立索引。"""

    definitions = {
        "sop_instances": (
            "agent_id",
            "current_plan_revision_id",
            "current_plan_checksum",
            "capability_checksum",
        ),
        "sop_node_executions": (
            "step_key",
            "plan_revision_id",
            "step_kind",
            "superseded_by_step_key",
        ),
        "execution_plan_revisions": (
            "tenant_id",
            "execution_id",
            "parent_revision_id",
            "status",
            "checksum",
            "capability_checksum",
            "created_by_proposal_id",
        ),
        "action_proposal_records": (
            "tenant_id",
            "execution_id",
            "plan_revision_id",
            "step_key",
            "provider_response_identity",
            "proposal_checksum",
            "status",
            "consumed_operation_id",
            "consumed_plan_revision_id",
        ),
        "managed_input_resources": (
            "tenant_id",
            "owner_user_id",
            "agent_id",
            "source_message_id",
            "content_checksum",
            "extraction_checksum",
            "ingestion_status",
        ),
        "input_resource_snapshots": (
            "tenant_id",
            "execution_id",
            "source_resource_id",
            "source_message_id",
            "content_checksum",
            "extraction_checksum",
            "ingestion_status",
            "identity_checksum",
        ),
    }
    for table_name, columns in definitions.items():
        existing = _index_names(bind, table_name)
        for column in columns:
            name = f"ix_{table_name}_{column}"
            if name not in existing:
                op.create_index(name, table_name, [column], unique=False)


def _preflight_downgrade(bind: sa.Connection) -> None:
    """拒绝删除 0038 无法表达的任何动态 Execution、计划、提案或输入历史。"""

    violations: list[str] = []
    if "kind" in _column_names(bind, "sop_instances"):
        count = bind.execute(
            sa.text("SELECT COUNT(*) FROM sop_instances WHERE kind = 'dynamic_task'")
        ).scalar_one()
        if count:
            violations.append("dynamic_execution")
    for table_name in NEW_TABLES:
        if sa.inspect(bind).has_table(table_name):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            if count:
                violations.append(table_name)
    if violations:
        raise RuntimeError(
            "cannot downgrade execution plan migration with managed history: "
            + ",".join(violations)
        )


def _drop_indexes_for_columns(
    bind: sa.Connection,
    table_name: str,
    columns: tuple[sa.Column[object], ...],
) -> None:
    """删除由 SQLModel 命名规则生成的新增列索引。"""

    existing = _index_names(bind, table_name)
    for column in columns:
        name = f"ix_{table_name}_{column.name}"
        if name in existing:
            op.drop_index(name, table_name=table_name)


def _drop_columns(
    bind: sa.Connection,
    table_name: str,
    columns: tuple[sa.Column[object], ...],
) -> None:
    """按逆序删除仍存在的新增列，支持降级重试。"""

    existing = _column_names(bind, table_name)
    with op.batch_alter_table(table_name) as batch:
        for column in reversed(columns):
            if column.name in existing:
                batch.drop_column(str(column.name))


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回实时列名集合。"""

    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


def _constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表上已命名的唯一、检查及外键约束。"""

    inspector = sa.inspect(bind)
    items = [
        *inspector.get_unique_constraints(table_name),
        *inspector.get_check_constraints(table_name),
        *inspector.get_foreign_keys(table_name),
    ]
    return {str(item["name"]) for item in items if item.get("name")}


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表上的显式索引名。"""

    return {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes(table_name)
        if item.get("name")
    }
