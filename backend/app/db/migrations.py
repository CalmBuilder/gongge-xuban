"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : migrations.py
@CallChain  : MySQL Adapter → assert_schema_current → Alembic migration metadata
@Description: 读取 Alembic 版本并检查 MySQL 数据库迁移就绪状态。
"""

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError


class SchemaNotCurrentError(RuntimeError):
    pass


class DatabaseConnectionError(RuntimeError):
    pass


def current_revision(engine: Engine) -> str | None:
    """读取数据库当前记录的 Alembic 修订版本。

    参数：
        engine: 待检查的 SQLAlchemy 数据库引擎。
    返回：
        当前修订标识；数据库尚无版本记录时返回 ``None``。
    异常：
        DatabaseConnectionError: 连接或检查数据库时发生 SQLAlchemy 错误。
        Alembic CommandError: Alembic 无法将数据库状态解析为单一修订版本时原样传播。
    """
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except SQLAlchemyError as exc:
        target = engine.url.render_as_string(hide_password=True)
        raise DatabaseConnectionError(
            f"Unable to inspect database schema at {target} ({type(exc).__name__})"
        ) from exc


def head_revision(config_path: Path | None = None) -> str:
    """读取 Alembic 配置所指迁移脚本的唯一头部修订版本。

    参数：
        config_path: Alembic 配置路径；省略时使用后端目录中的配置。
    返回：
        当前头部修订标识。
    异常：
        SchemaNotCurrentError: 迁移脚本不存在头部修订版本。
        Alembic CommandError: 配置或迁移脚本无法加载，或存在多个头部版本时原样传播。
    """
    path = config_path or Path(__file__).resolve().parents[2] / "alembic.ini"
    config = Config(str(path))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise SchemaNotCurrentError("Alembic has no head revision")
    return head


def assert_schema_current(engine: Engine, expected_head: str | None = None) -> None:
    """确认数据库修订版本与预期的 Alembic 头部版本一致。

    参数：
        engine: 待检查的 SQLAlchemy 数据库引擎。
        expected_head: 指定的预期修订标识；省略时读取 Alembic 配置。
    异常：
        DatabaseConnectionError: 连接或检查数据库时发生 SQLAlchemy 错误。
        SchemaNotCurrentError: 当前修订版本与预期头部版本不一致，或迁移脚本无头部版本。
        Alembic CommandError: 数据库状态、配置或迁移脚本无法解析为单一版本时原样传播。
    """
    current = current_revision(engine)
    head = expected_head or head_revision()
    if current != head:
        raise SchemaNotCurrentError(
            "Database schema is not current: "
            f"current={current or '<none>'}, head={head}. "
            "Run: alembic -c alembic.ini upgrade head"
        )
