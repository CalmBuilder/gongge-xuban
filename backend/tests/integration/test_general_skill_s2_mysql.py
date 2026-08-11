"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : test_general_skill_s2_mysql.py
@CallChain  : pytest MySQL fixture → Alembic 0055 → Skill resolver/governance services
@Description: 验证 S2 legacy 回填、授权事件和绑定 CAS 在 MySQL 8.4 的生产契约。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillAuthorizationEvent,
    GeneralSkillAuthorizationState,
    GeneralSkillRevision,
    Tenant,
    User,
)
from app.general_skills.governance import (
    GeneralSkillGovernanceError,
    GeneralSkillGovernanceService,
)


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str, revision: str = "head") -> None:
    """把隔离 MySQL 数据库升级到指定 revision。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, revision)


def _content_checksum(resource_checksum: str) -> str:
    """生成与 resolver 相同的单资源内容校验和。"""

    payload = [{"path": "SKILL.md", "checksum": resource_checksum}]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_s2_mysql_backfills_legacy_revision_and_event_once(mysql_database_url: str) -> None:
    """验证 0054 历史行升级后在 MySQL 生成唯一 revision、pointer、state 与事件。"""

    _upgrade(mysql_database_url, "20260812_0054")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO general_skills (id, tenant_id, slug, name, description, "
                "skill_markdown, skill_files_json, metadata_json, status, permissions_json, "
                "runtime_config_json, usage_mode, owner_user_id, visibility_scope, "
                "current_published_revision_id, row_version, planning_guidance_json, "
                "created_at, updated_at) VALUES ('genskill_mysql_legacy', 'tenant_mysql_legacy', "
                "'legacy', 'Legacy', 'legacy', '# Legacy', JSON_ARRAY(), JSON_OBJECT(), "
                "'published', JSON_OBJECT(), JSON_OBJECT(), 'planning_guidance', "
                "'user_mysql_legacy', 'user_private', NULL, 1, JSON_OBJECT(), "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
    _upgrade(mysql_database_url)
    _upgrade(mysql_database_url)

    assert {
        "general_skill_authorization_states",
        "general_skill_authorization_events",
    } <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        revision = connection.execute(
            text(
                "SELECT id, status FROM general_skill_revisions "
                "WHERE skill_id = 'genskill_mysql_legacy'"
            )
        ).one()
        assert revision.status == "published"
        assert connection.execute(
            text(
                "SELECT current_published_revision_id FROM general_skills "
                "WHERE id = 'genskill_mysql_legacy'"
            )
        ).scalar_one() == revision.id
        assert connection.execute(
            text(
                "SELECT revision FROM general_skill_authorization_states "
                "WHERE tenant_id = 'tenant_mysql_legacy'"
            )
        ).scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM general_skill_authorization_events "
                "WHERE tenant_id = 'tenant_mysql_legacy' AND authorization_revision = 1"
            )
        ).scalar_one() == 1
    engine.dispose()


def test_s2_mysql_concurrent_binding_update_has_one_winner_and_one_event(
    mysql_database_url: str,
) -> None:
    """验证两个 MySQL 写者使用同一 ETag 时仅一个提交并只推进一次授权 revision。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    resource_checksum = hashlib.sha256(b"# MySQL S2").hexdigest()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_mysql_s2", name="MySQL S2"))
        db.add(
            User(
                id="user_mysql_s2",
                tenant_id="tenant_mysql_s2",
                username="mysql-s2",
                role="member",
                password_hash="unused",
            )
        )
        db.add(
            AgentProfile(
                id="agent_mysql_s2",
                tenant_id="tenant_mysql_s2",
                name="MySQL S2 Agent",
                owner_user_id="user_mysql_s2",
            )
        )
        skill = GeneralSkill(
            id="genskill_mysql_s2",
            tenant_id="tenant_mysql_s2",
            slug="mysql-s2",
            name="MySQL S2",
            skill_markdown="# MySQL S2",
            status="published",
            usage_mode="planning_guidance",
            owner_user_id="user_mysql_s2",
            visibility_scope="agent_private",
        )
        db.add(skill)
        db.flush()
        revision = GeneralSkillRevision(
            id="gsrev_mysql_s2",
            tenant_id="tenant_mysql_s2",
            skill_id=skill.id,
            revision_number=1,
            content_checksum=_content_checksum(resource_checksum),
            manifest_checksum=resource_checksum,
            normalized_skill_markdown="# MySQL S2",
            resource_manifest_json=[
                {"path": "SKILL.md", "checksum": resource_checksum}
            ],
            status="published",
            created_by="user_mysql_s2",
        )
        db.add(revision)
        db.flush()
        skill.current_published_revision_id = revision.id
        db.add(skill)
        db.add(
            AgentResourceBinding(
                id="agentres_mysql_s2",
                tenant_id="tenant_mysql_s2",
                agent_id="agent_mysql_s2",
                resource_type="general_skill",
                resource_id=skill.id,
                status="active",
                metadata_json={
                    "schema_version": 1,
                    "revision_policy": "pinned",
                    "pinned_revision_id": revision.id,
                    "invocation_policy": "model_allowed",
                    "atomic_execution_allowed": False,
                    "created_by_user_id": "user_mysql_s2",
                },
            )
        )
        db.commit()
    barrier = Barrier(2)

    def update_once(status: str) -> str:
        """从独立连接同时提交相同 row_version 的相反状态。"""

        with Session(engine) as db:
            owner = db.get(User, "user_mysql_s2")
            assert owner is not None
            barrier.wait(timeout=10)
            try:
                GeneralSkillGovernanceService(db).update_binding_configuration(
                    current_user=owner,
                    agent_id="agent_mysql_s2",
                    binding_id="agentres_mysql_s2",
                    status=status,
                    revision_policy="pinned",
                    pinned_revision_id="gsrev_mysql_s2",
                    invocation_policy="model_allowed",
                    expected_row_version=1,
                )
                return "success"
            except GeneralSkillGovernanceError as exc:
                return exc.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update_once, ["active", "inactive"]))
    assert sorted(outcomes) == ["GENERAL_SKILL_STATE_CONFLICT", "success"]

    with Session(engine) as db:
        binding = db.get(AgentResourceBinding, "agentres_mysql_s2")
        state = db.get(GeneralSkillAuthorizationState, "tenant_mysql_s2")
        events = db.exec(
            select(GeneralSkillAuthorizationEvent).where(
                GeneralSkillAuthorizationEvent.tenant_id == "tenant_mysql_s2"
            )
        ).all()
        assert binding is not None and binding.row_version == 2
        assert state is not None and state.revision == 1
        assert len(events) == 1 and events[0].authorization_revision == 1
    engine.dispose()
