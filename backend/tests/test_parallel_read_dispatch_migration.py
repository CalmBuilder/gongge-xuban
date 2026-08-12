"""
@Time       : 2026/08/12 23:58
@Author     : zhanglp8181
@File       : test_parallel_read_dispatch_migration.py
@CallChain  : pytest → Alembic 0064/0065 → SQLite parallel dispatch schema
@Description: 验证并行读派发表、逐 dispatch attempt 字段以及安全降级门禁。
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


def test_parallel_read_dispatch_migration_roundtrip(tmp_path: Path) -> None:
    """确认 0065 建立 inbox 与多 attempt 约束，并在存在派发事实时拒绝降级。"""

    database_url = f"sqlite:///{tmp_path / 'parallel-dispatch.db'}"
    config = _config(database_url)
    command.stamp(config, "20260814_0064")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE sop_operations (id VARCHAR(128) PRIMARY KEY, "
                "status VARCHAR(32) NOT NULL, CONSTRAINT ck_sop_operation_status CHECK "
                "(status IN ('prepared','running','succeeded','failed','unknown','cancelled')))"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE sop_operation_attempts (id VARCHAR(128) PRIMARY KEY, "
                "tenant_id VARCHAR(128) NOT NULL, operation_id VARCHAR(128) NOT NULL, "
                "node_execution_id VARCHAR(128) NOT NULL, status VARCHAR(32) NOT NULL, "
                "CONSTRAINT uq_sop_operation_attempt_execution UNIQUE "
                "(tenant_id, operation_id, node_execution_id), "
                "CONSTRAINT ck_sop_operation_attempt_status CHECK "
                "(status IN ('prepared','running','succeeded','failed','unknown','cancelled','reused')))"
            )
        )
    command.upgrade(config, "20260814_0065")
    inspector = sa.inspect(engine)
    assert {
        "dynamic_read_dispatch_batches",
        "dynamic_read_dispatch_items",
        "dynamic_read_dispatch_results",
    } <= set(inspector.get_table_names())
    columns = {item["name"] for item in inspector.get_columns("sop_operation_attempts")}
    assert {"dispatch_token", "deadline_at", "retry_at"} <= columns
    uniques = {
        str(item.get("name"))
        for item in inspector.get_unique_constraints("sop_operation_attempts")
    }
    assert "uq_sop_operation_attempt_dispatch_token" in uniques
    assert "uq_sop_operation_attempt_execution" not in uniques

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO dynamic_read_dispatch_batches "
                "(id, tenant_id, execution_id, plan_revision_id, wave_checksum, "
                "ordered_step_keys_json, status, parallelism, revision, created_at, updated_at) "
                "VALUES ('batch','tenant','execution','plan',:checksum,'[]','ready',2,0,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="parallel dispatch facts"):
        command.downgrade(config, "20260814_0064")
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM dynamic_read_dispatch_batches"))
    command.downgrade(config, "20260814_0064")
    assert "dynamic_read_dispatch_batches" not in sa.inspect(engine).get_table_names()
    engine.dispose()
