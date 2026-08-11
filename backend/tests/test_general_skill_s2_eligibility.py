"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : test_general_skill_s2_eligibility.py
@CallChain  : pytest → EffectiveGeneralSkillResolver → Skill/Revision/Agent binding
@Description: 固定 S2 用户隔离、绑定版本策略、撤权和权威目录哈希契约。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    GeneralSkill,
    GeneralSkillAuthorizationEvent,
    GeneralSkillAuthorizationState,
    GeneralSkillRevision,
    Tenant,
    User,
)
from app.general_skills.eligibility import EffectiveGeneralSkillResolver
from app.general_skills.governance import GeneralSkillGovernanceService
from app.general_skills.governance import GeneralSkillGovernanceError


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _checksum(value: object) -> str:
    """生成与测试内容绑定的稳定 SHA-256。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _context() -> tuple[Session, User, User, AgentProfile]:
    """建立包含所有者、采用者和已发布数字员工的隔离上下文。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = User(
        id="user_skill_owner",
        tenant_id="tenant_skill_s2",
        username="skill-owner",
        role="member",
        password_hash="unused",
    )
    adopter = User(
        id="user_skill_adopter",
        tenant_id="tenant_skill_s2",
        username="skill-adopter",
        role="member",
        password_hash="unused",
    )
    agent = AgentProfile(
        id="agent_skill_s2",
        tenant_id="tenant_skill_s2",
        name="S2 能力分身",
        owner_user_id=owner.id,
        status="active",
        published_to_gallery=True,
        visibility_scope="tenant",
    )
    db.add(Tenant(id="tenant_skill_s2", name="Skill S2 Tenant"))
    db.add(owner)
    db.add(adopter)
    db.add(agent)
    db.add(
        AgentUsage(
            tenant_id="tenant_skill_s2",
            user_id=adopter.id,
            agent_id=agent.id,
        )
    )
    db.commit()
    return db, owner, adopter, agent


def _skill_with_revisions(
    db: Session,
    owner: User,
    agent: AgentProfile,
    *,
    visibility_scope: str,
    revision_policy: str,
) -> tuple[GeneralSkill, GeneralSkillRevision, GeneralSkillRevision, AgentResourceBinding]:
    """写入一个稳定 Skill、两个不可变修订和一条版本化绑定。"""

    first_payload = {"path": "SKILL.md", "checksum": hashlib.sha256(b"revision-one").hexdigest()}
    second_payload = {"path": "SKILL.md", "checksum": hashlib.sha256(b"revision-two").hexdigest()}
    skill = GeneralSkill(
        id=f"genskill_{visibility_scope}",
        tenant_id=owner.tenant_id,
        slug=f"review-helper-{visibility_scope}",
        name="Review Helper",
        description="Review a change safely.",
        skill_markdown="# legacy compatibility",
        status="published",
        usage_mode="planning_guidance",
        owner_user_id=owner.id,
        visibility_scope=visibility_scope,
    )
    db.add(skill)
    db.flush()
    first = GeneralSkillRevision(
        id=f"gsrev_{visibility_scope}_one",
        tenant_id=owner.tenant_id,
        skill_id=skill.id,
        revision_number=1,
        content_checksum=_checksum([first_payload]),
        manifest_checksum=first_payload["checksum"],
        normalized_skill_markdown="# revision one",
        parsed_metadata_json={"name": "review-helper", "revision": 1},
        resource_manifest_json=[first_payload],
        status="superseded",
        created_by=owner.id,
    )
    second = GeneralSkillRevision(
        id=f"gsrev_{visibility_scope}_two",
        tenant_id=owner.tenant_id,
        skill_id=skill.id,
        revision_number=2,
        content_checksum=_checksum([second_payload]),
        manifest_checksum=second_payload["checksum"],
        normalized_skill_markdown="# revision two",
        parsed_metadata_json={"name": "review-helper", "revision": 2},
        resource_manifest_json=[second_payload],
        status="published",
        created_by=owner.id,
    )
    db.add(first)
    db.add(second)
    db.flush()
    skill.current_published_revision_id = second.id
    binding = AgentResourceBinding(
        id=f"agentres_{visibility_scope}",
        tenant_id=owner.tenant_id,
        agent_id=agent.id,
        resource_type="general_skill",
        resource_id=skill.id,
        status="active",
        metadata_json={
            "schema_version": 1,
            "revision_policy": revision_policy,
            "pinned_revision_id": first.id if revision_policy == "pinned" else None,
            "invocation_policy": "model_allowed",
            "atomic_execution_allowed": False,
            "created_by_user_id": owner.id,
        },
    )
    db.add(skill)
    db.add(binding)
    db.commit()
    return skill, first, second, binding


def test_user_private_skill_is_visible_only_to_owner_even_on_shared_agent() -> None:
    """验证采用同一公开分身的其他用户不能借绑定读取所有者私有 Skill。"""

    db, owner, adopter, agent = _context()
    _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="user_private",
        revision_policy="pinned",
    )
    resolver = EffectiveGeneralSkillResolver(db)

    assert [item.revision_number for item in resolver.resolve(owner, agent.id).items] == [1]
    assert resolver.resolve(adopter, agent.id).items == ()


def test_agent_private_skill_is_shared_only_through_authorized_agent_usage() -> None:
    """验证 agent_private 可供已采用该分身的用户使用但跨租户主体始终不可见。"""

    db, owner, adopter, agent = _context()
    _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="agent_private",
        revision_policy="pinned",
    )
    outsider = User(
        id="user_other_tenant",
        tenant_id="tenant_other",
        username="outsider",
        role="member",
        password_hash="unused",
    )
    db.add(Tenant(id="tenant_other", name="Other Tenant"))
    db.add(outsider)
    db.commit()
    resolver = EffectiveGeneralSkillResolver(db)

    assert len(resolver.resolve(owner, agent.id).items) == 1
    assert len(resolver.resolve(adopter, agent.id).items) == 1
    assert resolver.resolve(outsider, agent.id).items == ()


def test_pinned_and_follow_latest_resolve_different_reviewed_revisions() -> None:
    """验证 pinned 保留旧修订，follow_latest 只取当前 published 指针。"""

    db, owner, _, agent = _context()
    _, first, second, binding = _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="agent_private",
        revision_policy="pinned",
    )
    resolver = EffectiveGeneralSkillResolver(db)
    pinned = resolver.resolve(owner, agent.id)

    assert pinned.items[0].revision_id == first.id
    GeneralSkillGovernanceService(db).update_binding_policy(
        current_user=owner,
        agent_id=agent.id,
        binding_id=binding.id,
        revision_policy="follow_latest",
        pinned_revision_id=None,
        expected_row_version=binding.row_version,
    )

    latest = resolver.resolve(owner, agent.id)
    assert latest.items[0].revision_id == second.id
    assert latest.authorization_revision > pinned.authorization_revision
    assert latest.eligibility_hash != pinned.eligibility_hash


def test_revoked_or_checksum_mismatched_revision_is_fail_closed() -> None:
    """验证撤销和不可变正文校验和漂移均从权威目录立即消失。"""

    db, owner, _, agent = _context()
    _, first, _, _ = _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="agent_private",
        revision_policy="pinned",
    )
    resolver = EffectiveGeneralSkillResolver(db)
    assert len(resolver.resolve(owner, agent.id).items) == 1

    first.status = "revoked"
    db.add(first)
    db.commit()
    assert resolver.resolve(owner, agent.id).items == ()

    first.status = "superseded"
    first.content_checksum = "0" * 64
    db.add(first)
    db.commit()
    assert resolver.resolve(owner, agent.id).items == ()


def test_binding_configuration_is_atomic_audited_and_stale_write_is_rejected() -> None:
    """验证策略、调用方式和停用一次提交，旧 row_version 不得覆盖新状态。"""

    db, owner, _, agent = _context()
    _, _, second, binding = _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="agent_private",
        revision_policy="pinned",
    )
    service = GeneralSkillGovernanceService(db)
    updated = service.update_binding_configuration(
        current_user=owner,
        agent_id=agent.id,
        binding_id=binding.id,
        status="inactive",
        revision_policy="follow_latest",
        pinned_revision_id=None,
        invocation_policy="user_only",
        expected_row_version=1,
    )

    assert updated.status == "inactive"
    assert updated.metadata_json["revision_policy"] == "follow_latest"
    assert updated.metadata_json["invocation_policy"] == "user_only"
    assert EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items == ()
    state = db.get(GeneralSkillAuthorizationState, owner.tenant_id)
    events = db.exec(
        select(GeneralSkillAuthorizationEvent).where(
            GeneralSkillAuthorizationEvent.tenant_id == owner.tenant_id
        )
    ).all()
    assert state is not None and state.revision == 1
    assert [(event.authorization_revision, event.event_type) for event in events] == [
        (1, "binding_policy_updated")
    ]
    with pytest.raises(GeneralSkillGovernanceError, match="changed"):
        service.update_binding_configuration(
            current_user=owner,
            agent_id=agent.id,
            binding_id=binding.id,
            status="active",
            revision_policy="pinned",
            pinned_revision_id=second.id,
            invocation_policy="model_allowed",
            expected_row_version=1,
        )


def test_owner_reuses_skill_across_agents_and_gallery_requires_active_adoption() -> None:
    """验证本人可多分身复用，其他用户只能把 tenant-gallery Skill 主动绑定到本人分身。"""

    db, owner, adopter, first_agent = _context()
    skill, _, current, _ = _skill_with_revisions(
        db,
        owner,
        first_agent,
        visibility_scope="tenant_gallery",
        revision_policy="pinned",
    )
    owner_agent = AgentProfile(
        id="agent_skill_owner_second",
        tenant_id=owner.tenant_id,
        name="Owner Second",
        owner_user_id=owner.id,
    )
    adopter_agent = AgentProfile(
        id="agent_skill_adopter",
        tenant_id=adopter.tenant_id,
        name="Adopter Agent",
        owner_user_id=adopter.id,
    )
    db.add(owner_agent)
    db.add(adopter_agent)
    db.commit()
    service = GeneralSkillGovernanceService(db)

    owner_binding = service.create_binding(
        current_user=owner,
        agent_id=owner_agent.id,
        skill_id=skill.id,
        revision_policy="pinned",
        pinned_revision_id=current.id,
        invocation_policy="model_allowed",
    )
    adopter_binding = service.create_binding(
        current_user=adopter,
        agent_id=adopter_agent.id,
        skill_id=skill.id,
        revision_policy="follow_latest",
        pinned_revision_id=None,
        invocation_policy="user_only",
    )

    assert owner_binding.agent_id == owner_agent.id
    assert adopter_binding.agent_id == adopter_agent.id
    assert len(EffectiveGeneralSkillResolver(db).resolve(owner, owner_agent.id).items) == 1
    assert len(EffectiveGeneralSkillResolver(db).resolve(adopter, adopter_agent.id).items) == 1


def test_rollback_changes_only_follow_latest_binding_and_preserves_pinned_revision() -> None:
    """验证回滚切换 current 指针，固定绑定仍保持原目标且事件 revision 单调。"""

    db, owner, _, agent = _context()
    skill, first, second, binding = _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="agent_private",
        revision_policy="follow_latest",
    )
    resolver = EffectiveGeneralSkillResolver(db)
    assert resolver.resolve(owner, agent.id).items[0].revision_id == second.id

    rolled_back = GeneralSkillGovernanceService(db).rollback_skill(
        current_user=owner,
        skill_id=skill.id,
        target_revision_id=first.id,
        expected_skill_row_version=skill.row_version,
        expected_target_row_version=first.row_version,
    )
    assert rolled_back.id == first.id
    assert rolled_back.status == "published"
    assert resolver.resolve(owner, agent.id).items[0].revision_id == first.id

    db.refresh(binding)
    GeneralSkillGovernanceService(db).update_binding_policy(
        current_user=owner,
        agent_id=agent.id,
        binding_id=binding.id,
        revision_policy="pinned",
        pinned_revision_id=second.id,
        expected_row_version=binding.row_version,
    )
    assert resolver.resolve(owner, agent.id).items[0].revision_id == second.id


def test_revoke_current_revision_soft_deletes_and_invalidates_all_workers() -> None:
    """验证 current 撤销保留修订审计事实且新旧 resolver 均实时 fail-closed。"""

    db, owner, _, agent = _context()
    skill, _, second, _ = _skill_with_revisions(
        db,
        owner,
        agent,
        visibility_scope="agent_private",
        revision_policy="follow_latest",
    )
    worker_one = EffectiveGeneralSkillResolver(db)
    worker_two = EffectiveGeneralSkillResolver(db)
    before = worker_one.resolve(owner, agent.id)
    assert before.items[0].revision_id == second.id

    GeneralSkillGovernanceService(db).revoke_revision(
        current_user=owner,
        skill_id=skill.id,
        revision_id=second.id,
        expected_skill_row_version=skill.row_version,
        expected_revision_row_version=second.row_version,
    )

    db.refresh(skill)
    db.refresh(second)
    assert skill.status == "archived"
    assert skill.current_published_revision_id is None
    assert second.status == "revoked"
    assert worker_one.resolve(owner, agent.id).items == ()
    assert worker_two.resolve(owner, agent.id).items == ()
    assert (
        worker_two.resolve(owner, agent.id).authorization_revision
        > before.authorization_revision
    )


def test_0055_backfills_legacy_revision_and_authorization_state_reentrantly(tmp_path) -> None:
    """验证 legacy Skill 升级生成确定修订、发布指针和单调授权状态且可重复执行。"""

    database_url = f"sqlite:///{tmp_path / 'skill-s2-backfill.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        SQLModel.metadata.create_all(connection)
        connection.execute(text("DROP TABLE general_skill_authorization_states"))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260812_0054')"))
        connection.execute(
            text(
                "INSERT INTO general_skills ("
                "id, tenant_id, slug, name, description, skill_markdown, skill_files_json, "
                "metadata_json, status, permissions_json, runtime_config_json, usage_mode, "
                "owner_user_id, visibility_scope, current_published_revision_id, row_version, "
                "planning_guidance_json, created_at, updated_at) VALUES ("
                "'genskill_legacy_s2', 'tenant_legacy_s2', 'legacy-review', 'Legacy Review', "
                "'Legacy skill', '# Legacy Review', :files, '{}', 'published', '{}', '{}', "
                "'planning_guidance', 'user_legacy_s2', 'user_private', NULL, 1, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "files": json.dumps(
                    [
                        {
                            "path": "reference.md",
                            "content": "# Reference",
                            "mime_type": "text/markdown",
                        }
                    ]
                )
            },
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert "general_skill_authorization_states" in inspect(engine).get_table_names()
    assert "general_skill_authorization_events" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        revision = connection.execute(
            text(
                "SELECT id, status, resource_manifest_json, source_snapshot_json "
                "FROM general_skill_revisions WHERE skill_id = 'genskill_legacy_s2'"
            )
        ).one()
        pointer = connection.execute(
            text(
                "SELECT current_published_revision_id FROM general_skills "
                "WHERE id = 'genskill_legacy_s2'"
            )
        ).scalar_one()
        state = connection.execute(
            text(
                "SELECT revision FROM general_skill_authorization_states "
                "WHERE tenant_id = 'tenant_legacy_s2'"
            )
        ).scalar_one()
        event = connection.execute(
            text(
                "SELECT authorization_revision, event_type FROM "
                "general_skill_authorization_events WHERE tenant_id = 'tenant_legacy_s2'"
            )
        ).one()
        assert revision.status == "published"
        assert "legacy_backfill" in revision.source_snapshot_json
        assert "reference.md" in revision.resource_manifest_json
        assert pointer == revision.id
        assert state == 1
        assert event.authorization_revision == 1
        assert event.event_type == "legacy_backfill"
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM general_skill_revisions "
                "WHERE skill_id = 'genskill_legacy_s2'"
            )
        ).scalar_one() == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE general_skill_authorization_states SET revision = 2 "
                "WHERE tenant_id = 'tenant_legacy_s2'"
            )
        )
    with pytest.raises(RuntimeError, match="authorization changes"):
        command.downgrade(config, "20260812_0054")
    engine.dispose()
