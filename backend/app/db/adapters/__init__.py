"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : factory.py → adapters package → concrete adapters
@Description: 统一导出数据库适配器接口及具体实现。
"""

from app.db.adapters.base import SQLAlchemyDatabaseAdapter
from app.db.adapters.mysql import MySQLDatabaseAdapter
from app.db.adapters.sqlite import SQLiteDatabaseAdapter

__all__ = [
    "MySQLDatabaseAdapter",
    "SQLAlchemyDatabaseAdapter",
    "SQLiteDatabaseAdapter",
]
