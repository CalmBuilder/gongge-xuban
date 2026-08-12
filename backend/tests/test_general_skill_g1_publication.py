"""
@Time       : 2026/08/13 22:00
@Author     : zhanglp8181
@File       : test_general_skill_g1_publication.py
@CallChain  : PublicationService → typed snapshot/Attention/Release → Skill or Agent adoption
@Description: 验证 D 与 G1.4 的职责分离、陈旧拒绝、固定 Revision 采用和整 Agent 私密状态排除。
"""

from __future__ import annotations

import hashlib
import json
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillRevision,
    MemoryRecord,
    PublicationRelease,
    ResourcePublicationRequest,
    Tenant,
    User,
)
from app.general_skills.publication import PublicationError, PublicationService
from app.general_skills.eligibility import EffectiveGeneralSkillResolver
from app.config import get_settings
from app.core.agent_loop import AgentLoop


def _context() -> tuple[Session, PublicationService, User, User, User, AgentProfile, GeneralSkill]:
    """建立管理员、A/B 用户、A/B 分身及可发布私有 Skill。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    tenant = Tenant(id="tenant_g1_publication", name="G1 Publication")
    admin = User(
        id="admin_g1_publication",
        tenant_id=tenant.id,
        username="admin",
        role="admin",
        password_hash="unused",
    )
    owner = User(
        id="owner_g1_publication",
        tenant_id=tenant.id,
        username="owner",
        role="member",
        password_hash="unused",
    )
    adopter = User(
        id="adopter_g1_publication",
        tenant_id=tenant.id,
        username="adopter",
        role="member",
        password_hash="unused",
    )
    owner_agent = AgentProfile(
        id="agent_g1_owner",
        tenant_id=tenant.id,
        name="A-问卷发布分身",
        owner_user_id=owner.id,
        description="把资料转换为问卷。",
        persona_prompt="只使用已审 Skill。",
    )
    adopter_agent = AgentProfile(
        id="agent_g1_adopter",
        tenant_id=tenant.id,
        name="B-问卷采用分身",
        owner_user_id=adopter.id,
    )
    markdown = "---\nname: to-questionnaire\ndescription: Convert docs to questions.\n---\n# Questionnaire\n"
    resource_checksum = hashlib.sha256(markdown.encode()).hexdigest()
    skill = GeneralSkill(
        id="genskill_g1_questionnaire",
        tenant_id=tenant.id,
        slug="to-questionnaire",
        name="to-questionnaire",
        description="Convert docs to questions.",
        skill_markdown=markdown,
        usage_mode="planning_guidance",
        status="published",
        owner_user_id=owner.id,
        visibility_scope="user_private",
        current_published_revision_id="gsrev_g1_questionnaire",
    )
    revision = GeneralSkillRevision(
        id="gsrev_g1_questionnaire",
        tenant_id=tenant.id,
        skill_id=skill.id,
        revision_number=1,
        content_checksum=hashlib.sha256(
            json.dumps(
                [{"path": "SKILL.md", "checksum": resource_checksum}],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        manifest_checksum="b" * 64,
        normalized_skill_markdown=markdown,
        parsed_metadata_json={"name": "to-questionnaire", "description": skill.description},
        resource_manifest_json=[
            {
                "relative_path": "SKILL.md",
                "content_checksum": resource_checksum,
                "size": len(markdown),
                "media_type": "text/markdown",
                "is_text": True,
            }
        ],
        requested_capabilities_json={"allowed_tools": [], "invocation_policy": "user_only"},
        source_snapshot_json={
            "source_kind": "github",
            "source_reference_redacted": "mattpocock/skills@fixed#to-questionnaire",
        },
        status="published",
        created_by=owner.id,
    )
    db.add(tenant)
    db.add(admin)
    db.add(owner)
    db.add(adopter)
    db.add(owner_agent)
    db.add(adopter_agent)
    db.add(skill)
    db.add(revision)
    db.add(
        AgentResourceBinding(
            tenant_id=tenant.id,
            agent_id=owner_agent.id,
            resource_type="general_skill",
            resource_id=skill.id,
            status="active",
            metadata_json={
                "schema_version": 1,
                "revision_policy": "pinned",
                "pinned_revision_id": revision.id,
                "invocation_policy": "user_only",
                "atomic_execution_allowed": False,
                "created_by_user_id": owner.id,
            },
        )
    )
    db.add(
        MemoryRecord(
            tenant_id=tenant.id,
            user_id=owner.id,
            username=owner.username,
            agent_id=owner_agent.id,
            kind="preference",
            content="PRIVATE-MEMORY-MUST-NOT-PUBLISH",
        )
    )
    db.commit()
    return db, PublicationService(db), admin, owner, adopter, adopter_agent, skill


def test_skill_publication_requires_separated_admin_and_adoption_pins_approved_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明 Skill D 组织审核职责分离，B 主动采用后固定批准 Revision。"""

    db, service, admin, owner, adopter, adopter_agent, skill = _context()
    submitted = service.submit("general_skill", skill.id, skill.row_version, owner)
    with pytest.raises(PublicationError) as self_review:
        service.review(
            submitted.id,
            command="approve",
            command_id="self-review",
            expected_request_row_version=submitted.row_version,
            expected_attention_revision=0,
            reviewer=owner,
            comment=None,
        )
    assert self_review.value.code == "PUBLICATION_REVIEWER_DENIED"
    approved = service.review(
        submitted.id,
        command="approve",
        command_id="admin-review",
        expected_request_row_version=submitted.row_version,
        expected_attention_revision=0,
        reviewer=admin,
        comment="来源与风险通过",
    )
    assert approved.status == "approved"
    release = db.exec(select(PublicationRelease)).one()
    adopted = service.adopt(release.id, adopter_agent.id, "skill-adopt-once", adopter)
    replay = service.adopt(release.id, adopter_agent.id, "skill-adopt-once", adopter)
    assert replay == adopted
    with pytest.raises(PublicationError) as conflict:
        service.adopt(release.id, None, "skill-adopt-once", adopter)
    assert conflict.value.code == "PUBLICATION_IDEMPOTENCY_CONFLICT"
    binding = db.get(AgentResourceBinding, adopted.binding_id)
    assert binding is not None
    assert binding.metadata_json["pinned_revision_id"] == "gsrev_g1_questionnaire"
    assert skill.visibility_scope == "tenant_gallery"
    catalog = EffectiveGeneralSkillResolver(db).resolve(adopter, adopter_agent.id)
    assert [(item.skill_id, item.revision_id) for item in catalog.items] == [
        (skill.id, "gsrev_g1_questionnaire")
    ]
    monkeypatch.setattr(get_settings(), "general_skill_resolver_v2_enabled", True)
    runtime_rows = AgentLoop(db)._list_published_general_skills(
        adopter.tenant_id,
        adopter_agent.id,
        adopter.id,
    )
    assert [(row.id, row.metadata_json["resolved_revision_id"]) for row in runtime_rows] == [
        (skill.id, "gsrev_g1_questionnaire")
    ]

    skill.current_published_revision_id = "gsrev_owner_future_private"
    db.add(skill)
    db.commit()
    db.refresh(binding)
    assert binding.metadata_json["pinned_revision_id"] == "gsrev_g1_questionnaire"


def test_skill_publication_becomes_stale_when_revision_changes_before_review() -> None:
    """证明批准前所有者改变当前 revision 会使申请 stale，管理员不能批准旧快照。"""

    db, service, admin, owner, _adopter, _agent, skill = _context()
    submitted = service.submit("general_skill", skill.id, skill.row_version, owner)
    skill.current_published_revision_id = "gsrev_changed_before_review"
    db.add(skill)
    db.commit()
    with pytest.raises(PublicationError) as stale:
        service.review(
            submitted.id,
            command="approve",
            command_id="stale-review",
            expected_request_row_version=submitted.row_version,
            expected_attention_revision=0,
            reviewer=admin,
            comment=None,
        )
    assert stale.value.code == "PUBLICATION_SNAPSHOT_STALE"
    request = db.get(ResourcePublicationRequest, submitted.id)
    assert request is not None and request.status == "stale"
    assert db.exec(select(PublicationRelease)).all() == []


def test_agent_publication_excludes_memory_and_adoption_clones_frozen_components() -> None:
    """证明整 Agent 快照不含记忆/凭据，B 采用后生成本人新 Agent 与固定组件。"""

    db, service, admin, owner, adopter, _adopter_agent, skill = _context()
    owner_agent = db.get(AgentProfile, "agent_g1_owner")
    assert owner_agent is not None
    submitted = service.submit("agent", owner_agent.id, owner_agent.profile_revision, owner)
    request = db.get(ResourcePublicationRequest, submitted.id)
    assert request is not None
    from app.db.models import AgentPublicationRevision

    snapshot = db.get(AgentPublicationRevision, request.snapshot_id)
    assert snapshot is not None
    assert "PRIVATE-MEMORY-MUST-NOT-PUBLISH" not in str(snapshot.model_dump())
    assert snapshot.governance_snapshot_json["excluded"] == [
        "memory", "conversation", "connection", "credential", "schedule"
    ]
    approved = service.review(
        submitted.id,
        command="approve",
        command_id="agent-admin-review",
        expected_request_row_version=submitted.row_version,
        expected_attention_revision=0,
        reviewer=admin,
        comment="Agent 快照通过",
    )
    assert approved.status == "approved"
    release = db.exec(
        select(PublicationRelease).where(PublicationRelease.resource_type == "agent")
    ).one()
    adopted = service.adopt(release.id, None, "agent-adopt-once", adopter)
    replay = service.adopt(release.id, None, "agent-adopt-once", adopter)
    assert replay == adopted
    clone = db.get(AgentProfile, adopted.adopted_agent_id)
    assert clone is not None and clone.owner_user_id == adopter.id
    assert clone.source_agent_id == owner_agent.id
    bindings = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.agent_id == clone.id)
    ).all()
    assert [(row.resource_type, row.resource_id) for row in bindings] == [
        ("general_skill", skill.id)
    ]
    assert bindings[0].metadata_json["publication_release_id"] == release.id
    assert EffectiveGeneralSkillResolver(db).resolve(adopter, clone.id).items[0].skill_id == skill.id
    assert db.exec(
        select(MemoryRecord).where(MemoryRecord.agent_id == clone.id)
    ).all() == []


def test_agent_publication_becomes_stale_when_binding_changes_before_review() -> None:
    """证明 Agent 组件绑定变化会终止旧审核，而不只检查 persona 版本。"""

    db, service, admin, owner, _adopter, _agent, _skill = _context()
    owner_agent = db.get(AgentProfile, "agent_g1_owner")
    assert owner_agent is not None
    submitted = service.submit("agent", owner_agent.id, owner_agent.profile_revision, owner)
    binding = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.agent_id == owner_agent.id)
    ).one()
    binding.status = "disabled"
    binding.row_version += 1
    db.add(binding)
    db.commit()
    with pytest.raises(PublicationError) as stale:
        service.review(
            submitted.id,
            command="approve",
            command_id="agent-stale-review",
            expected_request_row_version=submitted.row_version,
            expected_attention_revision=0,
            reviewer=admin,
            comment=None,
        )
    assert stale.value.code == "PUBLICATION_SNAPSHOT_STALE"


def test_release_unpublish_preserves_existing_use_but_security_revoke_fails_closed() -> None:
    """证明普通下架只停止发现/采用，安全撤销则让既有跨用户 Binding 立即失效。"""

    db, service, admin, owner, adopter, adopter_agent, skill = _context()
    submitted = service.submit("general_skill", skill.id, skill.row_version, owner)
    service.review(
        submitted.id,
        command="approve",
        command_id="release-transition-review",
        expected_request_row_version=submitted.row_version,
        expected_attention_revision=0,
        reviewer=admin,
        comment=None,
    )
    release = db.exec(select(PublicationRelease)).one()
    service.adopt(release.id, adopter_agent.id, "transition-adopt", adopter)
    assert len(EffectiveGeneralSkillResolver(db).resolve(adopter, adopter_agent.id).items) == 1
    service.transition_release(
        release.id,
        command="unpublish",
        command_id="release-unpublish-once",
        expected_row_version=release.row_version,
        actor=admin,
        reason="普通版本下架",
    )
    replay = service.transition_release(
        release.id,
        command="unpublish",
        command_id="release-unpublish-once",
        expected_row_version=release.row_version,
        actor=admin,
        reason="普通版本下架",
    )
    assert replay.status == "unpublished"
    assert service.list_releases(owner.tenant_id) == []
    assert len(EffectiveGeneralSkillResolver(db).resolve(adopter, adopter_agent.id).items) == 1

    skill.row_version += 1
    db.add(skill)
    db.commit()
    replacement = service.submit("general_skill", skill.id, skill.row_version, owner)
    service.review(
        replacement.id,
        command="approve",
        command_id="release-security-review",
        expected_request_row_version=replacement.row_version,
        expected_attention_revision=0,
        reviewer=admin,
        comment=None,
    )
    active = db.exec(
        select(PublicationRelease).where(PublicationRelease.status == "active")
    ).one()
    service.adopt(active.id, adopter_agent.id, "security-adopt", adopter)
    service.transition_release(
        active.id,
        command="security_revoke",
        command_id="release-security-revoke-once",
        expected_row_version=active.row_version,
        actor=admin,
        reason="供应链安全事件",
    )
    assert EffectiveGeneralSkillResolver(db).resolve(adopter, adopter_agent.id).items == ()
