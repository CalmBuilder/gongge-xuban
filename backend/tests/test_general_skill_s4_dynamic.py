"""
@Time       : 2026/08/13 04:20
@Author     : zhanglp8181
@File       : test_general_skill_s4_dynamic.py
@CallChain  : pytest → DynamicTaskAgent → guidance selector/loader → PlanRevision
@Description: 验证动态任务两阶段选择固定 Skill、加载全文并冻结 Use 因果的生产契约。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    ExecutionPlanRevision,
    GeneralSkill,
    GeneralSkillRevision,
    GeneralSkillUse,
    ModelConfig,
    SopOperation,
    Tenant,
    User,
)
from app.dynamic_tasks.agent import DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.action_proposer import DynamicActionProposer
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.planning import NormalizedPlan
from app.sop_runtime.execution_store import (
    SopExecutionSkillAuthorizationError,
    SopExecutionStore,
)


class _TwoPhasePlanningClient:
    """先从无正文目录选择 Skill，再要求全文实际进入计划模型。"""

    def __init__(self, *, on_plan: Callable[[], None] | None = None) -> None:
        """初始化两阶段 payload 记录。"""

        self.calls: list[dict[str, object]] = []
        self.on_plan = on_plan

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """按 payload 阶段返回选择或引用固定指导的收敛计划。"""

        self.calls.append(user_payload)
        if "skill_catalog" in user_payload:
            assert "S4-DIAGNOSE-FULL-INSTRUCTIONS" not in str(user_payload)
            return {
                "selected_skill_names": ["diagnosing-bugs"],
                "reason": "任务需要先复现再提出可证伪假设",
            }
        if "candidate_options" in user_payload:
            candidates = user_payload["candidate_options"]
            requirements = user_payload.get("requirements", [])
            identities = []
            if isinstance(candidates, list) and isinstance(requirements, list):
                for requirement in requirements:
                    if not isinstance(requirement, dict):
                        continue
                    principle = str(requirement.get("principle") or "").strip()
                    candidate_index = next(
                        (
                            index for index, candidate in enumerate(candidates)
                            if isinstance(candidate, dict)
                            and str(candidate.get("principle") or "").strip() == principle
                        ),
                        None,
                    )
                    if candidate_index is not None:
                        identities.append({
                            "index": requirement.get("index"),
                            "candidate_index": candidate_index,
                        })
            return {"identities": identities}
        assert "S4-DIAGNOSE-FULL-INSTRUCTIONS" not in str(user_payload["loaded_guidance"])
        assert "S4-DIAGNOSE-FULL-INSTRUCTIONS" in str(
            user_payload["guidance_principle_candidates"]
        )
        assert "先称呼我为张工" in str(user_payload["memory_context"])
        assert "memory-secret-must-not-leak" not in str(user_payload)
        if self.on_plan is not None:
            self.on_plan()
        return {
            "goal": "模型不得改写目标",
            "success_criteria": [
                {"id": "model_placeholder", "type": "assertion", "spec": {}}
            ],
            "constraints": ["先复现再修复"],
            "guidance_requirements": [
                {
                    "skill_ref": "diagnosing-bugs",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle": "先复现再形成假设",
                    "task_mapping": "先复现记忆缺失，再形成可证伪假设",
                    "observable_acceptance": "诊断计划同时列出复现步骤和假设验证",
                    "disposition": "apply",
                }
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成诊断计划",
                    "kind": "answer",
                    "guidance_skill_refs": ["diagnosing-bugs"],
                }
            ],
        }


class _GuidedActionClient:
    """记录执行阶段 provider payload，证明 Skill 不只参与首轮规划。"""

    def __init__(self) -> None:
        """初始化执行 payload 记录。"""

        self.payload: dict[str, object] | None = None

    def generate_json_with_metadata(
        self,
        system_prompt: str,
        user_payload: dict,
    ) -> tuple[dict, dict]:
        """返回合法 answer，并保存包含固定 Skill 指导的 provider 视图。"""

        self.payload = user_payload
        return (
            {
                "action_kind": "answer",
                "arguments": {
                    "markdown": "已按先复现后假设的方法形成诊断计划。",
                    "criterion_evidence": {},
                    "pending_questions": [],
                },
                "capability_ref": None,
                "expected_output_schema": {},
                "rationale": "按固定 Skill 方法完成当前步骤",
            },
            {
                "response_id": "response_s4_guided_action",
                "finish_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )


def _checksum(value: object) -> str:
    """生成 resolver 使用的规范 JSON checksum。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_dynamic_task_selects_loads_and_freezes_guidance_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """规划期间允许独立写库，模型返回后再固定 Skill Use 与 PlanRevision。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'dynamic-skill-planning.db'}",
        connect_args={"check_same_thread": False, "timeout": 1},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        tenant = Tenant(id="tenant_s4", name="S4 Tenant")
        user = User(
            id="user_s4",
            tenant_id=tenant.id,
            username="s4-user",
            password_hash="unused",
            role="member",
        )
        agent = AgentProfile(
            id="agent_s4",
            tenant_id=tenant.id,
            owner_user_id=user.id,
            name="诊断分身",
            status="active",
        )
        chat = ChatSession(
            id="session_s4",
            tenant_id=tenant.id,
            user_id=user.id,
            agent_id=agent.id,
        )
        model_capabilities = {
            "protocol_version": "dynamic-v1",
            "sdk_available": True,
            "credentials_verified": True,
            "tool_calling": True,
            "structured_output": True,
        }
        model = ModelConfig(
            id="model_s4",
            tenant_id=tenant.id,
            name="S4 Model",
            api_key_encrypted="unused",
            model="s4-model",
            enabled=True,
            preflight_status="ready",
            capability_snapshot_json=model_capabilities,
            capability_checksum=capability_checksum(model_capabilities),
        )
        markdown = "# diagnosing-bugs\nS4-DIAGNOSE-FULL-INSTRUCTIONS：先复现再形成假设。"
        skill = GeneralSkill(
            id="genskill_s4_diagnose",
            tenant_id=tenant.id,
            slug="diagnosing-bugs",
            name="诊断缺陷",
            description="先复现，再提出可证伪假设。",
            skill_markdown=markdown,
            status="published",
            usage_mode="planning_guidance",
            owner_user_id=user.id,
            visibility_scope="agent_private",
        )
        db.add(tenant)
        db.add(user)
        db.add(agent)
        db.add(chat)
        db.add(model)
        db.add(skill)
        db.flush()
        revision = GeneralSkillRevision(
            id="gsrev_s4_diagnose_1",
            tenant_id=tenant.id,
            skill_id=skill.id,
            revision_number=1,
            content_checksum=_checksum([]),
            manifest_checksum=hashlib.sha256(markdown.encode()).hexdigest(),
            normalized_skill_markdown=markdown,
            parsed_metadata_json={"name": skill.name, "description": skill.description},
            resource_manifest_json=[],
            requested_capabilities_json={
                "allowed_tools": ["crm.order.read"],
                "invocation_policy": "model_allowed",
            },
            source_snapshot_json={"source_kind": "legacy_backfill"},
            status="published",
            created_by=user.id,
        )
        db.add(revision)
        db.flush()
        skill.current_published_revision_id = revision.id
        db.add(skill)
        db.add(
            AgentResourceBinding(
                id="agentres_s4_diagnose",
                tenant_id=tenant.id,
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
                    "created_by_user_id": user.id,
                },
            )
        )
        db.commit()
        settings = get_settings()
        monkeypatch.setattr(settings, "general_skill_resolver_v2_enabled", True)
        def write_during_plan() -> None:
            """从独立会话提交事件，证明真实规划等待期间不存在 SQLite 写锁。"""

            with Session(engine) as contender:
                contender.add(
                    AgentEvent(
                        tenant_id=tenant.id,
                        session_id=chat.id,
                        event_type="planning_concurrent_write_probe",
                        payload_json={},
                    )
                )
                contender.commit()

        client = _TwoPhasePlanningClient(on_plan=write_during_plan)

        dynamic = DynamicTaskAgent(db, planner=DynamicTaskPlanner(client))
        instance, created = dynamic.start_task(
            tenant_id=tenant.id,
            session_id=chat.id,
            agent_id=agent.id,
            initiator_user_id=user.id,
            goal="诊断并修复记忆缺失问题",
            success_criteria=("形成可验证诊断计划",),
            model_config=model,
            source_ref="msg_s4_dynamic",
            memory_context=(
                {
                    "kind": "profile",
                    "content": "先称呼我为张工",
                    "metadata": {
                        "preference": "formal",
                        "secret": "memory-secret-must-not-leak",
                    },
                },
            ),
        )
        assert created is True
        # 选择目录、生成计划后，宿主还会为缺少稳定 candidate ID 的旧式 mock
        # 发起一次有界 identity repair；这仍然属于同一 PlanRevision，不是第二套 Runtime。
        assert len(client.calls) == 3
        assert "candidate_options" in client.calls[2]
        use = db.exec(select(GeneralSkillUse)).one()
        assert use.revision_id == revision.id
        assert use.execution_id == instance.id
        plan_revision = db.exec(select(ExecutionPlanRevision)).one()
        assert plan_revision.plan_json["steps"][0]["guidance_skill_use_ids"] == [use.id]
        assert plan_revision.plan_json["guidance_requirements"][0]["skill_use_id"] == use.id
        assert "skill_markdown" not in str(instance.capability_snapshot_json["general_skills"])
        loaded_event = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "skill_loaded")
        ).one()
        assert loaded_event.payload_json["consumer"] == "dynamic_task"
        assert db.exec(
            select(AgentEvent).where(
                AgentEvent.session_id == chat.id,
                AgentEvent.event_type == "planning_concurrent_write_probe",
            )
        ).one()
        replayed, replay_created = dynamic.start_task(
            tenant_id=tenant.id,
            session_id=chat.id,
            agent_id=agent.id,
            initiator_user_id=user.id,
            goal="诊断并修复记忆缺失问题",
            success_criteria=("形成可验证诊断计划",),
            model_config=model,
            source_ref="msg_s4_dynamic",
            memory_context=(
                {
                    "kind": "profile",
                    "content": "先称呼我为张工",
                    "metadata": {"preference": "formal"},
                },
            ),
        )
        assert replayed.id == instance.id
        assert replay_created is False
        assert len(client.calls) == 3
        assert len(db.exec(select(GeneralSkillUse)).all()) == 1
        assert len(
            db.exec(select(AgentEvent).where(AgentEvent.event_type == "skill_loaded")).all()
        ) == 1

        plan = NormalizedPlan.model_validate(plan_revision.plan_json)
        action_client = _GuidedActionClient()
        restarted_dynamic = DynamicTaskAgent(
            db,
            action_proposer=DynamicActionProposer(action_client),
        )
        restarted_dynamic._propose_action(
            instance=instance,
            step=plan.steps[0],
            model_config=model,
            worker_id="worker_s4_guidance",
        )
        assert action_client.payload is not None
        assert "S4-DIAGNOSE-FULL-INSTRUCTIONS" in str(action_client.payload)
        assert "先称呼我为张工" in str(action_client.payload)
        assert use.id in str(action_client.payload)

        second_use = GeneralSkillUse(
            id="gsuse_s4_second_cause",
            tenant_id=use.tenant_id,
            session_id=use.session_id,
            turn_id="turn_s4_second_cause",
            execution_id=instance.id,
            agent_id=use.agent_id,
            user_id=use.user_id,
            skill_id=use.skill_id,
            revision_id=use.revision_id,
            content_checksum=use.content_checksum,
            selection_mode="auto",
            status="active",
            idempotency_key="s4-second-cause",
        )
        db.add(second_use)
        db.commit()
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="worker_s4_causes"):
            node = store.enter_node(
                instance,
                plan.steps[0].step_key,
                step_key=plan.steps[0].step_key,
                plan_revision_id=instance.current_plan_revision_id,
                step_kind=plan.steps[0].kind,
                title=plan.steps[0].title,
            )
            operation, _ = store.prepare_operation(
                instance,
                node,
                operation_name="crm.order.read",
                request={},
                logical_action_id="s4-multi-skill-allowed",
                caused_by_skill_use_ids=(use.id, second_use.id),
                capability_snapshot={
                    "capability_type": "tool",
                    "name": "crm.order.read",
                },
            )
            assert operation.caused_by_skill_use_ids_json == [use.id, second_use.id]
            store.start_operation(operation)
        assert operation.status == "running"

        skill_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant.id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        skill_binding.status = "inactive"
        db.add(skill_binding)
        db.commit()
        with store.owned(instance, worker_id="worker_s4_binding_countermand"):
            revoked_binding_operation = SopOperation(
                tenant_id=instance.tenant_id,
                instance_id=instance.id,
                node_execution_id=node.id,
                operation_name="crm.order.read",
                idempotency_key="s4-binding-denied-idempotency",
                logical_action_id="s4-binding-denied",
                request_fingerprint="s4-binding-denied-fingerprint",
                caused_by_skill_use_id=use.id,
                caused_by_skill_use_ids_json=[use.id],
                capability_snapshot_json={
                    "capability_type": "tool",
                    "name": "crm.order.read",
                },
            )
            db.add(revoked_binding_operation)
            db.flush()
            with pytest.raises(SopExecutionSkillAuthorizationError) as revoked_binding:
                store.start_operation(revoked_binding_operation)
        assert revoked_binding.value.authorization_code == "GENERAL_SKILL_COUNTERMANDED"
        assert revoked_binding_operation.status == "prepared"
        skill_binding.status = "active"
        db.add(skill_binding)
        db.commit()

        second_use.status = "invalidated"
        second_use.invalidation_reason = "GENERAL_SKILL_COUNTERMANDED"
        db.add(second_use)
        db.commit()
        with store.owned(instance, worker_id="worker_s4_countermand"):
            denied = SopOperation(
                tenant_id=instance.tenant_id,
                instance_id=instance.id,
                node_execution_id=node.id,
                operation_name="crm.order.read",
                idempotency_key="s4-denied-idempotency",
                logical_action_id="s4-multi-skill-denied",
                request_fingerprint="s4-denied-fingerprint",
                caused_by_skill_use_id=use.id,
                caused_by_skill_use_ids_json=[use.id, second_use.id],
                capability_snapshot_json={
                    "capability_type": "tool",
                    "name": "crm.order.read",
                },
            )
            db.add(denied)
            db.flush()
            with pytest.raises(SopExecutionSkillAuthorizationError) as countermanded:
                store.start_operation(denied)
        assert countermanded.value.authorization_code == "GENERAL_SKILL_COUNTERMANDED"

        use.status = "invalidated"
        use.invalidation_reason = "GENERAL_SKILL_COUNTERMANDED"
        db.add(use)
        db.commit()
        blocked_client = _GuidedActionClient()
        blocked_dynamic = DynamicTaskAgent(
            db,
            action_proposer=DynamicActionProposer(blocked_client),
        )
        with pytest.raises(DynamicTaskAgentError, match="GENERAL_SKILL_COUNTERMANDED"):
            blocked_dynamic._propose_action(
                instance=instance,
                step=plan.steps[0],
                model_config=model,
                worker_id="worker_s4_countermanded_model",
            )
        assert blocked_client.payload is None
