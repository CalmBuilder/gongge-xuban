"""
@Time       : 2026/10/23
@Author     : zhanglp8181
@File       : test_skill_version_checksum_compatibility.py
@CallChain  : pytest → 0093兼容迁移/SQLite旧快照 → SkillVersion校验
@Description: 验证历史编译校验兼容记录可重放且正文指纹损坏时拒绝迁移。
"""

from importlib.util import module_from_spec, spec_from_file_location
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.versioning import skill_content_checksum


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261023_0093_skill_version_checksum_compatibility.py"
)
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _migration_module():
    """加载待测 Alembic 模块，避免测试进程依赖模块文件名导入规则。"""

    spec = spec_from_file_location("skill_version_checksum_compatibility", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _content() -> dict[str, object]:
    """构造编译器可接受的最小发布正文。"""

    return {
        "skill_id": "checksum-compatibility",
        "name": "校验兼容迁移",
        "version": "1.0.0",
        "nodes": [{"node_id": "done", "type": "response", "name": "完成"}],
        "edges": [],
        "start_node_id": "done",
        "terminal_node_ids": ["done"],
    }


def _create_legacy_table(engine, content: dict[str, object], *, stored_checksum: str) -> None:
    """创建不含新兼容列的历史 skill_versions 表并写入一条发布快照。"""

    metadata = sa.MetaData()
    table = sa.Table(
        "skill_versions",
        metadata,
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("content_checksum", sa.String(64), nullable=True),
        sa.Column("compiled_definition_checksum", sa.String(64), nullable=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert().values(
                id="skill_version_legacy_1",
                content_json=content,
                status="published",
                content_checksum=skill_content_checksum(content),
                compiled_definition_checksum=stored_checksum,
            )
        )


def _upgrade(engine) -> None:
    """在当前 SQLite 连接上执行一次目标迁移。"""

    module = _migration_module()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


def _upgrade_database(url: str, revision: str) -> None:
    """把隔离 MySQL 数据库迁移到指定修订。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = url
    command.upgrade(config, revision)


def test_legacy_checksum_migration_records_current_and_old_values() -> None:
    """历史主校验值保持不变，同时登记当前编译值并支持重复执行。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    content = _content()
    compiled = compile_legacy_skill_card(content)
    _create_legacy_table(engine, content, stored_checksum="legacy-compiler-checksum")

    _upgrade(engine)
    _upgrade(engine)

    with engine.connect() as connection:
        columns = {item["name"] for item in sa.inspect(connection).get_columns("skill_versions")}
        row = connection.execute(
            sa.text(
                "SELECT compiled_definition_checksum, compiled_definition_compatibility_json "
                "FROM skill_versions WHERE id = :id"
            ),
            {"id": "skill_version_legacy_1"},
        ).mappings().one()

    assert "compiled_definition_compatibility_json" in columns
    assert row["compiled_definition_checksum"] == "legacy-compiler-checksum"
    compatibility = json.loads(row["compiled_definition_compatibility_json"])
    assert compatibility["current_checksum"] == compiled.checksum
    assert compatibility["legacy_checksums"] == ["legacy-compiler-checksum"]


def test_legacy_checksum_migration_rejects_changed_content() -> None:
    """正文指纹已经变化时拒绝迁移，不能把篡改内容登记为兼容快照。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    content = _content()
    _create_legacy_table(engine, content, stored_checksum="legacy-compiler-checksum")
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE skill_versions SET content_checksum = :checksum"),
            {"checksum": "tampered-content-checksum"},
        )

    with pytest.raises(RuntimeError, match="SKILL_VERSION_CONTENT_CHECKSUM_INVALID"):
        _upgrade(engine)


@pytest.mark.mysql
def test_legacy_checksum_migration_runs_on_mysql_84(mysql_database_url: str) -> None:
    """在隔离 MySQL 8.4 上从 0092 升级并保留历史发布校验值。"""

    _upgrade_database(mysql_database_url, "20261002_0092")
    content = _content()
    compiled = compile_legacy_skill_card(content)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO skill_versions (
                        id, tenant_id, skill_id, scope_agent_id, version, name,
                        content_json, status, content_checksum, compiled_definition_checksum,
                        created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :skill_id, :scope_agent_id, :version, :name,
                        :content_json, 'published', :content_checksum, :compiled_checksum,
                        :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": "skill_version_mysql_legacy_1",
                    "tenant_id": "tenant_checksum_mysql",
                    "skill_id": content["skill_id"],
                    "scope_agent_id": "",
                    "version": content["version"],
                    "name": content["name"],
                    "content_json": json.dumps(content, ensure_ascii=False),
                    "content_checksum": skill_content_checksum(content),
                    "compiled_checksum": "legacy-compiler-checksum",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        _upgrade_database(mysql_database_url, "head")
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT compiled_definition_checksum, compiled_definition_compatibility_json "
                    "FROM skill_versions WHERE id = :id"
                ),
                {"id": "skill_version_mysql_legacy_1"},
            ).mappings().one()
        compatibility = row["compiled_definition_compatibility_json"]
        if isinstance(compatibility, str):
            compatibility = json.loads(compatibility)
        assert row["compiled_definition_checksum"] == "legacy-compiler-checksum"
        assert compatibility["current_checksum"] == compiled.checksum
        assert compatibility["legacy_checksums"] == ["legacy-compiler-checksum"]
    finally:
        engine.dispose()
