"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : factory.py
@CallChain  : config.Settings → database.py → SQLAlchemyDatabaseAdapterFactory → SQLite/MySQL Adapter
@Description: 根据数据库 URL 选择适配器并创建 SQLAlchemy 运行时。
"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import Engine, make_url
from sqlalchemy.engine import URL

from app.config import Settings
from app.db.adapters.base import SQLAlchemyDatabaseAdapter
from app.db.adapters.mysql import MySQLDatabaseAdapter
from app.db.adapters.sqlite import SQLiteDatabaseAdapter


class UnsupportedDatabaseError(ValueError):
    pass


def normalize_database_url(url: str) -> str:
    """将相对 SQLite 数据库路径解析到应用数据目录或后端目录。

    参数：
        url: 待规范化的 SQLAlchemy 数据库 URL。
    返回：
        规范化后的 URL；非相对文件型 SQLite URL 保持不变。
    """
    if not url.startswith("sqlite:///") or url.startswith("sqlite:////") or url == "sqlite:///:memory:":
        return url
    raw_path = unquote(url.removeprefix("sqlite:///"))
    if not raw_path or raw_path == ":memory:":
        return url
    path = Path(raw_path)
    if path.is_absolute():
        return url

    from app import paths

    base_dir = paths.user_data_dir() if paths.is_frozen() else Path(__file__).resolve().parents[2]
    return f"sqlite:///{(base_dir / path).resolve()}"


@dataclass(frozen=True)
class DatabaseRuntime:
    url: URL
    engine: Engine
    adapter: SQLAlchemyDatabaseAdapter
    settings: Settings

    def initialize(self) -> None:
        """使用运行时适配器初始化对应数据库引擎。"""
        self.adapter.initialize(self.engine)


class SQLAlchemyDatabaseAdapterFactory:
    _adapters: dict[str, type[SQLAlchemyDatabaseAdapter]] = {
        "sqlite": SQLiteDatabaseAdapter,
        "mysql": MySQLDatabaseAdapter,
    }

    @classmethod
    def create(cls, database_url: str, settings: Settings) -> DatabaseRuntime:
        """按数据库后端创建适配器、引擎和运行时对象。

        参数：
            database_url: SQLAlchemy 数据库 URL。
            settings: 构造引擎时使用的应用设置。
        返回：
            包含规范化 URL、引擎、适配器和设置的数据库运行时。
        异常：
            UnsupportedDatabaseError: URL 对应的数据库后端没有已注册适配器。
        """
        url = make_url(normalize_database_url(database_url))
        backend_name = url.get_backend_name()
        adapter_type = cls._adapters.get(backend_name)
        if adapter_type is None:
            raise UnsupportedDatabaseError(
                f"Unsupported SQLAlchemy database backend: {backend_name}"
            )
        adapter = adapter_type()
        engine = adapter.create_engine(url, settings)
        return DatabaseRuntime(url=url, engine=engine, adapter=adapter, settings=settings)
