"""
@Time       : 2026/08/13 09:30
@Author     : zhanglp8181
@File       : test_general_skill_proposal_migration.py
@CallChain  : pytest → Alembic 0060 → SQLite proposal state constraints
@Description: 验证 Agent Skill proposal 表可升级、约束非法状态并可安全降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    """构造指向临时 SQLite 的真实 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_general_skill_proposal_migration_roundtrip_and_constraints(tmp_path: Path) -> None:
    """确认 0060 可升级、拒绝非法状态，并在降级时只移除 proposal 编排表。"""

    database_url = f"sqlite:///{tmp_path / 'proposal.db'}"
    config = _config(database_url)
    command.stamp(config, "20260813_0059")
    command.upgrade(config, "20260813_0060")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert "general_skill_proposals" in inspector.get_table_names()
    with engine.begin() as connection:
        values = {
            "id": "gsproposal_test",
            "tenant_id": "tenant_test",
            "execution_id": "execution_test",
            "session_id": "session_test",
            "agent_id": "agent_test",
            "initiator_user_id": "user_test",
            "operation_id": "operation_test",
            "skill_id": "skill_test",
            "revision_id": "revision_test",
            "review_artifact_id": "artifact_test",
            "proposal_checksum": "a" * 64,
            "status": "awaiting_approval",
            "row_version": 1,
            "created_at": "2026-08-13 00:00:00",
            "updated_at": "2026-08-13 00:00:00",
        }
        connection.execute(
            sa.text(
                "INSERT INTO general_skill_proposals "
                "(id, tenant_id, execution_id, session_id, agent_id, initiator_user_id, "
                "operation_id, skill_id, revision_id, review_artifact_id, proposal_checksum, "
                "status, row_version, created_at, updated_at) VALUES "
                "(:id, :tenant_id, :execution_id, :session_id, :agent_id, :initiator_user_id, "
                ":operation_id, :skill_id, :revision_id, :review_artifact_id, "
                ":proposal_checksum, :status, :row_version, :created_at, :updated_at)"
            ),
            values,
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO general_skill_proposals "
                    "(id, tenant_id, execution_id, session_id, agent_id, initiator_user_id, "
                    "operation_id, skill_id, revision_id, review_artifact_id, proposal_checksum, "
                    "status, row_version, created_at, updated_at) VALUES "
                    "('gsproposal_bad', 'tenant_bad', 'execution_bad', 'session_bad', "
                    "'agent_bad', 'user_bad', 'operation_bad', 'skill_bad', 'revision_bad', "
                    "'artifact_bad', :checksum, 'invented', 1, :created_at, :updated_at)"
                ),
                {
                    "checksum": "b" * 64,
                    "created_at": "2026-08-13 00:00:00",
                    "updated_at": "2026-08-13 00:00:00",
                },
            )
    command.downgrade(config, "20260813_0059")
    assert "general_skill_proposals" not in sa.inspect(engine).get_table_names()
