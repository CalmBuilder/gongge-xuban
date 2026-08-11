"""
@Time       : 2026/08/13 03:37
@Author     : zhanglp8181
@File       : test_local_operation_kinds_migration.py
@CallChain  : pytest → Alembic 0059 → SQLite Operation effect_kind constraint
@Description: 验证本地写/执行类别升级、降级保护和旧类别兼容。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向隔离 SQLite 数据库的 Alembic 配置。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_local_operation_kind_migration_expands_and_guards_downgrade(tmp_path: Path) -> None:
    """确认 0059 接受新类别，存在真实新数据时拒绝恢复旧约束。"""

    database_url = f"sqlite:///{tmp_path / 'local-operation.db'}"
    config = _config(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR(128) PRIMARY KEY, "
                "effect_kind VARCHAR(64) NOT NULL, "
                "CONSTRAINT ck_sop_operation_effect_kind CHECK ("
                "effect_kind IN ('read', 'external_write', 'legacy_unknown')))"
            )
        )
        connection.execute(
            text("INSERT INTO sop_operations VALUES ('op_read', 'read')")
        )
    command.stamp(config, "20260813_0058")
    command.upgrade(config, "20260813_0059")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO sop_operations VALUES ('op_local', 'local_write')")
        )
        connection.execute(
            text("INSERT INTO sop_operations VALUES ('op_execute', 'execute')")
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text("INSERT INTO sop_operations VALUES ('op_bad', 'arbitrary_shell')")
            )
    with pytest.raises(RuntimeError, match="local workspace operations"):
        command.downgrade(config, "20260813_0058")
