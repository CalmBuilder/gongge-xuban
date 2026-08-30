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
from sqlalchemy import CheckConstraint, MetaData, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db.models import SopOperation
from app.db.sqlite_legacy import migrate_sqlite_skill_schema


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


def test_destructive_operation_kind_migration_expands_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    """确认 0077 在 SQLite 上扩展 destructive，并拒绝带数据回退。"""

    database_url = f"sqlite:///{tmp_path / 'destructive-operation.db'}"
    config = _config(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR(128) PRIMARY KEY, "
                "effect_kind VARCHAR(64) NOT NULL, "
                "CONSTRAINT ck_sop_operation_effect_kind CHECK ("
                "effect_kind IN ('read', 'local_write', 'execute', 'external_write', 'legacy_unknown')))"
            )
        )
        connection.execute(
            text("INSERT INTO sop_operations VALUES ('op_read', 'read')")
        )
    command.stamp(config, "20260830_0076")
    command.upgrade(config, "20260830_0077")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO sop_operations VALUES ('op_destructive', 'destructive')")
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text("INSERT INTO sop_operations VALUES ('op_bad', 'arbitrary_shell')")
            )
    with pytest.raises(RuntimeError, match="destructive operations exist"):
        command.downgrade(config, "20260830_0076")


def test_sqlite_startup_repairs_existing_operation_check_for_destructive(
    tmp_path: Path,
) -> None:
    """确认已完成字段迁移的旧 SQLite 库启动时也能持久化 destructive。"""

    database_url = f"sqlite:///{tmp_path / 'legacy-operation.db'}"
    engine = create_engine(database_url)
    old_metadata = MetaData()
    old_table = SopOperation.__table__.to_metadata(old_metadata)
    for constraint in list(old_table.constraints):
        if constraint.name == "ck_sop_operation_effect_kind":
            old_table.constraints.remove(constraint)
    old_table.append_constraint(
        CheckConstraint(
            "effect_kind IN ('read', 'local_write', 'execute', 'external_write', 'legacy_unknown')",
            name="ck_sop_operation_effect_kind",
        )
    )
    with engine.begin() as connection:
        old_table.create(connection)
        connection.execute(
            old_table.insert().values(
                id="op_legacy",
                tenant_id="tenant_a",
                instance_id="instance_a",
                node_execution_id="node_a",
                operation_name="legacy.read",
                idempotency_key="legacy-key",
                logical_action_id="legacy-action",
                request_fingerprint="legacy-fingerprint",
                effect_kind="read",
                effect_state="none",
                status="succeeded",
            )
        )

    migrate_sqlite_skill_schema(engine)

    with Session(engine) as db:
        db.add(
            SopOperation(
                id="op_destructive",
                tenant_id="tenant_a",
                instance_id="instance_a",
                node_execution_id="node_a",
                operation_name="isolated.delete",
                idempotency_key="destructive-key",
                logical_action_id="destructive-action",
                request_fingerprint="destructive-fingerprint",
                effect_kind="destructive",
            )
        )
        db.commit()

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT effect_kind FROM sop_operations WHERE id = 'op_legacy'")
        ).scalar_one() == "read"
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO sop_operations "
                    "(id, tenant_id, instance_id, node_execution_id, operation_name, "
                    "idempotency_key, logical_action_id, request_fingerprint, effect_kind) "
                    "VALUES ('op_bad', 'tenant_a', 'instance_a', 'node_a', 'bad', "
                    "'bad-key', 'bad-action', 'bad-fingerprint', 'arbitrary_shell')"
                )
            )
