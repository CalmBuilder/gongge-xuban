"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : 20261001_0087_memory_capture_contract.py
@CallChain  : Alembic upgrade/downgrade → MemoryCaptureScope/Job/KeyState → 可靠记忆提取
@Description: 为 SD-6B 建立来源序号、失效代数和持久提取任务合同。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20261001_0087"
down_revision: str | None = "20260928_0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_JSON = sa.JSON()
_PRECISE_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def _table_names() -> set[str]:
    """读取当前数据库表名，兼容 SQLite 与 MySQL。"""

    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    """读取目标表当前字段，允许历史库幂等升级。"""

    if table_name not in _table_names():
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    """只为既有模型表增加 SD-6B 所需的代数字段。"""

    if table_name not in _table_names() or column.name in _columns(table_name):
        return
    op.add_column(table_name, column)


def _create_index_if_missing(name: str, table_name: str, columns: Sequence[str]) -> None:
    """按名称幂等创建跨数据库查询索引。"""

    if table_name not in _table_names():
        return
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)}
    if name not in existing:
        op.create_index(name, table_name, list(columns))


def _create_capture_tables() -> None:
    """创建来源 scope、持久任务和结构化键状态三张基础表。"""

    tables = _table_names()
    if "memory_capture_scopes" not in tables:
        op.create_table(
            "memory_capture_scopes",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("scope_kind", sa.String(64), nullable=False, server_default="user"),
            sa.Column("agent_scope_key", sa.String(128), nullable=False, server_default="__user__"),
            sa.Column("source_sequence", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("clear_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("permission_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("row_version", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("created_at", _PRECISE_DATETIME, nullable=False),
            sa.Column("updated_at", _PRECISE_DATETIME, nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "user_id",
                "scope_kind",
                "agent_scope_key",
                name="uq_memory_capture_scope_identity",
            ),
        )
    if "memory_capture_jobs" not in tables:
        op.create_table(
            "memory_capture_jobs",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.String(128), nullable=True),
            sa.Column("agent_scope_key", sa.String(128), nullable=False),
            sa.Column("scope_id", sa.String(512), nullable=False),
            sa.Column("source_message_id", sa.String(128), nullable=False),
            sa.Column("source_assistant_message_id", sa.String(128), nullable=True),
            sa.Column("source_sequence", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("user_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("agent_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column(
                "user_permission_generation",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "agent_permission_generation",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("agent_capture_generation", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("source_manifest_json", _JSON, nullable=False),
            sa.Column("source_hash", sa.String(64), nullable=False),
            sa.Column("model_config_id", sa.String(128), nullable=False),
            sa.Column("model_capture_generation", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("model_contract_hash", sa.String(64), nullable=False),
            sa.Column("extractor_schema_version", sa.String(64), nullable=False, server_default="sd6b.v1"),
            sa.Column("prompt_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(64), nullable=False, server_default="pending"),
            sa.Column("row_version", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("model_dispatch_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", _PRECISE_DATETIME, nullable=True),
            sa.Column("deadline_at", _PRECISE_DATETIME, nullable=True),
            sa.Column("lease_owner", sa.String(128), nullable=True),
            sa.Column("lease_expires_at", _PRECISE_DATETIME, nullable=True),
            sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("created_at", _PRECISE_DATETIME, nullable=False),
            sa.Column("finished_at", _PRECISE_DATETIME, nullable=True),
            sa.Column("terminal_reason", sa.String(64), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("applied_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("discarded_count", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint(
                "tenant_id",
                "user_id",
                "agent_scope_key",
                "source_message_id",
                name="uq_memory_capture_job_source",
            ),
        )
    if "memory_key_states" not in tables:
        op.create_table(
            "memory_key_states",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("agent_scope_key", sa.String(128), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("normalized_key", sa.String(64), nullable=False),
            sa.Column("record_id", sa.String(128), nullable=True),
            sa.Column("last_source_sequence", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("last_job_id", sa.String(128), nullable=True),
            sa.Column("clear_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("permission_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("user_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("agent_generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column(
                "user_permission_generation",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "agent_permission_generation",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("tombstoned", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("row_version", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("created_at", _PRECISE_DATETIME, nullable=False),
            sa.Column("updated_at", _PRECISE_DATETIME, nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "user_id",
                "agent_scope_key",
                "kind",
                "normalized_key",
                name="uq_memory_key_state_identity",
            ),
        )

    _create_index_if_missing(
        "ix_memory_capture_scope_tenant_user_kind",
        "memory_capture_scopes",
        ("tenant_id", "user_id", "scope_kind", "agent_scope_key"),
    )
    _create_index_if_missing(
        "ix_memory_capture_job_claimable",
        "memory_capture_jobs",
        ("status", "next_attempt_at", "lease_expires_at"),
    )
    _create_index_if_missing(
        "ix_memory_capture_job_tenant_user_created",
        "memory_capture_jobs",
        ("tenant_id", "user_id", "created_at"),
    )
    _create_index_if_missing(
        "ix_memory_key_state_tenant_user_scope",
        "memory_key_states",
        ("tenant_id", "user_id", "agent_scope_key"),
    )


def upgrade() -> None:
    """升级历史 SQLite/MySQL，补齐模型代数并建立 SD-6B 持久合同。"""

    _add_column_if_missing(
        "model_configs",
        sa.Column("memory_capture_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    _add_column_if_missing(
        "agent_profiles",
        sa.Column("memory_capture_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    _create_capture_tables()
    for name in (
        "user_generation",
        "agent_generation",
        "user_permission_generation",
        "agent_permission_generation",
    ):
        _add_column_if_missing(
            "memory_key_states",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    """在可回退的历史库上删除本迁移新增字段。"""

    if table_name not in _table_names() or column_name not in _columns(table_name):
        return
    with op.batch_alter_table(table_name) as batch:
        batch.drop_column(column_name)


def downgrade() -> None:
    """回退 SD-6B schema；业务任务若已存在，仍由上层迁移门禁决定是否允许。"""

    inspector = sa.inspect(op.get_bind())
    for table_name in ("memory_key_states", "memory_capture_jobs", "memory_capture_scopes"):
        if table_name in inspector.get_table_names():
            op.drop_table(table_name)
    _drop_column_if_exists("agent_profiles", "memory_capture_generation")
    _drop_column_if_exists("model_configs", "memory_capture_generation")
