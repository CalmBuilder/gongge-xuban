"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : mysql.py
@CallChain  : factory/runtime → MySQLDatabaseAdapter → QueuePool/Alembic
@Description: 配置 MySQL 连接池并执行 Alembic 就绪检查。
"""

from typing import Any

from sqlalchemy import Engine
from sqlalchemy.engine import URL

from app.config import Settings
from app.db.adapters.base import SQLAlchemyDatabaseAdapter


class MySQLDatabaseAdapter(SQLAlchemyDatabaseAdapter):
    backend_name = "mysql"

    def engine_options(self, url: URL, settings: Settings) -> dict[str, Any]:
        """构造启用连接探活及池容量限制的 MySQL 引擎选项。"""
        return {
            "echo": False,
            "pool_pre_ping": True,
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_timeout": settings.database_pool_timeout_seconds,
            "pool_recycle": settings.database_pool_recycle_seconds,
        }

    def initialize(self, engine: Engine) -> None:
        """检查 MySQL 数据库是否已经迁移到当前 Alembic 版本。"""
        from app.db.migrations import assert_schema_current

        assert_schema_current(engine)
