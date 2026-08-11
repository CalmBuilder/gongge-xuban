"""
@Time       : 2026/08/13 01:30
@Author     : zhanglp8181
@File       : test_general_skill_s3_mysql.py
@CallChain  : pytest MySQL fixture → Alembic 0057 → GeneralSkillRuntimeService
@Description: 验证 S3 会话 override、Use 幂等和 Operation Skill 因果字段的 MySQL 契约。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect
from sqlmodel import Session, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    GeneralSkillRevision,
    GeneralSkillUse,
    Tenant,
    User,
)
from app.general_skills.runtime import GeneralSkillRuntimeService


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把隔离 MySQL 数据库升级到当前 head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def _content_checksum(resource_checksum: str) -> str:
    """生成与生产 resolver 相同的单资源内容 checksum。"""

    return hashlib.sha256(
        json.dumps(
            [{"path": "SKILL.md", "checksum": resource_checksum}],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_s3_mysql_runtime_load_is_idempotent_and_mute_is_durable(
    mysql_database_url: str,
) -> None:
    """在 MySQL 8.4 验证固定修订只写一个 Use，mute 则持久 countermand。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    assert "caused_by_skill_use_id" in {
        column["name"] for column in inspect(engine).get_columns("sop_operations")
    }
    markdown = "# MySQL S3\nFollow reviewed guidance."
    resource_checksum = hashlib.sha256(markdown.encode()).hexdigest()
    with Session(engine) as db:
        owner = User(
            id="user_mysql_s3",
            tenant_id="tenant_mysql_s3",
            username="mysql-s3",
            role="member",
            password_hash="unused",
        )
        agent = AgentProfile(
            id="agent_mysql_s3",
            tenant_id=owner.tenant_id,
            name="MySQL S3 Agent",
            owner_user_id=owner.id,
        )
        chat = ChatSession(
            id="session_mysql_s3",
            tenant_id=owner.tenant_id,
            user_id=owner.id,
            agent_id=agent.id,
        )
        skill = GeneralSkill(
            id="genskill_mysql_s3",
            tenant_id=owner.tenant_id,
            slug="mysql-s3",
            name="MySQL S3",
            skill_markdown=markdown,
            status="published",
            usage_mode="planning_guidance",
            owner_user_id=owner.id,
            visibility_scope="agent_private",
        )
        db.add(Tenant(id=owner.tenant_id, name="MySQL S3"))
        db.add(owner)
        db.add(agent)
        db.add(chat)
        db.add(skill)
        db.flush()
        revision = GeneralSkillRevision(
            id="gsrev_mysql_s3",
            tenant_id=owner.tenant_id,
            skill_id=skill.id,
            revision_number=1,
            content_checksum=_content_checksum(resource_checksum),
            manifest_checksum=resource_checksum,
            normalized_skill_markdown=markdown,
            resource_manifest_json=[
                {"path": "SKILL.md", "checksum": resource_checksum}
            ],
            requested_capabilities_json={"invocation_policy": "model_allowed"},
            source_snapshot_json={"source_kind": "test"},
            status="published",
            created_by=owner.id,
        )
        db.add(revision)
        db.flush()
        skill.current_published_revision_id = revision.id
        db.add(skill)
        db.add(
            AgentResourceBinding(
                id="agentres_mysql_s3",
                tenant_id=owner.tenant_id,
                agent_id=agent.id,
                resource_type="general_skill",
                resource_id=skill.id,
                status="active",
                metadata_json={
                    "schema_version": 1,
                    "revision_policy": "pinned",
                    "pinned_revision_id": revision.id,
                    "invocation_policy": "model_allowed",
                    "atomic_execution_allowed": False,
                    "created_by_user_id": owner.id,
                },
            )
        )
        db.commit()
        runtime = GeneralSkillRuntimeService(db)
        first = runtime.load_bundle(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            turn_id="turn_mysql_s3",
            skill_id=skill.id,
            selection_mode="forced",
        )
        replay = runtime.load_bundle(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            turn_id="turn_mysql_s3",
            skill_id=skill.id,
            selection_mode="forced",
        )
        assert first[0].use_id == replay[0].use_id
        assert len(db.exec(select(GeneralSkillUse)).all()) == 1
        runtime.set_session_enabled(
            owner,
            session_id=chat.id,
            agent_id=agent.id,
            skill_id=skill.id,
            enabled=False,
            expected_row_version=None,
        )
        assert runtime.session_catalog(owner, session_id=chat.id, agent_id=agent.id) == ()
        assert [row.id for row in runtime.invalidate_unavailable(
            owner, session_id=chat.id, agent_id=agent.id
        )] == [first[0].use_id]
        db.commit()
        assert db.get(GeneralSkillUse, first[0].use_id).status == "invalidated"
    engine.dispose()
