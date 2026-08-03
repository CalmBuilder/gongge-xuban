"""
@Time       : 2026/07/28 21:10
@Author     : zhanglp8181
@File       : 20260728_0024_agent_identity.py
@CallChain  : Alembic upgrade/downgrade → M4-A 数字员工责任、发布、来源与会话锚点
@Description: 将可信 legacy metadata 回填为正式 Agent 字段，并把历史会话补为使用关系。

Revision ID: 20260728_0024
Revises: 20260728_0023
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0024"
down_revision: str | None = "20260728_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SESSION_USAGE_BACKFILL_SOURCE = "m4_session_backfill"


def upgrade() -> None:
    """增加正式数字员工关系与会话锚点，并只从可验证历史事实回填。"""

    _add_agent_columns()
    _add_session_columns()
    bind = op.get_bind()
    _backfill_agent_profiles(bind)
    _backfill_session_usages(bind)
    _make_agent_defaults_required(bind)
    _create_agent_indexes()


def downgrade() -> None:
    """删除 0024 字段和仅由本迁移生成的 Usage，不影响原有使用关系与会话。"""

    bind = op.get_bind()
    _delete_backfilled_session_usages(bind)
    for index_name in (
        "ix_agent_profiles_tenant_owner",
        "ix_agent_profiles_tenant_gallery_status",
        "ix_agent_profiles_tenant_category_status",
        "ix_agent_profiles_tenant_source",
    ):
        op.drop_index(index_name, table_name="agent_profiles")
    for column_name in ("origin", "capability_snapshot_json", "agent_profile_revision"):
        op.drop_column("sessions", column_name)
    for column_name in (
        "visibility_scope",
        "agent_category_code",
        "gallery_published_by",
        "gallery_published_at",
        "published_to_gallery",
        "profile_revision",
        "source_agent_version",
        "source_agent_id",
        "owner_user_id",
    ):
        op.drop_column("agent_profiles", column_name)


def _add_agent_columns() -> None:
    """先以可空列扩展 Agent 表，允许双方言完成安全回填后再收口。"""

    columns = (
        sa.Column("owner_user_id", sa.String(length=128), nullable=True),
        sa.Column("source_agent_id", sa.String(length=128), nullable=True),
        sa.Column("source_agent_version", sa.String(length=64), nullable=True),
        sa.Column("profile_revision", sa.Integer(), nullable=True),
        sa.Column("published_to_gallery", sa.Boolean(), nullable=True),
        sa.Column("gallery_published_at", sa.DateTime(), nullable=True),
        sa.Column("gallery_published_by", sa.String(length=128), nullable=True),
        sa.Column("agent_category_code", sa.String(length=128), nullable=True),
        sa.Column("visibility_scope", sa.String(length=64), nullable=True),
    )
    for column in columns:
        op.add_column("agent_profiles", column)


def _add_session_columns() -> None:
    """为新会话预留修订、非敏感能力快照和来源，旧会话保持明确空值。"""

    op.add_column(
        "sessions",
        sa.Column("agent_profile_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("capability_snapshot_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("origin", sa.String(length=64), nullable=True),
    )


def _backfill_agent_profiles(bind: sa.Connection) -> None:
    """只把同租户真实用户和明确 metadata 转成正式字段，不推测责任人。"""

    users = bind.execute(sa.text("SELECT id, tenant_id, username FROM users")).mappings().all()
    user_ids_by_tenant = {
        (str(row["tenant_id"]), str(row["id"])): str(row["id"]) for row in users
    }
    user_ids_by_username = {
        (str(row["tenant_id"]), str(row["username"])): str(row["id"]) for row in users
    }
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, is_overall, metadata_json "
            "FROM agent_profiles"
        )
    ).mappings()
    for row in rows:
        tenant_id = str(row["tenant_id"])
        metadata = _json_dict(row["metadata_json"])
        owner_candidate = str(metadata.get("owner_user_id") or "").strip()
        owner_user_id = user_ids_by_tenant.get((tenant_id, owner_candidate))
        publisher_candidate = str(metadata.get("gallery_published_by") or "").strip()
        gallery_published_by = user_ids_by_tenant.get(
            (tenant_id, publisher_candidate)
        ) or user_ids_by_username.get((tenant_id, publisher_candidate))
        published = metadata.get("published_to_gallery") is True
        category = (
            "professional"
            if str(metadata.get("employee_type") or "").strip() == "expert"
            else "assistant"
        )
        requested_visibility = str(
            metadata.get("visibility_scope") or metadata.get("visibility") or ""
        ).strip()
        visibility_scope = (
            requested_visibility
            if requested_visibility in {"private", "tenant"}
            else "tenant" if published else "private"
        )
        bind.execute(
            sa.text(
                "UPDATE agent_profiles SET "
                "owner_user_id = :owner_user_id, "
                "profile_revision = :profile_revision, "
                "published_to_gallery = :published_to_gallery, "
                "gallery_published_at = :gallery_published_at, "
                "gallery_published_by = :gallery_published_by, "
                "agent_category_code = :agent_category_code, "
                "visibility_scope = :visibility_scope "
                "WHERE id = :agent_id"
            ),
            {
                "owner_user_id": None if row["is_overall"] else owner_user_id,
                "profile_revision": _positive_revision(metadata.get("profile_revision")),
                "published_to_gallery": published,
                "gallery_published_at": _optional_datetime(
                    metadata.get("gallery_published_at")
                ),
                "gallery_published_by": gallery_published_by,
                "agent_category_code": category,
                "visibility_scope": visibility_scope,
                "agent_id": row["id"],
            },
        )


def _backfill_session_usages(bind: sa.Connection) -> None:
    """将有效历史会话幂等转换为 AgentUsage，使使用关系不再靠查询时拼接。"""

    valid_users = {
        (str(row["tenant_id"]), str(row["id"]))
        for row in bind.execute(sa.text("SELECT id, tenant_id FROM users")).mappings()
    }
    valid_agents = {
        (str(row["tenant_id"]), str(row["id"]))
        for row in bind.execute(
            sa.text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = :is_overall"),
            {"is_overall": False},
        ).mappings()
    }
    existing = {
        (str(row["tenant_id"]), str(row["user_id"]), str(row["agent_id"]))
        for row in bind.execute(
            sa.text("SELECT tenant_id, user_id, agent_id FROM agent_usages")
        ).mappings()
    }
    sessions = bind.execute(
        sa.text(
            "SELECT tenant_id, user_id, agent_id, MIN(created_at) AS first_used_at "
            "FROM sessions "
            "WHERE user_id IS NOT NULL AND agent_id IS NOT NULL "
            "GROUP BY tenant_id, user_id, agent_id"
        )
    ).mappings()
    metadata_json = json.dumps(
        {"source": SESSION_USAGE_BACKFILL_SOURCE},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for row in sessions:
        key = (str(row["tenant_id"]), str(row["user_id"]), str(row["agent_id"]))
        if key in existing:
            continue
        if key[:2] not in valid_users or (key[0], key[2]) not in valid_agents:
            continue
        created_at = row["first_used_at"] or datetime(1970, 1, 1)
        digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:24]
        bind.execute(
            sa.text(
                "INSERT INTO agent_usages "
                "(id, tenant_id, user_id, agent_id, metadata_json, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :user_id, :agent_id, :metadata_json, "
                ":created_at, :updated_at)"
            ),
            {
                "id": f"agentuse_m4_{digest}",
                "tenant_id": key[0],
                "user_id": key[1],
                "agent_id": key[2],
                "metadata_json": metadata_json,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )
        existing.add(key)


def _make_agent_defaults_required(bind: sa.Connection) -> None:
    """在回填后把新建必需的稳定默认字段收口为非空。"""

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("agent_profiles") as batch_op:
            batch_op.alter_column(
                "profile_revision",
                existing_type=sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
            batch_op.alter_column(
                "published_to_gallery",
                existing_type=sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
            batch_op.alter_column(
                "agent_category_code",
                existing_type=sa.String(length=128),
                nullable=False,
                server_default="assistant",
            )
            batch_op.alter_column(
                "visibility_scope",
                existing_type=sa.String(length=64),
                nullable=False,
                server_default="private",
            )
        return
    op.alter_column(
        "agent_profiles",
        "profile_revision",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("1"),
    )
    op.alter_column(
        "agent_profiles",
        "published_to_gallery",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )
    op.alter_column(
        "agent_profiles",
        "agent_category_code",
        existing_type=sa.String(length=128),
        nullable=False,
        server_default="assistant",
    )
    op.alter_column(
        "agent_profiles",
        "visibility_scope",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default="private",
    )


def _create_agent_indexes() -> None:
    """创建所有权、广场、分类和来源的租户内稳定查询索引。"""

    op.create_index(
        "ix_agent_profiles_tenant_owner",
        "agent_profiles",
        ["tenant_id", "owner_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_profiles_tenant_gallery_status",
        "agent_profiles",
        ["tenant_id", "published_to_gallery", "status"],
        unique=False,
    )
    op.create_index(
        "ix_agent_profiles_tenant_category_status",
        "agent_profiles",
        ["tenant_id", "agent_category_code", "status"],
        unique=False,
    )
    op.create_index(
        "ix_agent_profiles_tenant_source",
        "agent_profiles",
        ["tenant_id", "source_agent_id"],
        unique=False,
    )


def _delete_backfilled_session_usages(bind: sa.Connection) -> None:
    """回退时仅删除仍保留迁移标记的自动 Usage，避免误删真实用户动作。"""

    rows = bind.execute(sa.text("SELECT id, metadata_json FROM agent_usages")).mappings()
    ids = [
        str(row["id"])
        for row in rows
        if _json_dict(row["metadata_json"]).get("source") == SESSION_USAGE_BACKFILL_SOURCE
    ]
    for usage_id in ids:
        bind.execute(
            sa.text("DELETE FROM agent_usages WHERE id = :usage_id"),
            {"usage_id": usage_id},
        )


def _json_dict(value: Any) -> dict[str, Any]:
    """把双方言返回的 JSON 对象或字符串规范成字典，非法历史值按空对象处理。"""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _positive_revision(value: Any) -> int:
    """读取正整数修订号，缺失或非法历史值统一从 1 开始。"""

    try:
        revision_value = int(value)
    except (TypeError, ValueError):
        return 1
    return max(revision_value, 1)


def _optional_datetime(value: Any) -> datetime | None:
    """解析 legacy ISO 发布时间；非法值不阻断迁移并保持为空。"""

    if isinstance(value, datetime):
        return (
            value.astimezone(UTC).replace(tzinfo=None)
            if value.tzinfo is not None
            else value
        )
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return (
            parsed.astimezone(UTC).replace(tzinfo=None)
            if parsed.tzinfo is not None
            else parsed
        )
    except ValueError:
        return None
