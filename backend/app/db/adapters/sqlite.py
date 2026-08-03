"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : sqlite.py
@CallChain  : factory/runtime → SQLiteDatabaseAdapter → PRAGMA/sqlite_legacy
@Description: 配置 SQLite 连接并调用旧版兼容初始化流程。
"""

from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.engine import URL

from app.config import Settings
from app.db.adapters.base import SQLAlchemyDatabaseAdapter


class SQLiteDatabaseAdapter(SQLAlchemyDatabaseAdapter):
    backend_name = "sqlite"

    def engine_options(self, url: URL, settings: Settings) -> dict[str, Any]:
        """构造允许跨线程连接并设置等待超时的 SQLite 引擎选项。"""
        return {
            "echo": False,
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }

    def configure_engine(self, engine: Engine) -> None:
        """为 SQLite 引擎注册新连接的忙等待超时配置。

        参数：
            engine: 需要注册连接事件监听器的 SQLAlchemy 引擎。
        副作用：
            在每个新建 DBAPI 连接上执行 ``PRAGMA busy_timeout=30000``。
        """
        @event.listens_for(engine, "connect")
        def configure_connection(dbapi_connection, connection_record) -> None:
            """为新建 SQLite 连接设置 30 秒的忙等待超时。"""
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    def initialize(self, engine: Engine) -> None:
        """调用旧版兼容流程初始化 SQLite 数据库。"""
        from app.db.sqlite_legacy import initialize_sqlite_database

        initialize_sqlite_database(engine)
