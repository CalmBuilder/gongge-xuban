"""
@Time       : 2026/08/13 20:10
@Author     : zhanglp8181
@File       : test_general_skill_g1_remote_proposals.py
@CallChain  : DynamicTask proposal → ImportJob preview → owner Attention → confirm/cancel
@Description: 验证 C1 固定 GitHub Skill 提案在批准前零安装，批准后按冻结预览安装，拒绝则释放。
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillImportJob,
    GeneralSkillProposal,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    Tenant,
    User,
)
from app.dynamic_tasks.artifacts import ArtifactService
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.proposals import (
    GeneralSkillProposalError,
    GeneralSkillProposalService,
    SKILL_PROPOSAL_TOOL_NAME,
)
from app.general_skills.remote_source import RemoteFetchResult


FIXED_COMMIT = "84fdeffd12f2ee307994d1eb6feb48173b6e0502"


class _Fetcher:
    """返回固定 TDD Skill 包并记录正式 archive URL。"""

    def fetch(self, source_url: str, **_: object) -> RemoteFetchResult:
        """构造只包含目标目录的确定性 GitHub archive 响应。"""

        assert source_url.endswith(f"{FIXED_COMMIT}.zip")
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(
                "skills-fixed/skills/engineering/tdd/SKILL.md",
                "---\nname: tdd\ndescription: Work test-first.\n---\n"
                "# Test-Driven Development\nWrite the failing test before implementation.\n",
            )
        return RemoteFetchResult(source_url, buffer.getvalue(), 0)


def _context(tmp_path: Path):
    """建立本人 Agent、动态 Execution、冻结 Operation 与隔离对象存储。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    tenant = Tenant(id="tenant_g1_c1", name="G1 C1")
    owner = User(
        id="user_g1_c1",
        tenant_id=tenant.id,
        username="owner",
        role="member",
        password_hash="unused",
    )
    agent = AgentProfile(
        id="agent_g1_c1",
        tenant_id=tenant.id,
        name="测试工程分身",
        owner_user_id=owner.id,
    )
    instance = SopInstance(
        id="execution_g1_c1",
        tenant_id=tenant.id,
        session_id="session_g1_c1",
        kind="dynamic_task",
        active_slot_key="dynamic:session_g1_c1",
        initiator_user_id=owner.id,
        agent_id=agent.id,
        goal_snapshot_json={"goal": "建议安装固定 TDD Skill"},
        current_plan_revision_id="plan_g1_c1",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"tools": []},
        status="waiting",
    )
    step = SopNodeExecution(
        id="node_g1_c1",
        tenant_id=tenant.id,
        instance_id=instance.id,
        node_id="propose_remote_skill",
        step_key="propose_remote_skill",
        plan_revision_id="plan_g1_c1",
        step_kind="tool.write",
        status="running",
    )
    arguments: dict[str, object] = {
        "proposal_kind": "remote_import",
        "source_url": "https://github.com/mattpocock/skills",
        "revision": FIXED_COMMIT,
        "source_subpath": "skills/engineering/tdd",
    }
    operation = SopOperation(
        id="sopop_g1_c1",
        tenant_id=tenant.id,
        instance_id=instance.id,
        node_execution_id=step.id,
        operation_name=SKILL_PROPOSAL_TOOL_NAME,
        idempotency_key="g1-c1-operation",
        logical_action_id="g1-c1-proposal",
        request_fingerprint=hashlib.sha256(b"g1-c1").hexdigest(),
        effect_kind="local_write",
        request_json=arguments,
        status="prepared",
    )
    db.add(tenant)
    db.add(owner)
    db.add(agent)
    db.add(instance)
    db.add(step)
    db.add(operation)
    db.commit()
    service = GeneralSkillProposalService(
        db,
        object_store=FileSystemSkillObjectStore(tmp_path / "objects"),
        artifact_service=ArtifactService(db, storage_root=tmp_path / "artifacts"),
    )
    return db, service, owner, agent, instance, step, operation, arguments


def _approve(
    db: Session,
    service: GeneralSkillProposalService,
    proposal: GeneralSkillProposal,
    instance: SopInstance,
    step: SopNodeExecution,
    operation: SopOperation,
) -> None:
    """构造 Runtime 同形的本人 allow_once Attention 证据。"""

    attention = SopWorkItem(
        id="attention_g1_c1",
        tenant_id=instance.tenant_id,
        instance_id=instance.id,
        node_execution_id=step.id,
        attention_kind="publication",
        attention_key="g1-c1:publication",
        attention_identity="g1-c1-attention",
        payload_json={"operation_id": operation.id},
        allowed_commands_json=["allow_once", "deny"],
        resolution_json={"command": "allow_once", "actor_user_id": instance.initiator_user_id},
        status="completed",
        initiator_user_id=instance.initiator_user_id,
        exclude_initiator=False,
    )
    db.add(attention)
    service.mark_awaiting_approval(proposal, attention_id=attention.id)
    operation.status = "running"
    operation.approval_work_item_id = attention.id
    operation.approved_by_user_id = instance.initiator_user_id
    db.add(operation)
    db.commit()


def test_remote_proposal_has_zero_install_before_owner_approval_then_installs_frozen_preview(
    tmp_path: Path,
) -> None:
    """证明 C1 预览不生成 Skill/Binding，本人批准后仅安装固定候选且可幂等重放。"""

    db, service, owner, agent, instance, step, operation, arguments = _context(tmp_path)
    proposal = service.stage(
        instance=instance,
        step=step,
        operation=operation,
        arguments=arguments,
        reviewer_user_ids=[owner.id],
        remote_fetcher=_Fetcher(),
    )
    payload = service.review_payload(proposal)
    assert proposal.proposal_kind == "remote_import"
    assert payload["source_reference_redacted"] == (
        f"https://github.com/mattpocock/skills@{FIXED_COMMIT}#skills/engineering/tdd"
    )
    assert payload["candidates"][0]["name"] == "tdd"
    assert db.exec(select(GeneralSkill)).all() == []
    assert db.exec(select(AgentResourceBinding)).all() == []

    _approve(db, service, proposal, instance, step, operation)
    first = service.publish_approved_operation(
        tenant_id=instance.tenant_id,
        execution_id=instance.id,
        operation_id=operation.id,
        initiator_user_id=owner.id,
    )
    second = service.publish_approved_operation(
        tenant_id=instance.tenant_id,
        execution_id=instance.id,
        operation_id=operation.id,
        initiator_user_id=owner.id,
    )
    assert first == second
    assert first["status"] == "published"
    assert first["skill_id"]
    assert first["revision_id"]
    binding = db.exec(select(AgentResourceBinding)).one()
    assert binding.agent_id == agent.id
    assert binding.metadata_json["pinned_revision_id"] == first["revision_id"]


@pytest.mark.parametrize("outcome", ["rejected", "expired", "failed"])
def test_remote_proposal_terminal_outcomes_cancel_job_and_never_install(
    tmp_path: Path,
    outcome: str,
) -> None:
    """证明拒绝、超时和失败均取消同一 ImportJob、释放暂存且不产生可消费能力。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    proposal = service.stage(
        instance=instance,
        step=step,
        operation=operation,
        arguments=arguments,
        reviewer_user_ids=[owner.id],
        remote_fetcher=_Fetcher(),
    )
    service.terminate(
        tenant_id=instance.tenant_id,
        operation_id=operation.id,
        outcome=outcome,  # type: ignore[arg-type]
        error_code=f"G1_C1_{outcome.upper()}",
    )
    db.commit()
    job = db.get(GeneralSkillImportJob, proposal.import_job_id)
    assert job is not None and job.status == "cancelled"
    assert db.exec(select(GeneralSkill)).all() == []
    assert db.exec(select(AgentResourceBinding)).all() == []
    assert not (tmp_path / "objects" / "staging" / job.id).exists()


def test_remote_proposal_rejects_changed_preview_before_install(tmp_path: Path) -> None:
    """证明审批期间 preview checksum 被篡改时 fail closed，不能安装另一份内容。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    proposal = service.stage(
        instance=instance,
        step=step,
        operation=operation,
        arguments=arguments,
        reviewer_user_ids=[owner.id],
        remote_fetcher=_Fetcher(),
    )
    _approve(db, service, proposal, instance, step, operation)
    job = db.get(GeneralSkillImportJob, proposal.import_job_id)
    assert job is not None
    job.preview_checksum = "f" * 64
    db.add(job)
    db.commit()
    with pytest.raises(GeneralSkillProposalError) as changed:
        service.publish_approved_operation(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            operation_id=operation.id,
            initiator_user_id=owner.id,
        )
    assert changed.value.code == "GENERAL_SKILL_PROPOSAL_PREVIEW_CHANGED"
    assert db.exec(select(GeneralSkill)).all() == []
