"""
@Time       : 2026/09/04 11:00
@Author     : zhanglp8181
@File       : 20260904_0081_browser_relay.py
@CallChain  : Alembic upgrade/downgrade → browser relay session/command → 宿主机浏览器连接
@Description: 为网站连接增加双传输模式，并持久化远程浏览器 Relay 的配对会话和固定命令队列。

Revision ID: 20260904_0081
Revises: 20260903_0080
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260904_0081"
down_revision: str | None = "20260903_0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加连接传输字段，并创建可重入的 Relay 会话与命令队列表。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("vertical_ability_connections"):
        columns = {column["name"] for column in inspector.get_columns("vertical_ability_connections")}
        if "transport" not in columns:
            op.add_column(
                "vertical_ability_connections",
                sa.Column("transport", sa.String(64), nullable=False, server_default="local_loopback"),
            )
        inspector = sa.inspect(bind)
        connection_indexes = {index["name"] for index in inspector.get_indexes("vertical_ability_connections")}
        if "ix_vertical_ability_connections_transport" not in connection_indexes:
            op.create_index(
                "ix_vertical_ability_connections_transport",
                "vertical_ability_connections",
                ["transport"],
            )

    if not inspector.has_table("vertical_ability_browser_relay_sessions"):
        op.create_table(
            "vertical_ability_browser_relay_sessions",
            sa.Column("id", sa.String(512), nullable=False),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("platform_id", sa.String(64), nullable=False),
            sa.Column("account_label", sa.String(191), nullable=False, server_default="默认账号"),
            sa.Column("expected_revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("transport", sa.String(64), nullable=False, server_default="browser_relay"),
            sa.Column("status", sa.String(64), nullable=False, server_default="pending"),
            sa.Column("connection_id", sa.String(128), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("connected_at", sa.DateTime(), nullable=True),
            sa.Column("disconnected_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint(
                "status IN ('pending', 'connected', 'disconnected', 'revoked', 'expired')",
                name="ck_vertical_ability_browser_relay_session_status",
            ),
            sa.CheckConstraint(
                "transport = 'browser_relay'",
                name="ck_vertical_ability_browser_relay_session_transport",
            ),
            sa.CheckConstraint(
                "expected_revision >= 0",
                name="ck_vertical_ability_browser_relay_session_revision",
            ),
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_sessions_tenant_id",
            "vertical_ability_browser_relay_sessions",
            ["tenant_id"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_sessions_user_id",
            "vertical_ability_browser_relay_sessions",
            ["user_id"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_sessions_platform_id",
            "vertical_ability_browser_relay_sessions",
            ["platform_id"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_sessions_token_hash",
            "vertical_ability_browser_relay_sessions",
            ["token_hash"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_sessions_transport",
            "vertical_ability_browser_relay_sessions",
            ["transport"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_sessions_status",
            "vertical_ability_browser_relay_sessions",
            ["status"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_session_owner",
            "vertical_ability_browser_relay_sessions",
            ["tenant_id", "user_id", "platform_id"],
        )
        op.create_index(
            "ix_vertical_ability_browser_relay_session_status_seen",
            "vertical_ability_browser_relay_sessions",
            ["status", "last_seen_at"],
        )

    if not inspector.has_table("vertical_ability_browser_relay_commands"):
        op.create_table(
            "vertical_ability_browser_relay_commands",
            sa.Column("id", sa.String(512), nullable=False),
            sa.Column("relay_session_id", sa.String(128), nullable=False),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("platform_id", sa.String(64), nullable=False),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column(
                "arguments_json",
                sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
                nullable=False,
            ),
            sa.Column("command_limit", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(64), nullable=False, server_default="queued"),
            sa.Column("lease_token_hash", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("deadline_at", sa.DateTime(), nullable=False),
            sa.Column(
                "result_json",
                sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
                nullable=True,
            ),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column(
                "error_message",
                sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint(
                "status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled', 'expired')",
                name="ck_vertical_ability_browser_relay_command_status",
            ),
            sa.CheckConstraint(
                "command_limit IS NULL OR (command_limit >= 1 AND command_limit <= 50)",
                name="ck_vertical_ability_browser_relay_command_limit",
            ),
        )
        for name, columns in (
            (
                "ix_vertical_ability_browser_relay_commands_relay_session_id",
                ["relay_session_id"],
            ),
            ("ix_vertical_ability_browser_relay_commands_tenant_id", ["tenant_id"]),
            ("ix_vertical_ability_browser_relay_commands_user_id", ["user_id"]),
            ("ix_vertical_ability_browser_relay_commands_platform_id", ["platform_id"]),
            ("ix_vertical_ability_browser_relay_commands_status", ["status"]),
            (
                "ix_vertical_ability_browser_relay_command_session_status",
                ["relay_session_id", "status", "created_at"],
            ),
            (
                "ix_vertical_ability_browser_relay_command_deadline",
                ["status", "deadline_at"],
            ),
        ):
            op.create_index(name, "vertical_ability_browser_relay_commands", columns)


def downgrade() -> None:
    """先检查全部回退条件，再移除 transport 的索引和命名约束，保留本机连接数据。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    relay_tables = [name for name in (
        "vertical_ability_browser_relay_commands",
        "vertical_ability_browser_relay_sessions",
    ) if inspector.has_table(name)]
    for table_name in relay_tables:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        if int(count) > 0:
            raise RuntimeError(f"cannot downgrade while {table_name} contains rows")

    columns = (
        {column["name"] for column in inspector.get_columns("vertical_ability_connections")}
        if inspector.has_table("vertical_ability_connections") else set()
    )
    indexes: set[str] = set()
    checks: set[str | None] = set()
    if "transport" in columns:
        remote_count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM vertical_ability_connections "
                "WHERE transport <> 'local_loopback'"
            )
        ).scalar_one()
        if int(remote_count) > 0:
            raise RuntimeError("cannot downgrade while browser relay connections exist")
        indexes = {index["name"] for index in inspector.get_indexes("vertical_ability_connections")}
        checks = {
            check["name"] for check in inspector.get_check_constraints("vertical_ability_connections")
        }

    # MySQL DDL 会隐式提交，SQLite 的部分 DDL 同样不能依赖异常回滚来恢复。
    for table_name in relay_tables:
        op.drop_table(table_name)
    if "transport" not in columns:
        return
    with op.batch_alter_table("vertical_ability_connections") as batch:
        if "ck_vertical_ability_connection_transport" in checks:
            batch.drop_constraint("ck_vertical_ability_connection_transport", type_="check")
        if "ix_vertical_ability_connections_transport" in indexes:
            batch.drop_index("ix_vertical_ability_connections_transport")
        batch.drop_column("transport")
