"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_skill_version_scope_migrations.py
@CallChain  : 原发布ID → 真实旧表/Alembic/桌面重启 → 范围唯一性及冻结历史
@Description: 双方言验证旧发布不改身份、同版本不同范围共存和危险降级拒绝。
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from app.db.sqlite_legacy import _sqlite_operations, initialize_sqlite_database
from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.versioning import write_skill_version
from tests.test_sop_versioning import _skill, _skill_card


def _old_snapshot(conn) -> None:
    """只插入隔离合成旧发布，不用新ORM列构造尚未升级的MySQL旧表。"""

    content = _skill_card().model_dump(mode="json")
    conn.execute(text(
        "INSERT INTO skill_versions (id,tenant_id,skill_id,version,name,description,"
        "content_json,status,created_at,updated_at) VALUES "
        "('old-publication','tenant_demo','purchase_request','1.0.0','采购申请','',"
        ":body,'published',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
    ), {"body": json.dumps(content, ensure_ascii=False)})


@pytest.mark.parametrize("dialect", ["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def test_scope_alembic_preserves_identity_and_enforces_exact_uniqueness(dialect, request, tmp_path):
    """真实升级保留旧ID，三范围同版本共存；同范围重复及丢范围降级拒绝。"""

    backend = Path(__file__).resolve().parents[1]
    url = (request.getfixturevalue("mysql_database_url") if dialect == "mysql"
           else "sqlite:///" + str(tmp_path / "scope.sqlite"))
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.attributes["database_url"] = url
    engine = create_engine(url)
    try:
        if dialect == "mysql":
            command.upgrade(config, "20261002_0090")
        else:
            # 原完整SQLite迁移链含历史MySQL DDL；只构造实际旧表后执行本批迁移。
            SQLModel.metadata.create_all(engine)
            with engine.begin() as conn:
                with _sqlite_operations(conn).batch_alter_table("skill_versions") as batch:
                    batch.drop_index("ix_skill_versions_scope_agent_id")
                    batch.drop_constraint("uq_skill_version", type_="unique")
                    batch.drop_column("scope_agent_id")
                    batch.create_unique_constraint("uq_skill_version",
                                                   ["tenant_id", "skill_id", "version"])
            command.stamp(config, "20261002_0090")
        with engine.begin() as conn:
            _old_snapshot(conn)
            before = dict(conn.execute(text(
                "SELECT * FROM skill_versions WHERE id='old-publication'"
            )).mappings().one())
        command.upgrade(config, "20261002_0091")
        command.upgrade(config, "20261002_0091")
        with engine.connect() as conn:
            after = dict(conn.execute(text(
                "SELECT * FROM skill_versions WHERE id='old-publication'"
            )).mappings().one())
        assert after.pop("scope_agent_id") == ""
        assert after == before
        # 当前 ORM 已包含 0093 新增的兼容列；先完成 0091 的身份断言，
        # 再把隔离数据库推进到当前头，避免用新模型读取尚未升级的旧表。
        if dialect == "mysql":
            command.upgrade(config, "head")
        with Session(engine) as db:
            ids = []
            for scope in ["alice", "bob"]:
                skill = _skill(_skill_card(name=scope))
                ids.append(write_skill_version(db, skill, scope_agent_id=scope,
                    compiled_definition=compile_legacy_skill_card(skill.content_json)).version.id)
            db.commit()
            assert len(set(ids + ["old-publication"])) == 3
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(text("INSERT INTO skill_versions (id,tenant_id,skill_id,scope_agent_id,"
                "version,name,content_json,status,created_at,updated_at) VALUES "
                "('duplicate','tenant_demo','purchase_request','alice','1.0.0','重复','{}',"
                "'draft',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        if dialect == "mysql":
            # 当前头先由 0093 保护历史编译别名；先验证该门禁，再清理隔离
            # 数据中的兼容元数据，继续验证 0091 原有的范围降级门禁。
            with pytest.raises(RuntimeError, match="SKILL_VERSION_COMPATIBILITY_PRESENT"):
                command.downgrade(config, "20261002_0090")
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE skill_versions "
                    "SET compiled_definition_compatibility_json = NULL"
                ))
        with pytest.raises(RuntimeError, match="SCOPED_SKILL_VERSIONS_PRESENT"):
            command.downgrade(config, "20261002_0090")
        assert not next(c for c in inspect(engine).get_columns("skill_versions")
                        if c["name"] == "scope_agent_id")["nullable"]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM skill_versions")).scalar_one() == 3
    finally:
        engine.dispose()


@pytest.mark.parametrize("old_unique", ["named", "anonymous", "absent"])
def test_sqlite_desktop_scope_upgrade_restarts_without_rewriting_old_id(tmp_path, old_unique):
    """三种实际旧桌面约束形态均可重复初始化，旧发布内容和身份保留。"""

    engine = create_engine("sqlite:///" + str(tmp_path / "desktop.sqlite"))
    constraint = {"named": ", CONSTRAINT uq_skill_version UNIQUE(tenant_id,skill_id,version)",
                  "anonymous": ", UNIQUE(tenant_id,skill_id,version)", "absent": ""}[old_unique]
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE skill_versions (id VARCHAR PRIMARY KEY,"
                "tenant_id VARCHAR NOT NULL,skill_id VARCHAR NOT NULL,version VARCHAR NOT NULL,"
                "name VARCHAR NOT NULL,description VARCHAR,business_domain VARCHAR,content_json JSON,"
                "status VARCHAR,created_at DATETIME,updated_at DATETIME" + constraint + ")"))
            _old_snapshot(conn)
            before = dict(conn.execute(text("SELECT * FROM skill_versions")).mappings().one())
        initialize_sqlite_database(engine)
        initialize_sqlite_database(engine)
        with engine.connect() as conn:
            after = dict(conn.execute(text("SELECT * FROM skill_versions")).mappings().one())
        assert after.pop("scope_agent_id") == ""
        assert after == before
        unique = inspect(engine).get_unique_constraints("skill_versions")
        assert any(item["column_names"] == ["tenant_id", "skill_id", "scope_agent_id", "version"]
                   for item in unique)
    finally:
        engine.dispose()
