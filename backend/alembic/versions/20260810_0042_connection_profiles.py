"""
@Time       : 2026/08/10 17:15
@Author     : zhanglp8181
@File       : 20260810_0042_connection_profiles.py
@CallChain  : Alembic upgrade/downgrade → Connector secret/profile/Agent binding tables
@Description: 创建多账号连接档案、版本化密钥引用和 Agent 授权绑定。

Revision ID: 20260810_0042
Revises: 20260804_0041
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0042"
down_revision: str | None = "20260804_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建连接控制面、命令回执和 OAuth state 表，并拒绝缺契约的半成品结构。"""

    bind = op.get_bind()
    _require_baseline(bind)
    if not sa.inspect(bind).has_table("connection_secrets"):
        op.create_table(
            "connection_secrets",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("reference_id", sa.String(128), nullable=False),
            sa.Column("encrypted_payload", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(64), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "tenant_id",
                "provider",
                "reference_id",
                "revision",
                name="uq_connection_secret_revision",
            ),
            sa.CheckConstraint("revision >= 1", name="ck_connection_secret_revision"),
            sa.CheckConstraint(
                "status IN ('active', 'superseded', 'revoked')",
                name="ck_connection_secret_status",
            ),
        )
    else:
        _validate_table(
            bind,
            "connection_secrets",
            {
                "id", "tenant_id", "provider", "reference_id", "encrypted_payload",
                "revision", "status", "created_at", "updated_at", "revoked_at",
            },
            {
                "uq_connection_secret_revision",
                "ck_connection_secret_revision",
                "ck_connection_secret_status",
            },
        )
    if not sa.inspect(bind).has_table("connection_profiles"):
        op.create_table(
            "connection_profiles",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("account_id", sa.String(128), nullable=False),
            sa.Column("display_name", sa.String(191), nullable=False),
            sa.Column("secret_ref_id", sa.String(128), nullable=False),
            sa.Column("secret_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("required_scopes_json", sa.JSON(), nullable=False),
            sa.Column("granted_scopes_json", sa.JSON(), nullable=False),
            sa.Column("tool_allowlist_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(64), nullable=False, server_default="active"),
            sa.Column("health_status", sa.String(64), nullable=False, server_default="unverified"),
            sa.Column("health_error_code", sa.String(128), nullable=True),
            sa.Column("rate_limited_until", sa.DateTime(), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_healthy_at", sa.DateTime(), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_by_user_id", sa.String(128), nullable=False),
            sa.Column("updated_by_user_id", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "provider", "account_id", name="uq_connection_profile_account"
            ),
            sa.CheckConstraint("revision >= 1", name="ck_connection_profile_revision"),
            sa.CheckConstraint(
                "secret_revision >= 1", name="ck_connection_profile_secret_revision"
            ),
            sa.CheckConstraint(
                "status IN ('active', 'disabled', 'reauth_required')",
                name="ck_connection_profile_status",
            ),
            sa.CheckConstraint(
                "health_status IN ('unverified', 'healthy', 'degraded', 'unhealthy')",
                name="ck_connection_profile_health",
            ),
        )
    else:
        _validate_table(
            bind,
            "connection_profiles",
            {
                "id", "tenant_id", "provider", "account_id", "display_name",
                "secret_ref_id", "secret_revision", "required_scopes_json",
                "granted_scopes_json", "tool_allowlist_json", "status", "health_status",
                "health_error_code",
                "rate_limited_until", "last_checked_at", "last_healthy_at", "revision",
                "created_by_user_id", "updated_by_user_id", "created_at", "updated_at",
            },
            {
                "uq_connection_profile_account", "ck_connection_profile_revision",
                "ck_connection_profile_secret_revision", "ck_connection_profile_status",
                "ck_connection_profile_health",
            },
        )
    if not sa.inspect(bind).has_table("agent_connection_bindings"):
        op.create_table(
            "agent_connection_bindings",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.String(128), nullable=False),
            sa.Column("profile_id", sa.String(128), nullable=False),
            sa.Column("allowed_scopes_json", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_by_user_id", sa.String(128), nullable=False),
            sa.Column("updated_by_user_id", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "agent_id", "profile_id", name="uq_agent_connection_binding"
            ),
            sa.CheckConstraint(
                "revision >= 1", name="ck_agent_connection_binding_revision"
            ),
        )
    else:
        _validate_table(
            bind,
            "agent_connection_bindings",
            {
                "id", "tenant_id", "agent_id", "profile_id", "allowed_scopes_json",
                "enabled", "revision", "created_by_user_id", "updated_by_user_id",
                "created_at", "updated_at",
            },
            {"uq_agent_connection_binding", "ck_agent_connection_binding_revision"},
        )
    if not sa.inspect(bind).has_table("connection_command_receipts"):
        op.create_table(
            "connection_command_receipts",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("command_id", sa.String(128), nullable=False),
            sa.Column("command_type", sa.String(64), nullable=False),
            sa.Column("actor_user_id", sa.String(128), nullable=False),
            sa.Column("payload_checksum", sa.String(128), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=False),
            sa.Column("resource_id", sa.String(128), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "command_id", name="uq_connection_command_receipt"
            ),
        )
    else:
        _validate_table(
            bind,
            "connection_command_receipts",
            {
                "id", "tenant_id", "command_id", "command_type", "actor_user_id",
                "payload_checksum", "resource_type", "resource_id", "result_json",
                "created_at",
            },
            {"uq_connection_command_receipt"},
        )
    if not sa.inspect(bind).has_table("connection_oauth_states"):
        op.create_table(
            "connection_oauth_states",
            sa.Column("id", sa.String(512), primary_key=True),
            sa.Column("state_hash", sa.String(128), nullable=False),
            sa.Column("encrypted_state", sa.Text(), nullable=False),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("actor_user_id", sa.String(128), nullable=False),
            sa.Column("flow_type", sa.String(64), nullable=False),
            sa.Column("profile_id", sa.String(128), nullable=True),
            sa.Column("attention_id", sa.String(128), nullable=True),
            sa.Column("display_name", sa.String(191), nullable=True),
            sa.Column("command_id", sa.String(128), nullable=False),
            sa.Column("expected_profile_revision", sa.Integer(), nullable=False),
            sa.Column("expected_attention_revision", sa.Integer(), nullable=True),
            sa.Column("required_scopes_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(64), nullable=False, server_default="pending"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("error_code", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("state_hash", name="uq_connection_oauth_state_hash"),
            sa.UniqueConstraint(
                "tenant_id", "command_id", name="uq_connection_oauth_tenant_command"
            ),
            sa.CheckConstraint(
                "flow_type IN ('create', 'reauthorize', 'reauthorize_attention')",
                name="ck_connection_oauth_flow_type",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'processing', 'consumed', 'failed')",
                name="ck_connection_oauth_state_status",
            ),
        )
    else:
        _validate_table(
            bind,
            "connection_oauth_states",
            {
                "id", "state_hash", "encrypted_state", "tenant_id", "actor_user_id", "flow_type",
                "profile_id", "attention_id", "display_name", "command_id",
                "expected_profile_revision", "expected_attention_revision",
                "required_scopes_json", "status", "expires_at", "consumed_at",
                "error_code", "created_at",
            },
            {
                "uq_connection_oauth_state_hash", "uq_connection_oauth_tenant_command",
                "ck_connection_oauth_flow_type",
                "ck_connection_oauth_state_status",
            },
        )
    _create_indexes(bind)


def downgrade() -> None:
    """存在任一连接、命令或 OAuth 事实时拒绝降级，避免凭据与回执失管。"""

    bind = op.get_bind()
    for table_name in (
        "connection_oauth_states",
        "connection_command_receipts",
        "agent_connection_bindings",
        "connection_profiles",
        "connection_secrets",
    ):
        if sa.inspect(bind).has_table(table_name):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            if int(count) > 0:
                raise RuntimeError("cannot downgrade connection profiles while facts exist")
    for table_name in (
        "connection_oauth_states",
        "connection_command_receipts",
        "agent_connection_bindings",
        "connection_profiles",
        "connection_secrets",
    ):
        if sa.inspect(bind).has_table(table_name):
            op.drop_table(table_name)


def _require_baseline(bind: sa.Connection) -> None:
    """在 DDL 前确认租户、用户和 Agent 身份基线均存在。"""

    required = {"tenants", "users", "agent_profiles"}
    missing = required - set(sa.inspect(bind).get_table_names())
    if missing:
        raise RuntimeError(f"connection profile migration requires tables: {sorted(missing)}")


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
    """创建密钥定位、档案健康筛选和 Agent 运行时解析索引。"""

    definitions = {
        "connection_secrets": (
            ("ix_connection_secrets_tenant_id", ["tenant_id"]),
            ("ix_connection_secrets_provider", ["provider"]),
            ("ix_connection_secrets_reference_id", ["reference_id"]),
            ("ix_connection_secrets_status", ["status"]),
        ),
        "connection_profiles": (
            ("ix_connection_profiles_tenant_id", ["tenant_id"]),
            ("ix_connection_profiles_provider", ["provider"]),
            ("ix_connection_profiles_account_id", ["account_id"]),
            ("ix_connection_profiles_secret_ref_id", ["secret_ref_id"]),
            ("ix_connection_profiles_status", ["status"]),
            ("ix_connection_profiles_health_status", ["health_status"]),
            ("ix_connection_profiles_created_by_user_id", ["created_by_user_id"]),
            ("ix_connection_profiles_updated_by_user_id", ["updated_by_user_id"]),
            (
                "ix_connection_profiles_tenant_provider_status",
                ["tenant_id", "provider", "status"],
            ),
        ),
        "agent_connection_bindings": (
            ("ix_agent_connection_bindings_tenant_id", ["tenant_id"]),
            ("ix_agent_connection_bindings_agent_id", ["agent_id"]),
            ("ix_agent_connection_bindings_profile_id", ["profile_id"]),
            ("ix_agent_connection_bindings_enabled", ["enabled"]),
            ("ix_agent_connection_bindings_created_by_user_id", ["created_by_user_id"]),
            ("ix_agent_connection_bindings_updated_by_user_id", ["updated_by_user_id"]),
            (
                "ix_agent_connection_bindings_resolve",
                ["tenant_id", "agent_id", "enabled"],
            ),
        ),
        "connection_command_receipts": (
            ("ix_connection_command_receipts_tenant_id", ["tenant_id"]),
            ("ix_connection_command_receipts_command_id", ["command_id"]),
            ("ix_connection_command_receipts_command_type", ["command_type"]),
            ("ix_connection_command_receipts_actor_user_id", ["actor_user_id"]),
            ("ix_connection_command_receipts_resource_id", ["resource_id"]),
        ),
        "connection_oauth_states": (
            ("ix_connection_oauth_states_state_hash", ["state_hash"]),
            ("ix_connection_oauth_states_tenant_id", ["tenant_id"]),
            ("ix_connection_oauth_states_actor_user_id", ["actor_user_id"]),
            ("ix_connection_oauth_states_flow_type", ["flow_type"]),
            ("ix_connection_oauth_states_profile_id", ["profile_id"]),
            ("ix_connection_oauth_states_attention_id", ["attention_id"]),
            ("ix_connection_oauth_states_command_id", ["command_id"]),
            ("ix_connection_oauth_states_status", ["status"]),
            ("ix_connection_oauth_states_expires_at", ["expires_at"]),
        ),
    }
    inspector = sa.inspect(bind)
    for table_name, indexes in definitions.items():
        existing = {str(item["name"]) for item in inspector.get_indexes(table_name)}
        for name, columns in indexes:
            if name not in existing:
                op.create_index(name, table_name, columns)
