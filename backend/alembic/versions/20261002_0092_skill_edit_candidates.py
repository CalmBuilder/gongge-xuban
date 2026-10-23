"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : 20261002_0092_skill_edit_candidates.py
@CallChain  : Alembic → 原User授权修订/SkillCard候选 → 原发布事务
@Description: 保存单一编辑候选和不可洗掉的当前关联，历史反馈不自动形成候选。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20261002_0092"
down_revision: str | None = "20261002_0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """原User补授权代数，候选默认空且不改Skill/分支/Runtime发布数据。"""

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("users") and "auth_revision" not in {
        item["name"] for item in inspector.get_columns("users")
    }:
        op.add_column("users", sa.Column("auth_revision", sa.Integer(), nullable=False,
                                        server_default="1"))
    if "skill_edit_candidates" in inspector.get_table_names():
        return
    op.create_table("skill_edit_candidates",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("actor_user_id", sa.String(512), nullable=False),
        sa.Column("skill_id", sa.String(128), nullable=False),
        sa.Column("scope_agent_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("editor_slot_key", sa.String(128), nullable=True),
        sa.Column("create_request_identity", sa.String(128), nullable=False),
        sa.Column("create_request_hash", sa.String(128), nullable=False),
        sa.Column("actor_auth_revision", sa.Integer(), nullable=False),
        sa.Column("scope_profile_id", sa.String(128), nullable=True),
        sa.Column("scope_profile_revision", sa.Integer(), nullable=True),
        sa.Column("base_skill_version_id", sa.String(512), nullable=False),
        sa.Column("base_checksum", sa.String(128), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column("candidate_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("base_difference_confirmed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(64), nullable=False, server_default="draft"),
        sa.Column("ordinary_context_json", sa.JSON(), nullable=False),
        sa.Column("publish_request_hash", sa.String(128), nullable=True),
        sa.Column("publish_receipt_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
                  nullable=False),
        sa.UniqueConstraint("tenant_id", "editor_slot_key", name="uq_skill_candidate_editor_slot"),
        sa.UniqueConstraint("tenant_id", "create_request_identity",
                            name="uq_skill_candidate_create_request"),
        sa.CheckConstraint("status IN ('draft','ready','rejected','closed','published')",
                           name="ck_skill_candidate_status"))
    for name in ["tenant_id", "actor_user_id", "skill_id", "scope_agent_id", "status"]:
        op.create_index("ix_skill_edit_candidates_" + name, "skill_edit_candidates", [name])


def downgrade() -> None:
    """存在候选编辑关联或发布回执时拒绝降级，不丢失旧请求的防旁路身份。"""

    inspector = sa.inspect(op.get_bind())
    if "skill_edit_candidates" in inspector.get_table_names():
        if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM skill_edit_candidates")).scalar_one():
            raise RuntimeError("SKILL_EDIT_CONTEXTS_PRESENT")
        op.drop_table("skill_edit_candidates")
    if inspector.has_table("users") and "auth_revision" in {
        item["name"] for item in inspector.get_columns("users")
    }:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("auth_revision")
