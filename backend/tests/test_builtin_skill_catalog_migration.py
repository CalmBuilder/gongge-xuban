"""
@Time       : 2026/08/29 15:40
@Author     : zhanglp8181
@File       : test_builtin_skill_catalog_migration.py
@CallChain  : pytest → Alembic 0073 → GeneralSkillCatalogCommand 表 → SQLite 回滚
@Description: 验证内置 Skill 快照命令回执迁移在 SQLite 上可重入、可约束并安全降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy import create_engine

from app.db.sqlite_legacy import migrate_sqlite_skill_schema


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向临时 SQLite 数据库的 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_builtin_catalog_command_migration_roundtrip(tmp_path: Path) -> None:
    """验证 0073 建表、唯一命令约束、重复升级和无回执安全降级。"""

    database_url = f"sqlite:///{tmp_path / 'builtin-catalog-command.db'}"
    config = _config(database_url)
    command.stamp(config, "20260828_0072")
    command.upgrade(config, "20260829_0073")
    command.upgrade(config, "20260829_0073")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "general_skill_catalog_commands" in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("general_skill_catalog_commands")}
    assert {
        "tenant_id",
        "command_type",
        "command_id",
        "request_checksum",
        "source_revision",
        "result_json",
    } <= columns
    unique_constraints = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("general_skill_catalog_commands")
    }
    assert ("tenant_id", "command_type", "command_id") in unique_constraints

    with engine.begin() as connection:
        values = {
            "id": "catalog_command_a",
            "tenant_id": "tenant_a",
            "command_type": "builtin_skill_import",
            "command_id": "batch_a",
            "request_checksum": "a" * 64,
            "source_revision": "b" * 40,
            "status": "committed",
            "result_json": "{}",
            "created_at": "2026-08-29 00:00:00",
            "updated_at": "2026-08-29 00:00:00",
        }
        connection.execute(
            text(
                "INSERT INTO general_skill_catalog_commands "
                "(id, tenant_id, command_type, command_id, request_checksum, source_revision, "
                "status, result_json, created_at, updated_at) VALUES "
                "(:id, :tenant_id, :command_type, :command_id, :request_checksum, "
                ":source_revision, :status, :result_json, :created_at, :updated_at)"
            ),
            values,
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO general_skill_catalog_commands "
                    "(id, tenant_id, command_type, command_id, request_checksum, source_revision, "
                    "status, result_json, created_at, updated_at) VALUES "
                    "(:id, :tenant_id, :command_type, :command_id, :request_checksum, "
                    ":source_revision, :status, :result_json, :created_at, :updated_at)"
                ),
                {**values, "id": "catalog_command_b"},
            )

    with pytest.raises(RuntimeError, match="command receipts"):
        command.downgrade(config, "20260828_0072")

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM general_skill_catalog_commands"))
    command.downgrade(config, "20260828_0072")
    assert "general_skill_catalog_commands" not in inspect(engine).get_table_names()


def test_platform_catalog_migration_merges_consistent_tenant_snapshots(tmp_path: Path) -> None:
    """验证 0075 合并一致的租户内置副本、重指向绑定并保留历史命令租户范围。"""

    database_url = f"sqlite:///{tmp_path / 'platform-catalog.db'}"
    config = _config(database_url)
    command.stamp(config, "20260829_0074")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE general_skills ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "slug VARCHAR(191) NOT NULL, name VARCHAR(191) NOT NULL, description TEXT, "
                "homepage TEXT, skill_markdown TEXT NOT NULL, skill_files_json TEXT, "
                "metadata_json TEXT, status VARCHAR(64) NOT NULL, permissions_json TEXT, "
                "runtime_config_json TEXT, usage_mode VARCHAR(64) NOT NULL, "
                "owner_user_id VARCHAR(128), visibility_scope VARCHAR(64) NOT NULL, "
                "current_published_revision_id VARCHAR(128), row_version INTEGER NOT NULL, "
                "planning_guidance_json TEXT, planning_guidance_checksum VARCHAR(64), "
                "planning_guidance_published_at DATETIME, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_general_skill_tenant_slug UNIQUE (tenant_id, slug), "
                "CONSTRAINT ck_general_skill_visibility_scope CHECK "
                "(visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery'))"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE general_skill_revisions ("
                "id VARCHAR(128) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "skill_id VARCHAR(128) NOT NULL, revision_number INTEGER NOT NULL, "
                "content_checksum VARCHAR(64) NOT NULL, manifest_checksum VARCHAR(64) NOT NULL, "
                "normalized_skill_markdown TEXT NOT NULL, parsed_metadata_json TEXT NOT NULL, "
                "resource_manifest_json TEXT NOT NULL, requested_capabilities_json TEXT NOT NULL, "
                "source_snapshot_json TEXT NOT NULL, status VARCHAR(64) NOT NULL, "
                "created_by VARCHAR(128) NOT NULL, row_version INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL, published_at DATETIME, revoked_at DATETIME, "
                "CONSTRAINT uq_general_skill_revision_number UNIQUE "
                "(tenant_id, skill_id, revision_number), "
                "CONSTRAINT uq_general_skill_revision_checksum UNIQUE "
                "(tenant_id, skill_id, content_checksum)"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE general_skill_catalog_commands ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "command_type VARCHAR(64) NOT NULL, command_id VARCHAR(128) NOT NULL, "
                "request_checksum VARCHAR(64) NOT NULL, source_revision VARCHAR(128) NOT NULL, "
                "status VARCHAR(64) NOT NULL, result_json TEXT NOT NULL, error_code VARCHAR(128), "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_general_skill_catalog_command UNIQUE "
                "(tenant_id, command_type, command_id)"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_resource_bindings ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "agent_id VARCHAR(128) NOT NULL, resource_type VARCHAR(64) NOT NULL, "
                "resource_id VARCHAR(128) NOT NULL, status VARCHAR(64) NOT NULL, "
                "metadata_json TEXT, row_version INTEGER NOT NULL, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL"
                ")"
            )
        )
        skill_values = {
            "skill_a": {
                "id": "legacy_skill_a",
                "tenant_id": "tenant_a",
                "slug": "review-skill",
                "status": "published",
                "current_revision": "legacy_revision_a",
            },
            "skill_b": {
                "id": "legacy_skill_b",
                "tenant_id": "tenant_b",
                "slug": "review-skill",
                "status": "draft",
                "current_revision": None,
            },
        }
        for item in skill_values.values():
            connection.execute(
                text(
                    "INSERT INTO general_skills "
                    "(id, tenant_id, slug, name, description, homepage, skill_markdown, "
                    "skill_files_json, metadata_json, status, permissions_json, runtime_config_json, "
                    "usage_mode, owner_user_id, visibility_scope, current_published_revision_id, "
                    "row_version, planning_guidance_json, planning_guidance_checksum, "
                    "planning_guidance_published_at, created_at, updated_at) VALUES "
                    "(:id, :tenant_id, :slug, 'Review Skill', 'same content', NULL, '# Review', "
                    "'[]', :metadata, :status, '{}', '{}', 'planning_guidance', NULL, "
                    "'tenant_gallery', :current_revision, 1, '{}', NULL, NULL, "
                    "'2026-08-30 00:00:00', '2026-08-30 00:00:00')"
                ),
                {
                    **item,
                    "metadata": (
                        '{"managed_catalog": true, "catalog_key": '
                        '"platform_builtin:test:skills/review/SKILL.md", '
                        '"content_checksum": "' + "c" * 64 + '", '
                        '"source_normalized_checksum": "' + "n" * 64 + '"}'
                    ),
                },
            )
        for revision_id, skill_id, status in (
            ("legacy_revision_a", "legacy_skill_a", "published"),
            ("legacy_revision_b", "legacy_skill_b", "draft"),
        ):
            connection.execute(
                text(
                    "INSERT INTO general_skill_revisions "
                    "(id, tenant_id, skill_id, revision_number, content_checksum, manifest_checksum, "
                    "normalized_skill_markdown, parsed_metadata_json, resource_manifest_json, "
                    "requested_capabilities_json, source_snapshot_json, status, created_by, "
                    "row_version, created_at, published_at, revoked_at) VALUES "
                    "(:id, :tenant_id, :skill_id, 1, :content_checksum, :manifest_checksum, "
                    "'# Review', '{}', '[]', '{}', :source_snapshot, :status, 'migration-test', "
                    "1, '2026-08-30 00:00:00', :published_at, NULL)"
                ),
                {
                    "id": revision_id,
                    "tenant_id": "tenant_a" if skill_id.endswith("a") else "tenant_b",
                    "skill_id": skill_id,
                    "content_checksum": "c" * 64,
                    "manifest_checksum": "m" * 64,
                    "source_snapshot": '{"source_kind": "platform_builtin"}',
                    "status": status,
                    "published_at": "2026-08-30 00:00:00" if status == "published" else None,
                },
            )
        connection.execute(
            text(
                "INSERT INTO agent_resource_bindings "
                "(id, tenant_id, agent_id, resource_type, resource_id, status, metadata_json, "
                "row_version, created_at, updated_at) VALUES "
                "('legacy_binding_b', 'tenant_b', 'agent_b', 'general_skill', "
                "'legacy_skill_b', 'active', '{}', 1, '2026-08-30 00:00:00', "
                "'2026-08-30 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO general_skill_catalog_commands "
                "(id, tenant_id, command_type, command_id, request_checksum, source_revision, "
                "status, result_json, error_code, created_at, updated_at) VALUES "
                "('legacy_command_a', 'tenant_a', 'builtin_skill_import', 'legacy-import', "
                ":request_checksum, :source_revision, 'committed', :result_json, NULL, "
                "'2026-08-30 00:00:00', '2026-08-30 00:00:00')"
            ),
            {
                "request_checksum": "r" * 64,
                "source_revision": "s" * 40,
                "result_json": '{"items": [{"skill_id": "legacy_skill_b"}]}',
            },
        )

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        skill = connection.execute(
            text(
                "SELECT id, tenant_id, catalog_scope, catalog_key, visibility_scope, "
                "current_published_revision_id FROM general_skills "
                "WHERE catalog_key = 'platform_builtin:test:skills/review/SKILL.md'"
            )
        ).mappings().one()
        assert skill["id"] == "legacy_skill_a"
        assert skill["tenant_id"] is None
        assert skill["catalog_scope"] == "platform"
        assert skill["visibility_scope"] == "platform_gallery"
        assert skill["current_published_revision_id"] == "legacy_revision_a"
        assert connection.execute(
            text("SELECT COUNT(*) FROM general_skills WHERE catalog_scope = 'platform'")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM general_skill_revisions WHERE catalog_scope = 'platform'")
        ).scalar_one() == 1
        binding = connection.execute(
            text("SELECT resource_id FROM agent_resource_bindings WHERE id = 'legacy_binding_b'")
        ).scalar_one()
        assert binding == "legacy_skill_a"
        command_row = connection.execute(
            text(
                "SELECT tenant_id, catalog_scope, scope_key FROM general_skill_catalog_commands "
                "WHERE id = 'legacy_command_a'"
            )
        ).mappings().one()
        assert command_row == {
            "tenant_id": "tenant_a",
            "catalog_scope": "tenant",
            "scope_key": "tenant_a",
        }


def test_sqlite_legacy_path_promotes_platform_catalog_without_tenant_copy(tmp_path: Path) -> None:
    """验证桌面 SQLite 旧库补列、合并副本、重指向绑定并可重复执行。"""

    database_url = f"sqlite:///{tmp_path / 'sqlite-legacy-platform-catalog.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE general_skills ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "slug VARCHAR(191) NOT NULL, name VARCHAR(191) NOT NULL, description TEXT, "
                "homepage TEXT, skill_markdown TEXT NOT NULL, skill_files_json TEXT, "
                "metadata_json TEXT, status VARCHAR(64) NOT NULL, permissions_json TEXT, "
                "runtime_config_json TEXT, usage_mode VARCHAR(64) NOT NULL, "
                "owner_user_id VARCHAR(128), visibility_scope VARCHAR(64) NOT NULL, "
                "current_published_revision_id VARCHAR(128), row_version INTEGER NOT NULL, "
                "planning_guidance_json TEXT, planning_guidance_checksum VARCHAR(64), "
                "planning_guidance_published_at DATETIME, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_general_skill_tenant_slug UNIQUE (tenant_id, slug), "
                "CONSTRAINT ck_general_skill_visibility_scope CHECK "
                "(visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery'))"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE general_skill_revisions ("
                "id VARCHAR(128) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "skill_id VARCHAR(128) NOT NULL, revision_number INTEGER NOT NULL, "
                "content_checksum VARCHAR(64) NOT NULL, manifest_checksum VARCHAR(64) NOT NULL, "
                "normalized_skill_markdown TEXT NOT NULL, parsed_metadata_json TEXT NOT NULL, "
                "resource_manifest_json TEXT NOT NULL, requested_capabilities_json TEXT NOT NULL, "
                "source_snapshot_json TEXT NOT NULL, status VARCHAR(64) NOT NULL, "
                "created_by VARCHAR(128) NOT NULL, row_version INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL, published_at DATETIME, revoked_at DATETIME, "
                "CONSTRAINT uq_general_skill_revision_number UNIQUE "
                "(tenant_id, skill_id, revision_number), "
                "CONSTRAINT uq_general_skill_revision_checksum UNIQUE "
                "(tenant_id, skill_id, content_checksum)"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE general_skill_catalog_commands ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "command_type VARCHAR(64) NOT NULL, command_id VARCHAR(128) NOT NULL, "
                "request_checksum VARCHAR(64) NOT NULL, source_revision VARCHAR(128) NOT NULL, "
                "status VARCHAR(64) NOT NULL, result_json TEXT NOT NULL, error_code VARCHAR(128), "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_general_skill_catalog_command UNIQUE "
                "(tenant_id, command_type, command_id)"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_resource_bindings ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "agent_id VARCHAR(128) NOT NULL, resource_type VARCHAR(64) NOT NULL, "
                "resource_id VARCHAR(128) NOT NULL, status VARCHAR(64) NOT NULL, "
                "metadata_json TEXT, row_version INTEGER NOT NULL, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL"
                ")"
            )
        )
        for skill_id, tenant_id, status, revision_id in (
            ("legacy_skill_a", "tenant_a", "published", "legacy_revision_a"),
            ("legacy_skill_b", "tenant_b", "draft", "legacy_revision_b"),
        ):
            connection.execute(
                text(
                    "INSERT INTO general_skills "
                    "(id, tenant_id, slug, name, description, homepage, skill_markdown, "
                    "skill_files_json, metadata_json, status, permissions_json, runtime_config_json, "
                    "usage_mode, owner_user_id, visibility_scope, current_published_revision_id, "
                    "row_version, planning_guidance_json, planning_guidance_checksum, "
                    "planning_guidance_published_at, created_at, updated_at) VALUES "
                    "(:id, :tenant_id, 'review-skill', 'Review Skill', 'same content', NULL, '# Review', "
                    "'[]', :metadata, :status, '{}', '{}', 'planning_guidance', NULL, "
                    "'tenant_gallery', :revision_id, 1, '{}', NULL, NULL, "
                    "'2026-08-30 00:00:00', '2026-08-30 00:00:00')"
                ),
                {
                    "id": skill_id,
                    "tenant_id": tenant_id,
                    "status": status,
                    "revision_id": revision_id if status == "published" else None,
                    "metadata": (
                        '{"managed_catalog": true, "catalog_key": '
                        '"platform_builtin:test:skills/review/SKILL.md", '
                        '"content_checksum": "' + "c" * 64 + '", '
                        '"source_normalized_checksum": "' + "n" * 64 + '"}'
                    ),
                },
            )
        for revision_id, skill_id, tenant_id, status, published_at in (
            (
                "legacy_revision_a",
                "legacy_skill_a",
                "tenant_a",
                "published",
                "2026-08-30 00:00:00",
            ),
            ("legacy_revision_b", "legacy_skill_b", "tenant_b", "draft", None),
        ):
            connection.execute(
                text(
                    "INSERT INTO general_skill_revisions "
                    "(id, tenant_id, skill_id, revision_number, content_checksum, manifest_checksum, "
                    "normalized_skill_markdown, parsed_metadata_json, resource_manifest_json, "
                    "requested_capabilities_json, source_snapshot_json, status, created_by, "
                    "row_version, created_at, published_at, revoked_at) VALUES "
                    "(:id, :tenant_id, :skill_id, 1, :content_checksum, :manifest_checksum, "
                    "'# Review', '{}', '[]', '{}', '{\"managed_catalog\": true}', :status, 'migration-test', "
                    "1, '2026-08-30 00:00:00', :published_at, NULL)"
                ),
                {
                    "id": revision_id,
                    "tenant_id": tenant_id,
                    "skill_id": skill_id,
                    "content_checksum": "c" * 64,
                    "manifest_checksum": "m" * 64,
                    "status": status,
                    "published_at": published_at,
                },
            )
        connection.execute(
            text(
                "INSERT INTO agent_resource_bindings "
                "(id, tenant_id, agent_id, resource_type, resource_id, status, metadata_json, "
                "row_version, created_at, updated_at) VALUES "
                "('legacy_binding_b', 'tenant_b', 'agent_b', 'general_skill', 'legacy_skill_b', "
                "'active', '{}', 1, '2026-08-30 00:00:00', '2026-08-30 00:00:00')"
            )
        )

    migrate_sqlite_skill_schema(engine)
    migrate_sqlite_skill_schema(engine)
    with engine.connect() as connection:
        skill = connection.execute(
            text(
                "SELECT id, tenant_id, catalog_scope, catalog_key, visibility_scope "
                "FROM general_skills WHERE catalog_key = "
                "'platform_builtin:test:skills/review/SKILL.md'"
            )
        ).mappings().one()
        assert skill["id"] == "legacy_skill_a"
        assert skill["tenant_id"] is None
        assert skill["catalog_scope"] == "platform"
        assert skill["visibility_scope"] == "platform_gallery"
        assert connection.execute(
            text("SELECT COUNT(*) FROM general_skills WHERE catalog_scope = 'platform'")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM general_skill_revisions WHERE catalog_scope = 'platform'")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT resource_id FROM agent_resource_bindings WHERE id = 'legacy_binding_b'")
        ).scalar_one() == "legacy_skill_a"
