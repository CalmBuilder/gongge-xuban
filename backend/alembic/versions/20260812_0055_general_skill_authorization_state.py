"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : 20260812_0055_general_skill_authorization_state.py
@CallChain  : Alembic upgrade/downgrade → legacy GeneralSkill revision backfill → S2 resolver
@Description: 回填旧通用技能不可变修订并建立租户级单调授权 revision。

Revision ID: 20260812_0055
Revises: 20260812_0054
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0055"
down_revision: str | None = "20260812_0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _checksum(value: object) -> str:
    """生成与运行时 resolver 一致的规范 JSON SHA-256。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _json_value(value: object, fallback: object) -> object:
    """兼容 SQLite 字符串 JSON 与 MySQL 原生 JSON 返回值。"""

    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
    return value


def upgrade() -> None:
    """可重入创建授权状态并把旧 GeneralSkill 内容回填为 revision 1。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skill_authorization_states"):
        op.create_table(
            "general_skill_authorization_states",
            sa.Column("tenant_id", sa.String(128), primary_key=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_event_checksum", sa.String(64), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "revision >= 1",
                name="ck_general_skill_authorization_revision",
            ),
        )
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skill_authorization_events"):
        op.create_table(
            "general_skill_authorization_events",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("authorization_revision", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(128), nullable=False),
            sa.Column("resource_id", sa.String(128), nullable=True),
            sa.Column("event_checksum", sa.String(128), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "authorization_revision",
                name="uq_general_skill_authorization_event_revision",
            ),
            sa.CheckConstraint(
                "authorization_revision >= 1",
                name="ck_general_skill_authorization_event_revision",
            ),
        )
        op.create_index(
            "ix_general_skill_authorization_event_tenant_id",
            "general_skill_authorization_events",
            ["tenant_id"],
        )
        op.create_index(
            "ix_general_skill_authorization_event_resource_id",
            "general_skill_authorization_events",
            ["resource_id"],
        )
        op.create_index(
            "ix_general_skill_authorization_event_event_checksum",
            "general_skill_authorization_events",
            ["event_checksum"],
        )
        op.create_index(
            "ix_general_skill_authorization_event_tenant_created",
            "general_skill_authorization_events",
            ["tenant_id", "created_at"],
        )
    if not all(
        inspector.has_table(name)
        for name in ("general_skills", "general_skill_revisions")
    ):
        return
    _backfill_revisions(bind)
    _backfill_authorization_states(bind)


def downgrade() -> None:
    """仅在授权 revision 未发生运行变更时移除迁移生成的 revision 与状态表。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("general_skill_authorization_states"):
        changed = int(
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM general_skill_authorization_states WHERE revision > 1"
                )
            ).scalar_one()
        )
        if changed:
            raise RuntimeError("cannot downgrade after general skill authorization changes")
    if inspector.has_table("general_skill_authorization_events"):
        op.drop_index(
            "ix_general_skill_authorization_event_tenant_created",
            table_name="general_skill_authorization_events",
        )
        op.drop_index(
            "ix_general_skill_authorization_event_event_checksum",
            table_name="general_skill_authorization_events",
        )
        op.drop_index(
            "ix_general_skill_authorization_event_resource_id",
            table_name="general_skill_authorization_events",
        )
        op.drop_index(
            "ix_general_skill_authorization_event_tenant_id",
            table_name="general_skill_authorization_events",
        )
        op.drop_table("general_skill_authorization_events")
    if inspector.has_table("general_skill_revisions") and inspector.has_table("general_skills"):
        backfilled = bind.execute(
            sa.text(
                "SELECT id, skill_id FROM general_skill_revisions "
                "WHERE source_snapshot_json LIKE '%legacy_backfill%'"
            )
        ).mappings().all()
        for row in backfilled:
            bind.execute(
                sa.text(
                    "UPDATE general_skills SET current_published_revision_id = NULL "
                    "WHERE id = :skill_id AND current_published_revision_id = :revision_id"
                ),
                {"skill_id": row["skill_id"], "revision_id": row["id"]},
            )
            bind.execute(
                sa.text("DELETE FROM general_skill_revisions WHERE id = :revision_id"),
                {"revision_id": row["id"]},
            )
    if inspector.has_table("general_skill_authorization_states"):
        op.drop_table("general_skill_authorization_states")


def _backfill_revisions(bind: sa.Connection) -> None:
    """为没有 revision pointer 的旧行生成确定性 revision，支持中断后重复运行。"""

    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, skill_markdown, skill_files_json, metadata_json, status, "
            "owner_user_id, created_at, updated_at FROM general_skills "
            "WHERE current_published_revision_id IS NULL"
        )
    ).mappings().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    for row in rows:
        revision_id = f"gsrev_legacy_{hashlib.sha256(str(row['id']).encode()).hexdigest()[:20]}"
        if bind.execute(
            sa.text("SELECT COUNT(*) FROM general_skill_revisions WHERE id = :id"),
            {"id": revision_id},
        ).scalar_one():
            _set_pointer(bind, row, revision_id)
            continue
        markdown = str(row["skill_markdown"] or "")
        raw_files = _json_value(row["skill_files_json"], [])
        files = raw_files if isinstance(raw_files, list) else []
        resources: dict[str, dict[str, object]] = {}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = str(item["path"]).replace("\\", "/").lstrip("/")
            if not path or ".." in path.split("/"):
                continue
            content = str(item.get("content") or "")
            resources[path] = {
                "path": path,
                "checksum": hashlib.sha256(content.encode()).hexdigest(),
                "size": len(content.encode()),
                "media_type": str(item.get("mime_type") or "text/plain"),
                "legacy_inline": True,
            }
        resources["SKILL.md"] = {
            "path": "SKILL.md",
            "checksum": hashlib.sha256(markdown.encode()).hexdigest(),
            "size": len(markdown.encode()),
            "media_type": "text/markdown",
            "legacy_inline": True,
        }
        manifest = [resources[path] for path in sorted(resources)]
        content_checksum = _checksum(
            [{"path": item["path"], "checksum": item["checksum"]} for item in manifest]
        )
        metadata = _json_value(row["metadata_json"], {})
        metadata = metadata if isinstance(metadata, dict) else {}
        owner = str(row["owner_user_id"] or metadata.get("created_by_user_id") or "migration")
        revision_status = "published" if row["status"] == "published" else "draft"
        bind.execute(
            sa.text(
                "INSERT INTO general_skill_revisions "
                "(id, tenant_id, skill_id, revision_number, content_checksum, "
                "manifest_checksum, normalized_skill_markdown, parsed_metadata_json, "
                "resource_manifest_json, requested_capabilities_json, source_snapshot_json, "
                "status, created_by, row_version, created_at, published_at, revoked_at) "
                "VALUES (:id, :tenant_id, :skill_id, 1, :content_checksum, "
                ":manifest_checksum, :markdown, :metadata, :manifest, :capabilities, "
                ":source_snapshot, :status, :created_by, 1, :created_at, :published_at, NULL)"
            ),
            {
                "id": revision_id,
                "tenant_id": row["tenant_id"],
                "skill_id": row["id"],
                "content_checksum": content_checksum,
                "manifest_checksum": resources["SKILL.md"]["checksum"],
                "markdown": markdown,
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "manifest": json.dumps(manifest, ensure_ascii=False),
                "capabilities": json.dumps(
                    {"allowed_tools": [], "invocation_policy": "model_allowed"}
                ),
                "source_snapshot": json.dumps(
                    {"source_kind": "legacy_backfill", "legacy_skill_id": row["id"]}
                ),
                "status": revision_status,
                "created_by": owner[:128],
                "created_at": row["created_at"] or now,
                "published_at": (row["updated_at"] or now) if revision_status == "published" else None,
            },
        )
        _set_pointer(bind, row, revision_id)


def _set_pointer(bind: sa.Connection, row: sa.RowMapping, revision_id: str) -> None:
    """仅为 published 旧行设置 current pointer，并保留草稿无发布指针语义。"""

    if row["status"] != "published":
        return
    bind.execute(
        sa.text(
            "UPDATE general_skills SET current_published_revision_id = :revision_id, "
            "row_version = CASE WHEN row_version < 1 THEN 1 ELSE row_version END "
            "WHERE id = :skill_id AND current_published_revision_id IS NULL"
        ),
        {"revision_id": revision_id, "skill_id": row["id"]},
    )


def _backfill_authorization_states(bind: sa.Connection) -> None:
    """为拥有 Skill 的租户建立初始 revision，重复升级不递增。"""

    tenant_ids = bind.execute(sa.text("SELECT DISTINCT tenant_id FROM general_skills")).scalars()
    now = datetime.now(UTC).replace(tzinfo=None)
    for tenant_id in tenant_ids:
        exists = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM general_skill_authorization_states "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        if exists:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO general_skill_authorization_states "
                "(tenant_id, revision, last_event_checksum, updated_at) "
                "VALUES (:tenant_id, 1, :checksum, :updated_at)"
            ),
            {
                "tenant_id": tenant_id,
                "checksum": _checksum({"event": "legacy_backfill", "tenant_id": tenant_id}),
                "updated_at": now,
            },
        )
        event_checksum = _checksum({"event": "legacy_backfill", "tenant_id": tenant_id})
        event_id = f"gsauth_legacy_{hashlib.sha256(str(tenant_id).encode()).hexdigest()[:20]}"
        bind.execute(
            sa.text(
                "INSERT INTO general_skill_authorization_events "
                "(id, tenant_id, authorization_revision, event_type, resource_id, "
                "event_checksum, payload_json, created_at) VALUES "
                "(:id, :tenant_id, 1, 'legacy_backfill', NULL, :checksum, :payload, :created_at)"
            ),
            {
                "id": event_id,
                "tenant_id": tenant_id,
                "checksum": event_checksum,
                "payload": json.dumps({"event": "legacy_backfill"}),
                "created_at": now,
            },
        )
