"""
@Time       : 2026/08/11 23:10
@Author     : zhanglp8181
@File       : 20260811_0049_general_skill_revision_foundation.py
@CallChain  : Alembic upgrade/downgrade → GeneralSkill revision/import foundation → S1 导入服务
@Description: 以 expand 方式增加通用技能不可变修订、暂存作业和版本化绑定基础。

Revision ID: 20260811_0049
Revises: 20260811_0048
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0049"
down_revision: str | None = "20260811_0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入扩展稳定 Skill 根、不可变修订、导入作业和绑定行版本。"""

    bind = op.get_bind()
    _require_tables(bind)
    _expand_existing_tables(bind)
    _backfill_existing_rows(bind)
    _tighten_existing_columns(bind)
    _create_foundation_tables(bind)
    _ensure_indexes(bind)


def downgrade() -> None:
    """仅在新修订和导入作业均无数据时移除 expand 结构。"""

    bind = op.get_bind()
    for table_name in ("general_skill_import_jobs", "general_skill_revisions"):
        if sa.inspect(bind).has_table(table_name):
            count = int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
            if count:
                raise RuntimeError(f"cannot downgrade with rows in {table_name}")
    if sa.inspect(bind).has_table("general_skills"):
        columns = _column_names(bind, "general_skills")
        if "current_published_revision_id" in columns:
            pointer_count = int(
                bind.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM general_skills "
                        "WHERE current_published_revision_id IS NOT NULL"
                    )
                ).scalar_one()
            )
            if pointer_count:
                raise RuntimeError("cannot downgrade with current published revision pointers")
    for table_name in ("general_skill_import_jobs", "general_skill_revisions"):
        if sa.inspect(bind).has_table(table_name):
            op.drop_table(table_name)
    _drop_existing_table_expansion(bind)


def _require_tables(bind: sa.Connection) -> None:
    """确认 expand 所依赖的旧稳定根与绑定表完整存在。"""

    missing = [
        table_name
        for table_name in ("general_skills", "agent_resource_bindings")
        if not sa.inspect(bind).has_table(table_name)
    ]
    if missing:
        raise RuntimeError("general skill revision foundation requires: " + ",".join(missing))


def _expand_existing_tables(bind: sa.Connection) -> None:
    """为旧表添加可空 expand 列，兼容 MySQL 非事务 DDL 中断续跑。"""

    additions: dict[str, tuple[sa.Column[object], ...]] = {
        "general_skills": (
            sa.Column("owner_user_id", sa.String(128), nullable=True),
            sa.Column("visibility_scope", sa.String(64), nullable=True),
            sa.Column("current_published_revision_id", sa.String(128), nullable=True),
            sa.Column("row_version", sa.Integer(), nullable=True),
        ),
        "agent_resource_bindings": (
            sa.Column("row_version", sa.Integer(), nullable=True),
        ),
    }
    for table_name, columns in additions.items():
        existing = _column_names(bind, table_name)
        missing = [column for column in columns if str(column.name) not in existing]
        if missing:
            with op.batch_alter_table(table_name) as batch:
                for column in missing:
                    batch.add_column(column)


def _backfill_existing_rows(bind: sa.Connection) -> None:
    """给存量广场技能和绑定补上保守默认值，不创建虚假修订。"""

    bind.execute(
        sa.text(
            "UPDATE general_skills SET visibility_scope = 'tenant_gallery' "
            "WHERE visibility_scope IS NULL"
        )
    )
    bind.execute(sa.text("UPDATE general_skills SET row_version = 1 WHERE row_version IS NULL"))
    bind.execute(
        sa.text("UPDATE agent_resource_bindings SET row_version = 1 WHERE row_version IS NULL")
    )


def _tighten_existing_columns(bind: sa.Connection) -> None:
    """在回填后收紧必填列并创建状态、版本检查约束。"""

    with op.batch_alter_table("general_skills") as batch:
        batch.alter_column(
            "visibility_scope",
            existing_type=sa.String(64),
            nullable=False,
            server_default="tenant_gallery",
        )
        batch.alter_column(
            "row_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
    with op.batch_alter_table("agent_resource_bindings") as batch:
        batch.alter_column(
            "row_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
    skill_checks = _constraint_names(bind, "general_skills")
    with op.batch_alter_table("general_skills") as batch:
        if "ck_general_skill_visibility_scope" not in skill_checks:
            batch.create_check_constraint(
                "ck_general_skill_visibility_scope",
                "visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery')",
            )
        if "ck_general_skill_row_version" not in skill_checks:
            batch.create_check_constraint("ck_general_skill_row_version", "row_version >= 1")
    binding_checks = _constraint_names(bind, "agent_resource_bindings")
    if "ck_agent_resource_binding_row_version" not in binding_checks:
        with op.batch_alter_table("agent_resource_bindings") as batch:
            batch.create_check_constraint(
                "ck_agent_resource_binding_row_version", "row_version >= 1"
            )


def _create_foundation_tables(bind: sa.Connection) -> None:
    """创建不可变修订和暂存导入作业表，不改旧读路径。"""

    if not sa.inspect(bind).has_table("general_skill_revisions"):
        op.create_table(
            "general_skill_revisions",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("skill_id", sa.String(128), nullable=False),
            sa.Column("revision_number", sa.Integer(), nullable=False),
            sa.Column("content_checksum", sa.String(64), nullable=False),
            sa.Column("manifest_checksum", sa.String(64), nullable=False),
            sa.Column("normalized_skill_markdown", sa.Text(), nullable=False),
            sa.Column("parsed_metadata_json", sa.JSON(), nullable=False),
            sa.Column("resource_manifest_json", sa.JSON(), nullable=False),
            sa.Column("requested_capabilities_json", sa.JSON(), nullable=False),
            sa.Column("source_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('draft', 'reviewing', 'published', 'rejected', "
                "'superseded', 'revoked')",
                name="ck_general_skill_revision_status",
            ),
            sa.CheckConstraint(
                "revision_number >= 1", name="ck_general_skill_revision_number"
            ),
            sa.CheckConstraint("row_version >= 1", name="ck_general_skill_revision_row_version"),
            sa.UniqueConstraint(
                "tenant_id",
                "skill_id",
                "revision_number",
                name="uq_general_skill_revision_number",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "skill_id",
                "content_checksum",
                name="uq_general_skill_revision_checksum",
            ),
        )
    if not sa.inspect(bind).has_table("general_skill_import_jobs"):
        op.create_table(
            "general_skill_import_jobs",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("owner_user_id", sa.String(128), nullable=False),
            sa.Column("target_agent_id", sa.String(128), nullable=False),
            sa.Column("source_kind", sa.String(64), nullable=False),
            sa.Column("source_reference_redacted", sa.String(2048), nullable=True),
            sa.Column("credential_reference", sa.String(128), nullable=True),
            sa.Column("raw_checksum", sa.String(64), nullable=True),
            sa.Column("normalized_checksum", sa.String(64), nullable=True),
            sa.Column("preview_checksum", sa.String(64), nullable=True),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("parent_job_id", sa.String(128), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("quota_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("error_detail_redacted", sa.Text(), nullable=True),
            sa.Column("staging_manifest_json", sa.JSON(), nullable=False),
            sa.Column("preview_json", sa.JSON(), nullable=False),
            sa.Column("installed_revision_ids_json", sa.JSON(), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=True),
            sa.Column("normalized_at", sa.DateTime(), nullable=True),
            sa.Column("analyzed_at", sa.DateTime(), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "source_kind IN ('upload', 'github', 'skillhub', 'https', 'manual', "
                "'agent_copy')",
                name="ck_general_skill_import_source_kind",
            ),
            sa.CheckConstraint(
                "status IN ('created', 'fetching', 'fetched', 'normalizing', 'normalized', "
                "'analyzing', 'awaiting_approval', 'confirming', 'installed', 'failed', "
                "'cancelled', 'expired')",
                name="ck_general_skill_import_status",
            ),
            sa.CheckConstraint("attempt >= 1", name="ck_general_skill_import_attempt"),
            sa.CheckConstraint("quota_bytes >= 0", name="ck_general_skill_import_quota"),
            sa.CheckConstraint("row_version >= 1", name="ck_general_skill_import_row_version"),
            sa.UniqueConstraint(
                "tenant_id",
                "owner_user_id",
                "idempotency_key",
                "attempt",
                name="uq_general_skill_import_attempt",
            ),
        )


def _ensure_indexes(bind: sa.Connection) -> None:
    """补齐稳定根、修订和作业的高频授权与状态查询索引。"""

    definitions = {
        "general_skills": {
            "ix_general_skills_owner_user_id": ["owner_user_id"],
            "ix_general_skills_visibility_scope": ["visibility_scope"],
            "ix_general_skills_current_published_revision_id": [
                "current_published_revision_id"
            ],
            "ix_general_skill_owner_visibility_status": [
                "tenant_id",
                "owner_user_id",
                "visibility_scope",
                "status",
            ],
        },
        "general_skill_revisions": {
            "ix_general_skill_revisions_tenant_id": ["tenant_id"],
            "ix_general_skill_revisions_skill_id": ["skill_id"],
            "ix_general_skill_revisions_content_checksum": ["content_checksum"],
            "ix_general_skill_revisions_manifest_checksum": ["manifest_checksum"],
            "ix_general_skill_revisions_status": ["status"],
            "ix_general_skill_revisions_created_by": ["created_by"],
            "ix_general_skill_revision_lookup": [
                "tenant_id",
                "skill_id",
                "status",
                "revision_number",
            ],
        },
        "general_skill_import_jobs": {
            "ix_general_skill_import_jobs_tenant_id": ["tenant_id"],
            "ix_general_skill_import_jobs_owner_user_id": ["owner_user_id"],
            "ix_general_skill_import_jobs_target_agent_id": ["target_agent_id"],
            "ix_general_skill_import_jobs_status": ["status"],
            "ix_general_skill_import_jobs_idempotency_key": ["idempotency_key"],
            "ix_general_skill_import_owner_status": [
                "tenant_id",
                "owner_user_id",
                "status",
                "expires_at",
            ],
        },
    }
    for table_name, indexes in definitions.items():
        existing = _index_names(bind, table_name)
        for name, columns in indexes.items():
            if name not in existing:
                op.create_index(name, table_name, columns)


def _drop_existing_table_expansion(bind: sa.Connection) -> None:
    """移除旧表 expand 列和本 revision 创建的索引/约束。"""

    definitions = {
        "general_skills": (
            (
                "ix_general_skill_owner_visibility_status",
                "ix_general_skills_owner_user_id",
                "ix_general_skills_visibility_scope",
                "ix_general_skills_current_published_revision_id",
            ),
            ("ck_general_skill_visibility_scope", "ck_general_skill_row_version"),
            (
                "owner_user_id",
                "visibility_scope",
                "current_published_revision_id",
                "row_version",
            ),
        ),
        "agent_resource_bindings": (
            (),
            ("ck_agent_resource_binding_row_version",),
            ("row_version",),
        ),
    }
    for table_name, (indexes, checks, columns) in definitions.items():
        if not sa.inspect(bind).has_table(table_name):
            continue
        existing_indexes = _index_names(bind, table_name)
        for name in indexes:
            if name in existing_indexes:
                op.drop_index(name, table_name=table_name)
        existing_checks = _constraint_names(bind, table_name)
        with op.batch_alter_table(table_name) as batch:
            for name in checks:
                if name in existing_checks:
                    batch.drop_constraint(name, type_="check")
            existing_columns = _column_names(bind, table_name)
            for name in reversed(columns):
                if name in existing_columns:
                    batch.drop_column(name)


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表的当前列名集合。"""

    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


def _constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表的具名检查约束集合。"""

    return {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(table_name)
        if item.get("name")
    }


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表的当前普通索引集合。"""

    return {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes(table_name)
        if item.get("name")
    }
