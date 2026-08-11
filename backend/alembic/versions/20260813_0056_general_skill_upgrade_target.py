"""
@Time       : 2026/08/13 00:20
@Author     : zhanglp8181
@File       : 20260813_0056_general_skill_upgrade_target.py
@CallChain  : Alembic upgrade/downgrade → GeneralSkillImportJob → revision upgrade confirm
@Description: 为安全导入作业增加可选稳定 Skill 升级目标。

Revision ID: 20260813_0056
Revises: 20260812_0055
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0056"
down_revision: str | None = "20260812_0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以 expand 方式增加 nullable 升级目标，不改写已有导入作业。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("general_skill_import_jobs")
    }
    if "target_skill_id" not in columns:
        op.add_column(
            "general_skill_import_jobs",
            sa.Column("target_skill_id", sa.String(128), nullable=True),
        )
        op.create_index(
            "ix_general_skill_import_jobs_target_skill_id",
            "general_skill_import_jobs",
            ["target_skill_id"],
        )


def downgrade() -> None:
    """移除仅用于 S2 升级作业的 nullable 目标列。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("general_skill_import_jobs")
    }
    if "target_skill_id" in columns:
        op.drop_index(
            "ix_general_skill_import_jobs_target_skill_id",
            table_name="general_skill_import_jobs",
        )
        op.drop_column("general_skill_import_jobs", "target_skill_id")
