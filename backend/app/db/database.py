"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : database.py
@CallChain  : app/main/routes/workers → init_db/get_session → DatabaseRuntime → Adapter
@Description: 创建全局数据库运行时并提供初始化和会话入口。
"""

from collections.abc import Generator

from sqlmodel import Session

from app.config import get_settings
from app.db.factory import SQLAlchemyDatabaseAdapterFactory


settings = get_settings()
database_runtime = SQLAlchemyDatabaseAdapterFactory.create(settings.database_url, settings)
engine = database_runtime.engine


def init_db() -> None:
    """通过当前数据库适配器执行数据库初始化。"""
    database_runtime.initialize()


def get_session() -> Generator[Session, None, None]:
    """生成绑定全局数据库引擎的会话。

    生成：
        供单次依赖调用使用的 SQLModel 会话。
    副作用：
        生成器结束时关闭会话。
    """
    with Session(engine) as session:
        yield session
