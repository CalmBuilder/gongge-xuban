import pytest
from sqlalchemy.pool import QueuePool

from app.config import Settings
from app.db.adapters.mysql import MySQLDatabaseAdapter
from app.db.adapters.sqlite import SQLiteDatabaseAdapter
from app.db.factory import SQLAlchemyDatabaseAdapterFactory, UnsupportedDatabaseError


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, public_mock_api_key="test-key", **overrides)


def test_factory_selects_mysql_adapter_and_sqlalchemy_queue_pool() -> None:
    runtime = SQLAlchemyDatabaseAdapterFactory.create(
        "mysql+pymysql://app:secret@127.0.0.1:3306/example",
        make_settings(
            database_pool_size=7,
            database_max_overflow=3,
            database_pool_timeout_seconds=11,
            database_pool_recycle_seconds=99,
        ),
    )

    assert isinstance(runtime.adapter, MySQLDatabaseAdapter)
    assert isinstance(runtime.engine.pool, QueuePool)
    assert runtime.url.drivername == "mysql+pymysql"
    assert runtime.adapter.engine_options(runtime.url, runtime.settings) == {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": 7,
        "max_overflow": 3,
        "pool_timeout": 11,
        "pool_recycle": 99,
    }


def test_factory_selects_sqlite_adapter_and_connect_args() -> None:
    runtime = SQLAlchemyDatabaseAdapterFactory.create("sqlite:///:memory:", make_settings())

    assert isinstance(runtime.adapter, SQLiteDatabaseAdapter)
    assert runtime.adapter.engine_options(runtime.url, runtime.settings) == {
        "echo": False,
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }


def test_factory_rejects_unregistered_sqlalchemy_backend() -> None:
    with pytest.raises(UnsupportedDatabaseError, match="postgresql"):
        SQLAlchemyDatabaseAdapterFactory.create(
            "postgresql+psycopg://app:secret@db/example",
            make_settings(),
        )
