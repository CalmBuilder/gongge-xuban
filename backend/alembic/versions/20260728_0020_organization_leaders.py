"""
@Time       : 2026/07/28 15:25
@Author     : zhanglp8181
@File       : 20260728_0020_organization_leaders.py
@CallChain  : Alembic upgrade/downgrade → 负责人类型码表与组织负责人任期
@Description: 创建负责人历史关系并为每个租户初始化四个可配置负责人类型。

Revision ID: 20260728_0020
Revises: 20260728_0019
"""

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0020"
down_revision: str | None = "20260728_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEADER_TYPE_ITEMS = (
    ("primary", "主要负责人", 10),
    ("deputy", "副负责人", 20),
    ("acting", "代理负责人", 30),
    ("project", "项目负责人", 40),
)


def upgrade() -> None:
    """创建组织负责人有效期关系并初始化负责人类型。"""

    op.create_table(
        "organization_leader_assignments",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_unit_id", sa.String(128), nullable=False),
        sa.Column("employee_profile_id", sa.String(128), nullable=False),
        sa.Column("position_assignment_id", sa.String(128), nullable=True),
        sa.Column("leader_type_code", sa.String(128), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_until", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column_name in (
        "tenant_id",
        "org_unit_id",
        "employee_profile_id",
        "position_assignment_id",
        "leader_type_code",
        "effective_from",
        "effective_until",
        "status",
        "source_kind",
        "created_by_user_id",
    ):
        op.create_index(
            f"ix_organization_leader_assignments_{column_name}",
            "organization_leader_assignments",
            [column_name],
            unique=False,
        )
    _seed_leader_types()


def downgrade() -> None:
    """移除负责人关系和负责人类型，不修改 M2 组织及任职历史。"""

    op.drop_table("organization_leader_assignments")
    connection = op.get_bind()
    set_ids = connection.execute(
        sa.text(
            "SELECT id FROM code_sets "
            "WHERE set_code = 'organization_leader_type'"
        )
    ).scalars().all()
    if set_ids:
        code_items = sa.table("code_items", sa.column("code_set_id"))
        code_sets = sa.table("code_sets", sa.column("id"))
        connection.execute(
            code_items.delete().where(code_items.c.code_set_id.in_(set_ids))
        )
        connection.execute(code_sets.delete().where(code_sets.c.id.in_(set_ids)))


def _seed_leader_types() -> None:
    """为每个历史租户写入稳定负责人类型，不推断任何负责人。"""

    connection = op.get_bind()
    tenants = connection.execute(sa.text("SELECT id FROM tenants")).scalars().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    for tenant_id in tenants:
        code_set_id = _stable_id("codeset", tenant_id, "organization_leader_type")
        connection.execute(
            sa.text(
                "INSERT INTO code_sets "
                "(id, tenant_id, set_code, name, description, allow_custom_items, "
                "is_system, status, revision, created_at, updated_at) "
                "VALUES (:id, :tenant_id, 'organization_leader_type', :name, "
                ":description, :allow_custom_items, :is_system, 'active', 0, :now, :now)"
            ),
            {
                "id": code_set_id,
                "tenant_id": tenant_id,
                "name": "负责人类型",
                "description": "组织责任关系的业务分类，不直接产生角色或权限。",
                "allow_custom_items": True,
                "is_system": True,
                "now": now,
            },
        )
        for item_code, name, sort_order in LEADER_TYPE_ITEMS:
            connection.execute(
                sa.text(
                    "INSERT INTO code_items "
                    "(id, tenant_id, code_set_id, item_code, name, description, "
                    "parent_item_id, sort_order, is_builtin, status, metadata_json, "
                    "revision, created_by_user_id, updated_by_user_id, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :code_set_id, :item_code, :name, NULL, "
                    "NULL, :sort_order, :is_builtin, 'active', :metadata_json, 0, "
                    "NULL, NULL, :now, :now)"
                ),
                {
                    "id": _stable_id("codeitem", tenant_id, f"leader:{item_code}"),
                    "tenant_id": tenant_id,
                    "code_set_id": code_set_id,
                    "item_code": item_code,
                    "name": name,
                    "sort_order": sort_order,
                    "is_builtin": True,
                    "metadata_json": "{}",
                    "now": now,
                },
            )


def _stable_id(prefix: str, tenant_id: str, code: str) -> str:
    """基于租户和码项生成跨方言稳定迁移标识。"""

    digest = hashlib.sha256(f"{tenant_id}:{code}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"
