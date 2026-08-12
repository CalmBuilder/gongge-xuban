"""
@Time       : 2026/08/13 16:20
@Author     : zhanglp8181
@File       : test_general_skill_g1_migration.py
@CallChain  : pytest → Alembic 0061/0062/0063 → SQLite G1 schema roundtrip
@Description: 验证 G1 提案、发布、对话安装和采用幂等表可独立升级并安全降级。
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向临时 SQLite 的真实 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_general_skill_g1_migration_roundtrip(tmp_path: Path) -> None:
    """确认 0061 升级保留 authored 默认值、创建发布表且降级回 0060。"""

    database_url = f"sqlite:///{tmp_path / 'g1.db'}"
    config = _config(database_url)
    command.stamp(config, "20260813_0059")
    command.upgrade(config, "20260813_0060")
    engine = sa.create_engine(database_url)
    now = "2026-08-13 00:00:00"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO general_skill_proposals "
                "(id, tenant_id, execution_id, session_id, agent_id, initiator_user_id, "
                "operation_id, skill_id, revision_id, review_artifact_id, proposal_checksum, "
                "status, row_version, created_at, updated_at) VALUES "
                "('legacy', 'tenant', 'execution', 'session', 'agent', 'user', 'operation', "
                "'skill', 'revision', 'artifact', :checksum, 'staged', 1, :now, :now)"
            ),
            {"checksum": "a" * 64, "now": now},
        )
    command.upgrade(config, "20260813_0061")
    inspector = sa.inspect(engine)
    expected = {
        "resource_publication_requests",
        "general_skill_publication_revisions",
        "agent_publication_revisions",
        "publication_releases",
        "general_skill_binding_batch_commands",
    }
    assert expected <= set(inspector.get_table_names())
    columns = {item["name"] for item in inspector.get_columns("general_skill_proposals")}
    assert {"proposal_kind", "import_job_id", "preview_checksum", "remote_candidate_ids_json"} <= columns
    with engine.begin() as connection:
        row = connection.execute(
            sa.text("SELECT proposal_kind FROM general_skill_proposals WHERE id='legacy'")
        ).one()
        assert row[0] == "authored"
    command.downgrade(config, "20260813_0060")
    inspector = sa.inspect(engine)
    assert not (expected & set(inspector.get_table_names()))
    columns = {item["name"] for item in inspector.get_columns("general_skill_proposals")}
    assert "proposal_kind" not in columns


def test_g1_indexed_identifier_widths_remain_mysql_utf8mb4_safe(tmp_path: Path) -> None:
    """确认 0061—0063 联合唯一键只使用 128 字符标识符，避免 MySQL 索引超长。"""

    database_url = f"sqlite:///{tmp_path / 'g1-widths.db'}"
    config = _config(database_url)
    command.stamp(config, "20260813_0059")
    command.upgrade(config, "20260814_0063")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    expected_widths = {
        "general_skill_binding_batch_commands": {
            "tenant_id", "owner_user_id", "skill_id", "revision_id"
        },
        "general_skill_install_intents": {
            "tenant_id", "session_id", "agent_id", "owner_user_id", "import_job_id"
        },
        "publication_adoption_commands": {
            "tenant_id", "actor_user_id", "release_id", "target_agent_id", "binding_id",
            "adopted_agent_id"
        },
    }
    for table_name, column_names in expected_widths.items():
        columns = {item["name"]: item["type"] for item in inspector.get_columns(table_name)}
        assert all(columns[name].length == 128 for name in column_names)


def test_publication_adoption_command_migration_roundtrip(tmp_path: Path) -> None:
    """确认 0063 可从 0062 独立升级、约束幂等键并在无事实时降级。"""

    database_url = f"sqlite:///{tmp_path / 'g1-adoption.db'}"
    config = _config(database_url)
    command.stamp(config, "20260813_0061")
    command.upgrade(config, "20260813_0062")
    command.upgrade(config, "20260814_0063")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert "publication_adoption_commands" in inspector.get_table_names()
    unique_columns = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("publication_adoption_commands")
    }
    assert ("tenant_id", "actor_user_id", "idempotency_key") in unique_columns
    command.downgrade(config, "20260813_0062")
    assert "publication_adoption_commands" not in sa.inspect(engine).get_table_names()
