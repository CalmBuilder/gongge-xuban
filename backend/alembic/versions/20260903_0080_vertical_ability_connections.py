"""
@Time       : 2026/09/03 15:25
@Author     : zhanglp8181
@File       : 20260903_0080_vertical_ability_connections.py
@CallChain  : Alembic upgrade/downgrade → vertical_ability_connections → 用户网站连接 Runtime
@Description: 增加按租户和用户隔离的网站连接密文、认证方式、账户标识和健康状态表。

Revision ID: 20260903_0080
Revises: 20260902_0079
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260903_0080"
down_revision: str | None = "20260902_0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建用户网站连接表及租户、平台和状态索引。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("vertical_ability_connections"):
        return
    op.create_table(
        "vertical_ability_connections",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        # User.id 的数据库边界是 128 字节；使用 512 会让 utf8mb4 下的
        # tenant+user+platform+account 唯一索引超过 InnoDB 3072 字节上限。
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("platform_id", sa.String(64), nullable=False),
        sa.Column("account_label", sa.String(191), nullable=False, server_default="默认账号"),
        sa.Column("account_identity", sa.String(191), nullable=True),
        sa.Column("auth_method", sa.String(64), nullable=False),
        sa.Column(
            "encrypted_credentials_json",
            sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(64), nullable=False, server_default="pending"),
        sa.Column(
            "health_status",
            sa.String(64),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("health_error_code", sa.String(64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_healthy_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "platform_id",
            "account_label",
            name="uq_vertical_ability_connection_account",
        ),
        sa.CheckConstraint(
            "auth_method IN ('api_token', 'cookie', 'browser_session', 'oauth')",
            name="ck_vertical_ability_connection_auth_method",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'connected', 'needs_reauth', 'revoked')",
            name="ck_vertical_ability_connection_status",
        ),
        sa.CheckConstraint(
            "health_status IN ('unverified', 'healthy', 'degraded', 'unhealthy')",
            name="ck_vertical_ability_connection_health",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_vertical_ability_connection_revision",
        ),
    )
    op.create_index(
        "ix_vertical_ability_connections_tenant_id",
        "vertical_ability_connections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_vertical_ability_connections_user_id",
        "vertical_ability_connections",
        ["user_id"],
    )
    op.create_index(
        "ix_vertical_ability_connections_platform_id",
        "vertical_ability_connections",
        ["platform_id"],
    )
    op.create_index(
        "ix_vertical_ability_connections_auth_method",
        "vertical_ability_connections",
        ["auth_method"],
    )
    op.create_index(
        "ix_vertical_ability_connections_status",
        "vertical_ability_connections",
        ["status"],
    )
    op.create_index(
        "ix_vertical_ability_connections_health_status",
        "vertical_ability_connections",
        ["health_status"],
    )
    op.create_index(
        "ix_vertical_ability_connections_tenant_user",
        "vertical_ability_connections",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_vertical_ability_connections_platform_status",
        "vertical_ability_connections",
        ["platform_id", "status"],
    )


def downgrade() -> None:
    """仅在用户网站连接表为空时删除，避免误删仍可恢复的登录态密文。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("vertical_ability_connections"):
        return
    count = bind.execute(sa.text("SELECT COUNT(*) FROM vertical_ability_connections")).scalar_one()
    if int(count) > 0:
        raise RuntimeError("cannot downgrade while vertical ability connections exist")
    op.drop_table("vertical_ability_connections")
