"""
@Time       : 2026/08/13 09:10
@Author     : zhanglp8181
@File       : test_general_skill_s5_proposals.py
@CallChain  : pytest → GeneralSkillProposalService → Artifact/Revision/Binding resolver
@Description: 验证 Agent 提案在批准前不可见、批准后发布绑定及拒绝/越权附件终态。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ExecutionArtifact,
    GeneralSkill,
    GeneralSkillRevision,
    ExecutionSignal,
    ModelConfig,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    Tenant,
    User,
)
from app.dynamic_tasks.artifacts import ArtifactService
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import DynamicCapabilityCatalog, capability_checksum
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.config import get_settings
from app.general_skills.eligibility import EffectiveGeneralSkillResolver
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.proposals import (
    GeneralSkillProposalError,
    GeneralSkillProposalService,
    SKILL_PROPOSAL_TOOL_NAME,
)
from app.scheduled_tasks.worker import process_expired_work_item
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore


class _SkillProposalActionProposer:
    """为完整动态链返回一次 Skill 提案和最终回答。"""

    def propose(self, *, view, step: PlanStep) -> CompletedProviderProposal:
        """按冻结步骤生成严格参数，不直接调用提案服务或数据库。"""

        if step.kind == "tool.write":
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref=SKILL_PROPOSAL_TOOL_NAME,
                arguments={
                    "name": "refund-retrospective",
                    "description": "在退款争议处理后形成可复用复盘。",
                    "instructions": "先按时间线汇总事实，再区分流程缺陷与执行偏差。",
                    "requested_tools": [],
                    "files": [],
                },
                rationale="把对话中完成且已验证的方法提交本人审核",
            )
        else:
            completed = [item["step_key"] for item in view.execution_context["completed_steps"]]
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "Skill 已经本人审核并绑定当前分身。",
                    "criterion_evidence": {"published": completed},
                    "pending_questions": [],
                },
                rationale="依据发布 Operation 回执形成结果",
            )
        return CompletedProviderProposal(
            response_id=f"response-{step.step_key}",
            finish_reason="stop",
            proposal=proposal,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _context(tmp_path: Path):
    """建立拥有分身的成员、运行中 Execution、写步骤和 prepared 提案 Operation。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    tenant = Tenant(id="tenant_proposal", name="Proposal tenant")
    owner = User(
        id="user_proposal",
        tenant_id=tenant.id,
        username="proposal-owner",
        password_hash="unused",
        role="member",
    )
    agent = AgentProfile(
        id="agent_proposal",
        tenant_id=tenant.id,
        name="售后复盘分身",
        owner_user_id=owner.id,
        status="active",
    )
    instance = SopInstance(
        id="execution_proposal",
        tenant_id=tenant.id,
        session_id="session_proposal",
        kind="dynamic_task",
        active_slot_key="dynamic:session_proposal",
        initiator_user_id=owner.id,
        agent_id=agent.id,
        goal_snapshot_json={"goal": "把已验证的售后复盘方法保存为 Skill"},
        current_plan_revision_id="plan_proposal",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"tools": []},
        status="waiting",
    )
    step = SopNodeExecution(
        id="node_proposal",
        tenant_id=tenant.id,
        instance_id=instance.id,
        node_id="save_skill",
        step_key="save_skill",
        plan_revision_id="plan_proposal",
        step_kind="tool.write",
        status="running",
    )
    arguments = {
        "name": "refund-retrospective",
        "description": "在退款争议处理后形成可复用复盘。",
        "instructions": "先按时间线汇总事实，再区分流程缺陷与执行偏差，最后输出行动项。",
        "requested_tools": [],
        "files": [],
    }
    operation = SopOperation(
        id="sopop_proposal",
        tenant_id=tenant.id,
        instance_id=instance.id,
        node_execution_id=step.id,
        operation_name=SKILL_PROPOSAL_TOOL_NAME,
        idempotency_key="proposal-idempotency",
        logical_action_id="proposal-action",
        request_fingerprint=hashlib.sha256(b"proposal").hexdigest(),
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
        object_store=FileSystemSkillObjectStore(tmp_path / "skill-objects"),
        artifact_service=ArtifactService(db, storage_root=tmp_path / "artifacts"),
    )
    return db, service, owner, agent, instance, step, operation, arguments


def _approve(
    db: Session,
    service: GeneralSkillProposalService,
    instance: SopInstance,
    step: SopNodeExecution,
    operation: SopOperation,
    arguments: dict[str, object],
):
    """暂存提案并构造与 Runtime 相同的已完成 publication Attention 证据。"""

    proposal = service.stage(
        instance=instance,
        step=step,
        operation=operation,
        arguments=arguments,
        reviewer_user_ids=[instance.initiator_user_id],
    )
    attention = SopWorkItem(
        id="attention_proposal",
        tenant_id=instance.tenant_id,
        instance_id=instance.id,
        node_execution_id=step.id,
        attention_kind="publication",
        attention_key="save_skill:publication",
        attention_identity="proposal-attention-identity",
        payload_json={"operation_id": operation.id},
        allowed_commands_json=["allow_once", "deny"],
        resolution_json={
            "command": "allow_once",
            "actor_user_id": instance.initiator_user_id,
        },
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
    return proposal, attention


def test_agent_skill_proposal_is_invisible_until_approved_then_bound_once(tmp_path: Path) -> None:
    """证明 reviewing 修订不进目录，批准后才发布 user_only pinned 绑定且重放不重复。"""

    db, service, owner, agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        proposal = service.stage(
            instance=instance,
            step=step,
            operation=operation,
            arguments=arguments,
            reviewer_user_ids=[owner.id],
        )
        skill = db.get(GeneralSkill, proposal.skill_id)
        revision = db.get(GeneralSkillRevision, proposal.revision_id)
        artifact = db.get(ExecutionArtifact, proposal.review_artifact_id)
        staging_dir = tmp_path / "skill-objects" / "staging" / proposal.revision_id
        assert skill is not None and skill.status == "draft"
        assert revision is not None and revision.status == "reviewing"
        assert artifact is not None and artifact.acl_json["user_ids"] == [owner.id]
        assert staging_dir.is_dir()
        assert EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items == ()

        proposal, _ = _approve(db, service, instance, step, operation, arguments)
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
        db.commit()
        assert first == second
        catalog = EffectiveGeneralSkillResolver(db).resolve(owner, agent.id)
        assert len(catalog.items) == 1
        assert catalog.items[0].revision_id == proposal.revision_id
        assert catalog.items[0].invocation_policy == "user_only"
        assert not staging_dir.exists()
    finally:
        db.close()


def test_approved_proposal_rechecks_active_owner_before_publication(tmp_path: Path) -> None:
    """Attention 批准后若所有者被停用，恢复派发必须拒绝发布并保持零绑定。"""

    db, service, owner, agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        proposal, _ = _approve(db, service, instance, step, operation, arguments)
        owner.membership_status = "suspended"
        db.add(owner)
        db.commit()

        with pytest.raises(GeneralSkillProposalError) as denied:
            service.publish_approved_operation(
                tenant_id=instance.tenant_id,
                execution_id=instance.id,
                operation_id=operation.id,
                initiator_user_id=owner.id,
            )
        assert denied.value.code == "GENERAL_SKILL_PROPOSAL_ACTOR_DENIED"
        db.refresh(proposal)
        assert proposal.status == "awaiting_approval"
        assert db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
            )
        ).all() == []
    finally:
        db.close()


@pytest.mark.parametrize("outcome", ["rejected", "expired", "failed"])
def test_agent_skill_proposal_terminal_outcomes_never_publish(
    tmp_path: Path,
    outcome: Literal["rejected", "expired", "failed"],
) -> None:
    """验证拒绝、超时和失败都终止 reviewing revision，且重复终止保持同一状态。"""

    db, service, owner, agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        proposal = service.stage(
            instance=instance,
            step=step,
            operation=operation,
            arguments=arguments,
            reviewer_user_ids=[owner.id],
        )
        service.terminate(
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            outcome=outcome,
            error_code=f"PROPOSAL_{outcome.upper()}",
        )
        service.terminate(
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            outcome=outcome,
            error_code="IGNORED_REPLAY",
        )
        db.commit()
        revision = db.get(GeneralSkillRevision, proposal.revision_id)
        skill = db.get(GeneralSkill, proposal.skill_id)
        assert revision is not None and revision.status == "rejected"
        assert skill is not None and skill.status == "draft"
        assert proposal.status == outcome
        assert proposal.error_code == f"PROPOSAL_{outcome.upper()}"
        assert not (tmp_path / "skill-objects" / "staging" / proposal.revision_id).exists()
        assert EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items == ()
    finally:
        db.close()


def test_agent_skill_proposal_rejects_artifact_from_another_execution(tmp_path: Path) -> None:
    """验证即使用户拥有 Artifact，跨 Execution 打包仍在任何 Revision 写入前被拒绝。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        foreign = ExecutionArtifact(
            id="artifact_foreign",
            tenant_id=instance.tenant_id,
            execution_id="execution_foreign",
            source_node_execution_id=step.id,
            source_step_key=step.step_key,
            artifact_key="foreign",
            filename="policy.md",
            mime_type="text/markdown",
            size_bytes=6,
            content_checksum=hashlib.sha256(b"policy").hexdigest(),
            storage_locator="missing",
            acl_json={"user_ids": [owner.id]},
        )
        db.add(foreign)
        db.commit()
        arguments["files"] = [{"artifact_id": foreign.id, "path": "references/policy.md"}]
        with pytest.raises(GeneralSkillProposalError) as caught:
            service.stage(
                instance=instance,
                step=step,
                operation=operation,
                arguments=arguments,
                reviewer_user_ids=[owner.id],
            )
        assert caught.value.code == "GENERAL_SKILL_PROPOSAL_ARTIFACT_INVALID"
    finally:
        db.close()


def test_agent_skill_proposal_cannot_grant_an_unbound_tool(tmp_path: Path) -> None:
    """验证 Skill 声明只能收窄当前分身已有工具，不能借提案完成自授权。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        arguments["requested_tools"] = ["crm.order.refund"]
        with pytest.raises(GeneralSkillProposalError) as caught:
            service.stage(
                instance=instance,
                step=step,
                operation=operation,
                arguments=arguments,
                reviewer_user_ids=[owner.id],
            )
        assert caught.value.code == "GENERAL_SKILL_PROPOSAL_SELF_AUTHORIZATION"
        assert db.exec(select(GeneralSkill)).all() == []
    finally:
        db.close()


def test_agent_skill_proposal_replay_freezes_arguments_and_same_name_does_not_overwrite(
    tmp_path: Path,
) -> None:
    """验证同 Operation 参数漂移被拒绝，另一 Operation 的同名提案创建独立 Skill 根。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        first = service.stage(
            instance=instance,
            step=step,
            operation=operation,
            arguments=arguments,
            reviewer_user_ids=[owner.id],
        )
        changed = {**arguments, "instructions": "被重放篡改的内容"}
        with pytest.raises(GeneralSkillProposalError) as caught:
            service.stage(
                instance=instance,
                step=step,
                operation=operation,
                arguments=changed,
                reviewer_user_ids=[owner.id],
            )
        assert caught.value.code == "GENERAL_SKILL_PROPOSAL_CONFLICT"

        second_operation = SopOperation(
            id="sopop_proposal_second",
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=step.id,
            operation_name=SKILL_PROPOSAL_TOOL_NAME,
            idempotency_key="proposal-second-idempotency",
            logical_action_id="proposal-second-action",
            request_fingerprint=hashlib.sha256(b"proposal-second").hexdigest(),
            effect_kind="local_write",
            request_json=arguments,
            status="prepared",
        )
        db.add(second_operation)
        db.flush()
        second = service.stage(
            instance=instance,
            step=step,
            operation=second_operation,
            arguments=arguments,
            reviewer_user_ids=[owner.id],
        )
        first_skill = db.get(GeneralSkill, first.skill_id)
        second_skill = db.get(GeneralSkill, second.skill_id)
        assert first.skill_id != second.skill_id
        assert first_skill is not None and second_skill is not None
        assert first_skill.name == second_skill.name
        assert first_skill.slug != second_skill.slug
    finally:
        db.close()


def test_agent_skill_proposal_preserves_artifact_path_and_content_after_publication(
    tmp_path: Path,
) -> None:
    """验证同 Execution Artifact 经统一规范化后保留相对路径、校验和与可读内容。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        artifact, _ = service.artifact_service.register(
            instance=instance,
            source_node=step,
            artifact_key="verified-policy",
            filename="policy.md",
            mime_type="text/markdown",
            data=b"# Verified refund policy\n",
        )
        artifact.acl_json = {"user_ids": [owner.id], "scope": "explicit_users"}
        db.add(artifact)
        db.commit()
        arguments["files"] = [
            {"artifact_id": artifact.id, "path": "references/refund/policy.md"}
        ]
        proposal, _ = _approve(db, service, instance, step, operation, arguments)
        service.publish_approved_operation(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            operation_id=operation.id,
            initiator_user_id=owner.id,
        )
        db.commit()

        revision = db.get(GeneralSkillRevision, proposal.revision_id)
        skill = db.get(GeneralSkill, proposal.skill_id)
        assert revision is not None and skill is not None
        manifest = {
            str(row["relative_path"]): row for row in revision.resource_manifest_json
        }
        resource = manifest["references/refund/policy.md"]
        assert resource["content_checksum"] == hashlib.sha256(
            b"# Verified refund policy\n"
        ).hexdigest()
        assert str(resource["object_key"]).startswith("sha256:")
        assert any(
            row["path"] == "references/refund/policy.md"
            and row["content"] == "# Verified refund policy\n"
            for row in skill.skill_files_json
        )
    finally:
        db.close()


def test_agent_skill_update_proposal_rejects_changed_published_base(tmp_path: Path) -> None:
    """验证更新提案冻结基线；审核期间当前发布 Revision 变化后不得覆盖新版本。"""

    db, service, owner, _agent, instance, step, operation, arguments = _context(tmp_path)
    try:
        first, _ = _approve(db, service, instance, step, operation, arguments)
        service.publish_approved_operation(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            operation_id=operation.id,
            initiator_user_id=owner.id,
        )
        published = db.get(GeneralSkillRevision, first.revision_id)
        assert published is not None

        update_step = SopNodeExecution(
            id="node_proposal_update",
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_id="save_skill_update",
            step_key="save_skill_update",
            plan_revision_id=instance.current_plan_revision_id,
            step_kind="tool.write",
            status="running",
        )
        db.add(update_step)
        db.flush()

        update_operation = SopOperation(
            id="sopop_proposal_update",
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=update_step.id,
            operation_name=SKILL_PROPOSAL_TOOL_NAME,
            idempotency_key="proposal-update-idempotency",
            logical_action_id="proposal-update-action",
            request_fingerprint=hashlib.sha256(b"proposal-update").hexdigest(),
            effect_kind="local_write",
            request_json={},
            status="prepared",
        )
        update_arguments = {
            **arguments,
            "instructions": "更新后的退款复盘流程。",
            "target_skill_id": first.skill_id,
        }
        update_operation.request_json = update_arguments
        db.add(update_operation)
        db.flush()
        update = service.stage(
            instance=instance,
            step=update_step,
            operation=update_operation,
            arguments=update_arguments,
            reviewer_user_ids=[owner.id],
        )
        attention = SopWorkItem(
            id="attention_proposal_update",
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=update_step.id,
            attention_kind="publication",
            attention_key="save_skill:update-publication",
            attention_identity="proposal-update-attention",
            payload_json={"operation_id": update_operation.id},
            allowed_commands_json=["allow_once", "deny"],
            resolution_json={"command": "allow_once", "actor_user_id": owner.id},
            status="completed",
            initiator_user_id=owner.id,
            exclude_initiator=False,
        )
        db.add(attention)
        service.mark_awaiting_approval(update, attention_id=attention.id)
        update_operation.status = "running"
        update_operation.approval_work_item_id = attention.id
        update_operation.approved_by_user_id = owner.id
        db.add(update_operation)
        db.commit()

        published.content_checksum = "f" * 64
        db.add(published)
        db.commit()
        with pytest.raises(GeneralSkillProposalError) as changed:
            service.publish_approved_operation(
                tenant_id=instance.tenant_id,
                execution_id=instance.id,
                operation_id=update_operation.id,
                initiator_user_id=owner.id,
            )
        assert changed.value.code == "GENERAL_SKILL_PROPOSAL_BASE_CHANGED"
        skill = db.get(GeneralSkill, first.skill_id)
        assert skill is not None and skill.current_published_revision_id == first.revision_id
    finally:
        db.close()


def test_expired_publication_terminates_proposal_and_execution(tmp_path: Path) -> None:
    """验证调度器先回收提案暂存对象，再把等待节点和 Execution 收敛为超时终态。"""

    db, service, owner, agent, instance, step, operation, arguments = _context(tmp_path)
    settings = get_settings()
    old_store = settings.general_skill_object_store_path
    settings.general_skill_object_store_path = str(tmp_path / "skill-objects")
    try:
        proposal = service.stage(
            instance=instance,
            step=step,
            operation=operation,
            arguments=arguments,
            reviewer_user_ids=[owner.id],
        )
        attention = SopWorkItem(
            id="attention_expired_proposal",
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=step.id,
            attention_kind="publication",
            attention_key="save_skill:expired-publication",
            attention_identity="expired-proposal-attention",
            payload_json={"operation_id": operation.id},
            allowed_commands_json=["allow_once", "deny"],
            status="expired",
            timeout_action="fail",
            initiator_user_id=owner.id,
            exclude_initiator=False,
        )
        db.add(attention)
        service.mark_awaiting_approval(proposal, attention_id=attention.id)
        db.commit()

        process_expired_work_item(db, attention)
        db.commit()

        db.refresh(instance)
        db.refresh(step)
        db.refresh(proposal)
        assert proposal.status == "expired"
        assert proposal.error_code == "GENERAL_SKILL_PROPOSAL_EXPIRED"
        assert instance.status == "timed_out"
        assert step.status == "timed_out"
        assert not (tmp_path / "skill-objects" / "staging" / proposal.revision_id).exists()
        assert EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items == ()
    finally:
        settings.general_skill_object_store_path = old_store
        db.close()


def test_dynamic_agent_proposes_waits_and_publishes_after_owner_approval(tmp_path: Path) -> None:
    """验证真实 DynamicTask 从内建能力到 publication Attention、恢复发布和回答闭环。"""

    settings = get_settings()
    old_enabled = settings.general_skill_agent_proposal_enabled
    old_store = settings.general_skill_object_store_path
    old_workspace = settings.dynamic_task_managed_workspace_enabled
    settings.general_skill_agent_proposal_enabled = True
    settings.general_skill_object_store_path = str(tmp_path / "dynamic-skill-objects")
    settings.dynamic_task_managed_workspace_enabled = False
    try:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as db:
            tenant = Tenant(id="tenant_dynamic_proposal", name="Dynamic proposal tenant")
            owner = User(
                id="user_dynamic_proposal",
                tenant_id=tenant.id,
                username="dynamic-proposal",
                password_hash="unused",
                role="member",
            )
            agent = AgentProfile(
                id="agent_dynamic_proposal",
                tenant_id=tenant.id,
                name="售后复盘分身",
                owner_user_id=owner.id,
                status="active",
            )
            facts = {
                "protocol_version": "dynamic-v1",
                "sdk_available": True,
                "credentials_verified": True,
                "tool_calling": True,
                "structured_output": True,
            }
            model = ModelConfig(
                id="model_dynamic_proposal",
                tenant_id=tenant.id,
                name="Proposal model",
                api_key_encrypted="unused",
                model="proposal-model",
                preflight_status="ready",
                capability_snapshot_json=facts,
                capability_checksum=capability_checksum(facts),
            )
            db.add(tenant)
            db.add(owner)
            db.add(agent)
            db.add(model)
            db.commit()
            catalog = DynamicCapabilityCatalog(db)
            snapshots = [
                *catalog.list_tools(tenant.id, agent.id),
                *catalog.list_actor_tools(tenant.id, agent.id, owner.id),
            ]
            assert [item.name for item in snapshots] == [SKILL_PROPOSAL_TOOL_NAME]
            plan = NormalizedPlan(
                goal="把已验证的退款复盘方法保存为当前分身的 Skill",
                success_criteria=(
                    SuccessCriterion(id="published", type="assertion", spec={"required": True}),
                ),
                steps=(
                    PlanStep(
                        step_key="propose_skill",
                        title="提交 Skill 提案",
                        kind="tool.write",
                        capability_refs=(SKILL_PROPOSAL_TOOL_NAME,),
                    ),
                    PlanStep(
                        step_key="answer",
                        title="确认 Skill 已发布",
                        kind="answer",
                        depends_on=("propose_skill",),
                    ),
                ),
                budget={"max_steps": 2, "max_tool_calls": 1, "max_model_calls": 3},
            )
            instance, _ = SopExecutionStore(db).start_dynamic_instance(
                tenant_id=tenant.id,
                session_id="session_dynamic_proposal",
                agent_id=agent.id,
                initiator_user_id=owner.id,
                plan=plan,
                capability_snapshot={
                    "tools": [item.model_dump(mode="json") for item in snapshots],
                    "connectors": [],
                    "knowledge": [],
                    "general_skills": [],
                    "model": {
                        "model_config_id": model.id,
                        "capabilities": facts,
                        "checksum": model.capability_checksum,
                    },
                },
                source_kind="chat",
                source_ref="message_dynamic_proposal",
            )
            instance.context_json = {
                "dynamic_budget_usage": {"model_calls": 0, "tool_calls": 0}
            }
            db.add(instance)
            db.commit()
            dynamic = DynamicTaskAgent(
                db,
                action_proposer=_SkillProposalActionProposer(),
                artifact_service=ArtifactService(db, storage_root=tmp_path / "dynamic-artifacts"),
            )
            waiting = dynamic.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model,
                worker_id="proposal-stage",
                actor_user_id=owner.id,
            )
            assert waiting.status == "waiting"
            attention = db.exec(
                select(SopWorkItem).where(
                    SopWorkItem.instance_id == instance.id,
                    SopWorkItem.attention_kind == "publication",
                )
            ).one()
            assert attention.allowed_commands_json == ["allow_once", "deny"]
            assert attention.exclude_initiator is False
            assert attention.payload_json["requested_tools"] == []
            assert EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items == ()

            store = SopExecutionStore(db)
            control = ExecutionControlService(db, store)
            with store.owned(instance, worker_id="proposal-approve"):
                control.resolve_attention(
                    instance,
                    attention,
                    actor_user_id=owner.id,
                    command_id="approve-proposal-once",
                    command="allow_once",
                    expected_revision=attention.revision,
                    comment=None,
                )
            db.commit()
            signal = db.exec(
                select(ExecutionSignal).where(
                    ExecutionSignal.execution_id == instance.id,
                    ExecutionSignal.signal_type == "attention_decided",
                )
            ).one()
            outcome = dynamic.resume_tool_approval_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id="proposal-resume",
                actor_user_id=owner.id,
            )
            assert outcome.status == "succeeded"
            items = EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items
            assert len(items) == 1
            assert items[0].invocation_policy == "user_only"
            assert items[0].name == "refund-retrospective"
    finally:
        settings.general_skill_agent_proposal_enabled = old_enabled
        settings.general_skill_object_store_path = old_store
        settings.dynamic_task_managed_workspace_enabled = old_workspace
