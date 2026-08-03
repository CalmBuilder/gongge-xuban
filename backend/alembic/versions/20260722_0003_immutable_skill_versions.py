"""
@Time       : 2026/07/22 09:10
@Author     : zhanglp8181
@File       : 20260722_0003_immutable_skill_versions.py
@CallChain  : Alembic upgrade/downgrade → skill_versions → SOP 发布与回滚
@Description: 增加 SOP 发布快照的校验和、编译来源、发布时间和派生关系字段。

Revision ID: 20260722_0003
Revises: 20260718_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0003"
down_revision: str | None = "20260718_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 SOP 版本增加不可变校验、编译来源和派生关系字段。"""

    op.add_column("skill_versions", sa.Column("content_checksum", sa.String(64), nullable=True))
    op.add_column(
        "skill_versions",
        sa.Column("compiled_definition_checksum", sa.String(64), nullable=True),
    )
    op.add_column("skill_versions", sa.Column("meta_model_version", sa.Integer(), nullable=True))
    op.add_column("skill_versions", sa.Column("source_schema_version", sa.Integer(), nullable=True))
    op.add_column("skill_versions", sa.Column("published_at", sa.DateTime(), nullable=True))
    op.add_column(
        "skill_versions",
        sa.Column("derived_from_version_id", sa.String(512), nullable=True),
    )
    op.create_index(
        "ix_skill_versions_content_checksum",
        "skill_versions",
        ["content_checksum"],
        unique=False,
    )
    op.create_index(
        "ix_skill_versions_derived_from_version_id",
        "skill_versions",
        ["derived_from_version_id"],
        unique=False,
    )


def downgrade() -> None:
    """移除 SOP 版本不可变元数据字段和辅助索引。"""

    op.drop_index("ix_skill_versions_derived_from_version_id", table_name="skill_versions")
    op.drop_index("ix_skill_versions_content_checksum", table_name="skill_versions")
    op.drop_column("skill_versions", "derived_from_version_id")
    op.drop_column("skill_versions", "published_at")
    op.drop_column("skill_versions", "source_schema_version")
    op.drop_column("skill_versions", "meta_model_version")
    op.drop_column("skill_versions", "compiled_definition_checksum")
    op.drop_column("skill_versions", "content_checksum")
