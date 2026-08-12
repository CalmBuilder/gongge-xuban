"""
@Time       : 2026/08/12 11:40
@Author     : zhanglp8181
@File       : test_general_skill_g1_library.py
@CallChain  : pytest → GeneralSkillLibraryService → private Skill/Revision/Binding/Audit
@Description: 验证本人 Skill 库隔离及多 Agent 装配的 preview、原子性、CAS 和幂等闭环。
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillBindingBatchCommand,
    GeneralSkillRevision,
    ManagementAuditLog,
    Tenant,
    User,
)
from app.general_skills.library import GeneralSkillLibraryError, GeneralSkillLibraryService
from app.general_skills.library_schema import (
    GeneralSkillBindingBatchCommitRequest,
    GeneralSkillBindingBatchPreviewRequest,
    GeneralSkillBindingBatchTarget,
)


def _context() -> tuple[Session, User, User, GeneralSkill, GeneralSkillRevision]:
    """建立两用户、三 Agent 和一个仅属于首用户的私有 Skill。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = User(
        id="user_g1_owner",
        tenant_id="tenant_g1_library",
        username="g1-owner",
        role="member",
        password_hash="unused",
    )
    other = User(
        id="user_g1_other",
        tenant_id=owner.tenant_id,
        username="g1-other",
        role="member",
        password_hash="unused",
    )
    db.add(Tenant(id=owner.tenant_id, name="G1 Library"))
    db.add(owner)
    db.add(other)
    for agent_id, name, user_id in (
        ("agent_g1_a", "A 文档规范分身", owner.id),
        ("agent_g1_b", "A 故障诊断分身", owner.id),
        ("agent_g1_other", "B 私有分身", other.id),
    ):
        db.add(
            AgentProfile(
                id=agent_id,
                tenant_id=owner.tenant_id,
                name=name,
                owner_user_id=user_id,
            )
        )
    skill = GeneralSkill(
        id="genskill_g1_a",
        tenant_id=owner.tenant_id,
        slug="writing-for-agents",
        name="writing-for-agents",
        skill_markdown="# Writing for agents",
        status="published",
        usage_mode="planning_guidance",
        owner_user_id=owner.id,
        visibility_scope="user_private",
    )
    db.add(skill)
    db.flush()
    revision = GeneralSkillRevision(
        id="gsrev_g1_a",
        tenant_id=owner.tenant_id,
        skill_id=skill.id,
        revision_number=1,
        content_checksum=hashlib.sha256(b"g1-a-content").hexdigest(),
        manifest_checksum=hashlib.sha256(b"g1-a-manifest").hexdigest(),
        normalized_skill_markdown="# Writing for agents",
        source_snapshot_json={"source_kind": "github"},
        status="published",
        created_by=owner.id,
    )
    db.add(revision)
    db.flush()
    skill.current_published_revision_id = revision.id
    db.add(skill)
    db.commit()
    return db, owner, other, skill, revision


def _target(agent_id: str, revision_id: str, version: int | None = None) -> GeneralSkillBindingBatchTarget:
    """构造 pinned 且允许模型选择的目标装配请求。"""

    return GeneralSkillBindingBatchTarget(
        agent_id=agent_id,
        pinned_revision_id=revision_id,
        expected_binding_row_version=version,
    )


def test_my_library_is_owner_only_and_lists_all_owned_agent_bindings() -> None:
    """证明本人库不按当前 Agent 截断，其他用户也无法枚举私有 Skill。"""

    db, owner, other, skill, revision = _context()
    service = GeneralSkillLibraryService(db)
    preview = service.preview(
        GeneralSkillBindingBatchPreviewRequest(
            skill_id=skill.id,
            targets=[_target("agent_g1_a", revision.id), _target("agent_g1_b", revision.id)],
        ),
        owner,
    )
    service.commit(
        GeneralSkillBindingBatchCommitRequest(
            skill_id=skill.id,
            targets=[_target("agent_g1_a", revision.id), _target("agent_g1_b", revision.id)],
            preview_checksum=preview.preview_checksum,
        ),
        idempotency_key="g1-library-two-agents",
        current_user=owner,
    )

    rows = service.list_owned(owner)
    assert len(rows) == 1
    assert rows[0].current_revision_id == revision.id
    assert {row.agent_id for row in rows[0].bindings} == {"agent_g1_a", "agent_g1_b"}
    assert service.list_owned(other) == []


def test_batch_commit_is_atomic_on_foreign_agent_and_stale_preview() -> None:
    """证明越权目标或 preview 后事实变化均整批零写入。"""

    db, owner, _, skill, revision = _context()
    service = GeneralSkillLibraryService(db)
    with pytest.raises(GeneralSkillLibraryError, match="target agent") as forbidden:
        service.preview(
            GeneralSkillBindingBatchPreviewRequest(
                skill_id=skill.id,
                targets=[
                    _target("agent_g1_a", revision.id),
                    _target("agent_g1_other", revision.id),
                ],
            ),
            owner,
        )
    assert forbidden.value.code == "GENERAL_SKILL_FORBIDDEN"
    assert db.exec(select(AgentResourceBinding)).all() == []

    request = GeneralSkillBindingBatchPreviewRequest(
        skill_id=skill.id,
        targets=[_target("agent_g1_a", revision.id), _target("agent_g1_b", revision.id)],
    )
    preview = service.preview(request, owner)
    revision.row_version += 1
    db.add(revision)
    db.commit()
    with pytest.raises(GeneralSkillLibraryError, match="stale") as stale:
        service.commit(
            GeneralSkillBindingBatchCommitRequest(
                **request.model_dump(), preview_checksum=preview.preview_checksum
            ),
            idempotency_key="g1-library-stale",
            current_user=owner,
        )
    assert stale.value.code == "GENERAL_SKILL_PREVIEW_STALE"
    assert db.exec(select(AgentResourceBinding)).all() == []


def test_batch_commit_replays_same_result_and_rejects_key_reuse() -> None:
    """证明相同命令重放不重复审计或绑定，不同请求复用键则拒绝。"""

    db, owner, _, skill, revision = _context()
    service = GeneralSkillLibraryService(db)
    preview_request = GeneralSkillBindingBatchPreviewRequest(
        skill_id=skill.id,
        targets=[_target("agent_g1_a", revision.id)],
    )
    preview = service.preview(preview_request, owner)
    commit_request = GeneralSkillBindingBatchCommitRequest(
        **preview_request.model_dump(), preview_checksum=preview.preview_checksum
    )
    first = service.commit(
        commit_request,
        idempotency_key="g1-library-replay",
        current_user=owner,
    )
    replay = service.commit(
        commit_request,
        idempotency_key="g1-library-replay",
        current_user=owner,
    )
    assert replay.command_id == first.command_id
    assert replay.replayed is True
    assert len(db.exec(select(GeneralSkillBindingBatchCommand)).all()) == 1
    assert len(db.exec(select(AgentResourceBinding)).all()) == 1
    assert len(db.exec(select(ManagementAuditLog)).all()) == 1

    other_preview = service.preview(
        GeneralSkillBindingBatchPreviewRequest(
            skill_id=skill.id,
            targets=[_target("agent_g1_b", revision.id)],
        ),
        owner,
    )
    with pytest.raises(GeneralSkillLibraryError) as conflict:
        service.commit(
            GeneralSkillBindingBatchCommitRequest(
                skill_id=skill.id,
                targets=[_target("agent_g1_b", revision.id)],
                preview_checksum=other_preview.preview_checksum,
            ),
            idempotency_key="g1-library-replay",
            current_user=owner,
        )
    assert conflict.value.code == "GENERAL_SKILL_IDEMPOTENCY_CONFLICT"
