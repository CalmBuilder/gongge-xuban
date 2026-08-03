"""
@Time       : 2026/07/28 11:45
@Author     : zhanglp8181
@File       : 20260728_0017_organization_units.py
@CallChain  : Alembic upgrade/downgrade → 组织类型码表与租户单根组织树
@Description: 创建组织树结构，为历史租户初始化唯一稳定根节点和组织类型码项。

Revision ID: 20260728_0017
Revises: 20260728_0016
"""

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0017"
down_revision: str | None = "20260728_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORGANIZATION_UNIT_TYPE_ITEMS = (
    ("company", "企业", 10),
    ("division", "事业部", 20),
    ("department", "部门", 30),
    ("center", "中心", 40),
    ("team", "团队", 50),
    ("project", "项目组", 60),
)


def upgrade() -> None:
    """创建组织树，并为每个历史租户初始化类型码表和唯一根节点。"""

    op.create_table(
        "organization_units",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("parent_id", sa.String(128), nullable=True),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("unit_type_code", sa.String(128), nullable=False),
        sa.Column("tree_path", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_root", sa.Boolean(), nullable=False),
        sa.Column("root_tenant_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "code",
            name="uq_organization_unit_tenant_code",
        ),
    )
    for column_name in (
        "tenant_id",
        "parent_id",
        "code",
        "unit_type_code",
        "is_root",
        "status",
    ):
        op.create_index(
            f"ix_organization_units_{column_name}",
            "organization_units",
            [column_name],
            unique=False,
        )
    op.create_index(
        "uq_organization_unit_root_tenant",
        "organization_units",
        ["root_tenant_id"],
        unique=True,
    )
    _seed_organization_foundations()


def downgrade() -> None:
    """移除组织树及本迁移创建的组织类型码表，不修改其他成员码表。"""

    connection = op.get_bind()
    set_ids = connection.execute(
        sa.text(
            "SELECT id FROM code_sets "
            "WHERE set_code = 'organization_unit_type'"
        )
    ).scalars().all()
    if set_ids:
        code_items = sa.table("code_items", sa.column("code_set_id"))
        code_sets = sa.table("code_sets", sa.column("id"))
        connection.execute(
            code_items.delete().where(code_items.c.code_set_id.in_(set_ids))
        )
        connection.execute(code_sets.delete().where(code_sets.c.id.in_(set_ids)))
    op.drop_table("organization_units")


def _seed_organization_foundations() -> None:
    """按租户写入组织类型和根节点，稳定 ID 保证双方言结果一致。"""

    connection = op.get_bind()
    tenants = connection.execute(sa.text("SELECT id, name FROM tenants")).mappings().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    code_sets = sa.table(
        "code_sets",
        sa.column("id"),
        sa.column("tenant_id"),
        sa.column("set_code"),
        sa.column("name"),
        sa.column("description"),
        sa.column("allow_custom_items"),
        sa.column("is_system"),
        sa.column("status"),
        sa.column("revision"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    code_items = sa.table(
        "code_items",
        sa.column("id"),
        sa.column("tenant_id"),
        sa.column("code_set_id"),
        sa.column("item_code"),
        sa.column("name"),
        sa.column("description"),
        sa.column("parent_item_id"),
        sa.column("sort_order"),
        sa.column("is_builtin"),
        sa.column("status"),
        sa.column("metadata_json", sa.JSON()),
        sa.column("revision"),
        sa.column("created_by_user_id"),
        sa.column("updated_by_user_id"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    organization_units = sa.table(
        "organization_units",
        sa.column("id"),
        sa.column("tenant_id"),
        sa.column("parent_id"),
        sa.column("code"),
        sa.column("name"),
        sa.column("unit_type_code"),
        sa.column("tree_path"),
        sa.column("depth"),
        sa.column("sort_order"),
        sa.column("is_root"),
        sa.column("root_tenant_id"),
        sa.column("status"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    for tenant in tenants:
        tenant_id = tenant["id"]
        code_set_id = _stable_id("codeset", tenant_id, "organization_unit_type")
        root_id = _stable_id("orgroot", tenant_id, "organization-root")
        op.bulk_insert(
            code_sets,
            [
                {
                    "id": code_set_id,
                    "tenant_id": tenant_id,
                    "set_code": "organization_unit_type",
                    "name": "组织类型",
                    "description": "组织节点的业务分类，不产生层级或权限。",
                    "allow_custom_items": True,
                    "is_system": True,
                    "status": "active",
                    "revision": 0,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        op.bulk_insert(
            code_items,
            [
                {
                    "id": _stable_id("codeitem", tenant_id, item_code),
                    "tenant_id": tenant_id,
                    "code_set_id": code_set_id,
                    "item_code": item_code,
                    "name": name,
                    "description": None,
                    "parent_item_id": None,
                    "sort_order": sort_order,
                    "is_builtin": True,
                    "status": "active",
                    "metadata_json": {},
                    "revision": 0,
                    "created_by_user_id": None,
                    "updated_by_user_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for item_code, name, sort_order in ORGANIZATION_UNIT_TYPE_ITEMS
            ],
        )
        op.bulk_insert(
            organization_units,
            [
                {
                    "id": root_id,
                    "tenant_id": tenant_id,
                    "parent_id": None,
                    "code": "ROOT",
                    "name": tenant["name"],
                    "unit_type_code": "company",
                    "tree_path": root_id,
                    "depth": 0,
                    "sort_order": 0,
                    "is_root": True,
                    "root_tenant_id": tenant_id,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )


def _stable_id(prefix: str, tenant_id: str, code: str) -> str:
    """基于租户和业务编码生成可重复计算的迁移种子 ID。"""

    digest = hashlib.sha256(f"{tenant_id}:{code}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"
