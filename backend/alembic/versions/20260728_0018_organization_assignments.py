"""
@Time       : 2026/07/28 15:40
@Author     : zhanglp8181
@File       : 20260728_0018_organization_assignments.py
@CallChain  : Alembic upgrade/downgrade → 岗位类型/组织归属/岗位任职结构与旧部门映射
@Description: 创建 M2-B 任职模型，安全映射可识别旧部门并记录无法判断的数据。

Revision ID: 20260728_0018
Revises: 20260728_0017
"""

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import re

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0018"
down_revision: str | None = "20260728_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POSITION_TYPE_ITEMS = (
    ("management", "管理岗位", 10),
    ("professional", "专业岗位", 20),
    ("operations", "运营岗位", 30),
    ("support", "支持岗位", 40),
    ("project", "项目岗位", 50),
)
LEGACY_DEPARTMENT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")


def upgrade() -> None:
    """创建组织归属、岗位、岗位任职和迁移问题表，并迁移活动员工的旧部门。"""

    _create_assignment_tables()
    _seed_position_types()
    _migrate_legacy_departments()


def downgrade() -> None:
    """移除 M2-B 结构和岗位类型码表，保留 M2-A 组织树。"""

    connection = op.get_bind()
    set_ids = connection.execute(
        sa.text("SELECT id FROM code_sets WHERE set_code = 'position_type'")
    ).scalars().all()
    if set_ids:
        code_items = sa.table("code_items", sa.column("code_set_id"))
        code_sets = sa.table("code_sets", sa.column("id"))
        connection.execute(
            code_items.delete().where(code_items.c.code_set_id.in_(set_ids))
        )
        connection.execute(code_sets.delete().where(code_sets.c.id.in_(set_ids)))
    op.drop_table("organization_migration_issues")
    op.drop_table("position_assignments")
    op.drop_table("positions")
    op.drop_table("member_org_assignments")


def _create_assignment_tables() -> None:
    """创建不使用永久主归属唯一约束的任职历史结构。"""

    op.create_table(
        "member_org_assignments",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("employee_profile_id", sa.String(128), nullable=False),
        sa.Column("org_unit_id", sa.String(128), nullable=False),
        sa.Column("assignment_type", sa.String(64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_until", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(
        "member_org_assignments",
        (
            "tenant_id",
            "employee_profile_id",
            "org_unit_id",
            "assignment_type",
            "is_primary",
            "effective_from",
            "effective_until",
            "status",
        ),
    )
    op.create_table(
        "positions",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_unit_id", sa.String(128), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("position_type_code", sa.String(128), nullable=False),
        sa.Column("grade_code", sa.String(128), nullable=True),
        sa.Column("reports_to_position_id", sa.String(128), nullable=True),
        sa.Column("headcount_limit", sa.Integer(), nullable=True),
        sa.Column("responsibility", sa.Text(), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_position_tenant_code"),
    )
    _indexes(
        "positions",
        (
            "tenant_id",
            "org_unit_id",
            "code",
            "position_type_code",
            "grade_code",
            "reports_to_position_id",
            "status",
        ),
    )
    op.create_table(
        "position_assignments",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("employee_profile_id", sa.String(128), nullable=False),
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("assignment_type", sa.String(64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_until", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(
        "position_assignments",
        (
            "tenant_id",
            "employee_profile_id",
            "position_id",
            "assignment_type",
            "is_primary",
            "effective_from",
            "effective_until",
            "status",
        ),
    )
    op.create_table(
        "organization_migration_issues",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("employee_profile_id", sa.String(128), nullable=False),
        sa.Column("source_field", sa.String(64), nullable=False),
        sa.Column("source_value", sa.String(1024), nullable=True),
        sa.Column("issue_code", sa.String(64), nullable=False),
        sa.Column("resolution_status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(
        "organization_migration_issues",
        (
            "tenant_id",
            "employee_profile_id",
            "issue_code",
            "resolution_status",
        ),
    )


def _seed_position_types() -> None:
    """为每个租户写入可配置的岗位类型内置项。"""

    connection = op.get_bind()
    tenants = connection.execute(sa.text("SELECT id FROM tenants")).scalars().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    for tenant_id in tenants:
        code_set_id = _stable_id("codeset", tenant_id, "position_type")
        connection.execute(
            sa.text(
                "INSERT INTO code_sets "
                "(id, tenant_id, set_code, name, description, allow_custom_items, "
                "is_system, status, revision, created_at, updated_at) "
                "VALUES (:id, :tenant_id, 'position_type', :name, :description, "
                ":allow_custom_items, :is_system, 'active', 0, :now, :now)"
            ),
            {
                "id": code_set_id,
                "tenant_id": tenant_id,
                "name": "岗位类型",
                "description": "岗位的业务分类，不直接产生任职或权限。",
                "allow_custom_items": True,
                "is_system": True,
                "now": now,
            },
        )
        for item_code, name, sort_order in POSITION_TYPE_ITEMS:
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
                    "id": _stable_id("codeitem", tenant_id, f"position:{item_code}"),
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


def _migrate_legacy_departments() -> None:
    """把可识别部门编码映射为根下节点，其余人员安全归根并写治理问题。"""

    connection = op.get_bind()
    profiles = connection.execute(
        sa.text(
            "SELECT id, tenant_id, department_id, join_date "
            "FROM employee_profiles WHERE status = 'active'"
        )
    ).mappings().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    roots = {
        row["tenant_id"]: row
        for row in connection.execute(
            sa.text(
                "SELECT id, tenant_id, tree_path FROM organization_units "
                "WHERE is_root = :is_root"
            ),
            {"is_root": True},
        ).mappings()
    }
    units_by_tenant: dict[str, dict[str, dict[str, object]]] = {}
    for row in connection.execute(
        sa.text("SELECT id, tenant_id, code, tree_path FROM organization_units")
    ).mappings():
        units_by_tenant.setdefault(row["tenant_id"], {})[row["code"].casefold()] = dict(row)

    for profile in profiles:
        tenant_id = profile["tenant_id"]
        root = roots[tenant_id]
        source = (profile["department_id"] or "").strip()
        target = root
        issue_code: str | None = None
        if source and source.upper() != "ROOT":
            normalized_key = source.casefold()
            existing = units_by_tenant.setdefault(tenant_id, {}).get(normalized_key)
            if existing is not None:
                if existing["code"] == source:
                    target = existing
                else:
                    issue_code = "LEGACY_DEPARTMENT_CODE_CONFLICT"
            elif LEGACY_DEPARTMENT_PATTERN.fullmatch(source):
                unit_id = _stable_id("orglegacy", tenant_id, source)
                target = {
                    "id": unit_id,
                    "tenant_id": tenant_id,
                    "code": source,
                    "tree_path": f"{root['tree_path']}/{unit_id}",
                }
                connection.execute(
                    sa.text(
                        "INSERT INTO organization_units "
                        "(id, tenant_id, parent_id, code, name, unit_type_code, tree_path, "
                        "depth, sort_order, is_root, root_tenant_id, status, created_at, updated_at) "
                        "VALUES (:id, :tenant_id, :parent_id, :code, :name, 'department', "
                        ":tree_path, 1, 0, :is_root, NULL, 'active', :now, :now)"
                    ),
                    {
                        "id": unit_id,
                        "tenant_id": tenant_id,
                        "parent_id": root["id"],
                        "code": source,
                        "name": source,
                        "tree_path": target["tree_path"],
                        "is_root": False,
                        "now": now,
                    },
                )
                units_by_tenant[tenant_id][normalized_key] = target
            else:
                issue_code = "UNRECOGNIZED_LEGACY_DEPARTMENT"
        connection.execute(
            sa.text(
                "INSERT INTO member_org_assignments "
                "(id, tenant_id, employee_profile_id, org_unit_id, assignment_type, "
                "is_primary, effective_from, effective_until, status, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :profile_id, :org_unit_id, 'primary', "
                ":is_primary, :effective_from, NULL, 'active', :now, :now)"
            ),
            {
                "id": _stable_id("memberorg", tenant_id, profile["id"]),
                "tenant_id": tenant_id,
                "profile_id": profile["id"],
                "org_unit_id": target["id"],
                "is_primary": True,
                "effective_from": profile["join_date"] or now,
                "now": now,
            },
        )
        if issue_code:
            connection.execute(
                sa.text(
                    "INSERT INTO organization_migration_issues "
                    "(id, tenant_id, employee_profile_id, source_field, source_value, "
                    "issue_code, resolution_status, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :profile_id, 'department_id', :source_value, "
                    ":issue_code, 'pending', :now, :now)"
                ),
                {
                    "id": _stable_id("orgissue", tenant_id, profile["id"]),
                    "tenant_id": tenant_id,
                    "profile_id": profile["id"],
                    "source_value": source,
                    "issue_code": issue_code,
                    "now": now,
                },
            )


def _indexes(table_name: str, column_names: tuple[str, ...]) -> None:
    """为领域查询字段创建与 SQLModel 声明一致的普通索引。"""

    for column_name in column_names:
        op.create_index(
            f"ix_{table_name}_{column_name}",
            table_name,
            [column_name],
            unique=False,
        )


def _stable_id(prefix: str, tenant_id: str, code: str) -> str:
    """基于租户和来源编码生成可重复的迁移标识。"""

    digest = hashlib.sha256(f"{tenant_id}:{code}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"
