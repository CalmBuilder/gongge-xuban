"""
@Time       : 2026/07/22 14:20
@Author     : zhanglp8181
@File       : 20260722_0010_permission_catalog.py
@CallChain  : Alembic upgrade/downgrade → 权限目录/角色权限映射 → 组织角色管理与授权
@Description: 创建可检索权限目录和规范角色权限关系，并回填已有角色权限与业务域。

Revision ID: 20260722_0010
Revises: 20260722_0009
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0010"
down_revision: str | None = "20260722_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建两张规范表、索引并把现有 JSON 权限转为可查询关系。"""

    op.create_table(
        "permission_definitions",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("permission_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("resource", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "permission_code", name="uq_permission_tenant_code"),
    )
    op.create_table(
        "business_role_permissions",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("business_role_id", sa.String(128), nullable=False),
        sa.Column("permission_definition_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "business_role_id",
            "permission_definition_id",
            name="uq_business_role_permission",
        ),
    )
    for table_name, columns in {
        "permission_definitions": (
            "tenant_id",
            "permission_code",
            "category",
            "resource",
            "action",
            "scope",
            "status",
        ),
        "business_role_permissions": (
            "tenant_id",
            "business_role_id",
            "permission_definition_id",
        ),
    }.items():
        for column_name in columns:
            op.create_index(
                f"ix_{table_name}_{column_name}", table_name, [column_name], unique=False
            )
    _backfill_existing_role_permissions()


def downgrade() -> None:
    """移除规范关系和权限目录，保留业务角色原有 permissions_json 缓存。"""

    op.drop_table("business_role_permissions")
    op.drop_table("permission_definitions")


def _backfill_existing_role_permissions() -> None:
    """按现有角色 JSON 生成目录和映射，并把旧分类归一为受控业务域。"""

    connection = op.get_bind()
    roles = sa.table(
        "business_roles",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("role_code", sa.String),
        sa.column("category", sa.String),
        sa.column("permissions_json", sa.JSON),
    )
    permission_definitions = sa.table(
        "permission_definitions",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("permission_code", sa.String),
        sa.column("name", sa.String),
        sa.column("category", sa.String),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("scope", sa.String),
        sa.column("description", sa.String),
        sa.column("status", sa.String),
        sa.column("metadata_json", sa.JSON),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    mappings = sa.table(
        "business_role_permissions",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("business_role_id", sa.String),
        sa.column("permission_definition_id", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    definitions: dict[tuple[str, str], str] = {}
    for role in connection.execute(sa.select(roles)).mappings():
        category = _category_for_role(str(role["role_code"]))
        connection.execute(
            sa.update(roles).where(roles.c.id == role["id"]).values(category=category)
        )
        for permission_code in _permission_codes(role["permissions_json"]):
            definition_key = (str(role["tenant_id"]), permission_code)
            permission_id = definitions.get(definition_key)
            if permission_id is None:
                permission_id = f"permission_{uuid.uuid4().hex}"
                definitions[definition_key] = permission_id
                resource, action, scope = _parse_permission_code(permission_code)
                connection.execute(
                    permission_definitions.insert().values(
                        id=permission_id,
                        tenant_id=role["tenant_id"],
                        permission_code=permission_code,
                        name=permission_code,
                        category=category,
                        resource=resource,
                        action=action,
                        scope=scope,
                        description="由历史业务角色权限回填。",
                        status="active",
                        metadata_json={"source": "alembic_0010_backfill"},
                        created_at=now,
                        updated_at=now,
                    )
                )
            connection.execute(
                mappings.insert().values(
                    id=f"roleperm_{uuid.uuid4().hex}",
                    tenant_id=role["tenant_id"],
                    business_role_id=role["id"],
                    permission_definition_id=permission_id,
                    created_at=now,
                )
            )


def _permission_codes(raw_value: object) -> list[str]:
    """兼容 SQLite 列表和 MySQL JSON 文本，返回去重后的历史权限编码。"""

    value = raw_value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _parse_permission_code(permission_code: str) -> tuple[str, str, str | None]:
    """按 resource.action[:scope] 兼容解析历史权限，异常编码仍可保留审计。"""

    base, separator, scope = permission_code.partition(":")
    resource, dot, action = base.rpartition(".")
    if not dot:
        return base, "use", scope or None
    return resource, action, scope if separator else None


def _category_for_role(role_code: str) -> str:
    """按稳定角色前缀把现有混合分类迁移到六个受控业务域。"""

    for prefix, category in (
        ("hr_", "human_resources"),
        ("finance_", "finance"),
        ("admin_", "administration"),
        ("it_", "information_technology"),
        ("legal_", "legal_compliance"),
    ):
        if role_code.startswith(prefix):
            return category
    return "cross_functional"
