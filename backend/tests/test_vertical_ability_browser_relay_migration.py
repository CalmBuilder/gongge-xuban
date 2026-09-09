"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_browser_relay_migration.py
@CallChain  : pytest → SQLite 旧表/当前模型、MySQL 正式迁移 → Alembic 0081 双传输表回退
@Description: 验证双传输表幂等升级、回退前全量检查及命名约束兼容，不得丢失本机连接。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Engine, Integer, MetaData, String, Table, Text, create_engine, inspect, select
from sqlmodel import SQLModel

from app.db.models import VerticalAbilityConnection


BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "alembic/versions/20260904_0081_browser_relay.py"


def _load_migration():
    """加载 0081 迁移模块，避免测试依赖安装包中不存在的版本模块。"""

    spec = importlib.util.spec_from_file_location("browser_relay_migration", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_0080_connection_table(engine) -> None:
    """构造没有 transport 字段的 0080 旧连接表，模拟升级前数据库。"""

    metadata = MetaData()
    Table(
        "vertical_ability_connections",
        metadata,
        Column("id", String(512), primary_key=True),
        Column("tenant_id", String(128), nullable=False),
        Column("user_id", String(128), nullable=False),
        Column("platform_id", String(64), nullable=False),
        Column("account_label", String(191), nullable=False),
        Column("account_identity", String(191)),
        Column("auth_method", String(64), nullable=False),
        Column("encrypted_credentials_json", Text(), nullable=False),
        Column("revision", Integer(), nullable=False),
        Column("status", String(64), nullable=False),
        Column("health_status", String(64), nullable=False),
        Column("health_error_code", String(64)),
        Column("last_checked_at", String),
        Column("last_healthy_at", String),
        Column("created_at", String, nullable=False),
        Column("updated_at", String, nullable=False),
    )
    metadata.create_all(engine)


def _run_upgrade(module, engine) -> None:
    """在 Alembic Operations 上下文中执行一次 0081 升级。"""

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module.upgrade()


def test_browser_relay_migration_upgrades_existing_connection_table_idempotently() -> None:
    """已有 0080 连接表应补齐双模式字段，并可重复执行而不重复建表或索引。"""

    engine = create_engine("sqlite:///:memory:")
    _create_0080_connection_table(engine)
    module = _load_migration()

    _run_upgrade(module, engine)
    _run_upgrade(module, engine)

    database = inspect(engine)
    connection_columns = {column["name"] for column in database.get_columns("vertical_ability_connections")}
    connection_indexes = {index["name"] for index in database.get_indexes("vertical_ability_connections")}
    assert "transport" in connection_columns
    assert "ix_vertical_ability_connections_transport" in connection_indexes
    assert "vertical_ability_browser_relay_sessions" in database.get_table_names()
    assert "vertical_ability_browser_relay_commands" in database.get_table_names()


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def downgrade_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    """SQLite 从旧表升级，MySQL 只在随机隔离库执行正式 0081 迁移。"""

    if request.param == "mysql":
        url = request.getfixturevalue("mysql_database_url")
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "20260904_0081")
        engine = create_engine(url)
    else:
        engine = create_engine("sqlite:///:memory:")
        _create_0080_connection_table(engine)
        _run_upgrade(_load_migration(), engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_downgrade_fixture(engine: Engine, kind: str) -> None:
    """为单个阻止条件插入合成记录，不使用真实账号或令牌。"""

    names = {
        "session": "vertical_ability_browser_relay_sessions",
        "command": "vertical_ability_browser_relay_commands",
        "remote": "vertical_ability_connections",
        "local": "vertical_ability_connections",
    }
    table = Table(names[kind], MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 5)
    values = {
        "id": "migration-fixture", "tenant_id": "fixture-tenant", "user_id": "fixture-user",
        "platform_id": "xiaohongshu", "created_at": now, "updated_at": now,
    }
    if kind == "session":
        values["token_hash"] = "fixture-hash"
    elif kind == "command":
        values.update(relay_session_id="fixture-session", action="search", arguments_json="[]", deadline_at=now)
    else:
        values.update(
            account_label="fixture", auth_method="browser_session", encrypted_credentials_json="{}",
            revision=1, status="pending", health_status="unverified",
            transport="browser_relay" if kind == "remote" else "local_loopback",
        )
    with engine.begin() as connection:
        connection.execute(table.insert().values(**values))


def _schema_snapshot(engine: Engine) -> dict[str, object]:
    """记录本批三张表的结构、索引与完整合成数据，防止只检查异常而漏掉已删表。"""

    inspector = inspect(engine)
    snapshot = {}
    for name in (
        "vertical_ability_connections", "vertical_ability_browser_relay_sessions",
        "vertical_ability_browser_relay_commands",
    ):
        if not inspector.has_table(name):
            continue
        table = Table(name, MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = connection.execute(select(table).order_by(table.c.id)).all()
        snapshot[name] = {
            "columns": [column["name"] for column in inspector.get_columns(name)],
            "indexes": inspector.get_indexes(name),
            "checks": inspector.get_check_constraints(name), "rows": rows,
        }
    return snapshot


@pytest.mark.parametrize("blocking_kind", ["session", "command", "remote"])
def test_refused_downgrade_preserves_every_table_index_and_row(
    downgrade_engine: Engine, blocking_kind: str,
) -> None:
    """任一回退条件不满足时必须在第一条 DDL 前拒绝，所有表、索引与行保持不变。"""

    _insert_downgrade_fixture(downgrade_engine, blocking_kind)
    before = _schema_snapshot(downgrade_engine)
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        with downgrade_engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
    assert _schema_snapshot(downgrade_engine) == before


def _assert_safe_downgrade_keeps_local_connection_and_can_repeat(downgrade_engine: Engine) -> None:
    """构造本机连接并连续回退两次，断言 Relay 表及 transport 依赖已移除而记录仍在。"""

    _insert_downgrade_fixture(downgrade_engine, "local")
    migration = _load_migration()
    for _ in range(2):
        with downgrade_engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
    inspector = inspect(downgrade_engine)
    assert not inspector.has_table("vertical_ability_browser_relay_sessions")
    assert not inspector.has_table("vertical_ability_browser_relay_commands")
    assert "transport" not in {
        item["name"] for item in inspector.get_columns("vertical_ability_connections")
    }
    assert "ck_vertical_ability_connection_transport" not in {
        item["name"] for item in inspector.get_check_constraints("vertical_ability_connections")
    }
    table = Table("vertical_ability_connections", MetaData(), autoload_with=downgrade_engine)
    with downgrade_engine.connect() as connection:
        assert connection.execute(select(table.c.id)).scalars().all() == ["migration-fixture"]


def test_safe_downgrade_keeps_local_connection_and_can_repeat(downgrade_engine: Engine) -> None:
    """空 Relay 表且仅有本机连接时回退可重复，移除 transport 索引但不丢原连接。"""

    _assert_safe_downgrade_keeps_local_connection_and_can_repeat(downgrade_engine)


def test_downgrade_from_current_sqlite_model_schema_preserves_local_connection() -> None:
    """桌面当前 SQLModel 建表含 transport CHECK，回退须连同约束移除而不在删表后失败。"""

    engine = create_engine("sqlite:///:memory:")
    try:
        SQLModel.metadata.create_all(engine)
        assert any(
            item["name"] == "ck_vertical_ability_connection_transport"
            for item in inspect(engine).get_check_constraints("vertical_ability_connections")
        )
        _assert_safe_downgrade_keeps_local_connection_and_can_repeat(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("downgrade_engine", [pytest.param("mysql", marks=pytest.mark.mysql)], indirect=True)
def test_mysql_downgrade_also_removes_an_existing_model_transport_check(
    downgrade_engine: Engine,
) -> None:
    """正式迁移后的随机 MySQL 库补现模型命名约束，验证有约束的兼容回退分支。"""

    constraint = next(
        item for item in VerticalAbilityConnection.__table__.constraints
        if item.name == "ck_vertical_ability_connection_transport"
    )
    with downgrade_engine.begin() as connection:
        Operations(MigrationContext.configure(connection)).create_check_constraint(
            constraint.name, "vertical_ability_connections", str(constraint.sqltext),
        )
    _assert_safe_downgrade_keeps_local_connection_and_can_repeat(downgrade_engine)
