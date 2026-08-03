"""
@Time       : 2026/07/29 15:30
@Author     : zhanglp8181
@File       : test_resource_detail_visibility.py
@CallChain  : 资源详情 API → 用户与 Agent 访问交集 → 版本和文档读取
@Description: 验证私有资源详情、列表、任务和历史版本均遵守统一访问范围。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_knowledge_base_version,
    ensure_private_resource_binding,
)
from app.api.knowledge import get_document, get_document_buckets, get_job, list_documents, list_jobs
from app.api.knowledge_bases import get_knowledge_base, list_knowledge_base_versions
from app.api.skills import get_skill, get_skill_version, list_skill_versions
from app.db.models import (
    AgentProfile,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Skill,
    Tenant,
    User,
)


def test_private_skill_detail_and_versions_require_the_bound_agent_scope() -> None:
    with _test_session() as db:
        owner_agent, other_agent = _seed_private_scope(db)
        skill = Skill(
            id="skill_private",
            tenant_id="tenant_demo",
            skill_id="private_flow",
            version="1.0.0",
            name="私有流程",
            status="published",
            content_json=_skill_content("private_flow"),
        )
        db.add(skill)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", owner_agent.id, "skill", skill.id)
        db.commit()

        for scope in (None, other_agent.id):
            with pytest.raises(HTTPException) as detail_error:
                get_skill(skill.skill_id, "tenant_demo", scope, db)
            assert detail_error.value.status_code == 404
            with pytest.raises(HTTPException) as versions_error:
                list_skill_versions(skill.skill_id, "tenant_demo", db, scope)
            assert versions_error.value.status_code == 404

        detail = get_skill(skill.skill_id, "tenant_demo", owner_agent.id, db)
        versions = list_skill_versions(skill.skill_id, "tenant_demo", db, owner_agent.id)
        assert detail.skill_id == skill.skill_id
        assert versions
        version = get_skill_version(skill.skill_id, versions[0].version, "tenant_demo", owner_agent.id, db)
        assert version.skill_id == skill.skill_id


def test_private_knowledge_details_documents_and_jobs_require_the_bound_agent_scope() -> None:
    """知识正文必须同时满足所有者身份与数字员工绑定范围。"""

    with _test_session() as db:
        owner_agent, other_agent = _seed_private_scope(db)
        owner = db.get(User, "user_owner")
        other = db.get(User, "user_other")
        assert owner is not None
        assert other is not None
        knowledge_base = KnowledgeBase(
            id="kb_private",
            tenant_id="tenant_demo",
            name="私有知识库",
            owner_user_id=owner.id,
            access_scope="owner",
        )
        db.add(knowledge_base)
        db.flush()
        branch = ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            owner_agent.id,
            knowledge_base,
        )
        db.flush()
        version = ensure_knowledge_base_version(db, knowledge_base, branch.head_version)
        document = KnowledgeDocument(
            id="kdoc_private",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename="private.md",
            file_type="md",
            status="ready",
        )
        job = KnowledgeIngestJob(
            id="kjob_private",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=document.id,
            filename=document.filename,
            status="succeeded",
        )
        db.add(document)
        db.add(job)
        db.commit()

        for scope, viewer in ((other_agent.id, owner), (owner_agent.id, other)):
            with pytest.raises(HTTPException) as detail_error:
                get_knowledge_base(
                    knowledge_base.id, "tenant_demo", scope, False, db, viewer
                )
            assert detail_error.value.status_code == 404
            with pytest.raises(HTTPException) as versions_error:
                list_knowledge_base_versions(
                    knowledge_base.id, "tenant_demo", scope, db, viewer
                )
            assert versions_error.value.status_code == 404
            with pytest.raises(HTTPException) as document_error:
                get_document(document.id, "tenant_demo", scope, db, viewer)
            assert document_error.value.status_code == 404
            with pytest.raises(HTTPException) as job_error:
                get_job(job.id, "tenant_demo", scope, db, viewer)
            assert job_error.value.status_code == 404
            assert list_documents("tenant_demo", None, scope, True, db, viewer) == []
            assert list_jobs("tenant_demo", scope, None, 8, db, viewer) == []

        assert (
            get_knowledge_base(
                knowledge_base.id, "tenant_demo", owner_agent.id, False, db, owner
            ).id
            == knowledge_base.id
        )
        assert list_knowledge_base_versions(
            knowledge_base.id, "tenant_demo", owner_agent.id, db, owner
        )
        assert get_document(document.id, "tenant_demo", owner_agent.id, db, owner).id == document.id
        assert get_job(job.id, "tenant_demo", owner_agent.id, db, owner).id == job.id
        assert [
            row.id
            for row in list_documents(
                "tenant_demo", None, owner_agent.id, True, db, owner
            )
        ] == [document.id]
        assert [
            row.id
            for row in list_jobs(
                "tenant_demo", owner_agent.id, None, 8, db, owner
            )
        ] == [job.id]


def test_visible_knowledge_history_has_consistent_list_and_detail_access() -> None:
    """Agent 范围只暴露当前分支版本，历史正文不因列表参数而泄漏。"""

    with _test_session() as db:
        owner_agent, _ = _seed_private_scope(db)
        owner = db.get(User, "user_owner")
        assert owner is not None
        knowledge_base = KnowledgeBase(
            id="kb_versioned",
            tenant_id="tenant_demo",
            name="版本知识库",
            owner_user_id=owner.id,
            access_scope="owner",
        )
        db.add(knowledge_base)
        db.flush()
        branch = ensure_agent_private_knowledge_branch(db, "tenant_demo", owner_agent.id, knowledge_base)
        db.flush()
        historical_version = ensure_knowledge_base_version(db, knowledge_base, branch.head_version)
        branch.base_version = branch.head_version
        branch.head_version = f"{branch.head_version}.next"
        db.add(branch)
        current_version = ensure_knowledge_base_version(db, knowledge_base, branch.head_version)
        historical_document = KnowledgeDocument(
            id="kdoc_historical",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=historical_version.id,
            filename="historical.md",
            file_type="md",
            status="ready",
        )
        current_document = KnowledgeDocument(
            id="kdoc_current",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=current_version.id,
            filename="current.md",
            file_type="md",
            status="ready",
        )
        db.add(historical_document)
        db.add(current_document)
        db.commit()

        current_rows = list_documents(
            "tenant_demo", knowledge_base.id, owner_agent.id, False, db, owner
        )
        history_rows = list_documents(
            "tenant_demo", knowledge_base.id, owner_agent.id, True, db, owner
        )

        assert [row.id for row in current_rows] == [current_document.id]
        assert [row.id for row in history_rows] == [current_document.id]
        with pytest.raises(HTTPException) as document_error:
            get_document(historical_document.id, "tenant_demo", owner_agent.id, db, owner)
        assert document_error.value.status_code == 404
        with pytest.raises(HTTPException) as buckets_error:
            get_document_buckets(
                historical_document.id, "tenant_demo", owner_agent.id, db, owner
            )
        assert buckets_error.value.status_code == 404


def _seed_private_scope(db: Session) -> tuple[AgentProfile, AgentProfile]:
    """创建两个不同所有者及其私有数字员工，用于交叉权限回归。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        password_hash="x",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        password_hash="x",
    )
    owner_agent = AgentProfile(
        id="agent_owner",
        tenant_id="tenant_demo",
        name="Owner agent",
        metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
    )
    other_agent = AgentProfile(
        id="agent_other",
        tenant_id="tenant_demo",
        name="Other agent",
        metadata_json={"owner_user_id": other.id, "owner_username": other.username},
    )
    db.add(owner)
    db.add(other)
    db.add(owner_agent)
    db.add(other_agent)
    db.commit()
    return owner_agent, other_agent


def _skill_content(skill_id: str) -> dict[str, object]:
    return {
        "skill_id": skill_id,
        "name": "私有流程",
        "version": "1.0.0",
        "nodes": [
            {
                "node_id": "reply",
                "type": "response",
                "name": "回复",
                "instruction": "回复用户",
                "allowed_actions": ["answer_user"],
            }
        ],
        "edges": [],
        "start_node_id": "reply",
        "terminal_node_ids": ["reply"],
    }


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
