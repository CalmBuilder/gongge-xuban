"""
@Time       : 2026/08/29 23:20
@Author     : zhanglp8181
@File       : test_agent_organizationization_migration.py
@CallChain  : pytest → Alembic 0073/0074 → AgentOrganizationizationCommand 表 → SQLite 回滚
@Description: 验证组织化命令回执迁移的字段、唯一约束、幂等升级和安全降级行为。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向隔离 SQLite 文件的 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_agent_organizationization_command_migration_roundtrip(tmp_path: Path) -> None:
    """验证 0074 建表、命令唯一性、重复升级和存在回执时拒绝降级。"""

    database_url = f"sqlite:///{tmp_path / 'agent-organizationization-command.db'}"
    config = _config(database_url)
    command.stamp(config, "20260829_0073")
    command.upgrade(config, "20260829_0074")
    command.upgrade(config, "20260829_0074")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "agent_organizationization_commands" in inspector.get_table_names()
        columns = {
            item["name"] for item in inspector.get_columns("agent_organizationization_commands")
        }
        assert {
            "tenant_id",
            "agent_id",
            "command_id",
            "request_checksum",
            "expected_profile_revision",
            "expected_relationship_checksum",
            "active_role_binding_id",
            "status",
            "result_json",
            "error_code",
        } <= columns
        unique_constraints = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("agent_organizationization_commands")
        }
        assert ("tenant_id", "agent_id", "command_id") in unique_constraints

        values = {
            "id": "organizationization_command_a",
            "tenant_id": "tenant_a",
            "agent_id": "agent_a",
            "command_id": "command_a",
            "request_checksum": "a" * 64,
            "expected_profile_revision": 1,
            "expected_relationship_checksum": "b" * 64,
            "active_role_binding_id": None,
            "status": "committed",
            "result_json": "{}",
            "error_code": None,
            "created_at": "2026-08-29 00:00:00",
            "updated_at": "2026-08-29 00:00:00",
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_organizationization_commands "
                    "(id, tenant_id, agent_id, command_id, request_checksum, "
                    "expected_profile_revision, expected_relationship_checksum, "
                    "active_role_binding_id, status, result_json, error_code, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :agent_id, :command_id, :request_checksum, "
                    ":expected_profile_revision, :expected_relationship_checksum, "
                    ":active_role_binding_id, :status, :result_json, :error_code, :created_at, :updated_at)"
                ),
                values,
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO agent_organizationization_commands "
                        "(id, tenant_id, agent_id, command_id, request_checksum, "
                        "expected_profile_revision, expected_relationship_checksum, "
                        "active_role_binding_id, status, result_json, error_code, created_at, updated_at) "
                        "VALUES (:id, :tenant_id, :agent_id, :command_id, :request_checksum, "
                        ":expected_profile_revision, :expected_relationship_checksum, "
                        ":active_role_binding_id, :status, :result_json, :error_code, :created_at, :updated_at)"
                    ),
                    {**values, "id": "organizationization_command_b"},
                )

        with pytest.raises(RuntimeError, match="organizationization command receipts"):
            command.downgrade(config, "20260829_0073")

        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM agent_organizationization_commands "
                    "WHERE tenant_id = :tenant_id AND agent_id = :agent_id AND command_id = :command_id"
                ),
                values,
            )
        command.downgrade(config, "20260829_0073")
        assert "agent_organizationization_commands" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
