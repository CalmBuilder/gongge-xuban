"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : sqlite_legacy.py
@CallChain  : SQLite Adapter → initialize_sqlite_database → metadata/legacy migrations → SQLite
@Description: 负责 SQLite 建表、兼容迁移和旧数据修复。
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from pathlib import Path

from sqlalchemy import Column, Engine, MetaData, String, Table, inspect, text
from sqlmodel import SQLModel

_DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID = "20260712_default_model_output_tokens_8192"
_LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS = 2048
_DEFAULT_MODEL_OUTPUT_TOKENS = 8192


def initialize_sqlite_database(engine: Engine) -> None:
    """初始化 SQLite：配置 WAL、按模型建表并执行遗留模式与数据迁移。"""
    import app.db.models  # noqa: F401

    configure_sqlite_runtime(engine)
    SQLModel.metadata.create_all(engine)
    migrate_sqlite_skill_schema(engine)


def configure_sqlite_runtime(engine: Engine) -> None:
    """将 SQLite 运行时 journal_mode 设置为 WAL；会执行 PRAGMA 并结束引擎事务上下文。"""
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))


def migrate_sqlite_skill_schema(engine: Engine) -> None:
    """迁移 SQLite 的模型、用户、会话、工具、知识及智能体相关表，并修复遗留数据。

    副作用：按现有表结构执行 ALTER、UPDATE、INSERT 或 DELETE，并在引擎事务上下文
    结束时提交；SQLite 的 DDL 原子性由实际驱动与数据库行为决定。
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    legacy_key = "so" + "p"
    legacy_active_column = f"active_{legacy_key}_id"
    legacy_stack_column = f"{legacy_key}_stack_json"
    legacy_allowed_column = f"allowed_{legacy_key}s_json"
    legacy_table = f"{legacy_key}_skills"
    legacy_id_column = f"{legacy_key}_id"
    legacy_id_prefix = f"{legacy_key}_"
    with engine.begin() as conn:
        _migrate_default_model_output_limit(conn, tables)
        _migrate_unique_default_model(conn, inspector, tables)

        if "model_configs" in tables:
            model_config_columns = {
                column["name"] for column in inspector.get_columns("model_configs")
            }
            if "extra_body_json" not in model_config_columns:
                conn.execute(text("ALTER TABLE model_configs ADD COLUMN extra_body_json JSON"))
                conn.execute(
                    text(
                        "UPDATE model_configs SET extra_body_json = '{}' "
                        "WHERE extra_body_json IS NULL"
                    )
                )

        if "users" in tables:
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "role" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'member'"))

        if "sessions" in tables:
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            if "agent_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN agent_id VARCHAR"))
            if "title" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN title VARCHAR"))
            if "active_skill_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN active_skill_id VARCHAR"))
                if legacy_active_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET active_skill_id = {legacy_active_column}"))
            if "skill_stack_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN skill_stack_json JSON"))
                if legacy_stack_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET skill_stack_json = {legacy_stack_column}"))
                else:
                    conn.execute(text("UPDATE sessions SET skill_stack_json = '[]'"))
            if "pending_tasks_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN pending_tasks_json JSON"))
                conn.execute(text("UPDATE sessions SET pending_tasks_json = '[]'"))
            if "awaiting_input_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN awaiting_input_json JSON"))
            if "knowledge_context_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN knowledge_context_json JSON"))
                conn.execute(text("UPDATE sessions SET knowledge_context_json = '[]'"))
            if "context_state_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN context_state_json JSON"))
                conn.execute(text("UPDATE sessions SET context_state_json = '{}'"))

        _migrate_agent_identity_fields(conn, tables)
        _migrate_execution_reliability_fields(conn, tables)
        _ensure_sqlite_operation_effect_kind_constraint(conn)
        _migrate_dynamic_capability_fields(conn, tables)
        _migrate_sqlite_platform_general_skill_catalog(conn, tables)
        _migrate_execution_plan_fields(conn, tables)
        _migrate_execution_control_fields(conn, tables)

        if "messages" in tables:
            message_columns = {column["name"] for column in inspector.get_columns("messages")}
            if "metadata_json" not in message_columns:
                conn.execute(text("ALTER TABLE messages ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE messages SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        if "tools" in tables:
            tool_columns = {column["name"] for column in inspector.get_columns("tools")}
            if "bucket" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN bucket VARCHAR NOT NULL DEFAULT '未分桶'"))
            if "tool_type" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN tool_type VARCHAR NOT NULL DEFAULT 'http'"))
            if "config_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN config_json JSON"))
                conn.execute(text("UPDATE tools SET config_json = '{}' WHERE config_json IS NULL"))
            if "allowed_skills_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN allowed_skills_json JSON"))
                if legacy_allowed_column in tool_columns:
                    conn.execute(text(f"UPDATE tools SET allowed_skills_json = {legacy_allowed_column}"))
                else:
                    conn.execute(text("UPDATE tools SET allowed_skills_json = '[]'"))
            if "mcp_server_id" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN mcp_server_id VARCHAR"))

        if "ui_configs" in tables:
            ui_columns = {column["name"] for column in inspector.get_columns("ui_configs")}
            if "reflection_max_rounds" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN reflection_max_rounds INTEGER NOT NULL DEFAULT 1")
                )
            if "agent_loop_max_actions" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN agent_loop_max_actions INTEGER NOT NULL DEFAULT 6")
                )
            if "context_token_budget" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN context_token_budget INTEGER NOT NULL DEFAULT 128000")
                )
            if "context_compaction_trigger_ratio" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN "
                        "context_compaction_trigger_ratio FLOAT NOT NULL DEFAULT 0.7"
                    )
                )
            if "context_recent_round_limit" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN context_recent_round_limit INTEGER NOT NULL DEFAULT 6")
                )
            if "long_summary_token_budget" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN long_summary_token_budget INTEGER NOT NULL DEFAULT 4000")
                )
            if "medium_summary_token_budget" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN medium_summary_token_budget INTEGER NOT NULL DEFAULT 4000")
                )

        if "skill_feedback" in tables:
            feedback_columns = {column["name"] for column in inspector.get_columns("skill_feedback")}
            if "skill_version" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN skill_version VARCHAR"))
            if "step_id" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN step_id VARCHAR"))

        if "message_feedback" in tables:
            message_feedback_columns = {column["name"] for column in inspector.get_columns("message_feedback")}
            feedback_column_sql = {
                "analysis_status": "ALTER TABLE message_feedback ADD COLUMN analysis_status VARCHAR NOT NULL DEFAULT 'pending'",
                "analysis_bucket": "ALTER TABLE message_feedback ADD COLUMN analysis_bucket VARCHAR",
                "analysis_reason": "ALTER TABLE message_feedback ADD COLUMN analysis_reason VARCHAR",
                "analysis_summary": "ALTER TABLE message_feedback ADD COLUMN analysis_summary VARCHAR",
                "analysis_confidence": "ALTER TABLE message_feedback ADD COLUMN analysis_confidence FLOAT",
                "analysis_json": "ALTER TABLE message_feedback ADD COLUMN analysis_json JSON",
                "analyzed_at": "ALTER TABLE message_feedback ADD COLUMN analyzed_at DATETIME",
            }
            for column_name, ddl in feedback_column_sql.items():
                if column_name not in message_feedback_columns:
                    conn.execute(text(ddl))
            if "analysis_json" not in message_feedback_columns:
                conn.execute(text("UPDATE message_feedback SET analysis_json = '{}' WHERE analysis_json IS NULL"))

        if "general_skills" in tables:
            general_skill_columns = {column["name"] for column in inspector.get_columns("general_skills")}
            if "skill_files_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN skill_files_json JSON"))
                conn.execute(text("UPDATE general_skills SET skill_files_json = '[]' WHERE skill_files_json IS NULL"))
            if "metadata_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE general_skills SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        _migrate_knowledge_governance_fields(conn, tables)
        _migrate_knowledge_base_schema(conn, inspector, tables)
        _seed_default_agents(conn, tables)

        if legacy_table in tables and "skills" in tables:
            rows = conn.execute(text(f"SELECT * FROM {legacy_table}")).mappings().all()
            for row in rows:
                skill_id = _normalize_skill_identifier(
                    row.get("skill_id") or row.get(legacy_id_column),
                    legacy_id_prefix,
                )
                if not skill_id:
                    continue
                target_id = str(row["id"]).replace(legacy_id_prefix, "skill_", 1)
                existing = conn.execute(
                    text("SELECT id FROM skills WHERE tenant_id = :tenant_id AND skill_id = :skill_id"),
                    {"tenant_id": row["tenant_id"], "skill_id": skill_id},
                ).first()
                if existing:
                    continue
                content = _migrate_skill_content(row.get("content_json"), skill_id)
                existing_id = conn.execute(
                    text("SELECT id FROM skills WHERE id = :id"),
                    {"id": target_id},
                ).first()
                if existing_id:
                    conn.execute(
                        text(
                            """
                            UPDATE skills
                            SET skill_id = :skill_id, content_json = :content_json, updated_at = :updated_at
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": target_id,
                            "skill_id": skill_id,
                            "content_json": json.dumps(content, ensure_ascii=False),
                            "updated_at": row.get("updated_at"),
                        },
                    )
                    continue
                conn.execute(
                    text(
                        """
                        INSERT INTO skills (
                            id, tenant_id, skill_id, version, name, business_domain,
                            description, content_json, status, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                            :description, :content_json, :status, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": row["tenant_id"],
                        "skill_id": skill_id,
                        "version": row.get("version") or "1.0.0",
                        "name": row["name"],
                        "business_domain": row.get("business_domain"),
                        "description": row.get("description"),
                        "content_json": json.dumps(content, ensure_ascii=False),
                        "status": row.get("status") or "draft",
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                    },
                )
        if "skills" in tables:
            _normalize_existing_skill_rows(conn, legacy_id_prefix)
            if "skill_versions" in tables:
                _normalize_existing_skill_version_rows(conn, legacy_id_prefix)
                _seed_skill_versions(conn)
            _normalize_agent_branch_rows(conn, tables)
            _seed_agent_branch_state(conn, inspector, tables)
            _sync_explicit_skill_tool_bindings(conn, tables)


def _migrate_agent_identity_fields(conn, tables: set[str]) -> None:
    """为非 Alembic 旧 SQLite 库补齐 M4-A 字段、正式关系和历史会话 Usage。"""

    if "agent_profiles" not in tables:
        return
    agent_columns = {
        column["name"] for column in inspect(conn).get_columns("agent_profiles")
    }
    agent_column_sql = {
        "owner_user_id": "ALTER TABLE agent_profiles ADD COLUMN owner_user_id VARCHAR(128)",
        "source_agent_id": "ALTER TABLE agent_profiles ADD COLUMN source_agent_id VARCHAR(128)",
        "source_agent_version": (
            "ALTER TABLE agent_profiles ADD COLUMN source_agent_version VARCHAR(64)"
        ),
        "profile_revision": (
            "ALTER TABLE agent_profiles ADD COLUMN profile_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "published_to_gallery": (
            "ALTER TABLE agent_profiles ADD COLUMN "
            "published_to_gallery BOOLEAN NOT NULL DEFAULT 0"
        ),
        "gallery_published_at": (
            "ALTER TABLE agent_profiles ADD COLUMN gallery_published_at DATETIME"
        ),
        "gallery_published_by": (
            "ALTER TABLE agent_profiles ADD COLUMN gallery_published_by VARCHAR(128)"
        ),
        "agent_category_code": (
            "ALTER TABLE agent_profiles ADD COLUMN "
            "agent_category_code VARCHAR(128) NOT NULL DEFAULT 'assistant'"
        ),
        "visibility_scope": (
            "ALTER TABLE agent_profiles ADD COLUMN "
            "visibility_scope VARCHAR(64) NOT NULL DEFAULT 'private'"
        ),
    }
    added_agent_columns = False
    for column_name, ddl in agent_column_sql.items():
        if column_name not in agent_columns:
            conn.execute(text(ddl))
            added_agent_columns = True

    if "sessions" in tables:
        session_columns = {
            column["name"] for column in inspect(conn).get_columns("sessions")
        }
        session_column_sql = {
            "agent_profile_revision": (
                "ALTER TABLE sessions ADD COLUMN agent_profile_revision INTEGER"
            ),
            "capability_snapshot_json": (
                "ALTER TABLE sessions ADD COLUMN capability_snapshot_json JSON"
            ),
            "origin": "ALTER TABLE sessions ADD COLUMN origin VARCHAR(64)",
        }
        for column_name, ddl in session_column_sql.items():
            if column_name not in session_columns:
                conn.execute(text(ddl))

    if added_agent_columns:
        _backfill_sqlite_agent_identity(conn)
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_profiles_tenant_owner "
            "ON agent_profiles (tenant_id, owner_user_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_profiles_tenant_gallery_status "
            "ON agent_profiles (tenant_id, published_to_gallery, status)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_profiles_tenant_category_status "
            "ON agent_profiles (tenant_id, agent_category_code, status)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_profiles_tenant_source "
            "ON agent_profiles (tenant_id, source_agent_id)"
        )
    )
    if {"sessions", "agent_usages", "users"} <= tables:
        _backfill_sqlite_session_usages(conn)


def _migrate_dynamic_capability_fields(conn, tables: set[str]) -> None:
    """为非 Alembic 桌面 SQLite 旧库补齐 B0.3 目录、快照和 provider 预检列。"""

    additions = {
        "tools": {
            "reliability_contract_json": (
                "ALTER TABLE tools ADD COLUMN reliability_contract_json "
                "JSON NOT NULL DEFAULT '{}'"
            ),
            "reliability_checksum": (
                "ALTER TABLE tools ADD COLUMN reliability_checksum VARCHAR(64)"
            ),
            "reliability_published_at": (
                "ALTER TABLE tools ADD COLUMN reliability_published_at DATETIME"
            ),
        },
        "general_skills": {
            "usage_mode": (
                "ALTER TABLE general_skills ADD COLUMN usage_mode "
                "VARCHAR(64) NOT NULL DEFAULT 'atomic_execution'"
            ),
            "planning_guidance_json": (
                "ALTER TABLE general_skills ADD COLUMN planning_guidance_json "
                "JSON NOT NULL DEFAULT '{}'"
            ),
            "planning_guidance_checksum": (
                "ALTER TABLE general_skills ADD COLUMN planning_guidance_checksum VARCHAR(64)"
            ),
            "planning_guidance_published_at": (
                "ALTER TABLE general_skills ADD COLUMN planning_guidance_published_at DATETIME"
            ),
        },
        "model_configs": {
            "capability_snapshot_json": (
                "ALTER TABLE model_configs ADD COLUMN capability_snapshot_json "
                "JSON NOT NULL DEFAULT '{}'"
            ),
            "capability_checksum": (
                "ALTER TABLE model_configs ADD COLUMN capability_checksum VARCHAR(64)"
            ),
            "preflight_status": (
                "ALTER TABLE model_configs ADD COLUMN preflight_status "
                "VARCHAR(64) NOT NULL DEFAULT 'unverified'"
            ),
            "preflight_error": (
                "ALTER TABLE model_configs ADD COLUMN preflight_error VARCHAR(2000)"
            ),
            "capability_verified_at": (
                "ALTER TABLE model_configs ADD COLUMN capability_verified_at DATETIME"
            ),
        },
        "sop_operations": {
            "capability_snapshot_json": (
                "ALTER TABLE sop_operations ADD COLUMN capability_snapshot_json "
                "JSON NOT NULL DEFAULT '{}'"
            ),
            "capability_checksum": (
                "ALTER TABLE sop_operations ADD COLUMN capability_checksum VARCHAR(64)"
            ),
        },
    }
    for table_name, column_definitions in additions.items():
        if table_name not in tables:
            continue
        existing = {
            column["name"] for column in inspect(conn).get_columns(table_name)
        }
        for column_name, ddl in column_definitions.items():
            if column_name not in existing:
                conn.execute(text(ddl))
    index_definitions = (
        (
            "tools",
            "ix_tools_reliability_checksum",
            "reliability_checksum",
        ),
        (
            "general_skills",
            "ix_general_skills_usage_mode",
            "usage_mode",
        ),
        (
            "general_skills",
            "ix_general_skills_planning_guidance_checksum",
            "planning_guidance_checksum",
        ),
        (
            "model_configs",
            "ix_model_configs_preflight_status",
            "preflight_status",
        ),
        (
            "model_configs",
            "ix_model_configs_capability_checksum",
            "capability_checksum",
        ),
        (
            "sop_operations",
            "ix_sop_operations_capability_checksum",
            "capability_checksum",
        ),
    )
    for table_name, index_name, column_name in index_definitions:
        if table_name in tables:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name} ({column_name})"
                )
            )


def _migrate_sqlite_platform_general_skill_catalog(conn, tables: set[str]) -> None:
    """把桌面 SQLite 中的租户内置副本合并为项目级目录资产。"""

    required_tables = {
        "general_skills",
        "general_skill_revisions",
        "general_skill_catalog_commands",
    }
    if not required_tables <= tables:
        return
    _sqlite_add_catalog_scope_columns(conn)
    _sqlite_expand_catalog_visibility_constraint(conn)
    _sqlite_merge_platform_catalog_rows(conn)
    _sqlite_tighten_catalog_scope_columns(conn)
    _sqlite_create_catalog_scope_constraints_and_indexes(conn)


def _sqlite_operations(conn):
    """为 SQLite 兼容迁移创建支持表重建的 Alembic 操作对象。"""

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    return Operations(MigrationContext.configure(conn))


def _sqlite_add_catalog_scope_columns(conn) -> None:
    """增加目录范围列，并把旧租户主键改成允许平台资产为空。"""

    additions: dict[str, tuple[Column[object], ...]] = {
        "general_skills": (
            Column("catalog_scope", String(64), nullable=True, server_default="tenant"),
            Column("catalog_key", String(128), nullable=True),
        ),
        "general_skill_revisions": (
            Column("catalog_scope", String(64), nullable=True, server_default="tenant"),
        ),
        "general_skill_catalog_commands": (
            Column("catalog_scope", String(64), nullable=True, server_default="tenant"),
            Column("scope_key", String(128), nullable=True),
        ),
    }
    for table_name, columns in additions.items():
        existing = {
            str(column["name"]): column
            for column in inspect(conn).get_columns(table_name)
        }
        missing = [column for column in columns if str(column.name) not in existing]
        tenant_column = existing.get("tenant_id")
        needs_nullable_tenant = bool(tenant_column and not tenant_column["nullable"])
        if not missing and not needs_nullable_tenant:
            continue
        operations = _sqlite_operations(conn)
        with operations.batch_alter_table(table_name, recreate="always") as batch:
            for column in missing:
                batch.add_column(column)
            if needs_nullable_tenant:
                batch.alter_column(
                    "tenant_id",
                    existing_type=String(128),
                    nullable=True,
                )


def _sqlite_expand_catalog_visibility_constraint(conn) -> None:
    """在提升旧快照前允许项目级 Skill 广场可见性。"""

    checks = inspect(conn).get_check_constraints("general_skills")
    visibility_check = next(
        (item for item in checks if item.get("name") == "ck_general_skill_visibility_scope"),
        None,
    )
    if visibility_check and "platform_gallery" in str(visibility_check.get("sqltext") or ""):
        return
    operations = _sqlite_operations(conn)
    with operations.batch_alter_table("general_skills", recreate="always") as batch:
        if visibility_check:
            batch.drop_constraint("ck_general_skill_visibility_scope", type_="check")
        batch.create_check_constraint(
            "ck_general_skill_visibility_scope",
            "visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery', 'platform_gallery')",
        )


def _sqlite_merge_platform_catalog_rows(conn) -> None:
    """合并内容一致的旧内置副本，冲突时中止迁移而不覆盖业务事实。"""

    rows = conn.execute(
        text(
            "SELECT id, tenant_id, slug, status, metadata_json, skill_markdown, "
            "current_published_revision_id FROM general_skills "
            "WHERE catalog_scope = 'tenant'"
        )
    ).mappings().all()
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        if metadata.get("managed_catalog") is True and isinstance(metadata.get("catalog_key"), str):
            groups[str(metadata["catalog_key"])].append(row)

    skill_map: dict[str, str] = {}
    revision_map: dict[str, str] = {}
    catalog_groups: list[tuple[str, list[Mapping[str, object]], Mapping[str, object]]] = []
    for catalog_key, group in sorted(groups.items()):
        _sqlite_ensure_same_catalog_content(catalog_key, group)
        canonical = sorted(
            group,
            key=lambda row: (str(row["status"]) != "published", str(row["id"])),
        )[0]
        catalog_groups.append((catalog_key, group, canonical))
        canonical_id = str(canonical["id"])
        for row in group:
            skill_map[str(row["id"])] = canonical_id

    published_pointers: dict[str, str | None] = {}
    for catalog_key, group, canonical in catalog_groups:
        canonical_id = str(canonical["id"])
        revisions_by_skill = {
            str(row["id"]): _sqlite_skill_revisions(conn, str(row["id"]))
            for row in group
        }
        _sqlite_ensure_same_revision_shapes(catalog_key, revisions_by_skill)
        canonical_revisions = revisions_by_skill[canonical_id]
        canonical_by_shape = {
            (int(row["revision_number"]), str(row["content_checksum"])): row
            for row in canonical_revisions
        }
        for revision in canonical_revisions:
            revision_map[str(revision["id"])] = str(revision["id"])
        for revisions in revisions_by_skill.values():
            for revision in revisions:
                shape = (int(revision["revision_number"]), str(revision["content_checksum"]))
                target = canonical_by_shape.get(shape)
                if target is None:
                    raise RuntimeError(
                        f"platform Skill catalog revision conflict for catalog key {catalog_key}"
                    )
                revision_map[str(revision["id"])] = str(target["id"])
        published_pointers[canonical_id] = _sqlite_published_revision_id(
            group,
            revisions_by_skill,
            revision_map,
        )

    for catalog_key, group, canonical in catalog_groups:
        canonical_id = str(canonical["id"])
        metadata = _json_object(canonical["metadata_json"])
        metadata["catalog_key"] = catalog_key
        metadata["catalog_scope"] = "platform"
        status = (
            "published"
            if any(str(row["status"]) == "published" for row in group)
            else str(canonical["status"])
        )
        conn.execute(
            text(
                "UPDATE general_skills SET tenant_id = NULL, catalog_scope = 'platform', "
                "catalog_key = :catalog_key, owner_user_id = NULL, "
                "visibility_scope = 'platform_gallery', status = :status, "
                "current_published_revision_id = :revision_id, metadata_json = :metadata "
                "WHERE id = :skill_id"
            ),
            {
                "catalog_key": catalog_key,
                "status": status,
                "revision_id": published_pointers[canonical_id],
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "skill_id": canonical_id,
            },
        )
        for revision in _sqlite_skill_revisions(conn, canonical_id):
            conn.execute(
                text(
                    "UPDATE general_skill_revisions SET tenant_id = NULL, "
                    "catalog_scope = 'platform' WHERE id = :revision_id"
                ),
                {"revision_id": revision["id"]},
            )

    _sqlite_remap_platform_catalog_references(conn, skill_map, revision_map)
    for revision_id, target_id in revision_map.items():
        if revision_id != target_id:
            conn.execute(
                text("DELETE FROM general_skill_revisions WHERE id = :revision_id"),
                {"revision_id": revision_id},
            )
    for skill_id, target_id in skill_map.items():
        if skill_id != target_id:
            conn.execute(
                text("DELETE FROM general_skills WHERE id = :skill_id"),
                {"skill_id": skill_id},
            )


def _sqlite_ensure_same_catalog_content(
    catalog_key: str,
    rows: Sequence[Mapping[str, object]],
) -> None:
    """确保同一内置来源键的旧 Skill 正文和来源摘要一致。"""

    fingerprints = {
        (
            _json_object(row["metadata_json"]).get("content_checksum"),
            _json_object(row["metadata_json"]).get("source_normalized_checksum"),
            str(row["skill_markdown"] or ""),
        )
        for row in rows
    }
    if len(fingerprints) > 1:
        raise RuntimeError(
            f"platform Skill catalog content conflict for catalog key {catalog_key}"
        )


def _sqlite_skill_revisions(conn, skill_id: str) -> list[Mapping[str, object]]:
    """读取旧 Skill 的租户范围 revision。"""

    return conn.execute(
        text(
            "SELECT id, revision_number, content_checksum, manifest_checksum, status, "
            "published_at FROM general_skill_revisions WHERE skill_id = :skill_id "
            "AND catalog_scope = 'tenant' ORDER BY revision_number, id"
        ),
        {"skill_id": skill_id},
    ).mappings().all()


def _sqlite_ensure_same_revision_shapes(
    catalog_key: str,
    revisions_by_skill: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    """确保重复快照的 revision 结构一致，避免迁移时丢失版本。"""

    shapes = {
        tuple(
            (
                int(row["revision_number"]),
                str(row["content_checksum"]),
                str(row["manifest_checksum"]),
            )
            for row in revisions
        )
        for revisions in revisions_by_skill.values()
    }
    if len(shapes) > 1:
        raise RuntimeError(
            f"platform Skill catalog revision conflict for catalog key {catalog_key}"
        )


def _sqlite_published_revision_id(
    group: Sequence[Mapping[str, object]],
    revisions_by_skill: Mapping[str, Sequence[Mapping[str, object]]],
    revision_map: Mapping[str, str],
) -> str | None:
    """选择旧发布指针映射后的平台 revision。"""

    candidates: list[str] = []
    for skill in group:
        if str(skill["status"]) != "published":
            continue
        pointer = skill.get("current_published_revision_id")
        if pointer:
            candidates.append(revision_map.get(str(pointer), str(pointer)))
        for revision in revisions_by_skill.get(str(skill["id"]), ()):
            if str(revision["status"]) == "published":
                candidates.append(revision_map.get(str(revision["id"]), str(revision["id"])))
    return sorted(set(candidates))[0] if candidates else None


def _sqlite_remap_platform_catalog_references(
    conn,
    skill_map: Mapping[str, str],
    revision_map: Mapping[str, str],
) -> None:
    """重定向旧绑定、提案、发布和命令 JSON 中的重复主体标识。"""

    table_names = set(inspect(conn).get_table_names())
    column_map = {
        "skill_id": skill_map,
        "parent_skill_id": skill_map,
        "child_skill_id": skill_map,
        "target_skill_id": skill_map,
        "revision_id": revision_map,
        "parent_revision_id": revision_map,
        "child_revision_id": revision_map,
        "approved_revision_id": revision_map,
        "base_revision_id": revision_map,
    }
    for table_name in sorted(table_names):
        if not (
            table_name.startswith("general_skill_")
            or table_name == "session_general_skill_overrides"
        ):
            continue
        if table_name in {"general_skills", "general_skill_revisions"}:
            continue
        columns = {str(column["name"]) for column in inspect(conn).get_columns(table_name)}
        for column_name, replacements in column_map.items():
            if column_name not in columns:
                continue
            for old_id, new_id in replacements.items():
                if old_id == new_id:
                    continue
                conn.execute(
                    text(
                        f"UPDATE {table_name} SET {column_name} = :new_id "
                        f"WHERE {column_name} = :old_id"
                    ),
                    {"old_id": old_id, "new_id": new_id},
                )

    if "agent_resource_bindings" in table_names:
        for old_id, new_id in skill_map.items():
            if old_id == new_id:
                continue
            conn.execute(
                text(
                    "UPDATE agent_resource_bindings SET resource_id = :new_id "
                    "WHERE resource_type = 'general_skill' AND resource_id = :old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )
    if "publication_releases" in table_names:
        for old_id, new_id in skill_map.items():
            if old_id == new_id:
                continue
            conn.execute(
                text(
                    "UPDATE publication_releases SET resource_id = :new_id "
                    "WHERE resource_type = 'general_skill' AND resource_id = :old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )

    for table_name, json_column in (
        ("general_skill_catalog_commands", "result_json"),
        ("general_skill_install_intents", "installed_revision_ids_json"),
    ):
        if table_name not in table_names:
            continue
        rows = conn.execute(
            text(f"SELECT id, {json_column} FROM {table_name}")
        ).mappings().all()
        for row in rows:
            original = _sqlite_json_value(row[json_column])
            replaced = _sqlite_replace_catalog_ids(original, skill_map, revision_map)
            if replaced == original:
                continue
            conn.execute(
                text(f"UPDATE {table_name} SET {json_column} = :payload WHERE id = :id"),
                {"id": row["id"], "payload": json.dumps(replaced, ensure_ascii=False)},
            )


def _sqlite_json_value(value: object) -> object:
    """解析 SQLite JSON 文本或原生值，供历史命令重写使用。"""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _sqlite_replace_catalog_ids(
    value: object,
    skill_map: Mapping[str, str],
    revision_map: Mapping[str, str],
) -> object:
    """递归替换历史 JSON 中已合并的 Skill/Revision 标识。"""

    if isinstance(value, str):
        return revision_map.get(value, skill_map.get(value, value))
    if isinstance(value, list):
        return [_sqlite_replace_catalog_ids(item, skill_map, revision_map) for item in value]
    if isinstance(value, dict):
        return {
            key: _sqlite_replace_catalog_ids(item, skill_map, revision_map)
            for key, item in value.items()
        }
    return value


def _sqlite_tighten_catalog_scope_columns(conn) -> None:
    """在合并完成后收紧范围列，阻止后续写入无效作用域。"""

    for table_name, columns in (
        ("general_skills", ("catalog_scope",)),
        ("general_skill_revisions", ("catalog_scope",)),
        ("general_skill_catalog_commands", ("catalog_scope", "scope_key")),
    ):
        current = {
            str(column["name"]): column
            for column in inspect(conn).get_columns(table_name)
        }
        if all(not current[column]["nullable"] and current[column]["default"] is None for column in columns):
            continue
        operations = _sqlite_operations(conn)
        with operations.batch_alter_table(table_name, recreate="always") as batch:
            for column_name in columns:
                batch.alter_column(
                    column_name,
                    existing_type=String(128 if column_name == "scope_key" else 64),
                    nullable=False,
                    server_default=None,
                )


def _sqlite_create_catalog_scope_constraints_and_indexes(conn) -> None:
    """建立 SQLite 兼容的范围检查、唯一索引和查询索引。"""

    checks = {
        str(item["name"])
        for item in inspect(conn).get_check_constraints("general_skills")
        if item.get("name")
    }
    if "ck_general_skill_catalog_scope" not in checks:
        operations = _sqlite_operations(conn)
        with operations.batch_alter_table("general_skills", recreate="always") as batch:
            batch.create_check_constraint(
                "ck_general_skill_catalog_scope",
                "(catalog_scope = 'platform' AND tenant_id IS NULL AND owner_user_id IS NULL "
                "AND visibility_scope = 'platform_gallery' AND catalog_key IS NOT NULL) OR "
                "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL "
                "AND visibility_scope <> 'platform_gallery')",
            )
    revision_checks = {
        str(item["name"])
        for item in inspect(conn).get_check_constraints("general_skill_revisions")
        if item.get("name")
    }
    if "ck_general_skill_revision_catalog_scope" not in revision_checks:
        operations = _sqlite_operations(conn)
        with operations.batch_alter_table("general_skill_revisions", recreate="always") as batch:
            batch.create_check_constraint(
                "ck_general_skill_revision_catalog_scope",
                "(catalog_scope = 'platform' AND tenant_id IS NULL) OR "
                "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL)",
            )
    command_checks = {
        str(item["name"])
        for item in inspect(conn).get_check_constraints("general_skill_catalog_commands")
        if item.get("name")
    }
    if "ck_general_skill_catalog_command_scope" not in command_checks:
        operations = _sqlite_operations(conn)
        with operations.batch_alter_table(
            "general_skill_catalog_commands",
            recreate="always",
        ) as batch:
            batch.create_check_constraint(
                "ck_general_skill_catalog_command_scope",
                "(catalog_scope = 'platform' AND tenant_id IS NULL AND scope_key = 'platform') OR "
                "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL AND scope_key = tenant_id)",
            )

    _sqlite_create_unique_index_if_missing(
        conn,
        "uq_general_skill_catalog_key",
        "general_skills",
        ("catalog_key",),
    )
    _sqlite_create_unique_index_if_missing(
        conn,
        "uq_general_skill_revision_scope_number",
        "general_skill_revisions",
        ("catalog_scope", "skill_id", "revision_number"),
    )
    _sqlite_create_unique_index_if_missing(
        conn,
        "uq_general_skill_revision_scope_checksum",
        "general_skill_revisions",
        ("catalog_scope", "skill_id", "content_checksum"),
    )
    _sqlite_create_unique_index_if_missing(
        conn,
        "uq_general_skill_catalog_scope_command",
        "general_skill_catalog_commands",
        ("scope_key", "command_type", "command_id"),
    )
    for table_name, index_name, columns in (
        (
            "general_skills",
            "ix_general_skill_catalog_scope_status",
            ("catalog_scope", "status", "catalog_key"),
        ),
        (
            "general_skill_revisions",
            "ix_general_skill_revisions_catalog_scope",
            ("catalog_scope",),
        ),
        (
            "general_skill_catalog_commands",
            "ix_general_skill_catalog_commands_catalog_scope",
            ("catalog_scope",),
        ),
    ):
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON {table_name} ({', '.join(columns)})"
            )
        )


def _sqlite_create_unique_index_if_missing(
    conn,
    index_name: str,
    table_name: str,
    columns: tuple[str, ...],
) -> None:
    """在 SQLite 中用具名唯一索引补齐旧表的新范围唯一性。"""

    inspector = inspect(conn)
    index_names = {str(item["name"]) for item in inspector.get_indexes(table_name)}
    unique_names = {
        str(item["name"])
        for item in inspector.get_unique_constraints(table_name)
        if item.get("name")
    }
    if index_name in index_names or index_name in unique_names:
        return
    conn.execute(
        text(
            f"CREATE UNIQUE INDEX {index_name} ON {table_name} ({', '.join(columns)})"
        )
    )


def _migrate_execution_plan_fields(conn, tables: set[str]) -> None:
    """为桌面 SQLite 旧库补齐 B0.4 Execution/Step 字段并回填稳定 step key。"""

    if "sop_instances" in tables:
        existing_instance_columns = {
            column["name"] for column in inspect(conn).get_columns("sop_instances")
        }
        legacy_dynamic_count = conn.execute(
            text("SELECT COUNT(*) FROM sop_instances WHERE kind = 'dynamic_task'")
        ).scalar_one()
        if legacy_dynamic_count:
            required_identity_columns = {
                "agent_id",
                "goal_snapshot_json",
                "current_plan_revision_id",
                "current_plan_checksum",
                "capability_snapshot_json",
            }
            if not required_identity_columns <= existing_instance_columns:
                raise RuntimeError("cannot infer identity for legacy dynamic executions")
            missing_identity_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM sop_instances WHERE kind = 'dynamic_task' AND "
                    "(agent_id IS NULL OR initiator_user_id IS NULL OR "
                    "goal_snapshot_json IS NULL OR current_plan_revision_id IS NULL OR "
                    "current_plan_checksum IS NULL OR capability_snapshot_json IS NULL)"
                )
            ).scalar_one()
            if missing_identity_count:
                raise RuntimeError("cannot infer identity for legacy dynamic executions")
        instance_additions = {
            "agent_id": "ALTER TABLE sop_instances ADD COLUMN agent_id VARCHAR(512)",
            "goal_snapshot_json": (
                "ALTER TABLE sop_instances ADD COLUMN goal_snapshot_json JSON"
            ),
            "current_plan_revision_id": (
                "ALTER TABLE sop_instances ADD COLUMN current_plan_revision_id VARCHAR(512)"
            ),
            "current_plan_checksum": (
                "ALTER TABLE sop_instances ADD COLUMN current_plan_checksum VARCHAR(64)"
            ),
            "capability_snapshot_json": (
                "ALTER TABLE sop_instances ADD COLUMN capability_snapshot_json JSON"
            ),
            "capability_checksum": (
                "ALTER TABLE sop_instances ADD COLUMN capability_checksum VARCHAR(64)"
            ),
            "budget_snapshot_json": (
                "ALTER TABLE sop_instances ADD COLUMN budget_snapshot_json "
                "JSON NOT NULL DEFAULT '{}'"
            ),
            "terminal_reason_json": (
                "ALTER TABLE sop_instances ADD COLUMN terminal_reason_json "
                "JSON NOT NULL DEFAULT '{}'"
            ),
        }
        for column_name, ddl in instance_additions.items():
            if column_name not in existing_instance_columns:
                conn.execute(text(ddl))
        for column_name in (
            "agent_id",
            "current_plan_revision_id",
            "current_plan_checksum",
            "capability_checksum",
        ):
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_sop_instances_{column_name} "
                    f"ON sop_instances ({column_name})"
                )
            )

    if "sop_node_executions" in tables:
        existing_step_columns = {
            column["name"] for column in inspect(conn).get_columns("sop_node_executions")
        }
        step_additions = {
            "step_key": (
                "ALTER TABLE sop_node_executions ADD COLUMN step_key VARCHAR(128)"
            ),
            "plan_revision_id": (
                "ALTER TABLE sop_node_executions ADD COLUMN plan_revision_id VARCHAR(512)"
            ),
            "step_kind": (
                "ALTER TABLE sop_node_executions ADD COLUMN step_kind "
                "VARCHAR(64) NOT NULL DEFAULT 'sop_node'"
            ),
            "title": "ALTER TABLE sop_node_executions ADD COLUMN title VARCHAR(191)",
            "required": (
                "ALTER TABLE sop_node_executions ADD COLUMN required "
                "BOOLEAN NOT NULL DEFAULT 1"
            ),
            "superseded_by_step_key": (
                "ALTER TABLE sop_node_executions ADD COLUMN "
                "superseded_by_step_key VARCHAR(128)"
            ),
        }
        for column_name, ddl in step_additions.items():
            if column_name not in existing_step_columns:
                conn.execute(text(ddl))
        conn.execute(
            text(
                "UPDATE sop_node_executions SET step_key = node_id "
                "WHERE step_key IS NULL OR step_key = ''"
            )
        )
        null_step_count = conn.execute(
            text("SELECT COUNT(*) FROM sop_node_executions WHERE step_key IS NULL OR step_key = ''")
        ).scalar_one()
        if null_step_count:
            raise RuntimeError("cannot infer stable step key for legacy node executions")
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_step_attempt "
                "ON sop_node_executions (tenant_id, instance_id, step_key, attempt)"
            )
        )
        for column_name in (
            "step_key",
            "plan_revision_id",
            "step_kind",
            "superseded_by_step_key",
        ):
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_sop_node_executions_{column_name} "
                    f"ON sop_node_executions ({column_name})"
                )
            )


def _migrate_execution_control_fields(conn, tables: set[str]) -> None:
    """为桌面 SQLite 旧库补齐 B0.5 控制字段，并重建可承载通用 Attention 的主表。"""

    if "sop_instances" in tables:
        instance_columns = {
            column["name"] for column in inspect(conn).get_columns("sop_instances")
        }
        if "current_result_id" not in instance_columns:
            conn.execute(
                text("ALTER TABLE sop_instances ADD COLUMN current_result_id VARCHAR(512)")
            )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_sop_instances_current_result_id "
                "ON sop_instances (current_result_id)"
            )
        )

    if "sop_work_items" in tables:
        columns = inspect(conn).get_columns("sop_work_items")
        column_names = {column["name"] for column in columns}
        node_nullable = next(
            (
                bool(column["nullable"])
                for column in columns
                if column["name"] == "node_execution_id"
            ),
            False,
        )
        if "attention_kind" not in column_names or not node_nullable:
            _rebuild_sqlite_attention_table(conn)

    if "agent_events" in tables:
        event_columns = {
            column["name"] for column in inspect(conn).get_columns("agent_events")
        }
        required_event_columns = {
            "schema_version",
            "aggregate_type",
            "aggregate_id",
            "aggregate_revision",
            "correlation_id",
            "causation_id",
            "payload_checksum",
        }
        event_constraints = {
            str(item.get("name") or "")
            for item in inspect(conn).get_check_constraints("agent_events")
        }
        if not required_event_columns <= event_columns or not {
            "ck_agent_event_schema_version",
            "ck_agent_event_aggregate_revision",
        } <= event_constraints:
            _rebuild_sqlite_agent_events(conn)


def _rebuild_sqlite_attention_table(conn) -> None:
    """原子重建工作项表，使节点身份可空且历史行获得确定性 Attention 字段。"""

    from app.db.models import SopWorkItem

    old_table = SQLModel.metadata.tables["sop_work_items"]
    reflected = Table("sop_work_items", MetaData(), autoload_with=conn)
    rows = conn.execute(reflected.select()).mappings().all()
    preparer = conn.dialect.identifier_preparer
    for index in inspect(conn).get_indexes("sop_work_items"):
        index_name = str(index.get("name") or "")
        if index_name:
            conn.execute(text(f"DROP INDEX {preparer.quote(index_name)}"))
    backup_name = "_legacy_b05_sop_work_items"
    if inspect(conn).has_table(backup_name):
        raise RuntimeError("legacy Attention table rebuild was interrupted")
    conn.execute(text(f"ALTER TABLE sop_work_items RENAME TO {backup_name}"))
    SopWorkItem.__table__.create(conn)
    now_defaults = {
        "attention_kind": "sop_human_task",
        "source_type": "formal_sop",
        "payload_json": {},
        "allowed_commands_json": ["claim", "unclaim", "complete"],
        "resolution_json": {},
        "required": True,
    }
    for row in rows:
        values = {
            column.name: row[column.name]
            for column in old_table.columns
            if column.name in row
        }
        node_execution_id = str(row.get("node_execution_id") or "")
        attention_key = f"sop-node:{node_execution_id}"
        raw_identity = json.dumps(
            {
                "tenant_id": str(row["tenant_id"]),
                "execution_id": str(row["instance_id"]),
                "attention_key": attention_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        values.update(now_defaults)
        values.update(
            {
                "attention_key": attention_key,
                "attention_identity": hashlib.sha256(
                    raw_identity.encode("utf-8")
                ).hexdigest(),
            }
        )
        conn.execute(SopWorkItem.__table__.insert().values(**values))
    conn.execute(text(f"DROP TABLE {backup_name}"))


def _rebuild_sqlite_agent_events(conn) -> None:
    """重建历史事件表，为版本和聚合 revision 加入数据库级契约。"""

    from app.db.models import AgentEvent

    reflected = Table("agent_events", MetaData(), autoload_with=conn)
    rows = conn.execute(reflected.select()).mappings().all()
    preparer = conn.dialect.identifier_preparer
    for index in inspect(conn).get_indexes("agent_events"):
        index_name = str(index.get("name") or "")
        if index_name:
            conn.execute(text(f"DROP INDEX {preparer.quote(index_name)}"))
    backup_name = "_legacy_b05_agent_events"
    if inspect(conn).has_table(backup_name):
        raise RuntimeError("legacy AgentEvent table rebuild was interrupted")
    conn.execute(text(f"ALTER TABLE agent_events RENAME TO {backup_name}"))
    AgentEvent.__table__.create(conn)
    for row in rows:
        values = {
            column.name: row[column.name]
            for column in AgentEvent.__table__.columns
            if column.name in row
        }
        values["schema_version"] = int(row.get("schema_version") or 1)
        conn.execute(AgentEvent.__table__.insert().values(**values))
    conn.execute(text(f"DROP TABLE {backup_name}"))


def _migrate_execution_reliability_fields(conn, tables: set[str]) -> None:
    """为桌面 SQLite 旧库补齐 B0.1/B0.2 所有权、幂等、未知效果和追加账本。"""

    if "sop_instances" not in tables or "sop_operations" not in tables:
        return
    initial_instance_columns = {
        column["name"] for column in inspect(conn).get_columns("sop_instances")
    }
    initial_operation_columns = {
        column["name"] for column in inspect(conn).get_columns("sop_operations")
    }
    instance_needs_migration = not {
        "kind",
        "active_slot_key",
        "fencing_token",
        "effect_state",
    }.issubset(initial_instance_columns)
    operation_needs_migration = not {
        "logical_action_id",
        "request_fingerprint",
        "effect_kind",
        "effect_state",
    }.issubset(initial_operation_columns)
    if not instance_needs_migration and not operation_needs_migration:
        return
    instance_rows = conn.execute(
        text(
            "SELECT id, tenant_id, session_id, status, skill_id, skill_version_id, "
            "skill_version, definition_checksum FROM sop_instances ORDER BY id"
        )
    ).mappings().all()
    active_statuses = {"created", "running", "waiting"}
    terminal_statuses = {"succeeded", "failed", "cancelled", "timed_out"}
    invalid = [
        str(row["id"])
        for row in instance_rows
        if instance_needs_migration
        and (
            row["status"] not in active_statuses | terminal_statuses
            or any(
                not str(row[field] or "").strip()
                for field in (
                    "skill_id",
                    "skill_version_id",
                    "skill_version",
                    "definition_checksum",
                )
            )
        )
    ]
    active_keys = [
        (str(row["tenant_id"]), str(row["session_id"]))
        for row in instance_rows
        if instance_needs_migration and row["status"] in active_statuses
    ]
    if invalid or len(active_keys) != len(set(active_keys)):
        raise RuntimeError(
            "legacy SQLite execution history cannot be mapped safely: "
            + ",".join(invalid or ["duplicate-active-slot"])
        )
    instance_additions = {
        "kind": "ALTER TABLE sop_instances ADD COLUMN kind VARCHAR(64) NOT NULL DEFAULT 'sop'",
        "active_slot_key": "ALTER TABLE sop_instances ADD COLUMN active_slot_key VARCHAR(512)",
        "initiator_user_id": "ALTER TABLE sop_instances ADD COLUMN initiator_user_id VARCHAR(128)",
        "source_kind": (
            "ALTER TABLE sop_instances ADD COLUMN source_kind "
            "VARCHAR(64) NOT NULL DEFAULT 'legacy'"
        ),
        "source_ref": "ALTER TABLE sop_instances ADD COLUMN source_ref VARCHAR(512)",
        "cancellation_requested_at": (
            "ALTER TABLE sop_instances ADD COLUMN cancellation_requested_at DATETIME"
        ),
        "cancellation_requested_by": (
            "ALTER TABLE sop_instances ADD COLUMN cancellation_requested_by VARCHAR(128)"
        ),
        "cancellation_reason": (
            "ALTER TABLE sop_instances ADD COLUMN cancellation_reason VARCHAR(2000)"
        ),
        "cancellation_disposition": (
            "ALTER TABLE sop_instances ADD COLUMN cancellation_disposition "
            "VARCHAR(64) NOT NULL DEFAULT 'none'"
        ),
        "lease_owner": "ALTER TABLE sop_instances ADD COLUMN lease_owner VARCHAR(128)",
        "lease_expires_at": "ALTER TABLE sop_instances ADD COLUMN lease_expires_at DATETIME",
        "lease_acquired_at": "ALTER TABLE sop_instances ADD COLUMN lease_acquired_at DATETIME",
        "lease_heartbeat_at": "ALTER TABLE sop_instances ADD COLUMN lease_heartbeat_at DATETIME",
        "fencing_token": (
            "ALTER TABLE sop_instances ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0"
        ),
        "effect_state": (
            "ALTER TABLE sop_instances ADD COLUMN effect_state "
            "VARCHAR(64) NOT NULL DEFAULT 'none'"
        ),
    }
    existing_instance_columns = {
        column["name"] for column in inspect(conn).get_columns("sop_instances")
    }
    for column_name, ddl in instance_additions.items():
        if column_name not in existing_instance_columns:
            conn.execute(text(ddl))
    if instance_needs_migration:
        for row in instance_rows:
            conn.execute(
                text(
                    "UPDATE sop_instances SET kind='sop', source_kind='legacy', "
                    "source_ref=COALESCE(source_ref, :source_ref), "
                    "active_slot_key=:active_slot, "
                    "cancellation_disposition=COALESCE(cancellation_disposition, 'none'), "
                    "fencing_token=COALESCE(fencing_token, 0), "
                    "effect_state=COALESCE(effect_state, 'none') WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "source_ref": row["session_id"],
                    "active_slot": (
                        f"foreground:{row['session_id']}"
                        if row["status"] in active_statuses
                        else None
                    ),
                },
            )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_tenant_active_slot "
            "ON sop_instances (tenant_id, active_slot_key)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sop_instances_tenant_lease_expiry "
            "ON sop_instances (tenant_id, lease_expires_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sop_instances_lease_owner "
            "ON sop_instances (lease_owner)"
        )
    )

    operation_additions = {
        "logical_action_id": (
            "ALTER TABLE sop_operations ADD COLUMN logical_action_id VARCHAR(128)"
        ),
        "request_fingerprint": (
            "ALTER TABLE sop_operations ADD COLUMN request_fingerprint VARCHAR(64)"
        ),
        "remote_idempotency_key": (
            "ALTER TABLE sop_operations ADD COLUMN remote_idempotency_key VARCHAR(128)"
        ),
        "idempotency_required": (
            "ALTER TABLE sop_operations ADD COLUMN idempotency_required "
            "BOOLEAN NOT NULL DEFAULT 1"
        ),
        "idempotency_scope": (
            "ALTER TABLE sop_operations ADD COLUMN idempotency_scope "
            "VARCHAR(64) NOT NULL DEFAULT 'instance'"
        ),
        "idempotency_key_fields_json": (
            "ALTER TABLE sop_operations ADD COLUMN idempotency_key_fields_json "
            "JSON NOT NULL DEFAULT '[]'"
        ),
        "effect_kind": (
            "ALTER TABLE sop_operations ADD COLUMN effect_kind "
            "VARCHAR(64) NOT NULL DEFAULT 'legacy_unknown'"
        ),
        "effect_state": (
            "ALTER TABLE sop_operations ADD COLUMN effect_state "
            "VARCHAR(64) NOT NULL DEFAULT 'none'"
        ),
        "cancellation_disposition": (
            "ALTER TABLE sop_operations ADD COLUMN cancellation_disposition "
            "VARCHAR(64) NOT NULL DEFAULT 'none'"
        ),
        "compensates_operation_id": (
            "ALTER TABLE sop_operations ADD COLUMN compensates_operation_id VARCHAR(512)"
        ),
        "reconciled_at": "ALTER TABLE sop_operations ADD COLUMN reconciled_at DATETIME",
    }
    existing_operation_columns = {
        column["name"] for column in inspect(conn).get_columns("sop_operations")
    }
    for column_name, ddl in operation_additions.items():
        if column_name not in existing_operation_columns:
            conn.execute(text(ddl))
    operations = (
        conn.execute(text("SELECT * FROM sop_operations ORDER BY id")).mappings().all()
        if operation_needs_migration
        else []
    )
    for row in operations:
        request = _strict_json_object(
            row["request_json"],
            context=f"sop_operations[{row['id']}].request_json",
        )
        canonical = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        logical_action_id = str(row.get("logical_action_id") or "").strip() or (
            "legacy:" + hashlib.sha256(str(row["id"]).encode("utf-8")).hexdigest()
        )
        method = str(request.get("method") or "").upper()
        effect_kind = (
            "read"
            if row["operation_name"] == "knowledge.search" or method == "GET"
            else "external_write"
        )
        effect_state = (
            "none"
            if effect_kind == "read"
            else "complete"
            if row["status"] == "succeeded"
            else "unknown"
            if row["status"] in {"running", "unknown"}
            else "none"
        )
        conn.execute(
            text(
                "UPDATE sop_operations SET logical_action_id=:logical_action_id, "
                "request_fingerprint=:request_fingerprint, "
                "idempotency_required=COALESCE(idempotency_required, 1), "
                "idempotency_scope=COALESCE(idempotency_scope, 'instance'), "
                "idempotency_key_fields_json=COALESCE(idempotency_key_fields_json, '[]'), "
                "effect_kind=:effect_kind, effect_state=:effect_state, "
                "cancellation_disposition=COALESCE(cancellation_disposition, 'none') "
                "WHERE id=:id"
            ),
            {
                "id": row["id"],
                "logical_action_id": logical_action_id,
                "request_fingerprint": fingerprint,
                "effect_kind": effect_kind,
                "effect_state": effect_state,
            },
        )
        _backfill_sqlite_operation_ledgers(
            conn,
            row,
            logical_action_id=logical_action_id,
            effect_state=effect_state,
        )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sop_operation_tenant_logical_action "
            "ON sop_operations (tenant_id, logical_action_id)"
        )
    )
    for column_name in (
        "logical_action_id",
        "remote_idempotency_key",
        "compensates_operation_id",
    ):
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_sop_operations_{column_name} "
                f"ON sop_operations ({column_name})"
            )
        )
    if operation_needs_migration:
        _backfill_sqlite_instance_effects(conn, instance_rows)


def _ensure_sqlite_operation_effect_kind_constraint(conn) -> None:
    """为已完成历史迁移的 SQLite Operation 表补齐 destructive 检查约束。

    SQLite 的 ``CREATE TABLE`` 不会把新模型约束应用到既有表；如果旧库已经具备
    B0.2 全部字段，常规字段迁移会提前返回，因而必须单独检查并在安全条件下重建表。
    重建前校验历史值和未知列，保留可反射的旧索引，避免静默丢失数据或结构。
    """

    from app.db.models import SopOperation

    inspector = inspect(conn)
    if not inspector.has_table("sop_operations"):
        return
    checks = inspector.get_check_constraints("sop_operations")
    effect_checks = [
        item
        for item in checks
        if "effect_kind" in str(item.get("sqltext") or "")
    ]
    if effect_checks and all(
        "destructive" in str(item.get("sqltext") or "") for item in effect_checks
    ):
        return

    model_columns = {column.name for column in SopOperation.__table__.columns}
    reflected = Table("sop_operations", MetaData(), autoload_with=conn)
    existing_columns = {column.name for column in reflected.columns}
    unknown_columns = existing_columns - model_columns
    if unknown_columns:
        raise RuntimeError(
            "legacy SQLite sop_operations has columns unsupported by the current model: "
            + ",".join(sorted(unknown_columns))
        )
    invalid_rows = conn.execute(
        text(
            "SELECT id, effect_kind FROM sop_operations "
            "WHERE effect_kind NOT IN "
            "('read', 'local_write', 'execute', 'external_write', 'destructive', 'legacy_unknown')"
        )
    ).mappings().all()
    if invalid_rows:
        raise RuntimeError(
            "legacy SQLite sop_operations contains unsupported effect_kind values: "
            + ",".join(
                f"{row['id']}={row['effect_kind']}" for row in invalid_rows
            )
        )

    rows = conn.execute(reflected.select()).mappings().all()
    indexes = [
        item
        for item in inspector.get_indexes("sop_operations")
        if item.get("name")
    ]
    preparer = conn.dialect.identifier_preparer
    for index in indexes:
        conn.execute(text(f"DROP INDEX {preparer.quote(str(index['name']))}"))
    backup_name = "_legacy_b077_sop_operations"
    if inspector.has_table(backup_name):
        raise RuntimeError("legacy SQLite Operation constraint rebuild was interrupted")
    conn.execute(text(f"ALTER TABLE sop_operations RENAME TO {backup_name}"))
    SopOperation.__table__.create(conn)
    for row in rows:
        values = {
            column.name: row[column.name]
            for column in SopOperation.__table__.columns
            if column.name in row
        }
        conn.execute(SopOperation.__table__.insert().values(**values))
    conn.execute(text(f"DROP TABLE {backup_name}"))

    current_indexes = {
        str(item.get("name"))
        for item in inspect(conn).get_indexes("sop_operations")
        if item.get("name")
    }
    for index in indexes:
        index_name = str(index["name"])
        columns = [str(column) for column in (index.get("column_names") or [])]
        if index_name in current_indexes or not columns:
            continue
        unique = "UNIQUE " if index.get("unique") else ""
        quoted_columns = ", ".join(preparer.quote(column) for column in columns)
        conn.execute(
            text(
                f"CREATE {unique}INDEX {preparer.quote(index_name)} "
                f"ON sop_operations ({quoted_columns})"
            )
        )


def _backfill_sqlite_operation_ledgers(
    conn,
    row,
    *,
    logical_action_id: str,
    effect_state: str,
) -> None:
    """为旧 Operation 幂等补一条 attempt，并为确定/未知外部效果补事实。"""

    operation_id = str(row["id"])
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    conn.execute(
        text(
            "INSERT OR IGNORE INTO sop_operation_attempts ("
            "id, tenant_id, instance_id, operation_id, node_execution_id, attempt_number, "
            "status, error_json, started_at, completed_at, created_at, updated_at"
            ") VALUES ("
            ":id, :tenant_id, :instance_id, :operation_id, :node_execution_id, 1, "
            ":status, :error_json, :started_at, :completed_at, :created_at, :updated_at)"
        ),
        {
            "id": f"legacyattempt:{digest}",
            "tenant_id": row["tenant_id"],
            "instance_id": row["instance_id"],
            "operation_id": operation_id,
            "node_execution_id": row["node_execution_id"],
            "status": row["status"],
            "error_json": row["error_json"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
    )
    if effect_state not in {"complete", "unknown"}:
        return
    conn.execute(
        text(
            "INSERT OR IGNORE INTO sop_operation_effects ("
            "id, tenant_id, instance_id, operation_id, logical_action_id, sequence, "
            "event_type, effect_state, external_reference, evidence_json, "
            "compensation_operation_id, created_at"
            ") VALUES ("
            ":id, :tenant_id, :instance_id, :operation_id, :logical_action_id, 1, "
            ":event_type, :effect_state, :external_reference, :evidence_json, NULL, :created_at)"
        ),
        {
            "id": f"legacyeffect:{digest}",
            "tenant_id": row["tenant_id"],
            "instance_id": row["instance_id"],
            "operation_id": operation_id,
            "logical_action_id": logical_action_id,
            "event_type": (
                "legacy_effect_confirmed"
                if effect_state == "complete"
                else "legacy_effect_unknown"
            ),
            "effect_state": effect_state,
            "external_reference": row["external_reference"],
            "evidence_json": '{"migration":"sqlite_legacy_b02"}',
            "created_at": row["updated_at"],
        },
    )


def _backfill_sqlite_instance_effects(conn, instance_rows) -> None:
    """从外部写操作聚合桌面旧库实例的 none/partial/complete/unknown 效果。"""

    for instance in instance_rows:
        states = conn.execute(
            text(
                "SELECT effect_state FROM sop_operations "
                "WHERE instance_id=:instance_id "
                "AND effect_kind IN ('external_write', 'destructive')"
            ),
            {"instance_id": instance["id"]},
        ).scalars().all()
        if "unknown" in states:
            aggregate = "unknown"
        else:
            completed = sum(state in {"complete", "compensated"} for state in states)
            aggregate = (
                "none"
                if completed == 0
                else "complete"
                if completed == len(states)
                else "partial"
            )
        conn.execute(
            text("UPDATE sop_instances SET effect_state=:state WHERE id=:id"),
            {"state": aggregate, "id": instance["id"]},
        )


def _backfill_sqlite_agent_identity(conn) -> None:
    """从同租户真实用户和明确 metadata 回填 SQLite 正式 Agent 字段。"""

    users = conn.execute(text("SELECT id, tenant_id, username FROM users")).mappings().all()
    user_ids = {
        (str(row["tenant_id"]), str(row["id"])): str(row["id"]) for row in users
    }
    usernames = {
        (str(row["tenant_id"]), str(row["username"])): str(row["id"]) for row in users
    }
    agents = conn.execute(
        text("SELECT id, tenant_id, is_overall, metadata_json FROM agent_profiles")
    ).mappings()
    for row in agents:
        metadata = _json_object(row["metadata_json"])
        tenant_id = str(row["tenant_id"])
        owner_candidate = str(metadata.get("owner_user_id") or "").strip()
        publisher_candidate = str(metadata.get("gallery_published_by") or "").strip()
        published = metadata.get("published_to_gallery") is True
        conn.execute(
            text(
                "UPDATE agent_profiles SET owner_user_id = :owner_user_id, "
                "published_to_gallery = :published, "
                "gallery_published_at = :published_at, "
                "gallery_published_by = :publisher_id, "
                "agent_category_code = :category, "
                "visibility_scope = :visibility "
                "WHERE id = :agent_id"
            ),
            {
                "owner_user_id": (
                    None
                    if row["is_overall"]
                    else user_ids.get((tenant_id, owner_candidate))
                ),
                "published": published,
                "published_at": metadata.get("gallery_published_at"),
                "publisher_id": (
                    user_ids.get((tenant_id, publisher_candidate))
                    or usernames.get((tenant_id, publisher_candidate))
                ),
                "category": (
                    "professional"
                    if metadata.get("employee_type") == "expert"
                    else "assistant"
                ),
                "visibility": "tenant" if published else "private",
                "agent_id": row["id"],
            },
        )


def _backfill_sqlite_session_usages(conn) -> None:
    """把有效旧会话幂等补成 Usage，生成 ID 稳定且不覆盖已有用户动作。"""

    rows = conn.execute(
        text(
            "SELECT s.tenant_id, s.user_id, s.agent_id, MIN(s.created_at) AS first_used_at "
            "FROM sessions s "
            "JOIN users u ON u.id = s.user_id AND u.tenant_id = s.tenant_id "
            "JOIN agent_profiles a ON a.id = s.agent_id AND a.tenant_id = s.tenant_id "
            "WHERE s.user_id IS NOT NULL AND s.agent_id IS NOT NULL AND a.is_overall = 0 "
            "GROUP BY s.tenant_id, s.user_id, s.agent_id"
        )
    ).mappings()
    metadata_json = json.dumps(
        {"source": "m4_session_backfill"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for row in rows:
        key = (
            str(row["tenant_id"]),
            str(row["user_id"]),
            str(row["agent_id"]),
        )
        digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:24]
        conn.execute(
            text(
                "INSERT OR IGNORE INTO agent_usages "
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
                "created_at": row["first_used_at"],
                "updated_at": row["first_used_at"],
            },
        )


def _migrate_default_model_output_limit(conn, tables: set[str]) -> None:
    """将 model_configs 中仍为 2048 的默认模型输出上限迁至 8192，并记录迁移编号。"""
    if "model_configs" not in tables:
        return

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_data_migrations (
                id VARCHAR PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    ).first()
    if applied:
        return

    conn.execute(
        text(
            """
            UPDATE model_configs
            SET max_output_tokens = :new_limit,
                updated_at = CURRENT_TIMESTAMP
            WHERE is_default = 1
              AND max_output_tokens = :legacy_limit
            """
        ),
        {
            "new_limit": _DEFAULT_MODEL_OUTPUT_TOKENS,
            "legacy_limit": _LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS,
        },
    )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    )


def _migrate_unique_default_model(conn, inspector, tables: set[str]) -> None:
    """为旧 SQLite 库清理重复默认项，并补齐生成列唯一索引。"""

    if "model_configs" not in tables:
        return

    defaults = conn.execute(
        text(
            """
            SELECT id, tenant_id
            FROM model_configs
            WHERE is_default = 1
            ORDER BY tenant_id, updated_at DESC, id
            """
        )
    ).all()
    seen_tenants: set[str] = set()
    for model_id, tenant_id in defaults:
        if tenant_id in seen_tenants:
            conn.execute(
                text(
                    """
                    UPDATE model_configs
                    SET is_default = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :model_id
                    """
                ),
                {"model_id": model_id},
            )
        else:
            seen_tenants.add(tenant_id)

    columns = {column["name"] for column in inspector.get_columns("model_configs")}
    if "default_tenant_id" not in columns:
        conn.execute(
            text(
                """
                ALTER TABLE model_configs
                ADD COLUMN default_tenant_id VARCHAR(128)
                GENERATED ALWAYS AS (
                    CASE WHEN is_default THEN tenant_id ELSE NULL END
                ) VIRTUAL
                """
            )
        )
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configs_tenant_default
            ON model_configs(default_tenant_id)
            """
        )
    )


def _migrate_skill_content(value: object, skill_id: str) -> dict[str, object]:
    """解析 skills.content_json：已有 skill_id 时以参数覆盖，否则优先保留遗留标识并以参数回退，再补齐图结构。"""
    if isinstance(value, str):
        try:
            content = json.loads(value)
        except json.JSONDecodeError:
            content = {}
    elif isinstance(value, dict):
        content = dict(value)
    else:
        content = {}
    if "skill_id" not in content:
        content["skill_id"] = content.pop("so" + "p_id", skill_id)
    else:
        content["skill_id"] = skill_id
    return _ensure_skill_graph(content)


def _normalize_existing_skill_rows(conn, legacy_id_prefix: str) -> None:
    """规范化 skills 表的 skill_id 与 content_json；会更新不存在标识冲突的现有行。"""
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skills")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        if skill_id == row.get("skill_id"):
            conn.execute(
                text("UPDATE skills SET content_json = :content_json WHERE id = :id"),
                {"id": row["id"], "content_json": json.dumps(content, ensure_ascii=False)},
            )
            continue
        existing = conn.execute(
            text("SELECT id FROM skills WHERE skill_id = :skill_id AND id != :id"),
            {"skill_id": skill_id, "id": row["id"]},
        ).first()
        if existing:
            continue
        conn.execute(
            text("UPDATE skills SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _normalize_existing_skill_version_rows(conn, legacy_id_prefix: str) -> None:
    """规范化 skill_versions 表每行的 skill_id 与 content_json，并写回更新。"""
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skill_versions")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        conn.execute(
            text("UPDATE skill_versions SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _sync_explicit_skill_tool_bindings(conn, tables: set[str]) -> None:
    """把 skills 图中显式调用的工具写入 tools.allowed_skills_json，补齐缺失绑定。"""
    if "skills" not in tables or "tools" not in tables:
        return
    skill_rows = conn.execute(
        text(
            "SELECT tenant_id, skill_id, content_json FROM skills "
            "WHERE status IS NULL OR status != 'deleted'"
        )
    ).mappings().all()
    for skill_row in skill_rows:
        content = _json_object(skill_row.get("content_json"))
        tool_names = _explicit_skill_tool_names(content)
        if not tool_names:
            continue
        tool_rows = conn.execute(
            text("SELECT id, name, allowed_skills_json FROM tools WHERE tenant_id = :tenant_id"),
            {"tenant_id": skill_row["tenant_id"]},
        ).mappings().all()
        for tool_row in tool_rows:
            if str(tool_row.get("name") or "") not in tool_names:
                continue
            allowed_skills = _json_string_list(tool_row.get("allowed_skills_json"))
            skill_id = str(skill_row.get("skill_id") or "").strip()
            if not skill_id or skill_id in allowed_skills:
                continue
            allowed_skills.append(skill_id)
            conn.execute(
                text(
                    "UPDATE tools SET allowed_skills_json = :allowed_skills, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {
                    "id": tool_row["id"],
                    "allowed_skills": json.dumps(allowed_skills, ensure_ascii=False),
                },
            )


def _explicit_skill_tool_names(content: dict[str, object]) -> set[str]:
    """从技能 nodes 或遗留 steps 的 allowed_actions 提取显式 call_tool 工具名。"""
    names: set[str] = set()
    for key in ("nodes", "steps"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            actions = item.get("allowed_actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                value = str(action or "").strip()
                if value.startswith("call_tool:"):
                    name = value.split(":", 1)[1].strip()
                    if name:
                        names.add(name)
    return names


def _json_string_list(value: object) -> list[str]:
    """将 tools.allowed_skills_json 的 JSON 文本或列表规范化为非空字符串列表。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _ensure_skill_graph(content: dict[str, object]) -> dict[str, object]:
    """将遗留技能 steps 转换为 nodes、edges 和起止节点字段，并移除 steps。"""
    nodes = content.get("nodes")
    steps = content.get("steps")
    if isinstance(nodes, list) and nodes:
        content.pop("steps", None)
        content.setdefault("start_node_id", _first_node_id(nodes))
        content.setdefault("terminal_node_ids", [_last_node_id(nodes)] if _last_node_id(nodes) else [])
        return content
    if not isinstance(steps, list) or not steps:
        content.setdefault("nodes", [])
        content.setdefault("edges", [])
        content.setdefault("terminal_node_ids", [])
        content.pop("steps", None)
        return content
    normalized_steps = [step for step in steps if isinstance(step, dict)]
    content["nodes"] = [_step_to_node_dict(step) for step in normalized_steps]
    content["edges"] = [
        {
            "source_node_id": str(normalized_steps[index].get("step_id") or f"step_{index + 1}"),
            "next_node_id": str(normalized_steps[index + 1].get("step_id") or f"step_{index + 2}"),
            "priority": index,
            "label": "默认推进",
        }
        for index in range(len(normalized_steps) - 1)
    ]
    if normalized_steps:
        content["start_node_id"] = content.get("start_node_id") or str(normalized_steps[0].get("step_id") or "step_1")
        content["terminal_node_ids"] = content.get("terminal_node_ids") or [
            str(normalized_steps[-1].get("step_id") or f"step_{len(normalized_steps)}")
        ]
    content.pop("steps", None)
    return content


def _step_to_node_dict(step: dict[str, object]) -> dict[str, object]:
    """把单个遗留技能步骤转换为图节点字典，并依据动作与待收集信息推断节点类型。"""
    actions = step.get("allowed_actions") if isinstance(step.get("allowed_actions"), list) else []
    expected = step.get("expected_user_info") if isinstance(step.get("expected_user_info"), list) else []
    node_type = "collect_info" if expected else "response"
    if any(isinstance(action, str) and action.startswith("call_tool:") for action in actions):
        node_type = "tool_call"
    if "handoff_human" in actions:
        node_type = "handoff"
    return {
        "node_id": str(step.get("step_id") or step.get("node_id") or "step"),
        "type": node_type,
        "name": str(step.get("name") or step.get("step_id") or "步骤"),
        "instruction": str(step.get("instruction") or ""),
        "optional": bool(step.get("optional") or False),
        "condition": step.get("condition") if isinstance(step.get("condition"), str) else None,
        "expected_user_info": expected,
        "allowed_actions": actions,
        "knowledge_scope": step.get("knowledge_scope") if isinstance(step.get("knowledge_scope"), dict) else {},
        "retry_policy": step.get("retry_policy") if isinstance(step.get("retry_policy"), dict) else {},
        "metadata": step.get("metadata") if isinstance(step.get("metadata"), dict) else {},
    }


def _first_node_id(nodes: object) -> str | None:
    """返回节点列表中首个有效 node_id；输入无效或不存在时返回空值。"""
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _last_node_id(nodes: object) -> str | None:
    """返回节点列表中末个有效 node_id；输入无效或不存在时返回空值。"""
    if not isinstance(nodes, list):
        return None
    for node in reversed(nodes):
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _seed_skill_versions(conn) -> None:
    """为 skills 中尚无同租户、标识和版本记录的行插入 skill_versions 快照。"""
    rows = conn.execute(text("SELECT * FROM skills")).mappings().all()
    for row in rows:
        version = row.get("version") or "1.0.0"
        existing = conn.execute(
            text(
                """
                SELECT id FROM skill_versions
                WHERE tenant_id = :tenant_id AND skill_id = :skill_id AND version = :version
                """
            ),
            {"tenant_id": row["tenant_id"], "skill_id": row["skill_id"], "version": version},
        ).first()
        if existing:
            continue
        conn.execute(
            text(
                """
                INSERT INTO skill_versions (
                    id, tenant_id, skill_id, version, name, business_domain,
                    description, content_json, status, created_at, updated_at
                )
                VALUES (
                    :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                    :description, :content_json, :status, :created_at, :updated_at
                )
                """
            ),
            {
                "id": f"skillver_{row['id']}",
                "tenant_id": row["tenant_id"],
                "skill_id": row["skill_id"],
                "version": version,
                "name": row["name"],
                "business_domain": row.get("business_domain"),
                "description": row.get("description"),
                "content_json": row.get("content_json"),
                "status": row.get("status") or "draft",
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
        )


def _normalize_skill_identifier(value: object, legacy_id_prefix: str) -> str:
    """将遗留技能标识前缀替换为 skill_；非字符串输入规范化为空字符串。"""
    if not isinstance(value, str):
        return ""
    if value.startswith(legacy_id_prefix):
        return f"skill_{value[len(legacy_id_prefix):]}"
    return value


def _migrate_knowledge_governance_fields(conn, tables: set[str]) -> None:
    """为旧 SQLite 知识库补齐 M5-A 治理字段、保守 owner 和组织访问关系。"""

    if "knowledge_bases" not in tables:
        return
    columns = {
        column["name"] for column in inspect(conn).get_columns("knowledge_bases")
    }
    column_sql = {
        "owner_user_id": (
            "ALTER TABLE knowledge_bases ADD COLUMN owner_user_id VARCHAR(128)"
        ),
        "responsible_org_unit_id": (
            "ALTER TABLE knowledge_bases ADD COLUMN responsible_org_unit_id VARCHAR(128)"
        ),
        "access_scope": (
            "ALTER TABLE knowledge_bases ADD COLUMN "
            "access_scope VARCHAR(64) NOT NULL DEFAULT 'owner'"
        ),
        "download_policy": (
            "ALTER TABLE knowledge_bases ADD COLUMN "
            "download_policy VARCHAR(64) NOT NULL DEFAULT 'restricted'"
        ),
        "revision": (
            "ALTER TABLE knowledge_bases ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
        ),
    }
    added = False
    for column_name, ddl in column_sql.items():
        if column_name not in columns:
            conn.execute(text(ddl))
            added = True
    if added:
        _backfill_sqlite_knowledge_owners(conn, tables)

    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS knowledge_base_org_access ("
            "id VARCHAR(512) NOT NULL PRIMARY KEY, "
            "tenant_id VARCHAR(128) NOT NULL, "
            "knowledge_base_id VARCHAR(128) NOT NULL, "
            "org_unit_id VARCHAR(128) NOT NULL, "
            "include_descendants BOOLEAN NOT NULL DEFAULT 1, "
            "status VARCHAR(64) NOT NULL DEFAULT 'active', "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_knowledge_base_org_access "
            "UNIQUE (tenant_id, knowledge_base_id, org_unit_id))"
        )
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_tenant_owner_status "
        "ON knowledge_bases (tenant_id, owner_user_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_tenant_responsible_org "
        "ON knowledge_bases (tenant_id, responsible_org_unit_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_tenant_access_status "
        "ON knowledge_bases (tenant_id, access_scope, status)",
        "CREATE INDEX IF NOT EXISTS ix_kb_org_access_tenant_org_status "
        "ON knowledge_base_org_access (tenant_id, org_unit_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_org_access_tenant_id "
        "ON knowledge_base_org_access (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_org_access_knowledge_base_id "
        "ON knowledge_base_org_access (knowledge_base_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_org_access_org_unit_id "
        "ON knowledge_base_org_access (org_unit_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_org_access_status "
        "ON knowledge_base_org_access (status)",
    ):
        conn.execute(text(index_sql))


def _backfill_sqlite_knowledge_owners(conn, tables: set[str]) -> None:
    """从同租户用户 ID 和明确 metadata 保守回填旧 SQLite 知识 owner。"""

    valid_users = set()
    if "users" in tables:
        valid_users = {
            (str(row["tenant_id"]), str(row["id"]))
            for row in conn.execute(
                text("SELECT id, tenant_id FROM users")
            ).mappings()
        }
    rows = conn.execute(
        text("SELECT id, tenant_id, metadata_json FROM knowledge_bases")
    ).mappings()
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        candidate = str(
            metadata.get("created_by_user_id") or metadata.get("owner_user_id") or ""
        ).strip()
        owner_user_id = (
            candidate if (str(row["tenant_id"]), candidate) in valid_users else None
        )
        conn.execute(
            text(
                "UPDATE knowledge_bases SET owner_user_id = :owner_user_id, "
                "access_scope = 'owner', download_policy = 'restricted', revision = 1 "
                "WHERE id = :knowledge_base_id"
            ),
            {
                "owner_user_id": owner_user_id,
                "knowledge_base_id": row["id"],
            },
        )


def _migrate_knowledge_base_schema(conn, inspector, tables: set[str]) -> None:
    """迁移知识库各表的 knowledge_base_id 与版本列，补种默认库和版本并拆分多文档旧库。

    副作用：可能变更 knowledge_bases、knowledge_base_versions、文档、分桶、分块、
    概念、发现建议及摄取任务表的数据与列定义；本函数不自行提交。
    """
    tenant_ids = _tenant_ids(conn, tables)
    if "knowledge_bases" in tables:
        for tenant_id in tenant_ids:
            default_id = _default_knowledge_base_id(tenant_id)
            existing = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": default_id},
            ).first()
            if not existing:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, owner_user_id,
                            responsible_org_unit_id, access_scope, download_policy, revision,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, NULL, NULL,
                            'owner', 'restricted', 1, 'active', '{}',
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": default_id,
                        "tenant_id": tenant_id,
                        "name": "默认知识库",
                        "description": "系统默认知识库",
                    },
                )

    table_names = {
        "knowledge_documents": "knowledge_base_id",
        "knowledge_buckets": "knowledge_base_id",
        "knowledge_chunks": "knowledge_base_id",
        "knowledge_concepts": "knowledge_base_id",
        "knowledge_discovery_suggestions": "knowledge_base_id",
        "knowledge_ingest_jobs": "knowledge_base_id",
    }
    for table_name, column_name in table_names.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR"))
        rows = conn.execute(
            text(f"SELECT DISTINCT tenant_id FROM {table_name} WHERE {column_name} IS NULL OR {column_name} = ''")
        ).mappings().all()
        for row in rows:
            tenant_id = str(row.get("tenant_id") or "")
            if tenant_id:
                conn.execute(
                    text(f"UPDATE {table_name} SET {column_name} = :knowledge_base_id WHERE tenant_id = :tenant_id AND ({column_name} IS NULL OR {column_name} = '')"),
                    {"tenant_id": tenant_id, "knowledge_base_id": _default_knowledge_base_id(tenant_id)},
                )

    if "knowledge_base_versions" in tables and "knowledge_bases" in tables:
        knowledge_bases = conn.execute(text("SELECT * FROM knowledge_bases")).mappings().all()
        for row in knowledge_bases:
            version_id = _knowledge_base_version_id(str(row["id"]), "1.0.0")
            existing = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not existing:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            :status, :metadata_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": row["tenant_id"],
                        "knowledge_base_id": row["id"],
                        "name": row["name"],
                        "description": row.get("description"),
                        "status": row.get("status") or "active",
                        "metadata_json": row.get("metadata_json") or "{}",
                    },
                )

    for table_name in table_names:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "knowledge_base_version_id" not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN knowledge_base_version_id VARCHAR"))
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT knowledge_base_id FROM {table_name}
                WHERE knowledge_base_id IS NOT NULL
                  AND knowledge_base_id != ''
                  AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                """
            )
        ).mappings().all()
        for row in rows:
            knowledge_base_id = str(row.get("knowledge_base_id") or "")
            if not knowledge_base_id:
                continue
            conn.execute(
                text(
                    f"""
                    UPDATE {table_name}
                    SET knowledge_base_version_id = :version_id
                    WHERE knowledge_base_id = :knowledge_base_id
                      AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                    """
                ),
                {
                    "knowledge_base_id": knowledge_base_id,
                    "version_id": _knowledge_base_version_id(knowledge_base_id, "1.0.0"),
                },
            )

    _split_document_backed_knowledge_bases(conn, tables)


def _split_document_backed_knowledge_bases(conn, tables: set[str]) -> None:
    """把含多个 knowledge_documents 的遗留知识库按文档拆成独立知识库和 1.0.0 版本。"""
    required_tables = {"knowledge_bases", "knowledge_base_versions", "knowledge_documents"}
    if not required_tables.issubset(tables):
        return

    document_groups = conn.execute(
        text(
            """
            SELECT knowledge_base_id, COUNT(id) AS document_count
            FROM knowledge_documents
            WHERE knowledge_base_id IS NOT NULL AND knowledge_base_id != ''
            GROUP BY knowledge_base_id
            """
        )
    ).mappings().all()
    multi_document_base_ids = {
        str(row["knowledge_base_id"])
        for row in document_groups
        if int(row.get("document_count") or 0) > 1
    }
    if not multi_document_base_ids:
        return

    for source_knowledge_base_id in sorted(multi_document_base_ids):
        source = conn.execute(
            text("SELECT * FROM knowledge_bases WHERE id = :id"),
            {"id": source_knowledge_base_id},
        ).mappings().first()
        if not source:
            continue
        documents = conn.execute(
            text(
                """
                SELECT *
                FROM knowledge_documents
                WHERE knowledge_base_id = :knowledge_base_id
                ORDER BY created_at, id
                """
            ),
            {"knowledge_base_id": source_knowledge_base_id},
        ).mappings().all()
        if len(documents) <= 1:
            continue
        for document in documents:
            target_id = _document_knowledge_base_id(str(document["id"]))
            target = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": target_id},
            ).first()
            target_name = _unique_migrated_knowledge_base_name(
                conn,
                str(source["tenant_id"]),
                _document_knowledge_base_name(document),
                target_id,
            )
            metadata = _json_object(source.get("metadata_json"))
            metadata.update(
                {
                    "created_from_document_upload": True,
                    "source_document_id": document["id"],
                    "source_filename": document.get("filename"),
                    "split_from_knowledge_base_id": source_knowledge_base_id,
                }
            )
            if not target:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, owner_user_id,
                            responsible_org_unit_id, access_scope, download_policy, revision,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, :owner_user_id, NULL,
                            'owner', 'restricted', 1, :status, :metadata_json,
                            :created_at, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": source["tenant_id"],
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "owner_user_id": source.get("owner_user_id"),
                        "status": "active",
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                        "created_at": document.get("created_at") or source.get("created_at"),
                    },
                )
            version_id = _knowledge_base_version_id(target_id, "1.0.0")
            version_exists = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not version_exists:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            'active', :metadata_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": source["tenant_id"],
                        "knowledge_base_id": target_id,
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    },
                )
            _move_document_knowledge_rows(conn, tables, str(document["id"]), target_id, version_id)


def _move_document_knowledge_rows(
    conn,
    tables: set[str],
    document_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    """把指定文档及其分桶、分块、概念、发现建议和摄取任务改绑到新知识库版本。"""
    document_scoped_tables = (
        "knowledge_buckets",
        "knowledge_chunks",
        "knowledge_concepts",
        "knowledge_discovery_suggestions",
    )
    if "knowledge_documents" in tables:
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    for table_name in document_scoped_tables:
        if table_name not in tables:
            continue
        conn.execute(
            text(
                f"""
                UPDATE {table_name}
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE document_id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    if "knowledge_ingest_jobs" not in tables:
        return
    conn.execute(
        text(
            """
            UPDATE knowledge_ingest_jobs
            SET knowledge_base_id = :knowledge_base_id,
                knowledge_base_version_id = :version_id
            WHERE document_id = :document_id
            """
        ),
        {
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "version_id": version_id,
        },
    )


def _json_object(value: object) -> dict[str, object]:
    """将遗留 JSON 对象文本或字典规范化为新字典，非法输入返回空字典。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _strict_json_object(value: object, *, context: str) -> dict[str, object]:
    """严格解析影响副作用判定的遗留 JSON；损坏或非对象数据必须中止迁移。"""
    if isinstance(value, dict):
        parsed = dict(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(
                value,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"unsupported JSON constant: {constant}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"invalid JSON object in {context}") from exc
    else:
        raise RuntimeError(f"invalid JSON object in {context}")
    if not isinstance(parsed, dict):
        raise RuntimeError(f"invalid JSON object in {context}")
    return parsed


def _document_knowledge_base_id(document_id: str) -> str:
    """为拆分后的文档知识库生成确定性的 knowledge_bases 主键。"""
    return f"kb_doc_{document_id}"


def _document_knowledge_base_name(document) -> str:
    """按文档标题、文件主名和文件名的优先级生成迁移后的知识库名称。"""
    title = str(document.get("title") or "").strip()
    if title:
        return title
    filename = str(document.get("filename") or "").strip()
    stem = Path(filename).stem.strip()
    return stem or filename or "未命名知识库"


def _unique_migrated_knowledge_base_name(
    conn,
    tenant_id: str,
    base_name: str,
    target_id: str,
) -> str:
    """在同租户 knowledge_bases 中为迁移目标生成不与其他记录重复的名称。"""
    normalized = base_name.strip() or "未命名知识库"
    existing_names = {
        str(row[0])
        for row in conn.execute(
            text("SELECT name FROM knowledge_bases WHERE tenant_id = :tenant_id AND id != :target_id"),
            {"tenant_id": tenant_id, "target_id": target_id},
        ).all()
        if row[0]
    }
    if normalized not in existing_names:
        return normalized
    index = 2
    while True:
        candidate = f"{normalized} {index}"
        if candidate not in existing_names:
            return candidate
        index += 1


def _seed_default_agents(conn, tables: set[str]) -> None:
    """按租户补种整体智能体并归档符合条件的遗留默认智能体；仅为归档后仍活跃的默认智能体补种绑定。"""
    if "agent_profiles" not in tables:
        return
    tenant_ids = _tenant_ids(conn, tables)
    for tenant_id in tenant_ids:
        for agent_id, name, is_overall in (
            (_overall_agent_id(tenant_id), "整体智能体", True),
        ):
            existing = conn.execute(text("SELECT id FROM agent_profiles WHERE id = :id"), {"id": agent_id}).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_profiles (
                        id, tenant_id, name, description, persona_prompt, is_overall,
                        status, metadata_json, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :name, :description, NULL, :is_overall,
                        'active', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": agent_id,
                    "tenant_id": tenant_id,
                    "name": name,
                    "description": "全局资源池" if is_overall else "默认对话可见域",
                    "is_overall": 1 if is_overall else 0,
                },
            )
        _archive_default_agent(conn, tenant_id)
        if "agent_resource_bindings" in tables:
            _seed_default_agent_bindings(conn, tenant_id)


def _seed_default_agent_bindings(conn, tenant_id: str) -> None:
    """为仍活跃的遗留默认智能体补种技能、通用技能和知识库资源绑定。"""
    default_agent = _default_agent_id(tenant_id)
    active_default = conn.execute(
        text(
            """
            SELECT id FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND status != 'archived'
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not active_default:
        return
    resource_queries = (
        ("skill", "SELECT id, status FROM skills WHERE tenant_id = :tenant_id AND status != 'deleted'"),
        ("general_skill", "SELECT id, status FROM general_skills WHERE tenant_id = :tenant_id AND status != 'deleted'"),
        ("knowledge_base", "SELECT id, status FROM knowledge_bases WHERE tenant_id = :tenant_id AND status != 'deleted'"),
    )
    for resource_type, sql in resource_queries:
        rows = conn.execute(text(sql), {"tenant_id": tenant_id}).mappings().all()
        for row in rows:
            resource_id = str(row.get("id") or "")
            if not resource_id:
                continue
            binding_status = "active" if str(row.get("status") or "") in {"active", "published"} else "inactive"
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_resource_bindings
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id
                      AND resource_type = :resource_type AND resource_id = :resource_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_resource_bindings (
                        id, tenant_id, agent_id, resource_type, resource_id, status,
                        metadata_json, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, :resource_type, :resource_id, :status,
                        '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_resource_binding_id(tenant_id, default_agent, resource_type, resource_id),
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "status": binding_status,
                },
            )


def _archive_default_agent(conn, tenant_id: str) -> None:
    """归档确定性默认 ID 下元数据为空、无法解析或带默认/管理员标记的非整体智能体，并补齐管理员元数据。"""
    default_agent = _default_agent_id(tenant_id)
    row = conn.execute(
        text(
            """
            SELECT metadata_json FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND is_overall = 0
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not row:
        return
    try:
        metadata = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if metadata and not (
        metadata.get("is_default_employee") is True
        or metadata.get("created_by") == "admin"
        or metadata.get("owner_user_id") == "admin"
    ):
        return
    metadata.update(
        {
            "is_default_employee": True,
            "hidden_from_product": True,
            "archived_by_seed": True,
            "owner_user_id": "admin",
            "owner_username": "admin",
            "owner_display_name": "Administrator",
            "created_by_user_id": "admin",
            "created_by_username": "admin",
            "created_by": "admin",
            "created_by_display_name": "Administrator",
            "creator_name": "admin",
        }
    )
    conn.execute(
        text(
            """
            UPDATE agent_profiles
            SET status = 'archived',
                metadata_json = :metadata_json,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {
            "id": default_agent,
            "tenant_id": tenant_id,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        },
    )


def _seed_agent_branch_state(conn, inspector, tables: set[str]) -> None:
    """依据资源绑定补种技能与知识库分支，并依据租户已启用的默认模型配置补种模型绑定。"""
    if "agent_profiles" not in tables:
        return
    if "agent_skill_branches" in tables and "skills" in tables:
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = 0 AND status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            agent_id = str(agent["id"])
            _seed_default_agent_bindings(conn, tenant_id)
            rows = conn.execute(
                text(
                    """
                    SELECT s.*
                    FROM skills s
                    JOIN agent_resource_bindings b
                      ON b.resource_id = s.id
                     AND b.resource_type = 'skill'
                     AND b.tenant_id = s.tenant_id
                    WHERE s.tenant_id = :tenant_id
                      AND b.agent_id = :agent_id
                      AND s.status != 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id},
            ).mappings().all()
            for row in rows:
                _seed_agent_skill_branch(conn, agent_id, row)

    if "agent_knowledge_branches" in tables and "knowledge_bases" in tables:
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = 0 AND status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            agent_id = str(agent["id"])
            rows = conn.execute(
                text(
                    """
                    SELECT kb.*
                    FROM knowledge_bases kb
                    JOIN agent_resource_bindings b
                      ON b.resource_id = kb.id
                     AND b.resource_type = 'knowledge_base'
                     AND b.tenant_id = kb.tenant_id
                    WHERE kb.tenant_id = :tenant_id
                      AND b.agent_id = :agent_id
                      AND kb.status != 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id},
            ).mappings().all()
            for row in rows:
                _seed_agent_knowledge_branch(conn, agent_id, row)

    if "agent_model_bindings" in tables and "model_configs" in tables:
        default_models = conn.execute(
            text("SELECT tenant_id, id FROM model_configs WHERE is_default = 1 AND enabled = 1")
        ).mappings().all()
        model_by_tenant = {str(row["tenant_id"]): str(row["id"]) for row in default_models}
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            model_id = model_by_tenant.get(tenant_id)
            if not model_id:
                continue
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_model_bindings
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id AND role = 'default'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent["id"]},
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_model_bindings (
                        id, tenant_id, agent_id, role, model_config_id, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, 'default', :model_config_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_model_binding_id(str(agent["id"]), "default"),
                    "tenant_id": tenant_id,
                    "agent_id": agent["id"],
                    "model_config_id": model_id,
                },
            )


def _normalize_agent_branch_rows(conn, tables: set[str]) -> None:
    """规范化智能体资源绑定、技能分支、分支版本和知识库分支表的确定性主键。"""
    if "agent_resource_bindings" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_resource_bindings",
            select_columns=("id", "tenant_id", "agent_id", "resource_type", "resource_id"),
            key_columns=("tenant_id", "agent_id", "resource_type", "resource_id"),
            id_factory=lambda row: _agent_resource_binding_id(
                str(row["tenant_id"]),
                str(row["agent_id"]),
                str(row["resource_type"]),
                str(row["resource_id"]),
            ),
        )
    if "agent_skill_branches" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_skill_branches",
            select_columns=("id", "tenant_id", "agent_id", "skill_id"),
            key_columns=("tenant_id", "agent_id", "skill_id"),
            id_factory=lambda row: _agent_skill_branch_id(str(row["agent_id"]), str(row["skill_id"])),
        )
    if "agent_skill_branch_versions" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_skill_branch_versions",
            select_columns=("id", "tenant_id", "agent_id", "skill_id", "version"),
            key_columns=("tenant_id", "agent_id", "skill_id", "version"),
            id_factory=lambda row: _agent_skill_branch_version_id(
                str(row["agent_id"]),
                str(row["skill_id"]),
                str(row["version"]),
            ),
        )
    if "agent_knowledge_branches" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_knowledge_branches",
            select_columns=("id", "tenant_id", "agent_id", "knowledge_base_id"),
            key_columns=("tenant_id", "agent_id", "knowledge_base_id"),
            id_factory=lambda row: _agent_knowledge_branch_id(str(row["agent_id"]), str(row["knowledge_base_id"])),
        )


def _normalize_canonical_ids(
    conn,
    *,
    table: str,
    select_columns: tuple[str, ...],
    key_columns: tuple[str, ...],
    id_factory: Callable[[dict[str, object]], str],
) -> None:
    """按业务键去重指定表并改写为规范主键；会删除重复行或更新现有行主键。"""
    columns_sql = ", ".join(select_columns)
    rows = conn.execute(text(f"SELECT {columns_sql} FROM {table}")).mappings().all()
    kept_keys: set[tuple[object, ...]] = set()
    for row in rows:
        row_dict = dict(row)
        row_id = str(row_dict["id"])
        key = tuple(row_dict[column] for column in key_columns)
        target_id = id_factory(row_dict)
        if key in kept_keys:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        kept_keys.add(key)
        if row_id == target_id:
            continue
        target_exists = conn.execute(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": target_id}).first()
        if target_exists:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        conn.execute(text(f"UPDATE {table} SET id = :target_id WHERE id = :id"), {"target_id": target_id, "id": row_id})


def _seed_agent_skill_branch(conn, agent_id: str, row) -> None:
    """仅在技能分支不存在时从 skills 行补种分支，并随新分支在版本表存在时补种对应版本。"""
    branch_id = _agent_skill_branch_id(agent_id, str(row["skill_id"]))
    existing = conn.execute(text("SELECT id FROM agent_skill_branches WHERE id = :id"), {"id": branch_id}).first()
    if existing:
        return
    version = row.get("version") or "1.0.0"
    content_json = row.get("content_json") or "{}"
    branch_status = "active" if str(row.get("status") or "") == "published" else "inactive"
    conn.execute(
        text(
            """
            INSERT INTO agent_skill_branches (
                id, tenant_id, agent_id, skill_id, source_skill_id, base_version, head_version,
                content_json, status, sync_state, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :skill_id, :source_skill_id, :base_version, :head_version,
                :content_json, :status, 'synced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "skill_id": row["skill_id"],
            "source_skill_id": row["id"],
            "base_version": version,
            "head_version": version,
            "content_json": content_json,
            "status": branch_status,
        },
    )
    if "agent_skill_branch_versions" not in {
        table for table in inspect(conn).get_table_names()
    }:
        return
    branch_version_id = _agent_skill_branch_version_id(agent_id, str(row["skill_id"]), version)
    existing_version = conn.execute(
        text("SELECT id FROM agent_skill_branch_versions WHERE id = :id"),
        {"id": branch_version_id},
    ).first()
    if existing_version:
        return
    conn.execute(
        text(
            """
            INSERT INTO agent_skill_branch_versions (
                id, tenant_id, agent_id, skill_id, source_skill_id, version, base_version,
                content_json, status, sync_state, change_summary, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :skill_id, :source_skill_id, :version, :base_version,
                :content_json, :status, 'synced', '初始化分支', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_version_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "skill_id": row["skill_id"],
            "source_skill_id": row["id"],
            "version": version,
            "base_version": version,
            "content_json": content_json,
            "status": branch_status,
        },
    )


def _seed_agent_knowledge_branch(conn, agent_id: str, row) -> None:
    """从 knowledge_bases 行补种指定智能体的 agent_knowledge_branches 记录。"""
    branch_id = _agent_knowledge_branch_id(agent_id, str(row["id"]))
    existing = conn.execute(text("SELECT id FROM agent_knowledge_branches WHERE id = :id"), {"id": branch_id}).first()
    if existing:
        return
    branch_status = "active" if str(row.get("status") or "") == "active" else "inactive"
    conn.execute(
        text(
            """
            INSERT INTO agent_knowledge_branches (
                id, tenant_id, agent_id, knowledge_base_id, base_version, head_version,
                status, sync_state, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :knowledge_base_id, '1.0.0', '1.0.0',
                :status, 'synced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "knowledge_base_id": row["id"],
            "status": branch_status,
        },
    )


def _tenant_ids(conn, tables: set[str]) -> list[str]:
    """从 tenants 以及 skills、general_skills、knowledge_documents、sessions 收集去重排序的租户标识。"""
    ids: set[str] = set()
    if "tenants" in tables:
        ids.update(str(row[0]) for row in conn.execute(text("SELECT id FROM tenants")).all() if row[0])
    for table_name in ("skills", "general_skills", "knowledge_documents", "sessions"):
        if table_name not in tables:
            continue
        ids.update(str(row[0]) for row in conn.execute(text(f"SELECT DISTINCT tenant_id FROM {table_name}")).all() if row[0])
    return sorted(ids)


def _default_knowledge_base_id(tenant_id: str) -> str:
    """生成租户默认 knowledge_bases 记录的确定性主键。"""
    return f"kb_{tenant_id}_default"


def _overall_agent_id(tenant_id: str) -> str:
    """生成租户整体 agent_profiles 记录的确定性主键。"""
    return f"agent_{tenant_id}_overall"


def _default_agent_id(tenant_id: str) -> str:
    """生成租户遗留默认 agent_profiles 记录的确定性主键。"""
    return f"agent_{tenant_id}_default"


def _knowledge_base_version_id(knowledge_base_id: str, version: str) -> str:
    """按知识库和规范化版本号生成 knowledge_base_versions 的确定性主键。"""
    return f"kbver_{knowledge_base_id}_{version.replace('.', '_').replace('-', '_')}"


def _agent_skill_branch_id(agent_id: str, skill_id: str) -> str:
    """生成 agent_skill_branches 的确定性主键。"""
    return f"agentbranch_{agent_id}_{skill_id}"


def _agent_skill_branch_version_id(agent_id: str, skill_id: str, version: str) -> str:
    """按智能体、技能和规范化版本号生成技能分支版本主键。"""
    safe_version = version.replace(".", "_").replace("-", "_")
    return f"agentbranchver_{agent_id}_{skill_id}_{safe_version}"


def _agent_knowledge_branch_id(agent_id: str, knowledge_base_id: str) -> str:
    """生成 agent_knowledge_branches 的确定性主键。"""
    return f"agentkb_{agent_id}_{knowledge_base_id}"


def _agent_resource_binding_id(tenant_id: str, agent_id: str, resource_type: str, resource_id: str) -> str:
    """由租户、智能体、资源类型和资源标识的 SHA-1 十六进制摘要前 16 位生成资源绑定主键。"""
    key = f"{tenant_id}:{agent_id}:{resource_type}:{resource_id}"
    return f"agentres_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _agent_model_binding_id(agent_id: str, role: str) -> str:
    """按智能体和角色生成 agent_model_bindings 的确定性主键。"""
    return f"agentmodel_{agent_id}_{role}"
