"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : 20260812_0054_general_skill_source_credentials.py
@CallChain  : Alembic upgrade/downgrade → private Skill source credential profiles
@Description: 建立用户级来源凭据档案，密文复用追加式 connection_secrets。

Revision ID: 20260812_0054
Revises: 20260812_0053
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0054"
down_revision: str | None = "20260812_0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入创建用户级私有来源档案和主体/状态查询索引。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("general_skill_source_credentials"):
        return
    op.create_table(
        "general_skill_source_credentials",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(512), nullable=False),
        sa.Column("owner_user_id", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("source_kind", sa.String(512), nullable=False),
        sa.Column("allowed_host", sa.String(2048), nullable=False),
        sa.Column("secret_reference_id", sa.String(512), nullable=False),
        sa.Column("secret_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(512), nullable=False, server_default="active"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "id",
            name="uq_general_skill_source_credential_owner",
        ),
        sa.CheckConstraint(
            "source_kind IN ('github', 'https')",
            name="ck_general_skill_source_credential_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_general_skill_source_credential_status",
        ),
        sa.CheckConstraint(
            "secret_revision >= 1",
            name="ck_general_skill_source_secret_revision",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_general_skill_source_row_version"),
    )
    op.create_index(
        "ix_general_skill_source_credential_owner_status",
        "general_skill_source_credentials",
        ["tenant_id", "owner_user_id", "status"],
    )
    op.create_index(
        "ix_general_skill_source_credentials_secret_reference_id",
        "general_skill_source_credentials",
        ["secret_reference_id"],
    )


def downgrade() -> None:
    """仅移除凭据档案；追加式密文保留供独立保留策略清理与审计。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("general_skill_source_credentials"):
        op.drop_table("general_skill_source_credentials")
