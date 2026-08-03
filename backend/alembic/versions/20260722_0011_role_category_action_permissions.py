"""
@Time       : 2026/07/22 16:05
@Author     : zhanglp8181
@File       : 20260722_0011_role_category_action_permissions.py
@CallChain  : Alembic upgrade/downgrade → 分类目录/工作项动作契约 → 组织治理与办理鉴权
@Description: 创建可治理角色分类目录，并为工作项增加冻结的动作权限映射。

Revision ID: 20260722_0011
Revises: 20260722_0010
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0011"
down_revision: str | None = "20260722_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ROLE_CATEGORIES = (
    ("human_resources", "人事", "员工服务、假勤和人事证明", "hr"),
    ("finance", "财务", "报销、预算和财务复核", "finance"),
    ("administration", "行政", "会议室、用品和印章事务", "admin"),
    ("information_technology", "IT", "故障、权限和技术支持", "it"),
    ("legal_compliance", "法务合规", "合同、条款和尽调", "legal"),
    ("cross_functional", "跨部门", "跨业务域流程治理和演示", "cross"),
)


def upgrade() -> None:
    """创建分类表、回填每个租户，并为既有工作项补空动作权限契约。"""

    op.create_table(
        "business_role_categories",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("category_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("role_code_prefix", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "category_code",
            name="uq_business_role_category_code",
        ),
    )
    for column_name in ("tenant_id", "category_code", "status"):
        op.create_index(
            f"ix_business_role_categories_{column_name}",
            "business_role_categories",
            [column_name],
            unique=False,
        )
    op.add_column(
        "sop_work_items",
        sa.Column("action_permissions_json", sa.JSON(), nullable=True),
    )
    _backfill_role_categories()
    connection = op.get_bind()
    work_items = sa.table(
        "sop_work_items",
        sa.column("action_permissions_json", sa.JSON),
    )
    connection.execute(
        work_items.update()
        .where(work_items.c.action_permissions_json.is_(None))
        .values(action_permissions_json={})
    )
    op.alter_column(
        "sop_work_items",
        "action_permissions_json",
        existing_type=sa.JSON(),
        nullable=False,
    )


def downgrade() -> None:
    """移除动作权限快照和分类目录，不改动角色上的分类编码。"""

    op.drop_column("sop_work_items", "action_permissions_json")
    op.drop_table("business_role_categories")


def _backfill_role_categories() -> None:
    """为 tenants 表中的每个租户写入六个内置业务域。"""

    connection = op.get_bind()
    tenant_ids = [row[0] for row in connection.execute(sa.text("SELECT id FROM tenants"))]
    now = datetime.now(UTC).replace(tzinfo=None)
    table = sa.table(
        "business_role_categories",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("category_code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("role_code_prefix", sa.String),
        sa.column("status", sa.String),
        sa.column("metadata_json", sa.JSON),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    for tenant_id in tenant_ids:
        for code, name, description, prefix in ROLE_CATEGORIES:
            connection.execute(
                table.insert().values(
                    id=f"rolecat_{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    category_code=code,
                    name=name,
                    description=description,
                    role_code_prefix=prefix,
                    status="active",
                    metadata_json={"source": "alembic_0011_builtin"},
                    created_at=now,
                    updated_at=now,
                )
            )
