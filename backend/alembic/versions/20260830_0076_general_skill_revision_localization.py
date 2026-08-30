"""
@Time       : 2026/08/30 10:35
@Author     : zhanglp8181
@File       : 20260830_0076_general_skill_revision_localization.py
@CallChain  : Alembic upgrade → Skill revision 中文展示记录 → 管理/开放广场 API
@Description: 增加 revision 级展示语言表，保留英文运行时正文并校验来源 checksum。

Revision ID: 20260830_0076
Revises: 20260830_0075
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql
import sqlmodel


revision: str = "20260830_0076"
down_revision: str | None = "20260830_0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建绑定精确 Skill 修订的中文展示表，允许 SQLite/MySQL 重复执行。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("general_skill_revision_localizations"):
        return
    long_text = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")
    op.create_table(
        "general_skill_revision_localizations",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column(
            "catalog_scope",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="platform",
        ),
        sa.Column("skill_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("revision_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column(
            "locale",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="zh-CN",
        ),
        sa.Column("localized_name", sqlmodel.sql.sqltypes.AutoString(length=191), nullable=False),
        sa.Column("localized_description", long_text, nullable=True),
        sa.Column("explanation_markdown", long_text, nullable=False),
        sa.Column(
            "translation_status",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("source_content_checksum", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("translation_checksum", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("translation_source", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("reviewed_by", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "(catalog_scope = 'platform' AND tenant_id IS NULL) OR "
            "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="ck_general_skill_revision_localization_scope",
        ),
        sa.CheckConstraint(
            "translation_status IN ('verified', 'draft', 'stale', 'rejected')",
            name="ck_general_skill_revision_localization_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_scope",
            "revision_id",
            "locale",
            name="uq_general_skill_revision_localization",
        ),
    )
    for column in (
        "tenant_id",
        "catalog_scope",
        "skill_id",
        "revision_id",
        "locale",
        "translation_status",
        "source_content_checksum",
        "translation_checksum",
        "created_at",
        "updated_at",
    ):
        op.create_index(
            op.f(f"ix_general_skill_revision_localizations_{column}"),
            "general_skill_revision_localizations",
            [column],
            unique=False,
        )
    op.create_index(
        op.f("ix_general_skill_revision_localization_lookup"),
        "general_skill_revision_localizations",
        ["catalog_scope", "tenant_id", "skill_id", "locale", "translation_status"],
        unique=False,
    )


def downgrade() -> None:
    """仅在没有中文展示记录时删除表，避免误删已审核展示历史。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("general_skill_revision_localizations"):
        return
    count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM general_skill_revision_localizations")
        ).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with general skill localization records")
    op.drop_table("general_skill_revision_localizations")
