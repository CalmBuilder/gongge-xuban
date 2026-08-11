"""
@Time       : 2026/08/12 22:05
@Author     : zhanglp8181
@File       : 20260812_0050_general_skill_dependencies.py
@CallChain  : Alembic upgrade/downgrade → GeneralSkill dependency graph → S1 confirm
@Description: 增加经人工确认、固定到父子修订的通用 Skill 依赖边。

Revision ID: 20260812_0050
Revises: 20260811_0049
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0050"
down_revision: str | None = "20260811_0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入创建稳定修订依赖表及父修订查询索引。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("general_skill_dependencies"):
        return
    op.create_table(
        "general_skill_dependencies",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(512), nullable=False),
        sa.Column("parent_skill_id", sa.String(512), nullable=False),
        sa.Column("parent_revision_id", sa.String(512), nullable=False),
        sa.Column("child_skill_id", sa.String(512), nullable=False),
        sa.Column("child_revision_id", sa.String(512), nullable=False),
        sa.Column("dependency_kind", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("allow_user_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("edge_checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "dependency_kind IN ('required', 'optional')",
            name="ck_general_skill_dependency_kind",
        ),
        sa.CheckConstraint(
            "source IN ('manifest', 'human_confirmed')",
            name="ck_general_skill_dependency_source",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_general_skill_dependency_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "parent_revision_id",
            "child_revision_id",
            name="uq_general_skill_dependency_revision_edge",
        ),
    )
    op.create_index(
        "ix_general_skill_dependency_parent",
        "general_skill_dependencies",
        ["tenant_id", "parent_skill_id", "parent_revision_id", "status"],
    )


def downgrade() -> None:
    """仅在依赖表无数据时移除本批结构，避免丢失已审核依赖。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("general_skill_dependencies"):
        return
    count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM general_skill_dependencies")).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with general skill dependency rows")
    op.drop_table("general_skill_dependencies")
