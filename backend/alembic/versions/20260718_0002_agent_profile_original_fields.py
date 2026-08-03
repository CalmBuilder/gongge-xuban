"""add agent profile original language fields

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "20260718_0002"
down_revision: str | None = "20260718_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column("original_name", sa.String(length=191), nullable=True),
    )
    op.add_column(
        "agent_profiles",
        sa.Column(
            "original_description",
            sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_profiles",
        sa.Column(
            "original_persona_prompt",
            sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_profiles",
        sa.Column("original_locale", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "original_locale")
    op.drop_column("agent_profiles", "original_persona_prompt")
    op.drop_column("agent_profiles", "original_description")
    op.drop_column("agent_profiles", "original_name")
