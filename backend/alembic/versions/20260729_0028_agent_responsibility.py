"""
@Time       : 2026/07/29 18:20
@Author     : zhanglp8181
@File       : 20260729_0028_agent_responsibility.py
@CallChain  : Alembic upgrade/downgrade → 数字员工治理责任组织
@Description: 为数字员工增加可空责任组织事实及租户查询索引，不由该字段派生授权。

Revision ID: 20260729_0028
Revises: 20260729_0027
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260729_0028"
down_revision: str | None = "20260729_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加责任组织字段和租户内责任组织查询索引。"""

    op.add_column(
        "agent_profiles",
        sa.Column("responsible_org_unit_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_agent_profiles_responsible_org_unit_id",
        "agent_profiles",
        ["responsible_org_unit_id"],
    )
    op.create_index(
        "ix_agent_profiles_tenant_responsible_org",
        "agent_profiles",
        ["tenant_id", "responsible_org_unit_id"],
    )


def downgrade() -> None:
    """移除责任组织索引和字段。"""

    op.drop_index(
        "ix_agent_profiles_tenant_responsible_org",
        table_name="agent_profiles",
    )
    op.drop_index(
        "ix_agent_profiles_responsible_org_unit_id",
        table_name="agent_profiles",
    )
    op.drop_column("agent_profiles", "responsible_org_unit_id")
