"""
@Time       : 2026/08/13 03:45
@Author     : zhanglp8181
@File       : test_skill_operation_causes_migration.py
@CallChain  : pytest → Alembic 0058 → SQLite schema inspection
@Description: 验证 Skill Operation 多因果字段可升级、默认空集合并安全降级。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向隔离 SQLite 数据库的 Alembic 配置。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_skill_operation_multi_cause_migration_round_trips_sqlite(tmp_path: Path) -> None:
    """确认 0058 可重复升级、旧行得到空集合，降级后只移除新增字段。"""

    database_url = f"sqlite:///{tmp_path / 'skill-causes.db'}"
    config = _config(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR(128) PRIMARY KEY, caused_by_skill_use_id VARCHAR(128) NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sop_operations (id, caused_by_skill_use_id) "
                "VALUES ('op_legacy', 'gsuse_legacy')"
            )
        )
    command.stamp(config, "20260813_0057")
    command.upgrade(config, "20260813_0058")
    command.upgrade(config, "20260813_0058")
    with engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("sop_operations")}
        assert "caused_by_skill_use_ids_json" in columns
        assert connection.execute(
            text(
                "SELECT caused_by_skill_use_ids_json FROM sop_operations WHERE id='op_legacy'"
            )
        ).scalar_one() == "[]"
    command.downgrade(config, "20260813_0057")
    assert "caused_by_skill_use_ids_json" not in {
        item["name"] for item in inspect(engine).get_columns("sop_operations")
    }
