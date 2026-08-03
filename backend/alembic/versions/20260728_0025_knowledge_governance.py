"""
@Time       : 2026/07/28 23:25
@Author     : zhanglp8181
@File       : 20260728_0025_knowledge_governance.py
@CallChain  : Alembic upgrade/downgrade → M5-A 知识责任、访问范围与下载治理
@Description: 增加知识库正式治理字段和组织访问根，并按可信创建者事实保守回填。

Revision ID: 20260728_0025
Revises: 20260728_0024
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0025"
down_revision: str | None = "20260728_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加知识治理字段、保守回填 owner，并创建规范化组织访问关系。"""

    op.add_column(
        "knowledge_bases",
        sa.Column("owner_user_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("responsible_org_unit_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "access_scope",
            sa.String(length=64),
            nullable=False,
            server_default="owner",
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "download_policy",
            sa.String(length=64),
            nullable=False,
            server_default="restricted",
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    bind = op.get_bind()
    _backfill_knowledge_owners(bind)
    op.create_table(
        "knowledge_base_org_access",
        sa.Column("id", sa.String(length=512), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=128), nullable=False),
        sa.Column("org_unit_id", sa.String(length=128), nullable=False),
        sa.Column("include_descendants", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "org_unit_id",
            name="uq_knowledge_base_org_access",
        ),
    )
    op.create_index(
        "ix_kb_org_access_tenant_org_status",
        "knowledge_base_org_access",
        ["tenant_id", "org_unit_id", "status"],
    )
    op.create_index(
        "ix_knowledge_base_org_access_tenant_id",
        "knowledge_base_org_access",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_base_org_access_knowledge_base_id",
        "knowledge_base_org_access",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_knowledge_base_org_access_org_unit_id",
        "knowledge_base_org_access",
        ["org_unit_id"],
    )
    op.create_index(
        "ix_knowledge_base_org_access_status",
        "knowledge_base_org_access",
        ["status"],
    )
    op.create_index(
        "ix_knowledge_base_tenant_owner_status",
        "knowledge_bases",
        ["tenant_id", "owner_user_id", "status"],
    )
    op.create_index(
        "ix_knowledge_base_tenant_responsible_org",
        "knowledge_bases",
        ["tenant_id", "responsible_org_unit_id"],
    )
    op.create_index(
        "ix_knowledge_base_tenant_access_status",
        "knowledge_bases",
        ["tenant_id", "access_scope", "status"],
    )


def downgrade() -> None:
    """删除 M5-A 组织访问事实和知识治理字段。"""

    for index_name in (
        "ix_knowledge_base_tenant_access_status",
        "ix_knowledge_base_tenant_responsible_org",
        "ix_knowledge_base_tenant_owner_status",
    ):
        op.drop_index(index_name, table_name="knowledge_bases")
    op.drop_table("knowledge_base_org_access")
    for column_name in (
        "revision",
        "download_policy",
        "access_scope",
        "responsible_org_unit_id",
        "owner_user_id",
    ):
        op.drop_column("knowledge_bases", column_name)


def _backfill_knowledge_owners(bind: sa.Connection) -> None:
    """只接受同租户真实用户 ID，不用用户名、Agent owner 或公开绑定推断知识 owner。"""

    valid_users = {
        (str(row["tenant_id"]), str(row["id"]))
        for row in bind.execute(sa.text("SELECT id, tenant_id FROM users")).mappings()
    }
    rows = bind.execute(
        sa.text("SELECT id, tenant_id, metadata_json FROM knowledge_bases")
    ).mappings()
    for row in rows:
        metadata = _json_dict(row["metadata_json"])
        candidate = str(
            metadata.get("created_by_user_id") or metadata.get("owner_user_id") or ""
        ).strip()
        owner_user_id = candidate if (str(row["tenant_id"]), candidate) in valid_users else None
        bind.execute(
            sa.text(
                "UPDATE knowledge_bases SET owner_user_id = :owner_user_id, "
                "access_scope = 'owner', download_policy = 'restricted', revision = 1 "
                "WHERE id = :knowledge_base_id"
            ),
            {
                "owner_user_id": owner_user_id,
                "knowledge_base_id": row["id"],
            },
        )


def _json_dict(value: object) -> dict[str, Any]:
    """把数据库 JSON 映射或字符串解析为字典，异常值保持空字典。"""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}
