"""
@Time       : 2026/08/12 22:48
@Author     : zhanglp8181
@File       : test_dynamic_skill_loading_migration.py
@CallChain  : pytest → Alembic 0063/0064 → SQLite ExecutionCommand constraint
@Description: 验证运行中 Skill 命令约束可升级、可写入并在存在事实时拒绝降级。
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向隔离 SQLite 文件库的 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_dynamic_skill_loading_command_constraint_roundtrip(tmp_path: Path) -> None:
    """确认 0064 接受 add_skill，且只有无新命令事实时才能回退旧约束。"""

    database_url = f"sqlite:///{tmp_path / 'dynamic-skill-loading.db'}"
    config = _config(database_url)
    command.stamp(config, "20260814_0063")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE execution_commands ("
                "id VARCHAR(128) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "execution_id VARCHAR(512) NOT NULL, command_id VARCHAR(128) NOT NULL, "
                "command_type VARCHAR(128) NOT NULL, actor_user_id VARCHAR(512), "
                "source_type VARCHAR(128) NOT NULL, source_message_id VARCHAR(512), "
                "expected_execution_revision INTEGER NOT NULL, payload_json JSON NOT NULL, "
                "payload_checksum VARCHAR(128) NOT NULL, status VARCHAR(128) NOT NULL, "
                "result_plan_revision_id VARCHAR(512), result_json JSON NOT NULL, "
                "reason_code VARCHAR(128), claimed_by VARCHAR(128), "
                "claimed_fencing_token INTEGER, issued_at DATETIME NOT NULL, "
                "claimed_at DATETIME, consumed_at DATETIME, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                "CONSTRAINT ck_execution_command_type "
                "CHECK (command_type IN ('cancel', 'steer')))"
            )
        )
    command.upgrade(config, "20260814_0064")
    checks = {
        str(item.get("name")): str(item.get("sqltext"))
        for item in sa.inspect(engine).get_check_constraints("execution_commands")
    }
    assert "add_skill" in checks["ck_execution_command_type"]

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO execution_commands "
                "(id, tenant_id, execution_id, command_id, command_type, actor_user_id, "
                "source_type, expected_execution_revision, payload_json, payload_checksum, "
                "status, result_json, issued_at, created_at, updated_at) VALUES "
                "('cmd', 'tenant', 'execution', 'key', 'add_skill', 'user', 'api', 0, "
                "'{}', :checksum, 'pending', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="add_skill execution commands"):
        command.downgrade(config, "20260814_0063")
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM execution_commands"))
    command.downgrade(config, "20260814_0063")
    checks = {
        str(item.get("name")): str(item.get("sqltext"))
        for item in sa.inspect(engine).get_check_constraints("execution_commands")
    }
    assert "add_skill" not in checks["ck_execution_command_type"]
    engine.dispose()
