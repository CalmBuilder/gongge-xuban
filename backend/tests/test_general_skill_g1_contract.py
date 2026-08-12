"""
@Time       : 2026/08/13 16:20
@Author     : zhanglp8181
@File       : test_general_skill_g1_contract.py
@CallChain  : pytest → SQLModel metadata → G1 proposal/publication constraints
@Description: 冻结 C1/C2 判别联合与 Skill/Agent 类型化发布聚合的数据库契约。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    GeneralSkillProposal,
    GeneralSkillPublicationRevision,
    PublicationRelease,
    ResourcePublicationRequest,
)


def _session() -> Session:
    """建立启用约束的内存 SQLite 会话，隔离验证 G1 元模型。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _proposal(**overrides: object) -> GeneralSkillProposal:
    """构造一个合法 authored 提案，并允许测试覆盖分型字段。"""

    values: dict[str, object] = {
        "id": "gsproposal_g1",
        "tenant_id": "tenant_g1",
        "execution_id": "execution_g1",
        "session_id": "session_g1",
        "agent_id": "agent_g1",
        "initiator_user_id": "user_g1",
        "operation_id": "operation_g1",
        "proposal_kind": "authored",
        "skill_id": "skill_g1",
        "revision_id": "revision_g1",
        "review_artifact_id": "artifact_g1",
        "proposal_checksum": "a" * 64,
    }
    values.update(overrides)
    return GeneralSkillProposal(**values)


def test_agent_skill_proposal_discriminates_authored_and_remote_import() -> None:
    """证明两个提案分支只能携带各自字段，不能混合或以空字符串绕过。"""

    db = _session()
    try:
        db.add(_proposal())
        db.commit()
        db.add(
            _proposal(
                id="gsproposal_remote",
                operation_id="operation_remote",
                proposal_kind="remote_import",
                skill_id=None,
                revision_id=None,
                review_artifact_id="artifact_remote",
                import_job_id="gsjob_remote",
                preview_checksum="b" * 64,
                remote_candidate_ids_json=["candidate_tdd"],
                proposal_checksum="c" * 64,
            )
        )
        db.commit()
        invalid_rows = [
            _proposal(id="bad_mixed", operation_id="bad_mixed", import_job_id="gsjob_bad"),
            _proposal(
                id="bad_remote",
                operation_id="bad_remote",
                proposal_kind="remote_import",
                skill_id=None,
                revision_id=None,
                import_job_id=None,
                preview_checksum="d" * 64,
                remote_candidate_ids_json=["candidate"],
            ),
        ]
        for row in invalid_rows:
            db.add(row)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
    finally:
        db.close()


def test_publication_request_requires_matching_snapshot_kind() -> None:
    """证明数据库拒绝资源类型与快照类型错配，跨表匹配留给事务服务复核。"""

    db = _session()
    now = datetime.now(timezone.utc)
    try:
        request = ResourcePublicationRequest(
            id="pubreq_skill",
            tenant_id="tenant_g1",
            owner_user_id="user_g1",
            resource_type="general_skill",
            resource_id="skill_g1",
            snapshot_kind="general_skill",
            snapshot_id="gspubrev_skill",
            snapshot_checksum="a" * 64,
            active_slot_key="active",
            status="submitted",
            created_at=now,
            updated_at=now,
        )
        snapshot = GeneralSkillPublicationRevision(
            id="gspubrev_skill",
            tenant_id="tenant_g1",
            request_id=request.id,
            skill_id="skill_g1",
            approved_revision_id="gsrev_g1",
            content_checksum="b" * 64,
            manifest_checksum="c" * 64,
            snapshot_checksum="a" * 64,
        )
        db.add(request)
        db.add(snapshot)
        db.commit()
        db.add(
            ResourcePublicationRequest(
                id="pubreq_bad_kind",
                tenant_id="tenant_g1",
                owner_user_id="user_g1",
                resource_type="general_skill",
                resource_id="skill_other",
                snapshot_kind="agent",
                snapshot_id="agentpubrev_bad",
                snapshot_checksum="e" * 64,
                active_slot_key="active",
                status="submitted",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_publication_active_slot_and_release_states_are_unique() -> None:
    """证明同一资源只有一个活动申请与一个 Release，终态历史仍可保留。"""

    db = _session()
    try:
        first = ResourcePublicationRequest(
            id="pubreq_first",
            tenant_id="tenant_g1",
            owner_user_id="user_g1",
            resource_type="agent",
            resource_id="agent_g1",
            snapshot_kind="agent",
            snapshot_id="agentpubrev_first",
            snapshot_checksum="a" * 64,
            active_slot_key="active",
            status="submitted",
        )
        db.add(first)
        db.commit()
        db.add(
            ResourcePublicationRequest(
                id="pubreq_second",
                tenant_id="tenant_g1",
                owner_user_id="user_g1",
                resource_type="agent",
                resource_id="agent_g1",
                snapshot_kind="agent",
                snapshot_id="agentpubrev_second",
                snapshot_checksum="b" * 64,
                active_slot_key="active",
                status="submitted",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(
            PublicationRelease(
                id="pubrel_first",
                tenant_id="tenant_g1",
                approved_request_id=first.id,
                resource_type="agent",
                resource_id="agent_g1",
                snapshot_kind="agent",
                snapshot_id="agentpubrev_first",
                snapshot_checksum="a" * 64,
            )
        )
        db.commit()
        db.add(
            PublicationRelease(
                id="pubrel_second",
                tenant_id="tenant_g1",
                approved_request_id="pubreq_other",
                resource_type="agent",
                resource_id="agent_g1",
                snapshot_kind="agent",
                snapshot_id="agentpubrev_other",
                snapshot_checksum="c" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()
