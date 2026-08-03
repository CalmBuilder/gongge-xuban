"""
@Time       : 2026/08/04 01:04
@Author     : zhanglp8181
@File       : agent.py
@CallChain  : Agent Loop/signal worker → DynamicTaskAgent → Execution Store/ToolExecutor
@Description: 以统一 Execution 账本推进动态动作、可信 Artifact，并支持崩溃后的安全恢复。
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlmodel import Session, select

from app.db.models import (
    ActionProposalRecord,
    ExecutionCommand,
    ExecutionArtifact,
    ExecutionPlanRevision,
    ExecutionSignal,
    AgentEvent,
    ChatSession,
    Message,
    InputResourceSnapshot,
    ManagedInputResource,
    ModelConfig,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    User,
)
from app.dynamic_tasks.action_proposer import DynamicActionProposer
from app.dynamic_tasks.artifacts import (
    ArtifactAccessDenied,
    ArtifactContractError,
    ArtifactService,
)
from app.dynamic_tasks.capability_catalog import (
    CapabilitySnapshot,
    DynamicCapabilityCatalog,
    capability_checksum,
)
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.planning import (
    CompletedProviderProposal,
    NormalizedPlan,
    PlanReason,
    PlanStep,
    SuccessCriterion,
)
from app.dynamic_tasks.provider_view import (
    build_provider_execution_view,
    require_dynamic_preflight,
)
from app.dynamic_tasks.result_verifier import DynamicTaskResult, verify_dynamic_result
from app.knowledge.access import accessible_knowledge_base_versions, resolve_knowledge_access
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.llm.client import LLMClient
from app.organization.permissions import user_permission_codes
from app.session.managed_resources import ManagedInputResourceService
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


class DynamicTaskAgentError(RuntimeError):
    """表示只读动态推进在 provider、能力或状态边界被确定性拒绝。"""


@dataclass(frozen=True)
class DynamicRunOutcome:
    """表达同步推进到终态或明确阻塞点的最小结果。"""

    status: str
    execution_id: str
    message: Message | None = None
    blocking_step_key: str | None = None


class DynamicToolExecutor(Protocol):
    """约束动态 Agent 使用既有工具执行器所需的最小接口。"""

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        actor_user_id: str | None = None,
        execution_org_unit_id: str | None = None,
        remote_idempotency_key: str | None = None,
    ) -> ToolResult:
        """执行已经过服务端验证和再授权的现有 ToolCall。"""


class DynamicTaskAgent:
    """首期仅串行执行已冻结、实时再授权的 read tool proposal。"""

    def __init__(
        self,
        db: Session,
        *,
        catalog: DynamicCapabilityCatalog | None = None,
        tool_executor: DynamicToolExecutor | None = None,
        planner: DynamicTaskPlanner | None = None,
        action_proposer: DynamicActionProposer | None = None,
        resource_service: ManagedInputResourceService | None = None,
        knowledge_service: KnowledgeService | None = None,
        artifact_service: ArtifactService | None = None,
    ) -> None:
        """绑定统一事务、能力目录和既有工具执行器，禁止创建第二套 Runtime。"""

        self.db = db
        self.store = SopExecutionStore(db)
        self.catalog = catalog or DynamicCapabilityCatalog(db)
        self.tool_executor = tool_executor or ToolExecutor(db)
        self.planner = planner
        self.action_proposer = action_proposer
        self.resource_service = resource_service or ManagedInputResourceService(db)
        self.knowledge_service = knowledge_service or KnowledgeService(db)
        self.artifact_service = artifact_service or ArtifactService(db)

    def advance_next_read_step(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
        organization_unit_id: str | None = None,
    ) -> tuple[PlanStep, ToolResult]:
        """在同一 Execution lease 内选择唯一就绪 read 步骤、取得完整提案并执行。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        verified_model = self.catalog.require_dynamic_model(instance.tenant_id, model_config.id)
        frozen_model = (instance.capability_snapshot_json or {}).get("model", {})
        if (
            not isinstance(frozen_model, dict)
            or frozen_model.get("model_config_id") != verified_model.id
            or frozen_model.get("checksum") != verified_model.capability_checksum
        ):
            raise DynamicTaskAgentError("DYNAMIC_MODEL_SNAPSHOT_CHANGED")
        with self.store.owned(instance, worker_id=worker_id):
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            step = next(
                (
                    item
                    for item in plan.steps
                    if item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if step is None:
                raise DynamicTaskAgentError("DYNAMIC_NO_READY_STEP")
            if step.kind != "tool.read":
                raise DynamicTaskAgentError("DYNAMIC_NEXT_STEP_NOT_READ")
            capabilities = dict(verified_model.capability_snapshot_json or {})
            completed_response = self._propose_action(
                instance=instance,
                step=step,
                model_config=verified_model,
                worker_id=worker_id,
            )
            result = self.execute_read_proposal(
                execution_id=instance.id,
                step_key=step.step_key,
                completed_response=completed_response,
                provider=verified_model.provider,
                model=verified_model.model,
                model_capabilities=capabilities,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
                organization_unit_id=organization_unit_id,
            )
            return step, result

    def run_until_blocked_or_complete(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
        organization_unit_id: str | None = None,
        resume_signal_id: str | None = None,
        signal_worker_id: str | None = None,
    ) -> DynamicRunOutcome:
        """按服务端预算串行推进 read 步骤，并在 answer、等待或无就绪步骤处确定收敛。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        plan = self._current_plan(instance)
        max_steps = int(plan.budget.get("max_steps", len(plan.steps)))
        if max_steps < 1 or max_steps > 100:
            raise DynamicTaskAgentError("DYNAMIC_BUDGET_INVALID")
        for _iteration in range(max_steps + 1):
            self.db.refresh(instance)
            if instance.status == "succeeded":
                message = self.db.exec(
                    select(Message)
                    .where(
                        Message.tenant_id == instance.tenant_id,
                        Message.session_id == instance.session_id,
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                ).first()
                return DynamicRunOutcome("succeeded", instance.id, message=message)
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            step = next(
                (
                    item
                    for item in plan.steps
                    if item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if step is None:
                return DynamicRunOutcome("blocked", instance.id)
            if step.kind == "tool.read":
                self.advance_next_read_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                    organization_unit_id=organization_unit_id,
                )
                self.db.commit()
                continue
            if step.kind == "knowledge":
                self.advance_next_knowledge_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                )
                self.db.commit()
                continue
            if step.kind == "answer":
                completed = self._propose_action(
                    instance=instance,
                    step=step,
                    model_config=model_config,
                    worker_id=worker_id,
                )
                message = self.complete_with_result_proposal(
                    execution_id=instance.id,
                    step_key=step.step_key,
                    completed_response=completed,
                    provider=model_config.provider,
                    model=model_config.model,
                    model_capabilities=dict(model_config.capability_snapshot_json or {}),
                    worker_id=worker_id,
                    resume_signal_id=resume_signal_id,
                    signal_worker_id=signal_worker_id,
                )
                self.db.commit()
                return DynamicRunOutcome("succeeded", instance.id, message=message)
            if step.kind == "clarification":
                self.advance_next_clarification_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                )
                if resume_signal_id is not None:
                    self._consume_resume_signal(
                        instance,
                        signal_id=resume_signal_id,
                        worker_id=signal_worker_id or worker_id,
                    )
                self.db.commit()
                return DynamicRunOutcome(
                    "waiting",
                    instance.id,
                    blocking_step_key=step.step_key,
                )
            return DynamicRunOutcome(
                "waiting",
                instance.id,
                blocking_step_key=step.step_key,
            )
        raise DynamicTaskAgentError("DYNAMIC_STEP_BUDGET_EXHAUSTED")

    def advance_next_clarification_step(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
    ) -> SopWorkItem:
        """把当前 clarification 提案持久化为仅发给发起人的统一 Attention。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        with self.store.owned(instance, worker_id=worker_id):
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            step_definition = next(
                (
                    item
                    for item in plan.steps
                    if item.kind == "clarification"
                    and item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if step_definition is None:
                raise DynamicTaskAgentError("DYNAMIC_NO_READY_CLARIFICATION_STEP")
            step = self._step(instance, step_definition.step_key)
            if step is not None:
                attention = self._step_attention(instance, step)
                if attention is not None:
                    return attention
            else:
                step = self.store.enter_node(
                    instance,
                    step_definition.step_key,
                    step_key=step_definition.step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind="clarification",
                    title=step_definition.title,
                    required=step_definition.required,
                )
            completed_response = self._propose_action(
                instance=instance,
                step=step_definition,
                model_config=model_config,
                worker_id=worker_id,
            )
            arguments = dict(completed_response.proposal.arguments)
            question = str(arguments.get("question") or "").strip()
            if not question or len(question) > 1000:
                raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_QUESTION_INVALID")
            options = arguments.get("options", [])
            if not isinstance(options, list) or len(options) > 20 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 256
                for item in options
            ):
                raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_OPTIONS_INVALID")
            proposal, _ = self.store.record_action_proposal(
                instance,
                step,
                provider=model_config.provider,
                model=model_config.model,
                model_capability_snapshot=dict(model_config.capability_snapshot_json or {}),
                completed_response=completed_response,
            )
            control = ExecutionControlService(self.db, self.store)
            attention, _ = control.offer_attention(
                instance,
                attention_kind="clarification",
                attention_key=f"{step_definition.step_key}:clarification",
                title=step_definition.title,
                payload={"question": question, "options": options},
                allowed_commands=["answer", "cancel"],
                candidate_user_ids=[instance.initiator_user_id],
                source_type="dynamic_task",
                source_ref=proposal.id,
                node_execution=step,
            )
            self.store.consume_attention_proposal(
                instance,
                proposal,
                attention_id=attention.id,
            )
            if step.status == "running":
                self.store.wait_for_work_item(instance, step, work_item_id=attention.id)
            self.db.commit()
            return attention

    def resume_clarification_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> DynamicRunOutcome:
        """消费已决定 clarification 的持久 signal，并从同一 Execution 继续执行。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        if signal is None or signal.signal_type != "attention_decided":
            raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_SIGNAL_INVALID")
        instance = self.db.get(SopInstance, signal.execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        control = ExecutionControlService(self.db, self.store)
        if signal.status == "consumed":
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        cancelled = False
        with self.store.owned(instance, worker_id=worker_id):
            attention_id = str(signal.payload_json.get("attention_id") or "")
            attention = self.db.get(SopWorkItem, attention_id)
            if (
                attention is None
                or attention.instance_id != instance.id
                or attention.attention_kind != "clarification"
                or attention.status != "completed"
                or attention.initiator_user_id != actor_user_id
            ):
                raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_RESOLUTION_INVALID")
            resolved_by = str(attention.resolution_json.get("actor_user_id") or "")
            actor = self.db.get(User, resolved_by)
            if (
                resolved_by != actor_user_id
                or actor is None
                or actor.tenant_id != instance.tenant_id
                or actor.membership_status != "active"
            ):
                raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_ACTOR_DENIED")
            command = str(attention.resolution_json.get("command") or "")
            if command == "cancel":
                control.consume_signal(instance, signal, worker_id=worker_id)
                self.store.request_cancellation(
                    instance,
                    actor_user_id=actor_user_id,
                    reason="clarification_cancelled",
                )
                self.db.commit()
                cancelled = True
            else:
                answer = str(attention.resolution_json.get("comment") or "").strip()
                if command != "answer" or not answer:
                    raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_ANSWER_REQUIRED")
                step = self.db.get(SopNodeExecution, attention.node_execution_id)
                if step is None or step.instance_id != instance.id or step.step_kind != "clarification":
                    raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_STEP_INVALID")
                if step.status == "waiting":
                    slots = dict(instance.slots_json or {})
                    slots.setdefault("clarifications", {})[step.step_key] = {
                        "answer": answer,
                        "attention_id": attention.id,
                        "resolved_by": actor_user_id,
                    }
                    self.store.resume_waiting_node(instance, step, slots=slots)
                    self.store.complete_node(
                        instance,
                        step,
                        output={"answer": answer, "attention_id": attention.id},
                    )
                elif step.status != "succeeded":
                    raise DynamicTaskAgentError("DYNAMIC_CLARIFICATION_STEP_INVALID")
                self.db.commit()
        self.db.commit()
        if cancelled:
            return DynamicRunOutcome("cancelled", instance.id)
        return self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=actor_user_id,
            resume_signal_id=signal.id,
            signal_worker_id=worker_id,
        )

    def resume_steer_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
        steering_enabled: bool,
    ) -> DynamicRunOutcome:
        """在安全动作边界追加用户约束，并以同一持久 signal 恢复原 Execution。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        if signal is None or signal.signal_type != "command":
            raise DynamicTaskAgentError("DYNAMIC_STEER_SIGNAL_INVALID")
        command = self.db.get(ExecutionCommand, signal.causation_id)
        instance = self.db.get(SopInstance, signal.execution_id)
        if (
            command is None
            or command.command_type != "steer"
            or command.execution_id != signal.execution_id
            or instance is None
            or instance.kind != "dynamic_task"
        ):
            raise DynamicTaskAgentError("DYNAMIC_STEER_COMMAND_INVALID")
        if command.actor_user_id != actor_user_id or command.tenant_id != instance.tenant_id:
            raise DynamicTaskAgentError("DYNAMIC_STEER_ACTOR_DENIED")
        control = ExecutionControlService(self.db, self.store)
        if signal.status == "consumed":
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        with self.store.owned(instance, worker_id=worker_id):
            self.db.refresh(command)
            self.db.refresh(instance)
            if command.status == "applied":
                pass
            elif command.status in {"conflicted", "rejected"}:
                control.consume_signal(instance, signal, worker_id=worker_id)
                self.db.commit()
                return DynamicRunOutcome(command.status, instance.id)
            elif command.status != "pending":
                raise DynamicTaskAgentError("DYNAMIC_STEER_COMMAND_NOT_PENDING")
            elif not self._steer_actor_authorized(instance, actor_user_id):
                self._settle_steer_command(
                    instance,
                    command,
                    status="rejected",
                    reason_code="DYNAMIC_STEER_ACTOR_DENIED",
                    worker_id=worker_id,
                )
                control.consume_signal(instance, signal, worker_id=worker_id)
                self.db.commit()
                return DynamicRunOutcome("rejected", instance.id)
            elif not steering_enabled:
                self._settle_steer_command(
                    instance,
                    command,
                    status="rejected",
                    reason_code="DYNAMIC_STEERING_DISABLED",
                    worker_id=worker_id,
                )
                control.consume_signal(instance, signal, worker_id=worker_id)
                self.db.commit()
                return DynamicRunOutcome("rejected", instance.id)
            else:
                base_revision_id = str(
                    (command.result_json or {}).get("base_plan_revision_id") or ""
                )
                if not base_revision_id or instance.current_plan_revision_id != base_revision_id:
                    self._settle_steer_command(
                        instance,
                        command,
                        status="conflicted",
                        reason_code="STEER_PLAN_REVISION_CONFLICT",
                        worker_id=worker_id,
                    )
                    control.consume_signal(instance, signal, worker_id=worker_id)
                    self.db.commit()
                    return DynamicRunOutcome("conflicted", instance.id)
                self._assert_steer_safe_boundary(instance)
                with self.db.begin_nested():
                    self._supersede_prepared_dynamic_actions(instance)
                    current_revision = self.db.get(ExecutionPlanRevision, base_revision_id)
                    if current_revision is None:
                        raise DynamicTaskAgentError("DYNAMIC_PLAN_NOT_FOUND")
                    current_plan = NormalizedPlan.model_validate(current_revision.plan_json)
                    instruction = str(command.payload_json.get("instruction") or "").strip()
                    constraints = tuple(dict.fromkeys((*current_plan.constraints, instruction)))
                    revised_plan = current_plan.model_copy(update={"constraints": constraints})
                    revision, _ = self.store.append_plan_revision(
                        instance,
                        plan=revised_plan,
                        reason=PlanReason.USER_CONSTRAINT,
                        capability_snapshot=dict(current_revision.capability_snapshot_json or {}),
                    )
                    self._settle_steer_command(
                        instance,
                        command,
                        status="applied",
                        reason_code=None,
                        worker_id=worker_id,
                        plan_revision_id=revision.id,
                    )
        self.db.commit()
        return self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
            resume_signal_id=signal.id,
            signal_worker_id=worker_id,
        )

    def _steer_actor_authorized(self, instance: SopInstance, actor_user_id: str) -> bool:
        """处理命令时重新验证成员状态和 Execution 管理资格，防止排队期间撤权失效。"""

        actor = self.db.get(User, actor_user_id)
        if (
            actor is None
            or actor.tenant_id != instance.tenant_id
            or actor.membership_status != "active"
        ):
            return False
        return actor.id == instance.initiator_user_id or "execution.manage" in set(
            user_permission_codes(
                self.db,
                tenant_id=instance.tenant_id,
                user_id=actor.id,
            )
        )

    def _assert_steer_safe_boundary(self, instance: SopInstance) -> None:
        """只允许在无已派发动作和无活动人工等待的边界修改后续计划。"""

        dispatched = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperation.status.in_(("running", "unknown")),
            )
        ).first()
        if dispatched is not None:
            raise DynamicTaskAgentError("DYNAMIC_STEER_OPERATION_UNSETTLED")
        attention = self.db.exec(
            select(SopWorkItem).where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.status.in_(("offered", "claimed")),
            )
        ).first()
        if attention is not None:
            raise DynamicTaskAgentError("DYNAMIC_STEER_ATTENTION_UNSETTLED")

    def _supersede_prepared_dynamic_actions(self, instance: SopInstance) -> None:
        """撤销尚未 dispatch 的动作提案和 Operation，并结束其旧计划节点 attempt。"""

        prepared = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperation.status == "prepared",
            )
        ).all()
        affected_node_ids: set[str] = set()
        for operation in prepared:
            self.store.cancel_prepared_operation(operation)
            affected_node_ids.add(operation.node_execution_id)
        proposals = self.db.exec(
            select(ActionProposalRecord).where(
                ActionProposalRecord.tenant_id == instance.tenant_id,
                ActionProposalRecord.execution_id == instance.id,
                ActionProposalRecord.status == "validated",
            )
        ).all()
        now = self.store.database_now()
        for proposal in proposals:
            proposal.status = "superseded"
            proposal.superseded_at = now
            self.db.add(proposal)
            node = self.db.exec(
                select(SopNodeExecution).where(
                    SopNodeExecution.tenant_id == instance.tenant_id,
                    SopNodeExecution.instance_id == instance.id,
                    SopNodeExecution.plan_revision_id == proposal.plan_revision_id,
                    SopNodeExecution.step_key == proposal.step_key,
                    SopNodeExecution.attempt == proposal.step_attempt,
                )
            ).first()
            if node is not None:
                affected_node_ids.add(node.id)
        for node_id in affected_node_ids:
            node = self.db.get(SopNodeExecution, node_id)
            if node is not None and node.status in {"scheduled", "running"}:
                self.store.fail_node(
                    instance,
                    node,
                    error={"code": "DYNAMIC_ACTION_SUPERSEDED_BY_STEERING"},
                )

    def _settle_steer_command(
        self,
        instance: SopInstance,
        command: ExecutionCommand,
        *,
        status: str,
        reason_code: str | None,
        worker_id: str,
        plan_revision_id: str | None = None,
    ) -> None:
        """以当前 fencing token 终结 steer 命令并追加可审计处置事件。"""

        now = self.store.database_now()
        command.status = status
        command.reason_code = reason_code
        command.claimed_by = worker_id
        command.claimed_fencing_token = instance.fencing_token
        command.claimed_at = command.claimed_at or now
        command.consumed_at = now
        command.result_plan_revision_id = plan_revision_id
        command.result_json = {
            **dict(command.result_json or {}),
            "plan_revision_id": plan_revision_id,
            "execution_revision": instance.revision,
        }
        command.updated_at = now
        self.db.add(command)
        ExecutionControlService(self.db, self.store).append_execution_event(
            instance,
            event_type=f"execution_steer_{status}",
            causation_id=command.id,
            payload={
                "command_id": command.command_id,
                "reason_code": reason_code,
                "plan_revision_id": plan_revision_id,
            },
        )

    def advance_next_knowledge_step(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> tuple[PlanStep, ToolResult]:
        """按冻结知识版本和当前成员/Agent 交集执行一个可恢复 knowledge Operation。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        snapshot = self._frozen_knowledge_snapshot(instance)
        self._reauthorize_knowledge(instance, snapshot, actor_user_id=actor_user_id)
        with self.store.owned(instance, worker_id=worker_id):
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            step_definition = next(
                (
                    item
                    for item in plan.steps
                    if item.kind == "knowledge"
                    and item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if step_definition is None:
                raise DynamicTaskAgentError("DYNAMIC_NO_READY_KNOWLEDGE_STEP")
            step = self._step(instance, step_definition.step_key)
            if step is not None:
                completed_operation = self._completed_operation(step)
                if completed_operation is not None:
                    return step_definition, self._operation_result(completed_operation)
            else:
                step = self.store.enter_node(
                    instance,
                    step_definition.step_key,
                    step_key=step_definition.step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind="knowledge",
                    title=step_definition.title,
                    required=step_definition.required,
                )
            completed_response = self._propose_action(
                instance=instance,
                step=step_definition,
                model_config=model_config,
                worker_id=worker_id,
            )
            proposal, _ = self.store.record_action_proposal(
                instance,
                step,
                provider=model_config.provider,
                model=model_config.model,
                model_capability_snapshot=dict(model_config.capability_snapshot_json or {}),
                completed_response=completed_response,
            )
            arguments = dict(completed_response.proposal.arguments)
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_QUERY_REQUIRED")
            operation, _ = self.store.prepare_operation_from_proposal(
                instance,
                step,
                proposal,
                operation_name="knowledge.search",
                request=arguments,
                effect_kind="read",
                capability_snapshot=snapshot.model_dump(
                    mode="json", exclude={"checksum", "agent_id"}
                ),
                capability_snapshot_checksum=snapshot.checksum,
            )
            if operation.status == "succeeded":
                return step_definition, self._operation_result(operation)
            if operation.status == "prepared":
                self.store.start_operation(operation)
            elif operation.status != "running":
                raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_OPERATION_NOT_RETRYABLE")
            self._consume_call_budget(instance, "tool_calls")
            self.db.commit()
            cards = snapshot.model_view.get("knowledge_bases")
            if (
                not isinstance(cards, list)
                or not cards
                or any(
                    not isinstance(item, dict)
                    or not item.get("id")
                    or not item.get("version_id")
                    for item in cards
                )
            ):
                raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_SNAPSHOT_INVALID")
            response = self.knowledge_service.search(
                KnowledgeSearchRequest(
                    tenant_id=instance.tenant_id,
                    agent_id=instance.agent_id,
                    query=query,
                    desired_evidence=str(arguments.get("desired_evidence") or "") or None,
                    knowledge_base_ids=[str(item["id"]) for item in cards],
                    knowledge_base_version_ids=[str(item["version_id"]) for item in cards],
                    model_config_id=model_config.id,
                ),
                model_config,
            )
            result_payload = response.model_dump(mode="json")
            self.store.finish_operation(operation, succeeded=True, result={"data": result_payload})
            self.store.complete_node(instance, step, output={"data": result_payload})
            self.db.commit()
            return step_definition, ToolResult(
                tool_name="knowledge.search",
                success=True,
                data=result_payload,
            )

    def start_task(
        self,
        *,
        tenant_id: str,
        session_id: str,
        agent_id: str,
        initiator_user_id: str,
        goal: str,
        success_criteria: Sequence[str],
        model_config: ModelConfig,
        source_ref: str | None = None,
        input_resource_ids: Sequence[str] = (),
        knowledge_capability: dict[str, object] | None = None,
    ) -> tuple[SopInstance, bool]:
        """经模型 preflight、实时能力目录和有界规划创建或复用统一动态 Execution。"""

        verified_model = self.catalog.require_dynamic_model(tenant_id, model_config.id)
        capabilities = [
            *self.catalog.list_tools(tenant_id, agent_id),
            *self.catalog.list_general_skills(tenant_id, agent_id),
        ]
        knowledge_snapshot = self._knowledge_snapshot(
            tenant_id=tenant_id,
            agent_id=agent_id,
            capability=knowledge_capability or {},
        )
        if knowledge_snapshot is not None:
            capabilities.append(knowledge_snapshot)
        if not capabilities:
            raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_EMPTY")
        criteria = tuple(
            SuccessCriterion(
                id=f"criterion_{index:02d}",
                type="assertion",
                spec={"description": value, "required": True},
            )
            for index, value in enumerate(success_criteria, start=1)
            if str(value).strip()
        )
        if not goal.strip() or not criteria:
            raise DynamicTaskAgentError("DYNAMIC_TASK_CONTRACT_INCOMPLETE")
        planner = self.planner or DynamicTaskPlanner(LLMClient(verified_model))
        resources: list[ManagedInputResource] = []
        for resource_id in dict.fromkeys(input_resource_ids):
            resource = self.db.get(ManagedInputResource, resource_id)
            if (
                resource is None
                or resource.tenant_id != tenant_id
                or resource.owner_user_id != initiator_user_id
                or resource.ingestion_status != "ready"
                or resource.revoked_at is not None
            ):
                raise DynamicTaskAgentError("DYNAMIC_INPUT_RESOURCE_UNAVAILABLE")
            resources.append(resource)
        existing = self.store.active_instance(tenant_id, session_id)
        if existing is not None:
            requested_criteria = [item.model_dump(mode="json") for item in criteria]
            frozen_model = (existing.capability_snapshot_json or {}).get("model", {})
            frozen_resource_ids = {
                row.source_resource_id
                for row in self.db.exec(
                    select(InputResourceSnapshot).where(
                        InputResourceSnapshot.tenant_id == tenant_id,
                        InputResourceSnapshot.execution_id == existing.id,
                    )
                ).all()
            }
            if (
                existing.kind == "dynamic_task"
                and existing.agent_id == agent_id
                and existing.initiator_user_id == initiator_user_id
                and existing.source_kind == "chat"
                and existing.source_ref == (source_ref or session_id)
                and (existing.goal_snapshot_json or {}).get("goal") == goal.strip()
                and (existing.goal_snapshot_json or {}).get("success_criteria")
                == requested_criteria
                and isinstance(frozen_model, dict)
                and frozen_model.get("model_config_id") == verified_model.id
                and frozen_model.get("checksum") == verified_model.capability_checksum
                and frozen_resource_ids == {item.id for item in resources}
            ):
                return existing, False
            raise DynamicTaskAgentError("DYNAMIC_ACTIVE_EXECUTION_CONFLICT")
        plan = planner.create_plan(
            goal=goal.strip(),
            success_criteria=criteria,
            capabilities=capabilities,
            input_resources=tuple(
                {
                    "resource_id": resource.id,
                    "version": resource.version,
                    "filename": resource.filename,
                    "mime_type": resource.mime_type,
                    "size_bytes": resource.size_bytes,
                    "content_checksum": resource.content_checksum,
                    "ingestion_status": resource.ingestion_status,
                }
                for resource in resources
            ),
        )
        if any(
            step.kind not in {"tool.read", "knowledge", "clarification", "answer"}
            for step in plan.steps
        ):
            raise DynamicTaskAgentError("DYNAMIC_PLAN_UNSUPPORTED_STEP")
        snapshot = {
            "tools": [
                item.model_dump(mode="json")
                for item in capabilities
                if item.capability_type == "tool"
            ],
            "general_skills": [
                item.model_dump(mode="json")
                for item in capabilities
                if item.capability_type == "general_skill"
            ],
            "knowledge": [
                item.model_dump(mode="json")
                for item in capabilities
                if item.capability_type == "knowledge"
            ],
            "model": {
                "model_config_id": verified_model.id,
                "capabilities": dict(verified_model.capability_snapshot_json or {}),
                "checksum": verified_model.capability_checksum,
            },
        }
        instance, _revision = self.store.start_dynamic_instance(
            tenant_id=tenant_id,
            session_id=session_id,
            agent_id=agent_id,
            initiator_user_id=initiator_user_id,
            plan=plan,
            capability_snapshot=snapshot,
            source_kind="chat",
            source_ref=source_ref or session_id,
        )
        instance.context_json = {
            **(instance.context_json or {}),
            "dynamic_budget_usage": {
                "model_calls": 1,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }
        self.db.add(instance)
        self.db.flush()
        if resources:
            with self.store.owned(instance, worker_id=f"input:{source_ref or session_id}"):
                for resource in resources:
                    self.store.snapshot_input_resource(
                        instance,
                        resource,
                        source_message_id=source_ref,
                    )
        return instance, True

    @staticmethod
    def _knowledge_snapshot(
        *,
        tenant_id: str,
        agent_id: str,
        capability: dict[str, object],
    ) -> CapabilitySnapshot | None:
        """把入口已计算的成员/Agent 知识交集冻结为只读能力，不包含正文。"""

        cards = capability.get("knowledge_bases")
        if capability.get("available") is not True or not isinstance(cards, list) or not cards:
            return None
        safe_cards = [
            {
                "id": str(item.get("id") or ""),
                "version_id": str(item.get("version_id") or ""),
                "version": str(item.get("version") or ""),
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
            }
            for item in cards
            if isinstance(item, dict) and item.get("id") and item.get("version_id")
        ]
        if not safe_cards:
            return None
        payload = {
            "capability_type": "knowledge",
            "capability_id": "knowledge.search",
            "tenant_id": tenant_id,
            "name": "knowledge.search",
            "contract": {"risk_class": "read", "side_effect": "none"},
            "model_view": {"name": "knowledge.search", "knowledge_bases": safe_cards},
            "user_view": {"name": "企业知识检索", "knowledge_base_count": len(safe_cards)},
            "audit_view": {"knowledge_bases": safe_cards},
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=agent_id,
            checksum=capability_checksum(payload),
        )

    def execute_read_proposal(
        self,
        *,
        execution_id: str,
        step_key: str,
        completed_response: CompletedProviderProposal,
        provider: str,
        model: str,
        model_capabilities: dict[str, object],
        worker_id: str,
        actor_user_id: str,
        organization_unit_id: str | None = None,
        lease_ttl_seconds: int = 30,
    ) -> ToolResult:
        """持久化完整提案后执行一次 read Operation；已成功动作恢复时直接复用回执。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        require_dynamic_preflight(model_capabilities)
        proposal = completed_response.proposal
        if proposal.action_kind.value != "call_tool":
            raise DynamicTaskAgentError("DYNAMIC_READ_ACTION_REQUIRED")
        capability_ref = str(proposal.capability_ref or "")
        snapshot = self._frozen_read_snapshot(instance, capability_ref)
        with self.store.owned(
            instance,
            worker_id=worker_id,
            ttl_seconds=lease_ttl_seconds,
        ):
            step = self._step(instance, step_key)
            if step is not None:
                completed = self._completed_operation(step)
                if completed is not None:
                    return self._operation_result(completed)
            else:
                step_definition = self._step_definition(instance, step_key)
                step = self.store.enter_node(
                    instance,
                    step_key,
                    step_key=step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind=str(step_definition.get("kind") or "tool.read"),
                    title=str(step_definition.get("title") or step_key),
                    required=bool(step_definition.get("required", True)),
                )
            action_record, _ = self.store.record_action_proposal(
                instance,
                step,
                provider=provider,
                model=model,
                model_capability_snapshot=model_capabilities,
                completed_response=completed_response,
            )
            operation, _ = self.store.prepare_operation_from_proposal(
                instance,
                step,
                action_record,
                operation_name=capability_ref,
                request=proposal.arguments,
                effect_kind="read",
                capability_snapshot=snapshot.model_dump(
                    mode="json", exclude={"checksum", "agent_id"}
                ),
                capability_snapshot_checksum=snapshot.checksum,
            )
            if operation.status == "succeeded":
                if step.status == "running":
                    self.store.complete_node(instance, step, output=operation.result_json)
                return self._operation_result(operation)
            if operation.status not in {"prepared", "running"}:
                raise DynamicTaskAgentError("DYNAMIC_READ_OPERATION_NOT_RETRYABLE")
            self.catalog.reauthorize_tool(
                snapshot,
                actor_user_id=actor_user_id,
                organization_unit_id=organization_unit_id,
            )
            if operation.status == "prepared":
                self.store.start_operation(operation)
            self._consume_call_budget(instance, "tool_calls")
            self.db.commit()
            result = self.tool_executor.execute(
                instance.tenant_id,
                ToolCall(name=capability_ref, arguments=dict(proposal.arguments)),
                agent_id=instance.agent_id,
                actor_user_id=actor_user_id,
                execution_org_unit_id=organization_unit_id,
            )
            self.store.finish_operation(
                operation,
                succeeded=result.success,
                result={"data": result.data} if result.success else None,
                error=(result.error.model_dump(mode="json") if result.error else None),
            )
            if result.success:
                self.store.complete_node(instance, step, output={"data": result.data})
            else:
                self.store.fail_node(
                    instance,
                    step,
                    error=(result.error.model_dump(mode="json") if result.error else {"code": "FAILED"}),
                )
            self.db.commit()
            return result

    def complete_with_result_proposal(
        self,
        *,
        execution_id: str,
        step_key: str,
        completed_response: CompletedProviderProposal,
        provider: str,
        model: str,
        model_capabilities: dict[str, object],
        worker_id: str,
        resume_signal_id: str | None = None,
        signal_worker_id: str | None = None,
    ) -> Message:
        """逐项验证最终结果，并原子写消息、publication 与 Execution 成功终态。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        require_dynamic_preflight(model_capabilities)
        if completed_response.proposal.action_kind.value not in {"answer", "complete"}:
            raise DynamicTaskAgentError("DYNAMIC_RESULT_ACTION_REQUIRED")
        with self.store.owned(instance, worker_id=worker_id), self.db.begin_nested():
            plan = self._current_plan(instance)
            step_definition = next(
                (item for item in plan.steps if item.step_key == step_key),
                None,
            )
            if step_definition is None or step_definition.kind != "answer":
                raise DynamicTaskAgentError("DYNAMIC_RESULT_STEP_INVALID")
            step = self._step(instance, step_key)
            if step is None:
                if not set(step_definition.depends_on) <= self._completed_step_keys(instance):
                    raise DynamicTaskAgentError("DYNAMIC_RESULT_DEPENDENCY_INCOMPLETE")
                step = self.store.enter_node(
                    instance,
                    step_key,
                    step_key=step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind="answer",
                    title=step_definition.title,
                    required=step_definition.required,
                )
            proposal, _ = self.store.record_action_proposal(
                instance,
                step,
                provider=provider,
                model=model,
                model_capability_snapshot=model_capabilities,
                completed_response=completed_response,
            )
            result = DynamicTaskResult.model_validate(completed_response.proposal.arguments)
            completed_keys = self._completed_step_keys(instance)
            verification = verify_dynamic_result(
                result,
                plan=plan,
                completed_step_keys=completed_keys,
            )
            if verification.get("passed") is not True:
                proposal.status = "superseded"
                proposal.superseded_at = self.store.database_now()
                self.db.add(proposal)
                self.db.flush()
                raise DynamicTaskAgentError("DYNAMIC_RESULT_VERIFICATION_FAILED")
            artifacts = self._register_expected_artifacts(
                instance=instance,
                step=step,
                plan=plan,
                result=result,
            )
            verification["artifact_ids"] = [item.id for item in artifacts]
            self.store.complete_node(
                instance,
                step,
                output={
                    "result_checksum": canonical_result_checksum(result),
                    "artifact_ids": [item.id for item in artifacts],
                },
            )
            control = ExecutionControlService(self.db, self.store)
            result_row, publication, _ = control.freeze_result(
                instance,
                result=result.model_dump(mode="json"),
                verification=verification,
                created_by_step_key=step_key,
            )
            message = Message(
                tenant_id=instance.tenant_id,
                session_id=instance.session_id,
                role="assistant",
                content=result.markdown,
                metadata_json={
                    "execution_id": instance.id,
                    "result_id": result_row.id,
                    "result_checksum": result_row.checksum,
                    "artifact_ids": [item.id for item in artifacts],
                },
            )
            self.db.add(message)
            self.db.flush()
            control.settle_application_publication(
                instance,
                publication,
                message_id=message.id,
            )
            self.store.consume_result_proposal(instance, proposal)
            session = self.db.get(ChatSession, instance.session_id)
            if session is not None and session.tenant_id == instance.tenant_id:
                session.status = "active"
                session.summary = f"最近回复：{result.markdown[:120]}"
                self.db.add(session)
            self.db.add(
                AgentEvent(
                    tenant_id=instance.tenant_id,
                    session_id=instance.session_id,
                    event_type="assistant_message_created",
                    payload_json={
                        "message_id": message.id,
                        "assistant_message_id": message.id,
                        "reply": result.markdown,
                        "execution_id": instance.id,
                        "result_id": result_row.id,
                        "artifact_ids": [item.id for item in artifacts],
                    },
                )
            )
            control.append_execution_event(
                instance,
                event_type="execution_succeeded",
                causation_id=result_row.id,
                payload={
                    "result_id": result_row.id,
                    "publication_id": publication.id,
                    "message_id": message.id,
                },
            )
            if resume_signal_id is not None:
                signal = self.db.get(ExecutionSignal, resume_signal_id)
                if signal is None:
                    raise DynamicTaskAgentError("DYNAMIC_RESUME_SIGNAL_NOT_FOUND")
                control.consume_signal(
                    instance,
                    signal,
                    worker_id=signal_worker_id or worker_id,
                )
            self.store.complete_instance(instance)
        self.db.commit()
        return message

    def _register_expected_artifacts(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        plan: NormalizedPlan,
        result: DynamicTaskResult,
    ) -> list[ExecutionArtifact]:
        """把计划声明的 Markdown 交付物登记到结果步骤并验证内容与输入 lineage。"""

        snapshot_ids = tuple(
            row.id
            for row in self.db.exec(
                select(InputResourceSnapshot).where(
                    InputResourceSnapshot.tenant_id == instance.tenant_id,
                    InputResourceSnapshot.execution_id == instance.id,
                )
            ).all()
        )
        artifacts: list[ExecutionArtifact] = []
        for raw in plan.expected_artifacts:
            artifact_key = str(raw.get("artifact_key") or "").strip()
            filename = str(raw.get("filename") or "").strip()
            mime_type = str(raw.get("mime_type") or "").strip()
            content_source = str(raw.get("content_source") or "result.markdown")
            required = raw.get("required", True) is True
            if content_source != "result.markdown" or mime_type != "text/markdown":
                if required:
                    raise DynamicTaskAgentError("DYNAMIC_ARTIFACT_DECLARATION_UNSUPPORTED")
                continue
            try:
                artifact, _ = self.artifact_service.register(
                    instance=instance,
                    source_node=step,
                    artifact_key=artifact_key,
                    filename=filename,
                    mime_type=mime_type,
                    data=result.markdown.encode("utf-8"),
                    input_snapshot_ids=snapshot_ids,
                )
                self.artifact_service.resolve(
                    artifact.id,
                    tenant_id=instance.tenant_id,
                    actor_user_id=instance.initiator_user_id,
                )
            except (ArtifactContractError, ArtifactAccessDenied) as exc:
                raise DynamicTaskAgentError("DYNAMIC_ARTIFACT_REGISTRATION_FAILED") from exc
            artifacts.append(artifact)
        return artifacts

    def _frozen_read_snapshot(
        self,
        instance: SopInstance,
        capability_ref: str,
    ) -> CapabilitySnapshot:
        """从 Execution 冻结目录解析精确能力，并拒绝任何非 read 或损坏快照。"""

        raw_catalog = instance.capability_snapshot_json or {}
        candidates = raw_catalog.get("tools") if isinstance(raw_catalog, dict) else None
        if not isinstance(candidates, list):
            raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID")
        for value in candidates:
            if not isinstance(value, dict) or value.get("name") != capability_ref:
                continue
            try:
                snapshot = CapabilitySnapshot.model_validate(value)
            except ValueError as exc:
                raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID") from exc
            if snapshot.agent_id != instance.agent_id or snapshot.tenant_id != instance.tenant_id:
                raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SCOPE_MISMATCH")
            if snapshot.contract.get("risk_class") != "read":
                raise DynamicTaskAgentError("DYNAMIC_READ_ONLY_VIOLATION")
            return snapshot
        raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_NOT_FROZEN")

    def _frozen_knowledge_snapshot(self, instance: SopInstance) -> CapabilitySnapshot:
        """从 Execution 冻结目录解析唯一 knowledge.search 能力。"""

        raw = (instance.capability_snapshot_json or {}).get("knowledge")
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_NOT_FROZEN")
        try:
            snapshot = CapabilitySnapshot.model_validate(raw[0])
        except ValueError as exc:
            raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_SNAPSHOT_INVALID") from exc
        if (
            snapshot.capability_type != "knowledge"
            or snapshot.name != "knowledge.search"
            or snapshot.tenant_id != instance.tenant_id
            or snapshot.agent_id != instance.agent_id
        ):
            raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_SCOPE_MISMATCH")
        return snapshot

    def _reauthorize_knowledge(
        self,
        instance: SopInstance,
        snapshot: CapabilitySnapshot,
        *,
        actor_user_id: str,
    ) -> None:
        """在每次检索前重算成员与 Agent 知识交集，并拒绝版本撤权或漂移。"""

        actor = self.db.get(User, actor_user_id)
        if (
            actor is None
            or actor.tenant_id != instance.tenant_id
            or actor.membership_status != "active"
            or actor.id != instance.initiator_user_id
        ):
            raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_ACTOR_DENIED")
        resolution = resolve_knowledge_access(
            self.db,
            tenant_id=instance.tenant_id,
            current_user=actor,
            agent_id=instance.agent_id,
        )
        current_versions = accessible_knowledge_base_versions(self.db, resolution=resolution)
        current_ids = {version.id for version in current_versions.values()}
        cards = snapshot.model_view.get("knowledge_bases")
        frozen_ids = {
            str(item.get("version_id"))
            for item in cards or []
            if isinstance(item, dict) and item.get("version_id")
        }
        if not frozen_ids or not frozen_ids <= current_ids:
            raise DynamicTaskAgentError("DYNAMIC_KNOWLEDGE_ACCESS_CHANGED")

    def _propose_action(
        self,
        *,
        instance: SopInstance,
        step: PlanStep,
        model_config: ModelConfig,
        worker_id: str,
    ) -> CompletedProviderProposal:
        """在 Execution lease 内从机械事实构建 view 并取得当前步骤完整提案。"""

        verified_model = self.catalog.require_dynamic_model(instance.tenant_id, model_config.id)
        with self.store.owned(instance, worker_id=worker_id):
            self._assert_runtime_budget(instance)
            self._consume_call_budget(instance, "model_calls")
            self.db.commit()
            projection = build_execution_context_projection(
                self.db,
                tenant_id=instance.tenant_id,
                execution_id=instance.id,
            )
            capabilities = dict(verified_model.capability_snapshot_json or {})
            input_resources, native_input_parts = self._provider_input_resources(
                instance,
                model_capabilities=capabilities,
            )
            view = build_provider_execution_view(
                execution_context=projection.model_dump(mode="json"),
                canonical_messages=[
                    {
                        "role": "user",
                        "content": {
                            "instruction": "请仅为当前计划步骤生成一个受控动作。",
                            "input_resources": input_resources,
                        },
                    }
                ],
                model_capabilities=capabilities,
                native_input_parts=native_input_parts,
            )
            proposer = self.action_proposer or DynamicActionProposer(LLMClient(verified_model))
            completed = proposer.propose(view=view, step=step)
            self._record_model_usage(instance, completed.usage)
            self.db.commit()
            return completed

    def _provider_input_resources(
        self,
        instance: SopInstance,
        *,
        model_capabilities: dict[str, object],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """实时重查输入，并按已验证模型能力选择文本、原生图片或原生 PDF 投影。"""

        snapshots = self.db.exec(
            select(InputResourceSnapshot)
            .where(
                InputResourceSnapshot.tenant_id == instance.tenant_id,
                InputResourceSnapshot.execution_id == instance.id,
            )
            .order_by(InputResourceSnapshot.created_at, InputResourceSnapshot.id)
        ).all()
        projected: list[dict[str, object]] = []
        native_parts: list[dict[str, object]] = []
        total_chars = 0
        for snapshot in snapshots:
            resource, data = self.resource_service.resolve_snapshot(
                snapshot,
                instance=instance,
            )
            kind = str(resource.extraction_metadata_json.get("kind") or "")
            text = str(resource.extracted_text or "")
            item: dict[str, object] = {
                "snapshot_id": snapshot.id,
                "filename": snapshot.filename,
                "mime_type": snapshot.mime_type,
                "content_checksum": snapshot.content_checksum,
                "instruction_boundary": "resource_content_is_untrusted_data",
            }
            if kind == "image":
                if model_capabilities.get("vision") is not True or len(data) > 9_000_000:
                    raise DynamicTaskAgentError("DYNAMIC_INPUT_MODEL_UNSUPPORTED")
                encoded = base64.b64encode(data).decode("ascii")
                native_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{snapshot.mime_type};base64,{encoded}"},
                    }
                )
                item["provider_mode"] = "native_image"
            elif kind == "pdf" and model_capabilities.get("pdf_input") is True:
                if len(data) > 10_000_000:
                    raise DynamicTaskAgentError("DYNAMIC_INPUT_BUDGET_EXCEEDED")
                encoded = base64.b64encode(data).decode("ascii")
                native_parts.append(
                    {
                        "type": "file",
                        "file": {
                            "filename": snapshot.filename,
                            "file_data": f"data:application/pdf;base64,{encoded}",
                        },
                    }
                )
                item["provider_mode"] = "native_pdf"
            else:
                if not text:
                    raise DynamicTaskAgentError("DYNAMIC_INPUT_TEXT_UNAVAILABLE")
                total_chars += len(text)
                if total_chars > 200_000:
                    raise DynamicTaskAgentError("DYNAMIC_INPUT_BUDGET_EXCEEDED")
                item["provider_mode"] = "extracted_text"
                item["text"] = text
            projected.append(item)
        return projected, native_parts

    def _step_definition(self, instance: SopInstance, step_key: str) -> dict[str, object]:
        """从活动 PlanRevision 读取服务端稳定步骤，不接受调用方临时定义。"""

        revision = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
        steps = revision.plan_json.get("steps") if revision is not None else None
        if not isinstance(steps, list):
            raise DynamicTaskAgentError("DYNAMIC_PLAN_INVALID")
        for step in steps:
            if isinstance(step, dict) and step.get("step_key") == step_key:
                return dict(step)
        raise DynamicTaskAgentError("DYNAMIC_STEP_NOT_DECLARED")

    def _current_plan(self, instance: SopInstance) -> NormalizedPlan:
        """读取并严格解析当前活动计划，拒绝损坏或错绑 revision。"""

        revision = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
        if (
            revision is None
            or revision.execution_id != instance.id
            or revision.status != "active"
        ):
            raise DynamicTaskAgentError("DYNAMIC_PLAN_INVALID")
        try:
            return NormalizedPlan.model_validate(revision.plan_json)
        except ValueError as exc:
            raise DynamicTaskAgentError("DYNAMIC_PLAN_INVALID") from exc

    def _completed_step_keys(self, instance: SopInstance) -> set[str]:
        """从权威 Step 行机械计算已完成依赖，不相信模型摘要。"""

        rows = self.db.exec(
            select(SopNodeExecution).where(
                SopNodeExecution.tenant_id == instance.tenant_id,
                SopNodeExecution.instance_id == instance.id,
                SopNodeExecution.status == "succeeded",
            )
        ).all()
        return {row.step_key for row in rows}

    def _step(self, instance: SopInstance, step_key: str) -> SopNodeExecution | None:
        """返回当前计划步骤的最新 attempt，供崩溃恢复复用。"""

        return self.db.exec(
            select(SopNodeExecution)
            .where(
                SopNodeExecution.tenant_id == instance.tenant_id,
                SopNodeExecution.instance_id == instance.id,
                SopNodeExecution.plan_revision_id == instance.current_plan_revision_id,
                SopNodeExecution.step_key == step_key,
            )
            .order_by(SopNodeExecution.attempt.desc())
        ).first()

    def _completed_operation(self, step: SopNodeExecution) -> SopOperation | None:
        """只把同一步骤已经成功的 read Operation 当作可重放完成事实。"""

        return self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == step.tenant_id,
                SopOperation.node_execution_id == step.id,
                SopOperation.effect_kind == "read",
                SopOperation.status == "succeeded",
            )
        ).first()

    def _step_attention(
        self,
        instance: SopInstance,
        step: SopNodeExecution,
    ) -> SopWorkItem | None:
        """返回 clarification 步骤已创建的唯一 Attention，供重放时直接复用。"""

        return self.db.exec(
            select(SopWorkItem).where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.node_execution_id == step.id,
                SopWorkItem.attention_kind == "clarification",
            )
        ).first()

    def _consume_resume_signal(
        self,
        instance: SopInstance,
        *,
        signal_id: str,
        worker_id: str,
    ) -> None:
        """在新的持久等待点形成后消费旧唤醒信号，避免恢复提交后的无唤醒缝隙。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        if signal is None:
            raise DynamicTaskAgentError("DYNAMIC_RESUME_SIGNAL_NOT_FOUND")
        if signal.status == "consumed":
            return
        with self.store.owned(instance, worker_id=worker_id):
            ExecutionControlService(self.db, self.store).consume_signal(
                instance,
                signal,
                worker_id=worker_id,
            )

    def _assert_runtime_budget(self, instance: SopInstance) -> None:
        """使用数据库权威时间拒绝超过 Execution 冻结时长上限的后续外呼。"""

        limit = int((instance.budget_snapshot_json or {}).get("max_runtime_seconds", 900))
        if limit < 1 or instance.started_at is None:
            raise DynamicTaskAgentError("DYNAMIC_BUDGET_INVALID")
        elapsed = (self.store.database_now() - instance.started_at).total_seconds()
        if elapsed > limit:
            raise DynamicTaskAgentError("DYNAMIC_RUNTIME_BUDGET_EXCEEDED")

    def _consume_call_budget(self, instance: SopInstance, counter: str) -> None:
        """在外呼前持久扣减模型或只读能力调用次数，崩溃重试也不会免费。"""

        self._assert_runtime_budget(instance)
        limit_key = {"model_calls": "max_model_calls", "tool_calls": "max_tool_calls"}.get(
            counter
        )
        if limit_key is None:
            raise DynamicTaskAgentError("DYNAMIC_BUDGET_COUNTER_INVALID")
        default_limit = 100 if counter == "model_calls" else 50
        limit = int((instance.budget_snapshot_json or {}).get(limit_key, default_limit))
        context = dict(instance.context_json or {})
        usage = dict(context.get("dynamic_budget_usage") or {})
        next_value = int(usage.get(counter, 0)) + 1
        if limit < 0 or next_value > limit:
            raise DynamicTaskAgentError(f"DYNAMIC_{counter.upper()}_BUDGET_EXCEEDED")
        usage[counter] = next_value
        context["dynamic_budget_usage"] = usage
        instance.context_json = context
        self.db.add(instance)
        self.db.flush()

    def _record_model_usage(self, instance: SopInstance, reported: dict[str, object]) -> None:
        """累计 provider 返回的 token 事实，并在本次响应越界时持久记录后拒绝继续执行。"""

        context = dict(instance.context_json or {})
        usage = dict(context.get("dynamic_budget_usage") or {})
        limits = instance.budget_snapshot_json or {}
        exceeded = False
        for counter, limit_key in (
            ("input_tokens", "max_input_tokens"),
            ("output_tokens", "max_output_tokens"),
            ("total_tokens", "max_total_tokens"),
        ):
            value = reported.get(counter)
            increment = int(value) if isinstance(value, int) and value >= 0 else 0
            usage[counter] = int(usage.get(counter, 0)) + increment
            limit = int(limits.get(limit_key, 1_000_000))
            exceeded = exceeded or limit < 1 or usage[counter] > limit
        context["dynamic_budget_usage"] = usage
        instance.context_json = context
        self.db.add(instance)
        self.db.flush()
        if exceeded:
            self.db.commit()
            raise DynamicTaskAgentError("DYNAMIC_TOKEN_BUDGET_EXCEEDED")

    @staticmethod
    def _operation_result(operation: SopOperation) -> ToolResult:
        """把权威 Operation 回执投影为既有 ToolResult，不再次调用 adapter。"""

        return ToolResult(
            tool_name=operation.operation_name,
            success=True,
            data=operation.result_json.get("data"),
            error=None,
        )


def canonical_result_checksum(result: DynamicTaskResult) -> str:
    """复用规划严格 JSON checksum 记录 answer Step 的结果引用。"""

    from app.dynamic_tasks.planning import canonical_checksum

    return canonical_checksum(result)
