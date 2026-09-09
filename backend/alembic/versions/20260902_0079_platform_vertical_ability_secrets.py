"""
@Time       : 2026/09/02 14:10
@Author     : zhanglp8181
@File       : 20260902_0079_platform_vertical_ability_secrets.py
@CallChain  : Alembic upgrade/downgrade → platform_secrets → vertical ability provider runtime
@Description: 增加平台级垂直能力 Provider 密钥密文与健康状态表。

Revision ID: 20260902_0079
Revises: 20260901_0078
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260902_0079"
down_revision: str | None = "20260901_0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建可重入的平台级 Provider 密钥表及查询索引。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("platform_secrets"):
        return
    op.create_table(
        "platform_secrets",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("scope_key", sa.String(128), nullable=False, server_default="platform"),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("secret_kind", sa.String(64), nullable=False),
        sa.Column(
            "encrypted_value",
            sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(64), nullable=False, server_default="active"),
        sa.Column(
            "health_status",
            sa.String(64),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("health_error_code", sa.String(64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_healthy_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.String(128), nullable=True),
        sa.Column("updated_by_user_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key",
            "provider",
            "secret_kind",
            name="uq_platform_secret_identity",
        ),
        sa.CheckConstraint("scope_key = 'platform'", name="ck_platform_secret_scope"),
        sa.CheckConstraint("revision >= 1", name="ck_platform_secret_revision"),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_platform_secret_status",
        ),
        sa.CheckConstraint(
            "health_status IN ('unverified', 'healthy', 'degraded', 'unhealthy')",
            name="ck_platform_secret_health",
        ),
    )
    op.create_index(
        "ix_platform_secrets_scope_key",
        "platform_secrets",
        ["scope_key"],
    )
    op.create_index(
        "ix_platform_secrets_provider",
        "platform_secrets",
        ["provider"],
    )
    op.create_index(
        "ix_platform_secrets_secret_kind",
        "platform_secrets",
        ["secret_kind"],
    )
    op.create_index(
        "ix_platform_secrets_status",
        "platform_secrets",
        ["status"],
    )
    op.create_index(
        "ix_platform_secrets_health_status",
        "platform_secrets",
        ["health_status"],
    )
    op.create_index(
        "ix_platform_secrets_provider_status",
        "platform_secrets",
        ["provider", "status"],
    )


def downgrade() -> None:
    """仅在表为空时删除平台密钥表，避免误删仍在使用的密文。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("platform_secrets"):
        return
    count = bind.execute(sa.text("SELECT COUNT(*) FROM platform_secrets")).scalar_one()
    if int(count) > 0:
        raise RuntimeError("cannot downgrade while platform vertical ability secrets exist")
    op.drop_table("platform_secrets")
