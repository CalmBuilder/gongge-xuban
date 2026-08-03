"""
@Time       : 2026/08/04 01:45
@Author     : zhanglp8181
@File       : agent.py
@CallChain  : Agent Loop/signal worker → DynamicTaskAgent → Execution Store/ToolExecutor
@Description: 以统一 Execution 账本串行推进只读动态动作，并支持崩溃后的安全恢复。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlmodel import Session, select

from app.db.models import (
    ExecutionPlanRevision,
    AgentEvent,
    ChatSession,
    Message,
    ModelConfig,
    SopInstance,
    SopNodeExecution,
    SopOperation,
)
from app.dynamic_tasks.action_proposer import DynamicActionProposer
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, DynamicCapabilityCatalog
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.planning import (
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    SuccessCriterion,
)
from app.dynamic_tasks.provider_view import (
    build_provider_execution_view,
    require_dynamic_preflight,
)
from app.dynamic_tasks.result_verifier import DynamicTaskResult, verify_dynamic_result
from app.llm.client import LLMClient
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
    ) -> None:
        """绑定统一事务、能力目录和既有工具执行器，禁止创建第二套 Runtime。"""

        self.db = db
        self.store = SopExecutionStore(db)
        self.catalog = catalog or DynamicCapabilityCatalog(db)
        self.tool_executor = tool_executor or ToolExecutor(db)
        self.planner = planner
        self.action_proposer = action_proposer

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
            projection = build_execution_context_projection(
                self.db,
                tenant_id=instance.tenant_id,
                execution_id=instance.id,
            )
            capabilities = dict(verified_model.capability_snapshot_json or {})
            view = build_provider_execution_view(
                execution_context=projection.model_dump(mode="json"),
                canonical_messages=[
                    {
                        "role": "user",
                        "content": "请仅为当前计划步骤生成一个受控动作。",
                    }
                ],
                model_capabilities=capabilities,
            )
            proposer = self.action_proposer or DynamicActionProposer(LLMClient(verified_model))
            completed_response = proposer.propose(view=view, step=step)
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
                )
                return DynamicRunOutcome("succeeded", instance.id, message=message)
            return DynamicRunOutcome(
                "waiting",
                instance.id,
                blocking_step_key=step.step_key,
            )
        raise DynamicTaskAgentError("DYNAMIC_STEP_BUDGET_EXHAUSTED")

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
    ) -> tuple[SopInstance, bool]:
        """经模型 preflight、实时能力目录和有界规划创建或复用统一动态 Execution。"""

        verified_model = self.catalog.require_dynamic_model(tenant_id, model_config.id)
        capabilities = [
            *self.catalog.list_tools(tenant_id, agent_id),
            *self.catalog.list_general_skills(tenant_id, agent_id),
        ]
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
        plan = planner.create_plan(
            goal=goal.strip(),
            success_criteria=criteria,
            capabilities=capabilities,
        )
        if any(step.kind not in {"tool.read", "answer"} for step in plan.steps):
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
            "model": {
                "model_config_id": verified_model.id,
                "capabilities": dict(verified_model.capability_snapshot_json or {}),
                "checksum": verified_model.capability_checksum,
            },
        }
        existing = self.store.active_instance(tenant_id, session_id)
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
        self.db.flush()
        return instance, existing is None

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
        if proposal.action_kind.value not in {"call_tool", "query_knowledge"}:
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
    ) -> Message:
        """逐项验证最终结果，并原子写消息、publication 与 Execution 成功终态。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        require_dynamic_preflight(model_capabilities)
        if completed_response.proposal.action_kind.value not in {"answer", "complete"}:
            raise DynamicTaskAgentError("DYNAMIC_RESULT_ACTION_REQUIRED")
        with self.store.owned(instance, worker_id=worker_id):
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
            self.store.complete_node(
                instance,
                step,
                output={"result_checksum": canonical_result_checksum(result)},
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
            self.store.complete_instance(instance)
            self.db.commit()
            return message

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
            projection = build_execution_context_projection(
                self.db,
                tenant_id=instance.tenant_id,
                execution_id=instance.id,
            )
            capabilities = dict(verified_model.capability_snapshot_json or {})
            view = build_provider_execution_view(
                execution_context=projection.model_dump(mode="json"),
                canonical_messages=[
                    {
                        "role": "user",
                        "content": "请仅为当前计划步骤生成一个受控动作。",
                    }
                ],
                model_capabilities=capabilities,
            )
            proposer = self.action_proposer or DynamicActionProposer(LLMClient(verified_model))
            return proposer.propose(view=view, step=step)

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
