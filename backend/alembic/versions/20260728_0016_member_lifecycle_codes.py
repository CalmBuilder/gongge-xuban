"""
@Time       : 2026/07/28 11:30
@Author     : zhanglp8181
@File       : 20260728_0016_member_lifecycle_codes.py
@CallChain  : Alembic upgrade/downgrade → 租户成员生命周期与数据库码表
@Description: 增加成员及员工档案生命周期字段，并初始化租户级成员类别码表。

Revision ID: 20260728_0016
Revises: 20260727_0015
"""

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0016"
down_revision: str | None = "20260727_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMBER_CATEGORY_ITEMS = (
    ("employee", "正式员工", 10),
    ("contractor", "合同员工", 20),
    ("consultant", "顾问", 30),
    ("intern", "实习生", 40),
    ("external_collaborator", "外部协作者", 50),
)


def upgrade() -> None:
    """创建码表并无损补齐现有成员和员工档案的生命周期事实。"""

    _create_reference_data_tables()
    _add_member_lifecycle_columns()
    _seed_existing_tenant_member_categories()


def downgrade() -> None:
    """移除本批码表及生命周期字段，恢复上一版结构。"""

    with op.batch_alter_table("employee_profiles") as batch_op:
        batch_op.drop_column("leave_date")
        batch_op.drop_column("join_date")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("left_at")
        batch_op.drop_column("joined_at")
        batch_op.drop_index("ix_users_member_category_code")
        batch_op.drop_column("member_category_code")
        batch_op.drop_index("ix_users_membership_status")
        batch_op.drop_column("membership_status")
    op.drop_table("code_items")
    op.drop_table("code_sets")


def _create_reference_data_tables() -> None:
    """创建租户隔离、编码稳定且支持乐观并发的码表结构。"""

    op.create_table(
        "code_sets",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("set_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column("allow_custom_items", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "set_code", name="uq_code_set_tenant_code"),
    )
    op.create_table(
        "code_items",
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("code_set_id", sa.String(128), nullable=False),
        sa.Column("item_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(191), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column("parent_item_id", sa.String(128), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.String(128), nullable=True),
        sa.Column("updated_by_user_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "code_set_id",
            "item_code",
            name="uq_code_item_tenant_set_code",
        ),
    )
    for table_name, columns in {
        "code_sets": ("tenant_id", "set_code", "status"),
        "code_items": (
            "tenant_id",
            "code_set_id",
            "item_code",
            "parent_item_id",
            "status",
            "created_by_user_id",
            "updated_by_user_id",
        ),
    }.items():
        for column_name in columns:
            op.create_index(
                f"ix_{table_name}_{column_name}",
                table_name,
                [column_name],
                unique=False,
            )


def _add_member_lifecycle_columns() -> None:
    """以现有创建时间回填加入时间，避免迁移伪造新的业务日期。"""

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "membership_status",
                sa.String(64),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.add_column(
            sa.Column(
                "member_category_code",
                sa.String(128),
                nullable=False,
                server_default="employee",
            )
        )
        batch_op.add_column(sa.Column("joined_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("left_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            "ix_users_membership_status", ["membership_status"], unique=False
        )
        batch_op.create_index(
            "ix_users_member_category_code", ["member_category_code"], unique=False
        )
    op.execute(sa.text("UPDATE users SET joined_at = created_at WHERE joined_at IS NULL"))
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("joined_at", existing_type=sa.DateTime(), nullable=False)

    with op.batch_alter_table("employee_profiles") as batch_op:
        batch_op.add_column(sa.Column("join_date", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("leave_date", sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE employee_profiles SET join_date = created_at WHERE join_date IS NULL"
        )
    )
    with op.batch_alter_table("employee_profiles") as batch_op:
        batch_op.alter_column("join_date", existing_type=sa.DateTime(), nullable=False)


def _seed_existing_tenant_member_categories() -> None:
    """为迁移时已存在的每个租户写入同一组稳定成员类别码项。"""

    connection = op.get_bind()
    tenant_ids = connection.execute(sa.text("SELECT id FROM tenants")).scalars().all()
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
    for tenant_id in tenant_ids:
        code_set_id = _stable_seed_id("codeset", tenant_id, "member_category")
        op.bulk_insert(
            code_sets,
            [
                {
                    "id": code_set_id,
                    "tenant_id": tenant_id,
                    "set_code": "member_category",
                    "name": "成员类别",
                    "description": "成员的业务用工或协作类别，不产生权限。",
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
                    "id": _stable_seed_id("codeitem", tenant_id, item_code),
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
                for item_code, name, sort_order in MEMBER_CATEGORY_ITEMS
            ],
        )


def _stable_seed_id(prefix: str, tenant_id: str, code: str) -> str:
    """基于租户和编码生成跨数据库一致、可重复计算的迁移种子 ID。"""

    digest = hashlib.sha256(f"{tenant_id}:{code}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"
