"""
@Time       : 2026/08/13 16:40
@Author     : zhanglp8181
@File       : 20260813_0061_general_skill_g1_governance.py
@CallChain  : Alembic upgrade/downgrade → G1 proposal/publication governance
@Description: 扩展 C1/C2 判别提案并创建 Skill/Agent 类型化发布申请、快照和 Release。

Revision ID: 20260813_0061
Revises: 20260813_0060
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260813_0061"
down_revision: str | None = "20260813_0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以 expand/backfill/constraint 顺序增加提案分型和发布治理表。"""

    bind = op.get_bind()
    _expand_proposals(bind)
    _create_publication_tables(bind)
    _create_binding_batch_command_table(bind)
    _create_indexes(bind)


def downgrade() -> None:
    """无 G1 发布或 remote_import 数据时移除扩展，保留 authored 历史提案。"""

    bind = op.get_bind()
    for table_name in (
        "general_skill_binding_batch_commands",
        "publication_releases",
        "agent_publication_revisions",
        "general_skill_publication_revisions",
        "resource_publication_requests",
    ):
        if sa.inspect(bind).has_table(table_name):
            count = int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
            if count:
                raise RuntimeError(f"cannot downgrade with rows in {table_name}")
    if sa.inspect(bind).has_table("general_skill_proposals"):
        remote_count = int(
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM general_skill_proposals "
                    "WHERE proposal_kind = 'remote_import'"
                )
            ).scalar_one()
        )
        if remote_count:
            raise RuntimeError("cannot downgrade with remote import proposals")
    for table_name in (
        "general_skill_binding_batch_commands",
        "publication_releases",
        "agent_publication_revisions",
        "general_skill_publication_revisions",
        "resource_publication_requests",
    ):
        if sa.inspect(bind).has_table(table_name):
            op.drop_table(table_name)
    existing_indexes = {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes("general_skill_proposals")
    }
    for index_name in (
        "ix_general_skill_proposals_preview_checksum",
        "ix_general_skill_proposals_import_job_id",
        "ix_general_skill_proposals_proposal_kind",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="general_skill_proposals")
    with op.batch_alter_table("general_skill_proposals") as batch:
        for name in (
            "ck_general_skill_proposal_payload",
            "ck_general_skill_proposal_kind",
        ):
            batch.drop_constraint(name, type_="check")
        batch.alter_column("revision_id", existing_type=sa.String(128), nullable=False)
        batch.alter_column("skill_id", existing_type=sa.String(128), nullable=False)
        for column_name in (
            "remote_candidate_ids_json",
            "preview_checksum",
            "import_job_id",
            "proposal_kind",
        ):
            batch.drop_column(column_name)


def _expand_proposals(bind: sa.Connection) -> None:
    """为 authored 存量回填判别值，再允许 remote_import 使用空 revision/skill 指针。"""

    inspector = sa.inspect(bind)
    columns = {str(item["name"]) for item in inspector.get_columns("general_skill_proposals")}
    existing_checks = {
        str(item["name"])
        for item in inspector.get_check_constraints("general_skill_proposals")
    }
    additions = (
        sa.Column("proposal_kind", sa.String(64), nullable=True),
        sa.Column("import_job_id", sa.String(128), nullable=True),
        sa.Column("preview_checksum", sa.String(128), nullable=True),
        sa.Column("remote_candidate_ids_json", sa.JSON(), nullable=True),
    )
    with op.batch_alter_table("general_skill_proposals") as batch:
        for column in additions:
            if str(column.name) not in columns:
                batch.add_column(column)
    bind.execute(
        sa.text(
            "UPDATE general_skill_proposals SET proposal_kind='authored' "
            "WHERE proposal_kind IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE general_skill_proposals SET remote_candidate_ids_json='[]' "
            "WHERE remote_candidate_ids_json IS NULL"
        )
    )
    with op.batch_alter_table("general_skill_proposals") as batch:
        batch.alter_column(
            "proposal_kind",
            existing_type=sa.String(64),
            nullable=False,
            server_default="authored",
        )
        batch.alter_column(
            "remote_candidate_ids_json",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch.alter_column("skill_id", existing_type=sa.String(128), nullable=True)
        batch.alter_column("revision_id", existing_type=sa.String(128), nullable=True)
        if "ck_general_skill_proposal_kind" not in existing_checks:
            batch.create_check_constraint(
                "ck_general_skill_proposal_kind",
                "proposal_kind IN ('authored', 'remote_import')",
            )
        if "ck_general_skill_proposal_payload" not in existing_checks:
            batch.create_check_constraint(
                "ck_general_skill_proposal_payload",
                "(proposal_kind = 'authored' AND skill_id IS NOT NULL "
                "AND revision_id IS NOT NULL AND import_job_id IS NULL "
                "AND preview_checksum IS NULL) OR "
                "(proposal_kind = 'remote_import' AND skill_id IS NULL "
                "AND revision_id IS NULL AND import_job_id IS NOT NULL "
                "AND preview_checksum IS NOT NULL)",
            )


def _create_publication_tables(bind: sa.Connection) -> None:
    """创建共享请求头、两类冻结快照和独立 Release 聚合。"""

    if not sa.inspect(bind).has_table("resource_publication_requests"):
        op.create_table(
            "resource_publication_requests",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("owner_user_id", sa.String(128), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=False),
            sa.Column("resource_id", sa.String(128), nullable=False),
            sa.Column("snapshot_kind", sa.String(64), nullable=False),
            sa.Column("snapshot_id", sa.String(128), nullable=False),
            sa.Column("snapshot_checksum", sa.String(128), nullable=False),
            sa.Column("active_slot_key", sa.String(64), nullable=True),
            sa.Column("attention_id", sa.String(128), nullable=True),
            sa.Column("submitted_by_user_id", sa.String(128), nullable=True),
            sa.Column("reviewed_by_user_id", sa.String(128), nullable=True),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "resource_type IN ('general_skill', 'agent')",
                name="ck_resource_publication_resource_type",
            ),
            sa.CheckConstraint(
                "snapshot_kind IN ('general_skill', 'agent') AND snapshot_kind = resource_type",
                name="ck_resource_publication_snapshot_kind",
            ),
            sa.CheckConstraint(
                "status IN ('draft', 'submitted', 'approved', 'rejected', 'expired', "
                "'withdrawn', 'stale')",
                name="ck_resource_publication_status",
            ),
            sa.CheckConstraint("row_version >= 1", name="ck_resource_publication_row_version"),
            sa.UniqueConstraint(
                "tenant_id",
                "resource_type",
                "resource_id",
                "active_slot_key",
                name="uq_resource_publication_active_slot",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "snapshot_kind",
                "snapshot_id",
                name="uq_resource_publication_snapshot",
            ),
        )
    if not sa.inspect(bind).has_table("general_skill_publication_revisions"):
        op.create_table(
            "general_skill_publication_revisions",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("request_id", sa.String(128), nullable=False),
            sa.Column("skill_id", sa.String(128), nullable=False),
            sa.Column("approved_revision_id", sa.String(128), nullable=False),
            sa.Column("content_checksum", sa.String(128), nullable=False),
            sa.Column("manifest_checksum", sa.String(128), nullable=False),
            sa.Column("snapshot_checksum", sa.String(128), nullable=False),
            sa.Column("source_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("license_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("capability_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("risk_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "request_id", name="uq_general_skill_publication_request"
            ),
        )
    if not sa.inspect(bind).has_table("agent_publication_revisions"):
        op.create_table(
            "agent_publication_revisions",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("request_id", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.String(128), nullable=False),
            sa.Column("persona_checksum", sa.String(128), nullable=False),
            sa.Column("snapshot_checksum", sa.String(128), nullable=False),
            sa.Column("persona_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("component_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("governance_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "request_id", name="uq_agent_publication_request"),
        )
    if not sa.inspect(bind).has_table("publication_releases"):
        op.create_table(
            "publication_releases",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("approved_request_id", sa.String(128), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=False),
            sa.Column("resource_id", sa.String(128), nullable=False),
            sa.Column("snapshot_kind", sa.String(64), nullable=False),
            sa.Column("snapshot_id", sa.String(128), nullable=False),
            sa.Column("snapshot_checksum", sa.String(128), nullable=False),
            sa.Column("active_slot_key", sa.String(64), nullable=True),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False),
            sa.Column("terminal_command_id", sa.String(512), nullable=True),
            sa.Column("terminal_by_user_id", sa.String(512), nullable=True),
            sa.Column("terminal_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "resource_type IN ('general_skill', 'agent')",
                name="ck_publication_release_resource_type",
            ),
            sa.CheckConstraint(
                "snapshot_kind IN ('general_skill', 'agent') AND snapshot_kind = resource_type",
                name="ck_publication_release_snapshot_kind",
            ),
            sa.CheckConstraint(
                "status IN ('active', 'unpublished', 'security_revoked')",
                name="ck_publication_release_status",
            ),
            sa.CheckConstraint("row_version >= 1", name="ck_publication_release_row_version"),
            sa.UniqueConstraint(
                "tenant_id", "approved_request_id", name="uq_publication_release_request"
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "resource_type",
                "resource_id",
                "active_slot_key",
                name="uq_publication_release_active_slot",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "terminal_command_id",
                name="uq_publication_release_terminal_command",
            ),
        )


def _create_indexes(bind: sa.Connection) -> None:
    """为 G1 高频 owner/status 和来源引用创建可重入索引。"""

    definitions: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
        "general_skill_proposals": (
            ("ix_general_skill_proposals_proposal_kind", ("proposal_kind",)),
            ("ix_general_skill_proposals_import_job_id", ("import_job_id",)),
            ("ix_general_skill_proposals_preview_checksum", ("preview_checksum",)),
        ),
        "resource_publication_requests": (
            ("ix_resource_publication_requests_tenant_id", ("tenant_id",)),
            ("ix_resource_publication_requests_owner_user_id", ("owner_user_id",)),
            ("ix_resource_publication_requests_status", ("status",)),
            (
                "ix_resource_publication_owner_status",
                ("tenant_id", "owner_user_id", "status"),
            ),
        ),
    }
    for table_name, indexes in definitions.items():
        existing = {str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)}
        for name, columns in indexes:
            if name not in existing:
                op.create_index(name, table_name, list(columns), unique=False)


def _create_binding_batch_command_table(bind: sa.Connection) -> None:
    """创建用户级批量装配幂等账本。"""

    if sa.inspect(bind).has_table("general_skill_binding_batch_commands"):
        return
    op.create_table(
        "general_skill_binding_batch_commands",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("preview_checksum", sa.String(128), nullable=False),
        sa.Column("request_checksum", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False, server_default="committed"),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('committed', 'failed')",
            name="ck_general_skill_binding_batch_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "idempotency_key",
            name="uq_general_skill_binding_batch_idempotency",
        ),
    )
    op.create_index(
        "ix_general_skill_binding_batch_owner_created",
        "general_skill_binding_batch_commands",
        ["tenant_id", "owner_user_id", "created_at"],
    )
    for column_name in ("tenant_id", "owner_user_id", "skill_id", "revision_id", "status"):
        op.create_index(
            f"ix_general_skill_binding_batch_commands_{column_name}",
            "general_skill_binding_batch_commands",
            [column_name],
        )
