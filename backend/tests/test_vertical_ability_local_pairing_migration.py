"""
@Time       : 2026/09/05 19:00
@Author     : zhanglp8181
@File       : test_vertical_ability_local_pairing_migration.py
@CallChain  : pytest → 隔离 SQLite/MySQL → Alembic 0081/0082 → 双模式设备配对
@Description: 验证旧 Relay 会话数据不丢失、两种实际传输模式可用且不安全回退被阻止。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, MetaData, Table, create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db.sqlite_legacy import initialize_sqlite_database

BACKEND_DIR = Path(__file__).resolve().parents[1]
SESSION_TABLE = "vertical_ability_browser_relay_sessions"


def _migration(filename: str):
    """从应用迁移目录加载指定版本，不依赖参考仓库。"""

    spec = importlib.util.spec_from_file_location(
        filename.removesuffix(".py"), BACKEND_DIR / "alembic" / "versions" / filename,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: Engine, module, direction: str = "upgrade") -> None:
    """在独立事务中执行指定迁移，不接触应用数据库。"""

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, direction)()


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def pairing_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    """MySQL 使用现有随机库夹具，SQLite 使用内存旧版本表。"""

    database_url = request.getfixturevalue("mysql_database_url") if request.param == "mysql" else "sqlite://"
    engine = create_engine(database_url)
    _run(engine, _migration("20260904_0081_browser_relay.py"))
    try:
        yield engine
    finally:
        engine.dispose()


def _insert(engine: Engine, identifier: str, transport: str | None = None) -> None:
    """插入不含真实凭据的最小旧版配对行，用于验证数据库 CHECK 和缺省值。"""

    table = Table(SESSION_TABLE, MetaData(), autoload_with=engine)
    values = {
        "id": identifier, "tenant_id": "tenant_fixture", "user_id": "user_fixture",
        "platform_id": "xiaohongshu", "token_hash": "fixture-hash",
        "created_at": datetime(2026, 9, 5), "updated_at": datetime(2026, 9, 5),
    }
    if transport is not None:
        values["transport"] = transport
    with engine.begin() as connection:
        connection.execute(table.insert().values(**values))


def test_upgrade_preserves_remote_rows_and_allows_local_pairing(pairing_engine: Engine) -> None:
    """重复升级应保持远程配对不变，并允许本机模式而拒绝未知传输值。"""

    _insert(pairing_engine, "old-remote")
    with pytest.raises((IntegrityError, OperationalError), match="[Cc][Hh][Ee][Cc][Kk] constraint"):
        _insert(pairing_engine, "before-upgrade", "local_loopback")
    module = _migration("20260905_0082_local_browser_pairing.py")
    _run(pairing_engine, module)
    _run(pairing_engine, module)
    _insert(pairing_engine, "new-local", "local_loopback")
    with pytest.raises((IntegrityError, OperationalError), match="[Cc][Hh][Ee][Cc][Kk] constraint"):
        _insert(pairing_engine, "bad-transport", "unknown")
    with pytest.raises(RuntimeError, match="local_loopback"):
        _run(pairing_engine, module, "downgrade")
    table = Table(SESSION_TABLE, MetaData(), autoload_with=pairing_engine)
    with pairing_engine.connect() as connection:
        rows = dict(connection.execute(select(table.c.id, table.c.transport)).all())
    assert rows == {"old-remote": "browser_relay", "new-local": "local_loopback"}


def test_downgrade_without_local_rows_retains_remote_pairing(pairing_engine: Engine) -> None:
    """未建立本机会话时可以安全回退，不能删除仍存在的远程会话。"""

    _insert(pairing_engine, "old-remote")
    module = _migration("20260905_0082_local_browser_pairing.py")
    _run(pairing_engine, module)
    _run(pairing_engine, module, "downgrade")
    _run(pairing_engine, module, "downgrade")
    with pytest.raises((IntegrityError, OperationalError), match="[Cc][Hh][Ee][Cc][Kk] constraint"):
        _insert(pairing_engine, "after-downgrade", "local_loopback")
    table = Table(SESSION_TABLE, MetaData(), autoload_with=pairing_engine)
    with pairing_engine.connect() as connection:
        assert connection.execute(select(table.c.id)).scalars().all() == ["old-remote"]


def test_sqlite_startup_upgrades_existing_pairing_without_manual_alembic(tmp_path: Path) -> None:
    """桌面旧库经真实初始化入口自动兼容本机配对，不要求用户执行迁移命令。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'desktop.db'}")
    try:
        _run(engine, _migration("20260904_0081_browser_relay.py"))
        _insert(engine, "old-remote")
        initialize_sqlite_database(engine)
        initialize_sqlite_database(engine)
        _insert(engine, "desktop-local", "local_loopback")
        table = Table(SESSION_TABLE, MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            assert set(connection.execute(select(table.c.id)).scalars()) == {
                "old-remote", "desktop-local",
            }
    finally:
        engine.dispose()
