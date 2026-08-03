"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : base.py
@CallChain  : factory.py → SQLAlchemyDatabaseAdapter → SQLAlchemy create_engine
@Description: 定义基于 SQLAlchemy 的数据库适配器抽象契约。
"""

from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from app.config import Settings


class SQLAlchemyDatabaseAdapter(ABC):
    backend_name: str

    @abstractmethod
    def engine_options(self, url: URL, settings: Settings) -> dict[str, Any]:
        """构造当前数据库后端的 SQLAlchemy 引擎选项。"""
        raise NotImplementedError

    def configure_engine(self, engine: Engine) -> None:
        """为已创建的引擎安装当前后端所需的附加配置。"""
        pass

    def create_engine(self, url: URL, settings: Settings) -> Engine:
        """创建并配置当前数据库后端的 SQLAlchemy 引擎。

        参数：
            url: 已解析的 SQLAlchemy 数据库 URL。
            settings: 构造引擎选项时使用的应用设置。
        返回：
            新建的 SQLAlchemy 引擎。
        副作用：
            创建引擎后调用可选的后端配置钩子；具体行为由适配器实现决定。
        """
        engine = create_engine(url, **self.engine_options(url, settings))
        self.configure_engine(engine)
        return engine

    @abstractmethod
    def initialize(self, engine: Engine) -> None:
        """执行当前数据库后端所需的初始化或就绪检查。"""
        raise NotImplementedError
