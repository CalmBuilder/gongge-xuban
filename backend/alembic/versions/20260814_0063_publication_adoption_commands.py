"""
@Time       : 2026/08/14 00:20
@Author     : zhanglp8181
@File       : 20260814_0063_publication_adoption_commands.py
@CallChain  : Alembic upgrade/downgrade → publication Release adoption
@Description: 创建跨重试持久化的发布物采用幂等账本，覆盖 Skill 绑定和整 Agent 克隆。

Revision ID: 20260814_0063
Revises: 20260813_0062
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260814_0063"
down_revision: str | None = "20260813_0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建采用命令表、唯一幂等键和常用查询索引。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("publication_adoption_commands"):
        return
    op.create_table(
        "publication_adoption_commands",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("actor_user_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_checksum", sa.String(128), nullable=False),
        sa.Column("release_id", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("target_agent_id", sa.String(128), nullable=True),
        sa.Column("binding_id", sa.String(128), nullable=True),
        sa.Column("adopted_agent_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('general_skill', 'agent')",
            name="ck_publication_adoption_resource_type",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "actor_user_id",
            "idempotency_key",
            name="uq_publication_adoption_idempotency",
        ),
    )
    for column in (
        "tenant_id",
        "actor_user_id",
        "idempotency_key",
        "request_checksum",
        "release_id",
        "resource_type",
        "target_agent_id",
        "binding_id",
        "adopted_agent_id",
    ):
        op.create_index(
            f"ix_publication_adoption_commands_{column}",
            "publication_adoption_commands",
            [column],
        )


def downgrade() -> None:
    """存在采用事实时拒绝删除审计账本。"""

    bind = op.get_bind()
    count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM publication_adoption_commands")).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with publication adoption commands")
    op.drop_table("publication_adoption_commands")
