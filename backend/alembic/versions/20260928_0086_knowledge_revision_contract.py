"""
@Time       : 2026/09/28
@Author     : zhanglp8181
@File       : 20260928_0086_knowledge_revision_contract.py
@CallChain  : Alembic upgrade/downgrade → 知识正文修订 → 候选版本与持久构建任务
@Description: 为 SD-5 建立知识内容 CAS、候选发布、正文 lineage 和可恢复任务合同。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260928_0086"
down_revision: str | None = "20260928_0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LONG_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def _columns(table_name: str) -> set[str]:
    """读取目标表当前列，支持重复执行时只补缺失字段。"""

    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    """只为历史数据库增加尚不存在的 SD-5 字段。"""

    if table_name not in sa.inspect(op.get_bind()).get_table_names():
        return
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(name: str, table_name: str, columns: Sequence[str]) -> None:
    """按名称幂等创建迁移所需索引。"""

    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_indexes(table_name)}
    if name not in existing:
        op.create_index(name, table_name, list(columns))


def _add_knowledge_base_fields() -> None:
    """增加整体知识库内容 CAS 序号。"""

    _add_column_if_missing(
        "knowledge_bases",
        sa.Column("content_revision", sa.Integer(), nullable=False, server_default="1"),
    )


def _add_version_fields() -> None:
    """增加候选版本的发布状态、来源和校验清单字段。"""

    for column in (
        sa.Column("scope", sa.String(512), nullable=False, server_default="overall"),
        sa.Column(
            "publication_state",
            sa.String(64),
            nullable=False,
            server_default="published",
        ),
        sa.Column("parent_version_id", sa.String(512), nullable=True),
        sa.Column("build_job_id", sa.String(512), nullable=True),
        sa.Column("content_manifest_json", sa.JSON(), nullable=True),
        sa.Column("content_manifest_hash", sa.String(64), nullable=True),
    ):
        _add_column_if_missing("knowledge_base_versions", column)
    _create_index_if_missing(
        "ix_knowledge_base_versions_scope",
        "knowledge_base_versions",
        ("scope",),
    )
    _create_index_if_missing(
        "ix_knowledge_base_versions_publication_state",
        "knowledge_base_versions",
        ("publication_state",),
    )
    _create_index_if_missing(
        "ix_knowledge_base_versions_parent_version_id",
        "knowledge_base_versions",
        ("parent_version_id",),
    )
    _create_index_if_missing(
        "ix_knowledge_base_versions_build_job_id",
        "knowledge_base_versions",
        ("build_job_id",),
    )
    _create_index_if_missing(
        "ix_knowledge_base_versions_content_manifest_hash",
        "knowledge_base_versions",
        ("content_manifest_hash",),
    )


def _add_branch_fields() -> None:
    """增加个人知识分支内容 CAS 序号。"""

    _add_column_if_missing(
        "agent_knowledge_branches",
        sa.Column("content_revision", sa.Integer(), nullable=False, server_default="1"),
    )


def _add_document_fields() -> None:
    """增加文档跨版本 lineage 和可验证正文来源。"""

    for column in (
        sa.Column("lineage_id", sa.String(512), nullable=True),
        sa.Column("parent_document_id", sa.String(512), nullable=True),
        sa.Column("normalized_source", _LONG_TEXT, nullable=True),
        sa.Column("source_checksum", sa.String(64), nullable=True),
        sa.Column("source_kind", sa.String(64), nullable=False, server_default="legacy"),
        sa.Column("ingest_schema_version", sa.Integer(), nullable=False, server_default="1"),
    ):
        _add_column_if_missing("knowledge_documents", column)
    if "knowledge_documents" in sa.inspect(op.get_bind()).get_table_names():
        op.execute(
            sa.text(
                "UPDATE knowledge_documents "
                "SET lineage_id = id "
                "WHERE lineage_id IS NULL"
            )
        )
    for name, columns in (
        ("ix_knowledge_documents_lineage_id", ("lineage_id",)),
        ("ix_knowledge_documents_parent_document_id", ("parent_document_id",)),
        ("ix_knowledge_documents_source_checksum", ("source_checksum",)),
        ("ix_knowledge_documents_source_kind", ("source_kind",)),
    ):
        _create_index_if_missing(name, "knowledge_documents", columns)


def _add_job_fields() -> None:
    """增加正文修订任务的幂等身份、租约、fence 和终态投影。"""

    for column in (
        sa.Column("job_kind", sa.String(64), nullable=False, server_default="legacy"),
        sa.Column("request_payload_json", sa.JSON(), nullable=True),
        sa.Column("actor_user_id", sa.String(128), nullable=True),
        sa.Column("scope", sa.String(512), nullable=False, server_default="overall"),
        sa.Column("source_version_id", sa.String(512), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("candidate_version_id", sa.String(512), nullable=True),
        sa.Column("client_request_id", sa.String(64), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(512), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("fence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("published_version_id", sa.String(512), nullable=True),
        sa.Column("published_document_id", sa.String(512), nullable=True),
    ):
        _add_column_if_missing("knowledge_ingest_jobs", column)
    for name, columns in (
        ("ix_knowledge_ingest_jobs_job_kind", ("job_kind",)),
        ("ix_knowledge_ingest_jobs_actor_user_id", ("actor_user_id",)),
        ("ix_knowledge_ingest_jobs_scope", ("scope",)),
        ("ix_knowledge_ingest_jobs_source_version_id", ("source_version_id",)),
        ("ix_knowledge_ingest_jobs_expected_revision", ("expected_revision",)),
        ("ix_knowledge_ingest_jobs_candidate_version_id", ("candidate_version_id",)),
        ("ix_knowledge_ingest_jobs_client_request_id", ("client_request_id",)),
        ("ix_knowledge_ingest_jobs_request_hash", ("request_hash",)),
        ("ix_knowledge_ingest_jobs_dedupe_key", ("dedupe_key",)),
        ("ix_knowledge_ingest_jobs_lease_until", ("lease_until",)),
        ("ix_knowledge_ingest_jobs_deadline_at", ("deadline_at",)),
        ("ix_knowledge_ingest_jobs_error_code", ("error_code",)),
        ("ix_knowledge_ingest_jobs_published_version_id", ("published_version_id",)),
        ("ix_knowledge_ingest_jobs_published_document_id", ("published_document_id",)),
    ):
        _create_index_if_missing(name, "knowledge_ingest_jobs", columns)
    inspector = sa.inspect(op.get_bind())
    if "knowledge_ingest_jobs" in inspector.get_table_names():
        constraints = {
            item["name"] for item in inspector.get_unique_constraints("knowledge_ingest_jobs")
        }
        if "uq_knowledge_ingest_job_tenant_dedupe" not in constraints:
            if op.get_bind().dialect.name == "sqlite":
                with op.batch_alter_table("knowledge_ingest_jobs", recreate="always") as batch:
                    batch.create_unique_constraint(
                        "uq_knowledge_ingest_job_tenant_dedupe",
                        ("tenant_id", "dedupe_key"),
                    )
            else:
                op.create_unique_constraint(
                    "uq_knowledge_ingest_job_tenant_dedupe",
                    "knowledge_ingest_jobs",
                    ("tenant_id", "dedupe_key"),
                )


def upgrade() -> None:
    """为现有知识表补充 SD-5 首个可持久化修订合同。"""

    _add_knowledge_base_fields()
    _add_version_fields()
    _add_branch_fields()
    _add_document_fields()
    _add_job_fields()


def _has_revision_data(table_name: str, condition: str) -> bool:
    """判断降级是否会丢失已创建的候选或修订任务事实。"""

    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return bool(
        op.get_bind()
        .execute(sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE {condition}"))
        .scalar_one()
    )


def _drop_indexes(table_name: str, names: Sequence[str]) -> None:
    """幂等删除 SD-5 新增索引。"""

    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_indexes(table_name)}
    for name in names:
        if name in existing:
            op.drop_index(name, table_name=table_name)


def downgrade() -> None:
    """仅在没有新修订事实时回退，避免删除候选、lineage 或任务审计。"""

    if _has_revision_data(
        "knowledge_base_versions",
        "publication_state <> 'published' OR parent_version_id IS NOT NULL OR build_job_id IS NOT NULL",
    ):
        raise RuntimeError("cannot downgrade while knowledge revision candidates exist")
    if _has_revision_data("knowledge_ingest_jobs", "job_kind = 'revision'"):
        raise RuntimeError("cannot downgrade while knowledge revision jobs exist")

    _drop_indexes(
        "knowledge_ingest_jobs",
        (
            "ix_knowledge_ingest_jobs_job_kind",
            "ix_knowledge_ingest_jobs_actor_user_id",
            "ix_knowledge_ingest_jobs_scope",
            "ix_knowledge_ingest_jobs_source_version_id",
            "ix_knowledge_ingest_jobs_expected_revision",
            "ix_knowledge_ingest_jobs_candidate_version_id",
            "ix_knowledge_ingest_jobs_client_request_id",
            "ix_knowledge_ingest_jobs_request_hash",
            "ix_knowledge_ingest_jobs_dedupe_key",
            "ix_knowledge_ingest_jobs_lease_until",
            "ix_knowledge_ingest_jobs_deadline_at",
            "ix_knowledge_ingest_jobs_error_code",
            "ix_knowledge_ingest_jobs_published_version_id",
            "ix_knowledge_ingest_jobs_published_document_id",
        ),
    )
    inspector = sa.inspect(op.get_bind())
    if "knowledge_ingest_jobs" in inspector.get_table_names():
        constraints = {
            item["name"] for item in inspector.get_unique_constraints("knowledge_ingest_jobs")
        }
        if "uq_knowledge_ingest_job_tenant_dedupe" in constraints:
            with op.batch_alter_table("knowledge_ingest_jobs") as batch:
                batch.drop_constraint("uq_knowledge_ingest_job_tenant_dedupe", type_="unique")
    _drop_indexes(
        "knowledge_documents",
        (
            "ix_knowledge_documents_lineage_id",
            "ix_knowledge_documents_parent_document_id",
            "ix_knowledge_documents_source_checksum",
            "ix_knowledge_documents_source_kind",
        ),
    )
    _drop_indexes(
        "knowledge_base_versions",
        (
            "ix_knowledge_base_versions_scope",
            "ix_knowledge_base_versions_publication_state",
            "ix_knowledge_base_versions_parent_version_id",
            "ix_knowledge_base_versions_build_job_id",
            "ix_knowledge_base_versions_content_manifest_hash",
        ),
    )
    for table_name, columns in (
        (
            "knowledge_ingest_jobs",
            (
                "job_kind",
                "request_payload_json",
                "actor_user_id",
                "scope",
                "source_version_id",
                "expected_revision",
                "candidate_version_id",
                "client_request_id",
                "request_hash",
                "dedupe_key",
                "lease_owner",
                "lease_until",
                "fence",
                "attempt_count",
                "deadline_at",
                "error_code",
                "published_version_id",
                "published_document_id",
            ),
        ),
        (
            "knowledge_documents",
            (
                "lineage_id",
                "parent_document_id",
                "normalized_source",
                "source_checksum",
                "source_kind",
                "ingest_schema_version",
            ),
        ),
        (
            "agent_knowledge_branches",
            ("content_revision",),
        ),
        (
            "knowledge_base_versions",
            (
                "scope",
                "publication_state",
                "parent_version_id",
                "build_job_id",
                "content_manifest_json",
                "content_manifest_hash",
            ),
        ),
        ("knowledge_bases", ("content_revision",)),
    ):
        if table_name in sa.inspect(op.get_bind()).get_table_names():
            with op.batch_alter_table(table_name) as batch:
                for column in columns:
                    if column in _columns(table_name):
                        batch.drop_column(column)
