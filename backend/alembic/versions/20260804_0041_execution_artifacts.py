"""
@Time       : 2026/08/04 18:10
@Author     : zhanglp8181
@File       : 20260804_0041_execution_artifacts.py
@CallChain  : Alembic upgrade/downgrade → Artifact metadata/input lineage
@Description: 创建可校验 Execution Artifact 与精确输入快照血缘表。

Revision ID: 20260804_0041
Revises: 20260803_0040
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260804_0041"
down_revision: str | None = "20260803_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 Artifact 权威元数据和输入血缘，并拒绝接受缺契约的半成品表。"""

    bind = op.get_bind()
    _require_baseline(bind)
    if not sa.inspect(bind).has_table("execution_artifacts"):
        op.create_table(
            "execution_artifacts",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("execution_id", sa.String(512), nullable=False),
            sa.Column("source_node_execution_id", sa.String(128), nullable=False),
            sa.Column("source_step_key", sa.String(128), nullable=False),
            sa.Column("artifact_key", sa.String(128), nullable=False),
            sa.Column("filename", sa.String(191), nullable=False),
            sa.Column("mime_type", sa.String(191), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("content_checksum", sa.String(64), nullable=False),
            sa.Column("storage_locator", sa.String(1000), nullable=False),
            sa.Column("acl_json", sa.JSON(), nullable=False),
            sa.Column("lineage_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(64), nullable=False, server_default="ready"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "tenant_id", "execution_id", "artifact_key", name="uq_execution_artifact_key"
            ),
            sa.CheckConstraint(
                "status IN ('ready', 'corrupt', 'revoked')",
                name="ck_execution_artifact_status",
            ),
            sa.CheckConstraint("size_bytes >= 0", name="ck_execution_artifact_size"),
        )
    else:
        _validate_table(
            bind,
            "execution_artifacts",
            {
                "id", "tenant_id", "execution_id", "source_node_execution_id",
                "source_step_key", "artifact_key", "filename", "mime_type", "size_bytes",
                "content_checksum", "storage_locator", "acl_json", "lineage_json", "status",
                "created_at", "updated_at", "revoked_at",
            },
            {"uq_execution_artifact_key", "ck_execution_artifact_status", "ck_execution_artifact_size"},
        )
    if not sa.inspect(bind).has_table("artifact_input_links"):
        op.create_table(
            "artifact_input_links",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("execution_id", sa.String(512), nullable=False),
            sa.Column("artifact_id", sa.String(128), nullable=False),
            sa.Column("input_snapshot_id", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "artifact_id", "input_snapshot_id", name="uq_artifact_input_link"
            ),
        )
    else:
        _validate_table(
            bind,
            "artifact_input_links",
            {"id", "tenant_id", "execution_id", "artifact_id", "input_snapshot_id", "created_at"},
            {"uq_artifact_input_link"},
        )
    _create_indexes(bind)


def downgrade() -> None:
    """存在任一 Artifact 或 lineage 事实时拒绝降级，避免元数据孤儿和文件失管。"""

    bind = op.get_bind()
    for table_name in ("artifact_input_links", "execution_artifacts"):
        if sa.inspect(bind).has_table(table_name):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            if int(count) > 0:
                raise RuntimeError("cannot downgrade execution artifacts while facts exist")
    for table_name in ("artifact_input_links", "execution_artifacts"):
        if sa.inspect(bind).has_table(table_name):
            op.drop_table(table_name)


def _require_baseline(bind: sa.Connection) -> None:
    """在 DDL 前确认 Execution、节点和输入快照基线均存在。"""

    required = {"sop_instances", "sop_node_executions", "input_resource_snapshots"}
    missing = required - set(sa.inspect(bind).get_table_names())
    if missing:
        raise RuntimeError(f"artifact migration requires tables: {sorted(missing)}")


def _validate_table(
    bind: sa.Connection,
    table_name: str,
    expected_columns: set[str],
    expected_constraints: set[str],
) -> None:
    """拒绝 MySQL 非事务 DDL 中断后留下的缺列或缺约束半成品表。"""

    inspector = sa.inspect(bind)
    columns = {str(item["name"]) for item in inspector.get_columns(table_name)}
    constraints = {str(item["name"]) for item in inspector.get_unique_constraints(table_name)}
    constraints |= {str(item["name"]) for item in inspector.get_check_constraints(table_name)}
    missing_columns = expected_columns - columns
    missing_constraints = expected_constraints - constraints
    if missing_columns or missing_constraints:
        raise RuntimeError(
            f"partial {table_name}: columns={sorted(missing_columns)}, "
            f"constraints={sorted(missing_constraints)}"
        )


def _create_indexes(bind: sa.Connection) -> None:
    """创建 ACL 查询、Execution 列表和精确 lineage 查找索引。"""

    definitions = {
        "execution_artifacts": (
            ("ix_execution_artifacts_tenant_execution", ["tenant_id", "execution_id"]),
            ("ix_execution_artifacts_tenant_checksum", ["tenant_id", "content_checksum"]),
        ),
        "artifact_input_links": (
            ("ix_artifact_input_links_execution", ["tenant_id", "execution_id"]),
            ("ix_artifact_input_links_snapshot", ["tenant_id", "input_snapshot_id"]),
        ),
    }
    inspector = sa.inspect(bind)
    for table_name, indexes in definitions.items():
        existing = {str(item["name"]) for item in inspector.get_indexes(table_name)}
        for name, columns in indexes:
            if name not in existing:
                op.create_index(name, table_name, columns)
