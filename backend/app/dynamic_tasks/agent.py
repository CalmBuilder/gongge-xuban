"""
@Time       : 2026/08/10 19:20
@Author     : zhanglp8181
@File       : agent.py
@CallChain  : Agent Loop/signal worker → DynamicTaskAgent → Execution Store/ToolExecutor
@Description: 以统一 Execution 账本推进动态动作、可信 Artifact，并支持崩溃后的安全恢复。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import ValidationError
from sqlmodel import Session, select

from app.connectors.service import ConnectionError, ConnectionService
from app.connectors.runtime import ConnectorRuntimeService
from app.db.models import (
    ActionProposalRecord,
    AgentProfile,
    ExecutionCommand,
    ExecutionArtifact,
    ExecutionPlanRevision,
    ExecutionSignal,
    GeneralSkillUse,
    AgentEvent,
    ChatSession,
    ConnectionProfile,
    ConnectorThreadBinding,
    Message,
    InputResourceSnapshot,
    ManagedInputResource,
    ModelConfig,
    ScheduledTaskRun,
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
    CapabilityAccessDenied,
    CapabilitySnapshot,
    DynamicCapabilityCatalog,
    capability_checksum,
)
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.general_skills.runtime import GeneralSkillRuntimeError, GeneralSkillRuntimeService
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.execution_context import project_result_for_model
from app.dynamic_tasks.explorer import (
    ReadOnlyExploreProposer,
    ReadOnlyExploreReport,
)
from app.dynamic_tasks.planning import (
    CompletedProviderProposal,
    NormalizedPlan,
    PlanReason,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.dynamic_tasks.quotas import DynamicTaskQuotaLimits, DynamicTaskQuotaService
from app.dynamic_tasks.provider_view import (
    build_provider_execution_view,
    require_dynamic_preflight,
)
from app.dynamic_tasks.result_verifier import DynamicTaskResult, verify_dynamic_result
from app.dynamic_tasks.standing_approvals import (
    StandingApprovalMatch,
    match_standing_approval_rule,
    record_standing_rule_hit,
)
from app.knowledge.access import accessible_knowledge_base_versions, resolve_knowledge_access
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.llm.client import LLMClient
from app.config import get_settings
from app.organization.governance import has_governance_permission
from app.organization.permissions import user_permission_codes
from app.security.permissions import can_use_agent_in_chat
from app.session.managed_resources import ManagedInputResourceService
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import (
    SopExecutionSkillAuthorizationError,
    SopExecutionStore,
)
from app.sop_runtime.contracts import IdempotencyPolicy
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


class DynamicTaskAgentError(RuntimeError):
    """表示动态推进在 provider、能力、审批或状态边界被确定性拒绝。"""


_CONNECTION_REAUTH_CODES = frozenset(
    {
        "CONNECTION_REAUTH_REQUIRED",
        "CONNECTION_SCOPE_MISSING",
        "CONNECTION_INVALID_AUTH",
        "CONNECTION_TOKEN_EXPIRED",
        "CONNECTION_TOKEN_REVOKED",
        "CONNECTION_ACCOUNT_INACTIVE",
        "CONNECTION_ACCOUNT_INVALID",
    }
)
_EXPLORE_MAX_MODEL_CALLS = 6
_EXPLORE_MAX_TOOL_CALLS = 5
_EXPLORE_MAX_RUNTIME_SECONDS = 120


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
        execution_id: str | None = None,
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
        connection_service: ConnectionService | None = None,
        explore_proposer: ReadOnlyExploreProposer | None = None,
        explore_enabled: bool | None = None,
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
        self.connection_service = connection_service or ConnectionService(db)
        self.explore_proposer = explore_proposer
        self.quota_limits: DynamicTaskQuotaLimits | None = None
        self.explore_enabled = (
            getattr(get_settings(), "dynamic_task_explore_enabled", False)
            if explore_enabled is None
            else explore_enabled
        )

    def _acquire_tool_quota(self, operation: SopOperation) -> None:
        """生产入口已注入配额时在 dispatch 前占用工具槽，直接领域测试可显式不注入。"""

        if self.quota_limits is None:
            return
        DynamicTaskQuotaService(self.db).acquire_tool_operation(
            operation,
            limit=self.quota_limits.tool,
        )

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
            if instance.status in {"failed", "cancelled", "timed_out"}:
                return DynamicRunOutcome(instance.status, instance.id)
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
                with self.store.owned(instance, worker_id=worker_id):
                    if resume_signal_id is not None:
                        signal = self.db.get(ExecutionSignal, resume_signal_id)
                        if signal is None:
                            raise DynamicTaskAgentError("DYNAMIC_RESUME_SIGNAL_NOT_FOUND")
                        ExecutionControlService(self.db, self.store).consume_signal(
                            instance,
                            signal,
                            worker_id=signal_worker_id or worker_id,
                        )
                    instance.terminal_reason_json = {
                        "code": "DYNAMIC_PLAN_TERMINAL_STEP_MISSING"
                    }
                    self.db.add(instance)
                    self.store.fail_instance(
                        instance,
                        context_patch={
                            "failure_code": "DYNAMIC_PLAN_TERMINAL_STEP_MISSING"
                        },
                    )
                self.db.commit()
                return DynamicRunOutcome("failed", instance.id)
            if step.kind == "tool.read":
                _step, result = self.advance_next_read_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                    organization_unit_id=organization_unit_id,
                )
                self.db.commit()
                if result.error is not None and result.error.code in {
                    "DYNAMIC_REAUTH_REQUIRED",
                    "DYNAMIC_RATE_LIMITED",
                }:
                    return DynamicRunOutcome(
                        "blocked",
                        instance.id,
                        blocking_step_key=step.step_key,
                    )
                continue
            if step.kind == "tool.write":
                if self._planned_step_risk(instance, step) == "local_write":
                    attention = self.advance_next_local_step(
                        execution_id=instance.id,
                        model_config=model_config,
                        worker_id=worker_id,
                        actor_user_id=actor_user_id,
                        step_kind="tool.write",
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
                        blocking_step_key=step.step_key if attention is not None else None,
                    )
                attention = self.advance_next_write_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                )
                if attention is None:
                    self.db.commit()
                    continue
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
                    blocking_step_key=step.step_key if attention is not None else None,
                )
            if step.kind == "tool.execute":
                attention = self.advance_next_local_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                    step_kind="tool.execute",
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
                    blocking_step_key=step.step_key if attention is not None else None,
                )
            if step.kind == "knowledge":
                self.advance_next_knowledge_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                )
                self.db.commit()
                continue
            if step.kind == "explore":
                self.advance_next_explore_step(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                    organization_unit_id=organization_unit_id,
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
                self.db.refresh(instance)
                if instance.status == "waiting":
                    return DynamicRunOutcome(
                        "waiting",
                        instance.id,
                        message=message,
                        blocking_step_key="external_publication",
                    )
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

    def fail_execution(
        self,
        *,
        execution_id: str,
        worker_id: str,
        error_code: str,
    ) -> None:
        """把委托阶段的确定性异常收敛为失败终态，避免入口结束后遗留 running 孤儿。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        if instance.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return
        safe_code = (error_code or "DYNAMIC_EXECUTION_FAILED")[:128]
        with self.store.owned(instance, worker_id=worker_id):
            instance.terminal_reason_json = {"code": safe_code}
            self.db.add(instance)
            self.store.fail_instance(
                instance,
                context_patch={"failure_code": safe_code},
            )
        self.db.commit()

    def advance_next_explore_step(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
        organization_unit_id: str | None = None,
    ) -> ReadOnlyExploreReport:
        """在父 Execution 内串行推进独立只读上下文，并只回写带 Operation 证据的报告。"""

        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        if not self.explore_enabled:
            raise DynamicTaskAgentError("DYNAMIC_EXPLORE_DISABLED")
        verified_model = self.catalog.require_dynamic_model(instance.tenant_id, model_config.id)
        frozen_model = (instance.capability_snapshot_json or {}).get("model", {})
        if (
            not isinstance(frozen_model, dict)
            or frozen_model.get("model_config_id") != verified_model.id
            or frozen_model.get("checksum") != verified_model.capability_checksum
        ):
            raise DynamicTaskAgentError("DYNAMIC_MODEL_SNAPSHOT_CHANGED")
        capabilities = dict(verified_model.capability_snapshot_json or {})
        with self.store.owned(instance, worker_id=worker_id, ttl_seconds=180) as lease:
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            step_definition = next(
                (
                    item
                    for item in plan.steps
                    if item.kind == "explore"
                    and item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if step_definition is None:
                raise DynamicTaskAgentError("DYNAMIC_NO_READY_EXPLORE_STEP")
            snapshots = {
                name: self._frozen_read_snapshot(instance, name)
                for name in step_definition.capability_refs
            }
            if any(
                snapshot.capability_type != "tool"
                or snapshot.contract.get("risk_class") != "read"
                or snapshot.contract.get("explore_safe") is not True
                for snapshot in snapshots.values()
            ):
                raise DynamicTaskAgentError("DYNAMIC_EXPLORE_CAPABILITY_NOT_SAFE")
            step = self._step(instance, step_definition.step_key)
            if step is None:
                step = self.store.enter_node(
                    instance,
                    step_definition.step_key,
                    step_key=step_definition.step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind="explore",
                    title=step_definition.title,
                    required=step_definition.required,
                )
            if step.status == "succeeded":
                return ReadOnlyExploreReport.model_validate(step.output_json)
            if step.status != "running":
                raise DynamicTaskAgentError("DYNAMIC_EXPLORE_STEP_NOT_RUNNING")
            deadline = self._explore_deadline(instance, step)

            while True:
                if self.store.database_now() > deadline:
                    self._fail_explore(
                        instance,
                        step,
                        error_code="DYNAMIC_EXPLORE_RUNTIME_EXCEEDED",
                    )
                operations = self._explore_operations(instance, step)
                failed = next((item for item in operations if item.status == "failed"), None)
                if failed is not None:
                    self._fail_explore(
                        instance,
                        step,
                        error_code=str(
                            (failed.error_json or {}).get("code")
                            or "DYNAMIC_EXPLORE_TOOL_FAILED"
                        ),
                    )
                pending = next(
                    (item for item in operations if item.status in {"prepared", "running"}),
                    None,
                )
                if pending is not None:
                    snapshot = snapshots.get(pending.operation_name)
                    if snapshot is None:
                        self._fail_explore(
                            instance,
                            step,
                            error_code="DYNAMIC_EXPLORE_CAPABILITY_NOT_SAFE",
                        )
                    self._dispatch_explore_operation(
                        instance=instance,
                        step=step,
                        operation=pending,
                        snapshot=snapshot,
                        actor_user_id=actor_user_id,
                        organization_unit_id=organization_unit_id,
                    )
                    lease = self.store.renew(lease, ttl_seconds=180)
                    continue

                skill_guidance = self._step_guidance(instance, step_definition)
                try:
                    usage = self._reserve_explore_model_call(instance, step.step_key)
                except DynamicTaskAgentError as exc:
                    self._fail_explore(instance, step, error_code=str(exc))
                observations = self._explore_observations(operations, snapshots=snapshots)
                remaining_tool_calls = _EXPLORE_MAX_TOOL_CALLS - len(operations)
                self._consume_call_budget(instance, "model_calls")
                self.db.commit()
                proposer = self.explore_proposer or ReadOnlyExploreProposer(
                    LLMClient(verified_model)
                )
                try:
                    completed = proposer.propose(
                        goal=plan.goal,
                        step=step_definition,
                        capabilities=tuple(
                            snapshots[name] for name in step_definition.capability_refs
                        ),
                        observations=observations,
                        remaining_tool_calls=max(0, remaining_tool_calls),
                        general_skill_guidance=skill_guidance,
                    )
                except ValueError:
                    self._fail_explore(
                        instance,
                        step,
                        error_code="DYNAMIC_EXPLORE_PROPOSAL_INVALID",
                    )
                try:
                    self._record_model_usage(instance, completed.usage)
                except DynamicTaskAgentError as exc:
                    self._fail_explore(instance, step, error_code=str(exc))
                lease = self.store.renew(lease, ttl_seconds=180)
                proposal, _ = self.store.record_action_proposal(
                    instance,
                    step,
                    provider=verified_model.provider,
                    model=verified_model.model,
                    model_capability_snapshot=capabilities,
                    completed_response=completed,
                    causation_id=f"explore:{step.id}:{usage}",
                )
                if completed.proposal.action_kind.value == "complete":
                    try:
                        report = self._validate_explore_report(
                            completed.proposal.arguments,
                            operations=operations,
                        )
                    except (DynamicTaskAgentError, ValidationError):
                        proposal.status = "superseded"
                        proposal.superseded_at = self.store.database_now()
                        self.db.add(proposal)
                        self._fail_explore(
                            instance,
                            step,
                            error_code="DYNAMIC_EXPLORE_EVIDENCE_INVALID",
                        )
                    self.store.consume_result_proposal(instance, proposal)
                    self.store.complete_node(
                        instance,
                        step,
                        output=report.model_dump(mode="json"),
                    )
                    self.db.commit()
                    return report
                if remaining_tool_calls < 1:
                    self._fail_explore(
                        instance,
                        step,
                        error_code="DYNAMIC_EXPLORE_TOOL_BUDGET_EXCEEDED",
                    )
                capability_ref = str(completed.proposal.capability_ref or "")
                snapshot = snapshots.get(capability_ref)
                if snapshot is None:
                    self._fail_explore(
                        instance,
                        step,
                        error_code="DYNAMIC_EXPLORE_CAPABILITY_NOT_SAFE",
                    )
                operation, _ = self.store.prepare_operation_from_proposal(
                    instance,
                    step,
                    proposal,
                    operation_name=capability_ref,
                    request=completed.proposal.arguments,
                    effect_kind="read",
                    caused_by_skill_use_ids=step_definition.guidance_skill_use_ids,
                    capability_snapshot=snapshot.model_dump(
                        mode="json", exclude={"checksum", "agent_id"}
                    ),
                    capability_snapshot_checksum=snapshot.checksum,
                )
                self._dispatch_explore_operation(
                    instance=instance,
                    step=step,
                    operation=operation,
                    snapshot=snapshot,
                    actor_user_id=actor_user_id,
                    organization_unit_id=organization_unit_id,
                )
                lease = self.store.renew(lease, ttl_seconds=180)

    def _reserve_explore_model_call(self, instance: SopInstance, step_key: str) -> int:
        """在 provider 外呼前持久预扣 Step 轮次，使崩溃不能重置探索上限。"""

        context = dict(instance.context_json or {})
        usage = dict(context.get("explore_model_calls") or {})
        reserved = int(usage.get(step_key, 0)) + 1
        if reserved > _EXPLORE_MAX_MODEL_CALLS:
            raise DynamicTaskAgentError("DYNAMIC_EXPLORE_MODEL_BUDGET_EXCEEDED")
        usage[step_key] = reserved
        context["explore_model_calls"] = usage
        instance.context_json = context
        self.db.add(instance)
        self.db.flush()
        return reserved

    def _explore_deadline(
        self,
        instance: SopInstance,
        step: SopNodeExecution,
    ) -> datetime:
        """以数据库权威时间冻结 Step 截止点，跨进程恢复不重新获得时长。"""

        context = dict(instance.context_json or {})
        deadlines = dict(context.get("explore_deadlines") or {})
        raw = deadlines.get(step.step_key)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                self._fail_explore(
                    instance,
                    step,
                    error_code="DYNAMIC_EXPLORE_DEADLINE_INVALID",
                )
        deadline = self.store.database_now() + timedelta(seconds=_EXPLORE_MAX_RUNTIME_SECONDS)
        deadlines[step.step_key] = deadline.isoformat()
        context["explore_deadlines"] = deadlines
        instance.context_json = context
        self.db.add(instance)
        self.db.flush()
        return deadline

    def _dispatch_explore_operation(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
        actor_user_id: str,
        organization_unit_id: str | None,
    ) -> None:
        """逐次重授权并串行派发一个可安全重试的纯读探索 Operation。"""

        try:
            self.catalog.reauthorize_tool(
                snapshot,
                actor_user_id=actor_user_id,
                organization_unit_id=organization_unit_id,
            )
        except CapabilityAccessDenied as exc:
            if operation.status == "prepared":
                self.store.cancel_prepared_operation(operation)
            elif operation.status == "running":
                self.store.finish_operation(
                    operation,
                    succeeded=False,
                    error={"code": str(exc)},
                )
            self._fail_explore(instance, step, error_code=str(exc))
        if operation.status == "prepared":
            self._acquire_tool_quota(operation)
            self.store.start_operation(operation)
            self._consume_call_budget(instance, "tool_calls")
        elif operation.status != "running":
            return
        self.db.commit()
        result = self.tool_executor.execute(
            instance.tenant_id,
            ToolCall(name=operation.operation_name, arguments=dict(operation.request_json or {})),
            agent_id=instance.agent_id,
            actor_user_id=actor_user_id,
            execution_org_unit_id=organization_unit_id,
            execution_id=instance.id,
        )
        self.store.finish_operation(
            operation,
            succeeded=result.success,
            result={"data": result.data} if result.success else None,
            error=(result.error.model_dump(mode="json") if result.error else {"code": "FAILED"}),
        )
        self.db.commit()

    def _explore_operations(
        self,
        instance: SopInstance,
        step: SopNodeExecution,
    ) -> list[SopOperation]:
        """按创建顺序读取本探索 Step 的 Operation，作为恢复与报告证据权威来源。"""

        return list(
            self.db.exec(
                select(SopOperation)
                .where(
                    SopOperation.tenant_id == instance.tenant_id,
                    SopOperation.instance_id == instance.id,
                    SopOperation.node_execution_id == step.id,
                    SopOperation.effect_kind == "read",
                )
                .order_by(SopOperation.created_at, SopOperation.id)
            ).all()
        )

    @staticmethod
    def _explore_observations(
        operations: Sequence[SopOperation],
        *,
        snapshots: Mapping[str, CapabilitySnapshot],
    ) -> tuple[dict[str, object], ...]:
        """只投影成功回执的发布 schema，不把完整 adapter 数据带入探索上下文。"""

        observations: list[dict[str, object]] = []
        for operation in operations:
            snapshot = snapshots.get(operation.operation_name)
            schema = snapshot.model_view.get("output_schema") if snapshot is not None else None
            if operation.status != "succeeded" or not isinstance(schema, Mapping):
                continue
            observations.append(
                {
                    "operation_id": operation.id,
                    "capability_ref": operation.operation_name,
                    "result": project_result_for_model(
                        (operation.result_json or {}).get("data"),
                        schema,
                    ),
                }
            )
        return tuple(observations)

    @staticmethod
    def _validate_explore_report(
        value: Mapping[str, object],
        *,
        operations: Sequence[SopOperation],
    ) -> ReadOnlyExploreReport:
        """要求每个报告引用精确匹配本 Step 已成功 Operation 及其能力。"""

        report = ReadOnlyExploreReport.model_validate(value)
        available = {
            (operation.id, operation.operation_name)
            for operation in operations
            if operation.status == "succeeded"
        }
        cited = {(item.operation_id, item.capability_ref) for item in report.evidence}
        if not cited <= available:
            raise DynamicTaskAgentError("DYNAMIC_EXPLORE_EVIDENCE_INVALID")
        return report

    def _fail_explore(
        self,
        instance: SopInstance,
        step: SopNodeExecution,
        *,
        error_code: str,
    ) -> None:
        """以统一 Step/Execution 终态收敛探索失败，不创建 Attention 或部分成功报告。"""

        code = error_code[:128]
        if step.status == "running":
            self.store.fail_node(instance, step, error={"code": code})
        instance.terminal_reason_json = {"code": code}
        self.db.add(instance)
        self.store.fail_instance(instance, context_patch={"failure_code": code})
        self.db.commit()
        raise DynamicTaskAgentError(code)

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

    def advance_next_write_step(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> SopWorkItem | None:
        """冻结 external-write；精确长期规则命中时派发，否则创建一次性审批。"""

        if not get_settings().dynamic_task_external_write_enabled:
            raise DynamicTaskAgentError("DYNAMIC_EXTERNAL_WRITE_DISABLED")
        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        dispatch: tuple[
            StandingApprovalMatch,
            SopNodeExecution,
            SopOperation,
            CapabilitySnapshot,
            dict[str, object],
            ExecutionSignal,
        ] | None = None
        attention: SopWorkItem | None = None
        with self.store.owned(instance, worker_id=worker_id):
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            step_definition = next(
                (
                    item
                    for item in plan.steps
                    if item.kind == "tool.write"
                    and item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if step_definition is None:
                raise DynamicTaskAgentError("DYNAMIC_NO_READY_WRITE_STEP")
            step = self._step(instance, step_definition.step_key)
            if step is not None:
                operation = self.db.exec(
                    select(SopOperation).where(
                        SopOperation.tenant_id == instance.tenant_id,
                        SopOperation.node_execution_id == step.id,
                        SopOperation.effect_kind == "external_write",
                    )
                ).first()
                if operation is not None:
                    attention = self.db.exec(
                        select(SopWorkItem)
                        .where(
                            SopWorkItem.tenant_id == instance.tenant_id,
                            SopWorkItem.instance_id == instance.id,
                            SopWorkItem.node_execution_id == step.id,
                            SopWorkItem.attention_kind.in_(("tool_approval", "exception")),
                        )
                        .order_by(SopWorkItem.created_at.desc())
                    ).first()
                    if operation.status in {"prepared", "unknown"} and attention is not None:
                        return attention
                    raise DynamicTaskAgentError("DYNAMIC_WRITE_OPERATION_NOT_RETRYABLE")
            else:
                step = self.store.enter_node(
                    instance,
                    step_definition.step_key,
                    step_key=step_definition.step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind="tool.write",
                    title=step_definition.title,
                    required=step_definition.required,
                )
            completed_response = self._propose_action(
                instance=instance,
                step=step_definition,
                model_config=model_config,
                worker_id=worker_id,
            )
            proposal = completed_response.proposal
            if proposal.action_kind.value != "call_tool":
                raise DynamicTaskAgentError("DYNAMIC_WRITE_ACTION_REQUIRED")
            capability_ref = str(proposal.capability_ref or "")
            snapshot = self._frozen_write_snapshot(instance, capability_ref)
            arguments = dict(proposal.arguments)
            content = str(arguments.get("content") or "")
            if set(arguments) != {"content"} or not content.strip() or len(content) > 4000:
                raise DynamicTaskAgentError("DYNAMIC_WRITE_ARGUMENTS_INVALID")
            action_record, _ = self.store.record_action_proposal(
                instance,
                step,
                provider=model_config.provider,
                model=model_config.model,
                model_capability_snapshot=dict(model_config.capability_snapshot_json or {}),
                completed_response=completed_response,
            )
            operation, _ = self.store.prepare_operation_from_proposal(
                instance,
                step,
                action_record,
                operation_name=capability_ref,
                request=arguments,
                idempotency_policy=IdempotencyPolicy(),
                effect_kind="external_write",
                caused_by_skill_use_ids=step_definition.guidance_skill_use_ids,
                capability_snapshot=snapshot.model_dump(
                    mode="json", exclude={"checksum", "agent_id"}
                ),
                capability_snapshot_checksum=snapshot.checksum,
            )
            match = match_standing_approval_rule(
                self.db,
                instance=instance,
                snapshot=snapshot,
                arguments=arguments,
            )
            connection_evidence: dict[str, object] | None = None
            if match is not None:
                try:
                    connection_evidence = self.connection_service.validate_wecom_message_dispatch(
                        tenant_id=instance.tenant_id,
                        profile_id=snapshot.capability_id,
                        agent_id=instance.agent_id,
                        actor_user_id=match.authorization_actor_user_id,
                        thread_binding_id=str(snapshot.audit_view.get("thread_binding_id") or ""),
                        expected_profile_revision=int(snapshot.contract.get("profile_revision") or 0),
                        expected_secret_revision=int(snapshot.contract.get("secret_revision") or 0),
                        expected_binding_revision=int(snapshot.contract.get("binding_revision") or 0),
                    )
                except ConnectionError:
                    match = None
            if match is not None and connection_evidence is not None:
                evidence = {**connection_evidence, **match.evidence}
                self._acquire_tool_quota(operation)
                self.store.authorize_external_operation_dispatch(
                    operation,
                    approval_work_item_id=None,
                    approval_fingerprint=capability_checksum(evidence),
                    approved_by_user_id=match.authorization_actor_user_id,
                    authorization_evidence=evidence,
                    authorization_source_type="standing_rule",
                    authorization_source_ref=f"{match.rule.id}:{match.rule.revision}",
                )
                record_standing_rule_hit(
                    self.db,
                    match=match,
                    instance=instance,
                    operation=operation,
                )
                control = ExecutionControlService(self.db, self.store)
                recovery_signal = control.enqueue_signal(
                    instance,
                    signal_type="operation_settled",
                    causation_type="standing_rule_dispatch",
                    causation_id=operation.id,
                    payload={
                        "kind": "standing_rule_dispatch",
                        "operation_id": operation.id,
                        "node_execution_id": step.id,
                        "rule_id": match.rule.id,
                        "rule_revision": match.rule.revision,
                    },
                    priority=20,
                )
                control.claim_signal(recovery_signal, worker_id=worker_id, ttl_seconds=300)
                self._consume_call_budget(instance, "tool_calls")
                dispatch = (match, step, operation, snapshot, arguments, recovery_signal)
            else:
                attention = self._offer_write_approval(
                    instance=instance,
                    step=step,
                    operation=operation,
                    snapshot=snapshot,
                    content=content,
                )
        self.db.commit()
        if attention is not None:
            return attention
        if dispatch is None:
            raise DynamicTaskAgentError("DYNAMIC_WRITE_AUTHORIZATION_MISSING")
        match, step, operation, snapshot, arguments, recovery_signal = dispatch
        return self._dispatch_standing_write(
            instance=instance,
            step=step,
            operation=operation,
            snapshot=snapshot,
            arguments=arguments,
            match=match,
            worker_id=worker_id,
            recovery_signal=recovery_signal,
        )

    def advance_next_local_step(
        self,
        *,
        execution_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
        step_kind: str,
    ) -> SopWorkItem:
        """冻结受管工作区本地写或隔离检查，并始终创建一次性人工批准。"""

        if not get_settings().dynamic_task_managed_workspace_enabled:
            raise DynamicTaskAgentError("DYNAMIC_MANAGED_WORKSPACE_DISABLED")
        if step_kind not in {"tool.write", "tool.execute"}:
            raise DynamicTaskAgentError("DYNAMIC_LOCAL_STEP_KIND_INVALID")
        instance = self.db.get(SopInstance, execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        expected_effect = "local_write" if step_kind == "tool.write" else "execute"
        with self.store.owned(instance, worker_id=worker_id):
            plan = self._current_plan(instance)
            completed_keys = self._completed_step_keys(instance)
            definition = next(
                (
                    item
                    for item in plan.steps
                    if item.kind == step_kind
                    and item.step_key not in completed_keys
                    and set(item.depends_on) <= completed_keys
                ),
                None,
            )
            if definition is None:
                raise DynamicTaskAgentError("DYNAMIC_NO_READY_LOCAL_STEP")
            step = self._step(instance, definition.step_key)
            action_record: ActionProposalRecord | None = None
            if step is not None:
                operation = self.db.exec(
                    select(SopOperation).where(
                        SopOperation.tenant_id == instance.tenant_id,
                        SopOperation.node_execution_id == step.id,
                        SopOperation.effect_kind == expected_effect,
                    )
                ).first()
                attention = self.db.exec(
                    select(SopWorkItem)
                    .where(
                        SopWorkItem.tenant_id == instance.tenant_id,
                        SopWorkItem.instance_id == instance.id,
                        SopWorkItem.node_execution_id == step.id,
                        SopWorkItem.attention_kind == "tool_approval",
                    )
                    .order_by(SopWorkItem.created_at.desc())
                ).first()
                if operation is not None and operation.status in {"prepared", "running"}:
                    if attention is None:
                        raise DynamicTaskAgentError("DYNAMIC_LOCAL_APPROVAL_MISSING")
                    return attention
                if operation is not None or step.status != "running":
                    raise DynamicTaskAgentError("DYNAMIC_LOCAL_OPERATION_NOT_RETRYABLE")
                action_record = self.db.exec(
                    select(ActionProposalRecord).where(
                        ActionProposalRecord.tenant_id == instance.tenant_id,
                        ActionProposalRecord.execution_id == instance.id,
                        ActionProposalRecord.plan_revision_id
                        == instance.current_plan_revision_id,
                        ActionProposalRecord.step_key == step.step_key,
                        ActionProposalRecord.step_attempt == step.attempt,
                        ActionProposalRecord.status == "validated",
                    )
                ).first()
            else:
                step = self.store.enter_node(
                    instance,
                    definition.step_key,
                    step_key=definition.step_key,
                    plan_revision_id=instance.current_plan_revision_id,
                    step_kind=step_kind,
                    title=definition.title,
                    required=definition.required,
                )
            if action_record is None:
                completed = self._propose_action(
                    instance=instance,
                    step=definition,
                    model_config=model_config,
                    worker_id=worker_id,
                )
                proposal = completed.proposal
                action_record, _ = self.store.record_action_proposal(
                    instance,
                    step,
                    provider=model_config.provider,
                    model=model_config.model,
                    model_capability_snapshot=dict(model_config.capability_snapshot_json or {}),
                    completed_response=completed,
                )
            else:
                try:
                    proposal = RuntimeActionProposal.model_validate(
                        action_record.normalized_proposal_json
                    )
                except ValidationError as exc:
                    raise DynamicTaskAgentError(
                        "DYNAMIC_LOCAL_ACTION_RECORD_INVALID"
                    ) from exc
            if proposal.action_kind.value != "call_tool":
                raise DynamicTaskAgentError("DYNAMIC_LOCAL_ACTION_REQUIRED")
            capability_ref = str(proposal.capability_ref or "")
            snapshot = self._frozen_local_snapshot(
                instance,
                capability_ref,
                expected_risk=expected_effect,
            )
            arguments = dict(proposal.arguments)
            self._validate_workspace_arguments(snapshot, arguments)
            operation, _ = self.store.prepare_operation_from_proposal(
                instance,
                step,
                action_record,
                operation_name=capability_ref,
                request=arguments,
                idempotency_policy=IdempotencyPolicy(),
                effect_kind=expected_effect,
                caused_by_skill_use_ids=definition.guidance_skill_use_ids,
                capability_snapshot=snapshot.model_dump(
                    mode="json", exclude={"checksum", "agent_id"}
                ),
                capability_snapshot_checksum=snapshot.checksum,
            )
            attention = self._offer_local_approval(
                instance=instance,
                step=step,
                operation=operation,
                snapshot=snapshot,
                arguments=arguments,
            )
        self.db.commit()
        return attention

    def _offer_local_approval(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
        arguments: Mapping[str, object],
    ) -> SopWorkItem:
        """为精确本地动作生成脱敏参数、能力修订和过期时间绑定的一次性审批。"""

        approver_ids = self._workspace_approver_ids(
            instance.tenant_id,
            exclude_user_id=instance.initiator_user_id,
        )
        if not approver_ids:
            raise DynamicTaskAgentError("DYNAMIC_LOCAL_APPROVER_UNAVAILABLE")
        expires_at = self.store.database_now() + timedelta(minutes=15)
        payload: dict[str, object] = {
            "operation_id": operation.id,
            "operation_name": operation.operation_name,
            "arguments": dict(arguments),
            "request_fingerprint": operation.request_fingerprint,
            "capability_checksum": snapshot.checksum,
            "workspace": dict(snapshot.audit_view.get("managed_workspace") or {}),
            "execution_id": instance.id,
            "plan_revision_id": instance.current_plan_revision_id,
            "expires_at": expires_at.isoformat(),
        }
        payload["approval_fingerprint"] = capability_checksum(payload)
        control = ExecutionControlService(self.db, self.store)
        attention, created = control.offer_attention(
            instance,
            attention_kind="tool_approval",
            attention_key=f"{step.step_key}:local_approval:{operation.id}",
            title=(
                "批准受管代码工作区执行检查"
                if operation.effect_kind == "execute"
                else "批准受管代码工作区变更"
            ),
            payload=payload,
            allowed_commands=["allow_once", "deny"],
            candidate_user_ids=approver_ids,
            source_type="dynamic_task",
            source_ref=operation.id,
            node_execution=step,
            exclude_initiator=True,
        )
        if created:
            attention.expires_at = expires_at
            self.db.add(attention)
        if step.status == "running":
            self.store.wait_for_work_item(instance, step, work_item_id=attention.id)
        return attention

    def _workspace_approver_ids(self, tenant_id: str, *, exclude_user_id: str) -> list[str]:
        """仅允许活动租户管理员审批代码变更，并保持发起人与批准人分离。"""

        return [
            user.id
            for user in self.db.exec(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.membership_status == "active",
                    User.role == "admin",
                    User.id != exclude_user_id,
                )
            ).all()
        ]

    def _offer_write_approval(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
        content: str,
    ) -> SopWorkItem:
        """为未命中长期规则或发生任何漂移的写动作创建原有一次性审批。"""

        approver_ids = self.catalog.write_approver_ids(
            instance.tenant_id,
            exclude_user_id=instance.initiator_user_id,
        )
        expires_at = self.store.database_now() + timedelta(minutes=15)
        approval_payload: dict[str, object] = {
            "operation_id": operation.id,
            "operation_name": operation.operation_name,
            "content": content,
            "content_checksum": capability_checksum(content),
            "request_fingerprint": operation.request_fingerprint,
            "capability_checksum": snapshot.checksum,
            "canonical_target": snapshot.contract.get("canonical_target"),
            "target_checksum": snapshot.contract.get("target_checksum"),
            "profile_id": snapshot.capability_id,
            "profile_revision": snapshot.contract.get("profile_revision"),
            "secret_revision": snapshot.contract.get("secret_revision"),
            "binding_id": snapshot.audit_view.get("binding_id"),
            "binding_revision": snapshot.contract.get("binding_revision"),
            "thread_binding_id": snapshot.audit_view.get("thread_binding_id"),
            "execution_id": instance.id,
            "plan_revision_id": instance.current_plan_revision_id,
            "expires_at": expires_at.isoformat(),
        }
        approval_payload["approval_fingerprint"] = capability_checksum(approval_payload)
        control = ExecutionControlService(self.db, self.store)
        attention, created = control.offer_attention(
            instance,
            attention_kind="tool_approval",
            attention_key=f"{step.step_key}:tool_approval:{operation.id}",
            title="批准企业微信消息发送",
            payload=approval_payload,
            allowed_commands=["allow_once", "deny"],
            candidate_user_ids=approver_ids,
            source_type="dynamic_task",
            source_ref=operation.id,
            node_execution=step,
            exclude_initiator=True,
        )
        if created:
            attention.expires_at = expires_at
            self.db.add(attention)
            control.append_execution_event(
                instance,
                event_type="external_write_approval_required",
                causation_id=attention.id,
                payload={
                    "attention_id": attention.id,
                    "operation_id": operation.id,
                    "approval_fingerprint": approval_payload["approval_fingerprint"],
                },
            )
        if step.status == "running":
            self.store.wait_for_work_item(instance, step, work_item_id=attention.id)
        return attention

    def _dispatch_standing_write(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
        arguments: Mapping[str, object],
        match: StandingApprovalMatch,
        worker_id: str,
        recovery_signal: ExecutionSignal,
    ) -> SopWorkItem | None:
        """提交长期授权事实后唯一外呼，并将成功、确定失败或 unknown 各自持久闭合。"""

        try:
            result = self.connection_service.send_wecom_approved_message(
                tenant_id=instance.tenant_id,
                profile_id=snapshot.capability_id,
                agent_id=instance.agent_id,
                actor_user_id=match.authorization_actor_user_id,
                thread_binding_id=str(snapshot.audit_view.get("thread_binding_id") or ""),
                content=str(arguments.get("content") or ""),
                expected_profile_revision=int(snapshot.contract.get("profile_revision") or 0),
                expected_secret_revision=int(snapshot.contract.get("secret_revision") or 0),
                expected_binding_revision=int(snapshot.contract.get("binding_revision") or 0),
            )
        except ConnectionError as exc:
            self._fail_standing_write(
                instance=instance,
                step=step,
                operation=operation,
                worker_id=worker_id,
                error_code=exc.code,
                recovery_signal=recovery_signal,
            )
            return None
        if result.success:
            with self.store.owned(instance, worker_id=worker_id):
                data = {
                    "delivery_status": "sent",
                    "message_id": str(result.data.get("message_id") or ""),
                }
                self.store.finish_operation(
                    operation,
                    succeeded=True,
                    result={"data": data},
                    external_reference=data["message_id"] or None,
                )
                self.store.complete_node(instance, step, output={"data": data})
                ExecutionControlService(self.db, self.store).consume_signal(
                    instance,
                    recovery_signal,
                    worker_id=worker_id,
                )
            self.db.commit()
            return None
        if result.error_code in {"WECOM_DELIVERY_UNKNOWN", "WECOM_PARTIAL_DELIVERY"}:
            self._park_unknown_write(
                instance=instance,
                step=step,
                operation=operation,
                worker_id=worker_id,
                error_code=str(result.error_code),
                signal=recovery_signal,
            )
            return self.db.exec(
                select(SopWorkItem)
                .where(
                    SopWorkItem.tenant_id == instance.tenant_id,
                    SopWorkItem.instance_id == instance.id,
                    SopWorkItem.attention_kind == "exception",
                    SopWorkItem.source_ref == operation.id,
                )
                .order_by(SopWorkItem.created_at.desc())
            ).first()
        self._fail_standing_write(
            instance=instance,
            step=step,
            operation=operation,
            worker_id=worker_id,
            error_code=str(result.error_code or "WECOM_DELIVERY_FAILED"),
            recovery_signal=recovery_signal,
        )
        return None

    def _fail_standing_write(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        worker_id: str,
        error_code: str,
        recovery_signal: ExecutionSignal,
    ) -> None:
        """收敛长期授权后可证明未成功的派发，并保留规则命中和 Operation 审计。"""

        with self.store.owned(instance, worker_id=worker_id):
            self.store.finish_operation(operation, succeeded=False, error={"code": error_code})
            self.store.fail_node(instance, step, error={"code": error_code})
            ExecutionControlService(self.db, self.store).consume_signal(
                instance,
                recovery_signal,
                worker_id=worker_id,
            )
            instance.terminal_reason_json = {"code": error_code[:128]}
            self.db.add(instance)
            self.store.fail_instance(
                instance,
                context_patch={"failure_code": error_code[:128]},
            )
        self.db.commit()

    def resume_standing_dispatch_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
    ) -> DynamicRunOutcome:
        """恢复长期授权派发窗口；无确定回执时只转人工对账，永不自动重发。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal is not None else None
        if (
            signal is None
            or signal.signal_type != "operation_settled"
            or signal.payload_json.get("kind") != "standing_rule_dispatch"
            or instance is None
            or instance.kind != "dynamic_task"
        ):
            raise DynamicTaskAgentError("DYNAMIC_STANDING_DISPATCH_SIGNAL_INVALID")
        if signal.status == "consumed":
            if instance.status in {"failed", "cancelled", "timed_out"}:
                return DynamicRunOutcome(instance.status, instance.id)
            if instance.status == "waiting":
                step_key = str(signal.payload_json.get("node_execution_id") or "")
                return DynamicRunOutcome("waiting", instance.id, blocking_step_key=step_key)
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control = ExecutionControlService(self.db, self.store)
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        operation = self.db.get(
            SopOperation,
            str(signal.payload_json.get("operation_id") or ""),
        )
        step = self.db.get(
            SopNodeExecution,
            str(signal.payload_json.get("node_execution_id") or ""),
        )
        if (
            operation is None
            or step is None
            or operation.instance_id != instance.id
            or operation.node_execution_id != step.id
            or operation.authorization_source_type != "standing_rule"
        ):
            raise DynamicTaskAgentError("DYNAMIC_STANDING_DISPATCH_OPERATION_INVALID")
        if operation.status == "running" and step.status == "running":
            return self._park_unknown_write(
                instance=instance,
                step=step,
                operation=operation,
                worker_id=worker_id,
                error_code="DYNAMIC_STANDING_DISPATCH_INTERRUPTED",
                signal=signal,
                control=control,
            )
        if operation.status == "succeeded" and step.status == "succeeded":
            with self.store.owned(instance, worker_id=worker_id):
                control.consume_signal(instance, signal, worker_id=worker_id)
            self.db.commit()
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        raise DynamicTaskAgentError("DYNAMIC_STANDING_DISPATCH_OPERATION_INVALID")

    def resume_tool_approval_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> DynamicRunOutcome:
        """办理一次性写批准；派发前重授权，效果未知时转异常对账而不重发。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        if signal is None or signal.signal_type != "attention_decided":
            raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_SIGNAL_INVALID")
        instance = self.db.get(SopInstance, signal.execution_id)
        if instance is None or instance.kind != "dynamic_task":
            raise DynamicTaskAgentError("DYNAMIC_EXECUTION_NOT_FOUND")
        attention_probe = self.db.get(
            SopWorkItem,
            str(signal.payload_json.get("attention_id") or ""),
        )
        operation_probe = (
            self.db.get(
                SopOperation,
                str(attention_probe.payload_json.get("operation_id") or ""),
            )
            if attention_probe is not None
            else None
        )
        if operation_probe is not None and operation_probe.effect_kind in {
            "local_write",
            "execute",
        }:
            return self._resume_local_tool_approval_signal(
                signal=signal,
                instance=instance,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        if signal.status == "consumed":
            if instance.status in {"failed", "cancelled", "timed_out"}:
                return DynamicRunOutcome(instance.status, instance.id)
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control = ExecutionControlService(self.db, self.store)
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        with self.store.owned(instance, worker_id=worker_id):
            attention = self.db.get(
                SopWorkItem,
                str(signal.payload_json.get("attention_id") or ""),
            )
            if (
                attention is None
                or attention.instance_id != instance.id
                or attention.attention_kind != "tool_approval"
                or attention.status != "completed"
                or str(attention.resolution_json.get("actor_user_id") or "")
                != actor_user_id
            ):
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_RESOLUTION_INVALID")
            operation = self.db.get(
                SopOperation,
                str(attention.payload_json.get("operation_id") or ""),
            )
            step = self.db.get(
                SopNodeExecution,
                attention.node_execution_id
                or str(attention.payload_json.get("node_execution_id") or ""),
            )
            if (
                operation is None
                or step is None
                or operation.instance_id != instance.id
                or operation.node_execution_id != step.id
            ):
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_OPERATION_INVALID")
            if (
                operation.status == "running"
                and step.status == "running"
                and operation.approval_work_item_id == attention.id
            ):
                return self._park_unknown_write(
                    instance=instance,
                    step=step,
                    operation=operation,
                    worker_id=worker_id,
                    error_code="DYNAMIC_WRITE_DISPATCH_INTERRUPTED",
                    signal=signal,
                    control=control,
                )
            if operation.status == "succeeded" and step.status == "succeeded":
                self.db.commit()
                return self.run_until_blocked_or_complete(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=instance.initiator_user_id,
                    resume_signal_id=signal.id,
                    signal_worker_id=worker_id,
                )
            if operation.status != "prepared" or step.status != "waiting":
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_OPERATION_INVALID")
            command = str(attention.resolution_json.get("command") or "")
            if command == "deny":
                self.store.cancel_prepared_operation(operation)
                self.store.resume_waiting_node(instance, step, slots=instance.slots_json or {})
                self.store.fail_node(instance, step, error={"code": "DYNAMIC_WRITE_DENIED"})
                control.consume_signal(instance, signal, worker_id=worker_id)
                instance.terminal_reason_json = {"code": "DYNAMIC_WRITE_DENIED"}
                self.db.add(instance)
                self.store.fail_instance(
                    instance,
                    context_patch={"failure_code": "DYNAMIC_WRITE_DENIED"},
                )
                self.db.commit()
                return DynamicRunOutcome("failed", instance.id)
            if command != "allow_once":
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_COMMAND_INVALID")
            if not get_settings().dynamic_task_external_write_enabled:
                raise DynamicTaskAgentError("DYNAMIC_EXTERNAL_WRITE_DISABLED")
            payload = dict(attention.payload_json or {})
            frozen_fingerprint = str(payload.pop("approval_fingerprint", ""))
            if (
                not frozen_fingerprint
                or capability_checksum(payload) != frozen_fingerprint
                or attention.expires_at is None
                or attention.expires_at <= self.store.database_now()
                or payload.get("request_fingerprint") != operation.request_fingerprint
                or payload.get("plan_revision_id") != instance.current_plan_revision_id
            ):
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_FINGERPRINT_INVALID")
            snapshot = self._frozen_write_snapshot(instance, operation.operation_name)
            if payload.get("capability_checksum") != snapshot.checksum:
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_CAPABILITY_CHANGED")
            try:
                evidence = self.connection_service.validate_wecom_message_dispatch(
                    tenant_id=instance.tenant_id,
                    profile_id=snapshot.capability_id,
                    agent_id=instance.agent_id,
                    actor_user_id=actor_user_id,
                    thread_binding_id=str(payload.get("thread_binding_id") or ""),
                    expected_profile_revision=int(payload.get("profile_revision") or 0),
                    expected_secret_revision=int(payload.get("secret_revision") or 0),
                    expected_binding_revision=int(payload.get("binding_revision") or 0),
                )
            except ConnectionError as exc:
                if exc.code == "CONNECTION_APPROVAL_REVISION_CHANGED":
                    return self._refresh_write_approval(
                        instance=instance,
                        step=step,
                        operation=operation,
                        attention=attention,
                        signal=signal,
                        control=control,
                        actor_user_id=actor_user_id,
                    )
                return self._fail_prepared_write(
                    instance=instance,
                    step=step,
                    operation=operation,
                    signal=signal,
                    control=control,
                    error_code=exc.code,
                )
            try:
                self.store.authorize_operation_skill_causes(operation)
            except SopExecutionSkillAuthorizationError as exc:
                return self._fail_prepared_write(
                    instance=instance,
                    step=step,
                    operation=operation,
                    signal=signal,
                    control=control,
                    error_code=exc.authorization_code,
                )
            self.store.resume_waiting_node(instance, step, slots=instance.slots_json or {})
            self._acquire_tool_quota(operation)
            try:
                self.store.authorize_external_operation_dispatch(
                    operation,
                    approval_work_item_id=attention.id,
                    approval_fingerprint=frozen_fingerprint,
                    approved_by_user_id=actor_user_id,
                    authorization_evidence=evidence,
                )
            except SopExecutionSkillAuthorizationError as exc:
                return self._fail_prepared_write(
                    instance=instance,
                    step=step,
                    operation=operation,
                    signal=signal,
                    control=control,
                    error_code=exc.authorization_code,
                )
            self._consume_call_budget(instance, "tool_calls")
        self.db.commit()
        full_payload = dict(attention.payload_json or {})
        try:
            result = self.connection_service.send_wecom_approved_message(
                tenant_id=instance.tenant_id,
                profile_id=str(full_payload.get("profile_id") or ""),
                agent_id=instance.agent_id,
                actor_user_id=actor_user_id,
                thread_binding_id=str(full_payload.get("thread_binding_id") or ""),
                content=str(operation.request_json.get("content") or ""),
                expected_profile_revision=int(full_payload.get("profile_revision") or 0),
                expected_secret_revision=int(full_payload.get("secret_revision") or 0),
                expected_binding_revision=int(full_payload.get("binding_revision") or 0),
            )
        except ConnectionError as exc:
            return self._fail_started_write(
                instance=instance,
                step=step,
                operation=operation,
                worker_id=worker_id,
                error_code=exc.code,
                signal=signal,
                control=control,
            )
        if result.success:
            with self.store.owned(instance, worker_id=worker_id):
                data = {
                    "delivery_status": "sent",
                    "message_id": str(result.data.get("message_id") or ""),
                }
                self.store.finish_operation(
                    operation,
                    succeeded=True,
                    result={"data": data},
                    external_reference=data["message_id"] or None,
                )
                self.store.complete_node(instance, step, output={"data": data})
            self.db.commit()
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
                resume_signal_id=signal.id,
                signal_worker_id=worker_id,
            )
        if result.error_code in {"WECOM_DELIVERY_UNKNOWN", "WECOM_PARTIAL_DELIVERY"}:
            return self._park_unknown_write(
                instance=instance,
                step=step,
                operation=operation,
                worker_id=worker_id,
                error_code=str(result.error_code),
                signal=signal,
                control=control,
            )
        return self._fail_started_write(
            instance=instance,
            step=step,
            operation=operation,
            worker_id=worker_id,
            error_code=str(result.error_code or "WECOM_DELIVERY_FAILED"),
            signal=signal,
            control=control,
        )

    def _resume_local_tool_approval_signal(
        self,
        *,
        signal: ExecutionSignal,
        instance: SopInstance,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> DynamicRunOutcome:
        """办理或恢复本地一次性审批；running 中断按内容幂等契约安全重派。"""

        if not get_settings().dynamic_task_managed_workspace_enabled:
            raise DynamicTaskAgentError("DYNAMIC_MANAGED_WORKSPACE_DISABLED")
        if signal.status == "consumed":
            if instance.status in {"failed", "cancelled", "timed_out"}:
                return DynamicRunOutcome(instance.status, instance.id)
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control = ExecutionControlService(self.db, self.store)
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        with self.store.owned(instance, worker_id=worker_id):
            attention = self.db.get(
                SopWorkItem,
                str(signal.payload_json.get("attention_id") or ""),
            )
            if (
                attention is None
                or attention.instance_id != instance.id
                or attention.attention_kind != "tool_approval"
                or attention.status != "completed"
                or str(attention.resolution_json.get("actor_user_id") or "")
                != actor_user_id
            ):
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_RESOLUTION_INVALID")
            operation = self.db.get(
                SopOperation,
                str(attention.payload_json.get("operation_id") or ""),
            )
            step = self.db.get(SopNodeExecution, attention.node_execution_id or "")
            if (
                operation is None
                or step is None
                or operation.instance_id != instance.id
                or operation.node_execution_id != step.id
                or operation.effect_kind not in {"local_write", "execute"}
            ):
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_OPERATION_INVALID")
            if operation.status == "succeeded" and step.status == "succeeded":
                self.db.commit()
                return self.run_until_blocked_or_complete(
                    execution_id=instance.id,
                    model_config=model_config,
                    worker_id=worker_id,
                    actor_user_id=instance.initiator_user_id,
                    resume_signal_id=signal.id,
                    signal_worker_id=worker_id,
                )
            command = str(attention.resolution_json.get("command") or "")
            if operation.status == "prepared":
                if step.status != "waiting":
                    raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_OPERATION_INVALID")
                if command == "deny":
                    self.store.cancel_prepared_operation(operation)
                    self.store.resume_waiting_node(instance, step, slots=instance.slots_json or {})
                    self.store.fail_node(instance, step, error={"code": "DYNAMIC_LOCAL_DENIED"})
                    control.consume_signal(instance, signal, worker_id=worker_id)
                    instance.terminal_reason_json = {"code": "DYNAMIC_LOCAL_DENIED"}
                    self.db.add(instance)
                    self.store.fail_instance(
                        instance,
                        context_patch={"failure_code": "DYNAMIC_LOCAL_DENIED"},
                    )
                    self.db.commit()
                    return DynamicRunOutcome("failed", instance.id)
                if command != "allow_once":
                    raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_COMMAND_INVALID")
                payload = dict(attention.payload_json or {})
                frozen_fingerprint = str(payload.pop("approval_fingerprint", ""))
                if (
                    not frozen_fingerprint
                    or capability_checksum(payload) != frozen_fingerprint
                    or attention.expires_at is None
                    or attention.expires_at <= self.store.database_now()
                    or payload.get("request_fingerprint") != operation.request_fingerprint
                    or payload.get("plan_revision_id") != instance.current_plan_revision_id
                ):
                    raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_FINGERPRINT_INVALID")
                expected_risk = operation.effect_kind
                snapshot = self._frozen_local_snapshot(
                    instance,
                    operation.operation_name,
                    expected_risk=expected_risk,
                )
                if payload.get("capability_checksum") != snapshot.checksum:
                    raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_CAPABILITY_CHANGED")
                if actor_user_id not in self._workspace_approver_ids(
                    instance.tenant_id,
                    exclude_user_id=instance.initiator_user_id,
                ):
                    raise DynamicTaskAgentError("DYNAMIC_LOCAL_APPROVER_DENIED")
                self.catalog.reauthorize_tool(
                    snapshot,
                    actor_user_id=instance.initiator_user_id,
                    organization_unit_id=None,
                )
                try:
                    self.store.authorize_operation_skill_causes(operation)
                except SopExecutionSkillAuthorizationError as exc:
                    return self._fail_prepared_write(
                        instance=instance,
                        step=step,
                        operation=operation,
                        signal=signal,
                        control=control,
                        error_code=exc.authorization_code,
                    )
                self.store.resume_waiting_node(instance, step, slots=instance.slots_json or {})
                self._acquire_tool_quota(operation)
                try:
                    self.store.authorize_local_operation_dispatch(
                        operation,
                        approval_work_item_id=attention.id,
                        approval_fingerprint=frozen_fingerprint,
                        approved_by_user_id=actor_user_id,
                        authorization_evidence={
                            "workspace": payload.get("workspace"),
                            "capability_checksum": snapshot.checksum,
                            "approved_actor_role": "admin",
                        },
                    )
                except SopExecutionSkillAuthorizationError as exc:
                    return self._fail_prepared_write(
                        instance=instance,
                        step=step,
                        operation=operation,
                        signal=signal,
                        control=control,
                        error_code=exc.authorization_code,
                    )
                self._consume_call_budget(instance, "tool_calls")
            elif operation.status == "running" and step.status == "running":
                snapshot = self._frozen_local_snapshot(
                    instance,
                    operation.operation_name,
                    expected_risk=operation.effect_kind,
                )
                self.catalog.reauthorize_tool(
                    snapshot,
                    actor_user_id=instance.initiator_user_id,
                    organization_unit_id=None,
                )
            else:
                raise DynamicTaskAgentError("DYNAMIC_TOOL_APPROVAL_OPERATION_INVALID")
        self.db.commit()
        result = self.tool_executor.execute(
            instance.tenant_id,
            ToolCall(name=operation.operation_name, arguments=dict(operation.request_json or {})),
            agent_id=instance.agent_id,
            actor_user_id=instance.initiator_user_id,
            execution_id=instance.id,
        )
        with self.store.owned(instance, worker_id=worker_id):
            self.store.finish_operation(
                operation,
                succeeded=result.success,
                result={"data": result.data} if result.data is not None else None,
                error=(result.error.model_dump(mode="json") if result.error else None),
            )
            if result.success:
                self.store.complete_node(instance, step, output={"data": result.data})
            else:
                self.store.fail_node(
                    instance,
                    step,
                    error=(
                        result.error.model_dump(mode="json")
                        if result.error
                        else {"code": "DYNAMIC_LOCAL_FAILED"}
                    ),
                )
                control.consume_signal(instance, signal, worker_id=worker_id)
                instance.terminal_reason_json = {"code": "DYNAMIC_LOCAL_FAILED"}
                self.db.add(instance)
                self.store.fail_instance(
                    instance,
                    context_patch={"failure_code": "DYNAMIC_LOCAL_FAILED"},
                )
                self.db.commit()
                return DynamicRunOutcome("failed", instance.id)
        self.db.commit()
        return self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
            resume_signal_id=signal.id,
            signal_worker_id=worker_id,
        )

    def _refresh_write_approval(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        attention: SopWorkItem,
        signal: ExecutionSignal,
        control: ExecutionControlService,
        actor_user_id: str,
    ) -> DynamicRunOutcome:
        """修订漂移时保持零外呼，并用当前授权事实生成新的显式一次性审批。"""

        old_payload = dict(attention.payload_json or {})
        try:
            evidence = self.connection_service.current_wecom_message_dispatch_evidence(
                tenant_id=instance.tenant_id,
                profile_id=str(old_payload.get("profile_id") or ""),
                agent_id=instance.agent_id,
                actor_user_id=actor_user_id,
                thread_binding_id=str(old_payload.get("thread_binding_id") or ""),
            )
        except ConnectionError as exc:
            return self._fail_prepared_write(
                instance=instance,
                step=step,
                operation=operation,
                signal=signal,
                control=control,
                error_code=exc.code,
            )
        expires_at = self.store.database_now() + timedelta(minutes=15)
        refreshed_payload = {
            key: value
            for key, value in old_payload.items()
            if key not in {"approval_fingerprint", "expires_at"}
        }
        refreshed_payload.update(
            {
                "profile_revision": evidence["profile_revision"],
                "secret_revision": evidence["secret_revision"],
                "binding_revision": evidence["binding_revision"],
                "node_execution_id": step.id,
                "previous_attention_id": attention.id,
                "expires_at": expires_at.isoformat(),
            }
        )
        refreshed_payload["approval_fingerprint"] = capability_checksum(refreshed_payload)
        approver_ids = self.catalog.write_approver_ids(
            instance.tenant_id,
            exclude_user_id=instance.initiator_user_id,
        )
        replacement, _ = control.offer_attention(
            instance,
            attention_kind="tool_approval",
            attention_key=f"{step.step_key}:tool_approval_refresh:{operation.id}:{attention.id}",
            title="连接配置已变化，请重新批准企业微信消息发送",
            payload=refreshed_payload,
            allowed_commands=["allow_once", "deny"],
            candidate_user_ids=approver_ids,
            source_type="dynamic_task",
            source_ref=operation.id,
            node_execution=None,
            exclude_initiator=True,
        )
        replacement.expires_at = expires_at
        self.db.add(replacement)
        control.consume_signal(instance, signal, worker_id=signal.lease_owner or "")
        self.store.retarget_waiting_work_item(
            instance,
            step,
            work_item_id=replacement.id,
        )
        control.append_execution_event(
            instance,
            event_type="external_write_approval_refreshed",
            causation_id=replacement.id,
            payload={
                "attention_id": replacement.id,
                "previous_attention_id": attention.id,
                "operation_id": operation.id,
                "approval_fingerprint": refreshed_payload["approval_fingerprint"],
            },
        )
        self.db.commit()
        return DynamicRunOutcome("waiting", instance.id, blocking_step_key=step.step_key)

    def _fail_prepared_write(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        signal: ExecutionSignal,
        control: ExecutionControlService,
        error_code: str,
    ) -> DynamicRunOutcome:
        """派发前授权失效时消费批准信号并确定失败，保证远端调用次数仍为零。"""

        if error_code == "GENERAL_SKILL_COUNTERMANDED":
            self._record_operation_skill_countermand(instance, operation)
        self.store.cancel_prepared_operation(operation)
        if step.status == "waiting":
            self.store.resume_waiting_node(instance, step, slots=instance.slots_json or {})
        self.store.fail_node(instance, step, error={"code": error_code})
        control.consume_signal(instance, signal, worker_id=signal.lease_owner or "")
        instance.terminal_reason_json = {"code": error_code[:128]}
        self.db.add(instance)
        self.store.fail_instance(
            instance,
            context_patch={"failure_code": error_code[:128]},
        )
        self.db.commit()
        return DynamicRunOutcome("failed", instance.id)

    def _record_operation_skill_countermand(
        self,
        instance: SopInstance,
        operation: SopOperation,
    ) -> None:
        """失效旧 Use 并记录显式撤权事件，使终止原因可由会话审计还原。"""

        use_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in (
                    *(operation.caused_by_skill_use_ids_json or ()),
                    operation.caused_by_skill_use_id or "",
                )
                if str(value).strip()
            )
        )
        now = self.store.database_now()
        for use_id in use_ids:
            use = self.db.get(GeneralSkillUse, use_id)
            if (
                use is None
                or use.tenant_id != instance.tenant_id
                or use.session_id != instance.session_id
                or use.execution_id != instance.id
                or use.status not in {"active", "completed"}
            ):
                continue
            use.status = "invalidated"
            use.invalidation_reason = "GENERAL_SKILL_COUNTERMANDED"
            use.completed_at = now
            use.updated_at = now
            self.db.add(use)
            self.db.add(
                AgentEvent(
                    tenant_id=instance.tenant_id,
                    session_id=instance.session_id,
                    event_type="skill_countermanded",
                    payload_json={
                        "skill_use_id": use.id,
                        "skill_id": use.skill_id,
                        "execution_id": instance.id,
                        "operation_id": operation.id,
                        "reason": use.invalidation_reason,
                    },
                )
            )

    def resume_write_reconciliation_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> DynamicRunOutcome:
        """由异常办理人用外部证据收敛 unknown，原逻辑动作永不自动重发。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal is not None else None
        if (
            signal is None
            or signal.signal_type != "attention_decided"
            or instance is None
            or instance.kind != "dynamic_task"
        ):
            raise DynamicTaskAgentError("DYNAMIC_RECONCILE_SIGNAL_INVALID")
        control = ExecutionControlService(self.db, self.store)
        if signal.status == "consumed":
            if instance.status == "failed":
                return DynamicRunOutcome("failed", instance.id)
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        with self.store.owned(instance, worker_id=worker_id):
            attention = self.db.get(
                SopWorkItem,
                str(signal.payload_json.get("attention_id") or ""),
            )
            if (
                attention is None
                or attention.attention_kind != "exception"
                or attention.status != "completed"
                or str(attention.resolution_json.get("actor_user_id") or "")
                != actor_user_id
            ):
                raise DynamicTaskAgentError("DYNAMIC_RECONCILE_RESOLUTION_INVALID")
            operation = self.db.get(
                SopOperation,
                str(attention.payload_json.get("operation_id") or ""),
            )
            step = self.db.get(
                SopNodeExecution,
                attention.node_execution_id
                or str(attention.payload_json.get("node_execution_id") or ""),
            )
            if (
                operation is None
                or step is None
                or operation.instance_id != instance.id
                or operation.status != "unknown"
                or step.status != "waiting"
            ):
                raise DynamicTaskAgentError("DYNAMIC_RECONCILE_OPERATION_INVALID")
            command = str(attention.resolution_json.get("command") or "")
            if command not in {"confirm_applied", "confirm_not_applied"}:
                raise DynamicTaskAgentError("DYNAMIC_RECONCILE_COMMAND_INVALID")
            self.store.resume_waiting_node(instance, step, slots=instance.slots_json or {})
            applied = command == "confirm_applied"
            evidence = {
                "code": "MANUAL_RECONCILIATION",
                "actor_user_id": actor_user_id,
                "attention_id": attention.id,
                "comment_present": bool(attention.comment),
            }
            data = {
                "delivery_status": "manually_confirmed",
                "message_id": operation.external_reference or "",
            }
            self.store.reconcile_operation(
                instance,
                operation,
                succeeded=applied,
                result={"data": data} if applied else None,
                error=None if applied else evidence,
                effect_confirmed=applied,
            )
            control.consume_signal(instance, signal, worker_id=worker_id)
            if applied:
                self.store.complete_node(instance, step, output={"data": data})
            else:
                self.store.fail_node(instance, step, error=evidence)
                instance.terminal_reason_json = {"code": "DYNAMIC_WRITE_NOT_APPLIED"}
                self.db.add(instance)
                self.store.fail_instance(
                    instance,
                    context_patch={"failure_code": "DYNAMIC_WRITE_NOT_APPLIED"},
                )
        self.db.commit()
        if not applied:
            return DynamicRunOutcome("failed", instance.id)
        return self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
        )

    def _park_unknown_write(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        worker_id: str,
        error_code: str,
        signal: ExecutionSignal | None = None,
        control: ExecutionControlService | None = None,
    ) -> DynamicRunOutcome:
        """把不确定外部效果冻结为 exception Attention，禁止普通恢复重新派发。"""

        with self.store.owned(instance, worker_id=worker_id):
            self.store.mark_operation_unknown(operation, error={"code": error_code})
            manager_ids = [
                user.id
                for user in self.db.exec(
                    select(User).where(
                        User.tenant_id == instance.tenant_id,
                        User.membership_status == "active",
                    )
                ).all()
                if has_governance_permission(
                    self.db,
                    tenant_id=instance.tenant_id,
                    user_id=user.id,
                    permission_code="connection_profile.manage",
                )
            ]
            active_control = control or ExecutionControlService(self.db, self.store)
            attention, _ = active_control.offer_attention(
                instance,
                attention_kind="exception",
                attention_key=f"{step.step_key}:write_unknown:{operation.id}",
                title="核对企业微信消息是否送达",
                payload={
                    "operation_id": operation.id,
                    "operation_name": operation.operation_name,
                    "node_execution_id": step.id,
                    "error_code": error_code,
                    "request_fingerprint": operation.request_fingerprint,
                    "instruction": "请依据企业微信后台或客户端证据确认是否已送达；系统不会自动重发。",
                },
                allowed_commands=["confirm_applied", "confirm_not_applied"],
                candidate_user_ids=manager_ids,
                source_type="dynamic_task",
                source_ref=operation.id,
                # 同一节点已经有一次性审批 WorkItem；异常对账是独立控制事项，
                # 通过 payload/Operation 保留关联，避免冒充第二个节点人工任务。
                node_execution=None,
            )
            self.store.wait_for_work_item(instance, step, work_item_id=attention.id)
            if signal is not None:
                active_control.consume_signal(
                    instance,
                    signal,
                    worker_id=signal.lease_owner or worker_id,
                )
        self.db.commit()
        return DynamicRunOutcome("waiting", instance.id, blocking_step_key=step.step_key)

    def _fail_started_write(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        worker_id: str,
        error_code: str,
        signal: ExecutionSignal,
        control: ExecutionControlService,
    ) -> DynamicRunOutcome:
        """收敛授权后可证明未产生副作用的确定错误，并消费对应恢复信号。"""

        with self.store.owned(instance, worker_id=worker_id):
            self.store.finish_operation(
                operation,
                succeeded=False,
                error={"code": error_code},
            )
            self.store.fail_node(instance, step, error={"code": error_code})
            control.consume_signal(instance, signal, worker_id=worker_id)
            instance.terminal_reason_json = {"code": error_code[:128]}
            self.db.add(instance)
            self.store.fail_instance(
                instance,
                context_patch={"failure_code": error_code[:128]},
            )
        self.db.commit()
        return DynamicRunOutcome("failed", instance.id)

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
                caused_by_skill_use_ids=step_definition.guidance_skill_use_ids,
                capability_snapshot=snapshot.model_dump(
                    mode="json", exclude={"checksum", "agent_id"}
                ),
                capability_snapshot_checksum=snapshot.checksum,
            )
            if operation.status == "succeeded":
                return step_definition, self._operation_result(operation)
            if operation.status == "prepared":
                self._acquire_tool_quota(operation)
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
        source_kind: str = "chat",
        input_resource_ids: Sequence[str] = (),
        knowledge_capability: dict[str, object] | None = None,
        forced_general_skill_id: str | None = None,
        memory_context: Sequence[Mapping[str, object]] = (),
    ) -> tuple[SopInstance, bool]:
        """经模型 preflight、实时能力目录和有界规划创建或复用统一动态 Execution。"""

        verified_model = self.catalog.require_dynamic_model(tenant_id, model_config.id)
        capabilities = [
            *self.catalog.list_tools(tenant_id, agent_id),
            *self.catalog.list_connector_reads(tenant_id, agent_id, initiator_user_id),
            *self.catalog.list_general_skills(tenant_id, agent_id, initiator_user_id),
        ]
        if get_settings().dynamic_task_external_write_enabled:
            capabilities.extend(
                self.catalog.list_connector_writes(
                    tenant_id,
                    agent_id,
                    initiator_user_id,
                    session_id,
                    source_kind=source_kind,
                    source_ref=source_ref,
                )
            )
        knowledge_snapshot = self._knowledge_snapshot(
            tenant_id=tenant_id,
            agent_id=agent_id,
            capability=knowledge_capability or {},
        )
        if knowledge_snapshot is not None:
            capabilities.append(knowledge_snapshot)
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
        memory_projection = self._memory_projection(memory_context)
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
                and existing.source_kind == source_kind
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
        planner = self.planner or DynamicTaskPlanner(
            LLMClient(verified_model),
            explore_enabled=self.explore_enabled,
        )
        guidance_catalog = [
            item for item in capabilities if item.capability_type == "general_skill"
        ]
        if forced_general_skill_id:
            selected_guidance = [
                item
                for item in guidance_catalog
                if item.capability_id == forced_general_skill_id
            ]
            if len(selected_guidance) != 1:
                raise DynamicTaskAgentError("GENERAL_SKILL_NOT_AVAILABLE")
            guidance_mode = "forced"
        elif guidance_catalog:
            selection = planner.select_guidance_skills(
                goal=goal.strip(),
                success_criteria=criteria,
                catalog=guidance_catalog,
            )
            selected_by_name = {item.name: item for item in guidance_catalog}
            selected_guidance = [
                selected_by_name[name] for name in selection.selected_skill_names
            ]
            guidance_mode = "auto"
        else:
            selected_guidance = []
            guidance_mode = "auto"
        loaded_guidance: list[dict[str, object]] = []
        loaded_use_ids: list[str] = []
        actor = self.db.get(User, initiator_user_id) if selected_guidance else None
        if selected_guidance and (actor is None or actor.tenant_id != tenant_id):
            raise DynamicTaskAgentError("DYNAMIC_ACTOR_NOT_AVAILABLE")
        runtime = GeneralSkillRuntimeService(self.db)
        for selected in selected_guidance:
            assert actor is not None
            bundle = runtime.load_bundle(
                actor,
                session_id=session_id,
                agent_id=agent_id,
                turn_id=source_ref or f"dynamic:{session_id}",
                skill_id=selected.capability_id,
                selection_mode=guidance_mode,
                commit=False,
            )
            loaded_use_ids.extend(item.use_id for item in bundle)
            loaded_guidance.append(
                {
                    "name": selected.name,
                    "skill_use_ids": [item.use_id for item in bundle],
                    "skills": [item.prompt_block() for item in bundle],
                }
            )
            for loaded in bundle:
                self.db.add(
                    AgentEvent(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type="skill_loaded",
                        payload_json={
                            "turn_id": source_ref or f"dynamic:{session_id}",
                            "user_message_id": source_ref or None,
                            "skill_use_id": loaded.use_id,
                            "skill_id": loaded.skill_id,
                            "revision_id": loaded.revision_id,
                            "selection_mode": loaded.selection_mode,
                            "consumer": "dynamic_task",
                        },
                    )
                )
        planning_inputs = tuple(
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
        )
        if loaded_guidance:
            plan = planner.create_plan(
                goal=goal.strip(),
                success_criteria=criteria,
                capabilities=capabilities,
                input_resources=planning_inputs,
                loaded_guidance=tuple(loaded_guidance),
                memory_context=memory_projection,
            )
        elif memory_projection:
            plan = planner.create_plan(
                goal=goal.strip(),
                success_criteria=criteria,
                capabilities=capabilities,
                input_resources=planning_inputs,
                memory_context=memory_projection,
            )
        else:
            plan = planner.create_plan(
                goal=goal.strip(),
                success_criteria=criteria,
                capabilities=capabilities,
                input_resources=planning_inputs,
            )
        if any(
            step.kind
            not in {
                "tool.read",
                "tool.write",
                "tool.execute",
                "knowledge",
                "explore",
                "clarification",
                "answer",
            }
            for step in plan.steps
        ):
            raise DynamicTaskAgentError("DYNAMIC_PLAN_UNSUPPORTED_STEP")
        snapshot = {
            "tools": [
                item.model_dump(mode="json")
                for item in capabilities
                if item.capability_type == "tool"
            ],
            "connectors": [
                item.model_dump(mode="json")
                for item in capabilities
                if item.capability_type == "connector"
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
            source_kind=source_kind,
            source_ref=source_ref or session_id,
        )
        for use_id in loaded_use_ids:
            use = self.db.get(GeneralSkillUse, use_id)
            if use is None:
                raise DynamicTaskAgentError("GENERAL_SKILL_USE_NOT_AVAILABLE")
            use.execution_id = instance.id
            use.updated_at = instance.created_at
            self.db.add(use)
        instance.context_json = {
            **(instance.context_json or {}),
            "dynamic_budget_usage": {
                "model_calls": 1,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "memory_context": list(memory_projection),
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
    def _memory_projection(
        memory_context: Sequence[Mapping[str, object]],
    ) -> tuple[dict[str, object], ...]:
        """以有界白名单冻结用户/Agent 记忆，剥离身份、凭据和存储侧带。"""

        projected: list[dict[str, object]] = []
        blocked_keys = {"token", "secret", "password", "authorization", "api_key"}
        for raw in memory_context[:20]:
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            metadata = raw.get("metadata")
            safe_metadata = {
                str(key): value
                for key, value in (metadata.items() if isinstance(metadata, Mapping) else ())
                if str(key).casefold() not in blocked_keys
                and (value is None or isinstance(value, (str, bool, int, float)))
            }
            projected.append(
                {
                    "kind": str(raw.get("kind") or "fact")[:64],
                    "content": content[:4000],
                    "metadata": safe_metadata,
                }
            )
        return tuple(projected)

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
                caused_by_skill_use_ids=tuple(
                    str(value)
                    for value in step_definition.get("guidance_skill_use_ids", ())
                    if str(value).strip()
                ),
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
            if snapshot.capability_type == "connector":
                return self._execute_connector_read(
                    instance=instance,
                    step=step,
                    operation=operation,
                    snapshot=snapshot,
                    arguments=dict(proposal.arguments),
                    actor_user_id=actor_user_id,
                )
            self.catalog.reauthorize_tool(
                snapshot,
                actor_user_id=actor_user_id,
                organization_unit_id=organization_unit_id,
            )
            if operation.status == "prepared":
                self._acquire_tool_quota(operation)
                self.store.start_operation(operation)
            self._consume_call_budget(instance, "tool_calls")
            self.db.commit()
            result = self.tool_executor.execute(
                instance.tenant_id,
                ToolCall(name=capability_ref, arguments=dict(proposal.arguments)),
                agent_id=instance.agent_id,
                actor_user_id=actor_user_id,
                execution_org_unit_id=organization_unit_id,
                execution_id=instance.id,
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

    def resume_reauth_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
        actor_user_id: str,
    ) -> DynamicRunOutcome:
        """验证凭据修订后消费 reauth signal，并恢复原 Step 和同一只读 Operation。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        if signal is None or signal.signal_type != "attention_decided":
            raise DynamicTaskAgentError("DYNAMIC_REAUTH_SIGNAL_INVALID")
        attention_id = str(signal.payload_json.get("attention_id") or "")
        attention = self.db.get(SopWorkItem, attention_id)
        instance = self.db.get(SopInstance, signal.execution_id)
        if (
            attention is None
            or attention.instance_id != signal.execution_id
            or attention.attention_kind != "reauth"
            or attention.status != "completed"
            or instance is None
            or instance.kind != "dynamic_task"
        ):
            raise DynamicTaskAgentError("DYNAMIC_REAUTH_RESOLUTION_INVALID")
        if (
            attention.resolution_json.get("command") != "reauthorize"
            or attention.resolution_json.get("actor_user_id") != actor_user_id
        ):
            raise DynamicTaskAgentError("DYNAMIC_REAUTH_COMMAND_INVALID")
        profile_id = str(attention.payload_json.get("profile_id") or "")
        blocked_revision = int(attention.payload_json.get("secret_revision") or 0)
        operation_id = str(attention.payload_json.get("operation_id") or "")
        profile = self.db.get(ConnectionProfile, profile_id)
        if (
            profile is None
            or profile.tenant_id != instance.tenant_id
            or profile.status != "active"
            or profile.secret_revision <= blocked_revision
        ):
            raise DynamicTaskAgentError("DYNAMIC_REAUTH_NOT_COMPLETED")
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
            operation = self.db.get(SopOperation, operation_id)
            step = self.db.get(SopNodeExecution, attention.node_execution_id or "")
            if (
                operation is None
                or operation.instance_id != instance.id
                or operation.node_execution_id != (step.id if step else None)
                or operation.status not in {"prepared", "running"}
                or step is None
                or step.status != "waiting"
            ):
                raise DynamicTaskAgentError("DYNAMIC_REAUTH_OPERATION_INVALID")
            snapshot = self._frozen_read_snapshot(instance, operation.operation_name)
            if snapshot.capability_type != "connector" or snapshot.capability_id != profile.id:
                raise DynamicTaskAgentError("DYNAMIC_REAUTH_CAPABILITY_MISMATCH")
            self.store.resume_waiting_node(instance, step, slots=dict(instance.slots_json or {}))
            result = self._execute_connector_read(
                instance=instance,
                step=step,
                operation=operation,
                snapshot=snapshot,
                arguments=dict(operation.request_json or {}),
                actor_user_id=instance.initiator_user_id,
            )
            if result.success:
                control.append_execution_event(
                    instance,
                    event_type="connection_profile_recovered",
                    causation_id=signal.id,
                    payload={
                        "profile_id": profile.id,
                        "profile_revision": profile.revision,
                        "secret_revision": profile.secret_revision,
                        "resume_signal_id": signal.id,
                    },
                )
            control.consume_signal(instance, signal, worker_id=worker_id)
            self.db.commit()
            if result.error is not None and result.error.code in {
                "DYNAMIC_REAUTH_REQUIRED",
                "DYNAMIC_RATE_LIMITED",
            }:
                return DynamicRunOutcome("blocked", instance.id, blocking_step_key=step.step_key)
        return self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
        )

    def resume_connector_timer_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
    ) -> DynamicRunOutcome:
        """消费到期 timer signal，并以同一 Operation 安全重试无副作用 Connector 读取。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal else None
        if (
            signal is None
            or signal.signal_type != "timer"
            or instance is None
            or instance.kind != "dynamic_task"
        ):
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_TIMER_INVALID")
        if signal.status == "consumed":
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control = ExecutionControlService(self.db, self.store)
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        with self.store.owned(instance, worker_id=worker_id):
            operation_id = str(signal.payload_json.get("operation_id") or "")
            operation = self.db.get(SopOperation, operation_id)
            step = self.db.get(SopNodeExecution, operation.node_execution_id) if operation else None
            if (
                operation is None
                or operation.instance_id != instance.id
                or operation.status != "running"
                or step is None
                or step.status != "waiting"
            ):
                raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_TIMER_OPERATION_INVALID")
            snapshot = self._frozen_read_snapshot(instance, operation.operation_name)
            if snapshot.capability_type != "connector":
                raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_TIMER_CAPABILITY_INVALID")
            self.store.resume_waiting_node(instance, step, slots=dict(instance.slots_json or {}))
            result = self._execute_connector_read(
                instance=instance,
                step=step,
                operation=operation,
                snapshot=snapshot,
                arguments=dict(operation.request_json or {}),
                actor_user_id=instance.initiator_user_id,
            )
            if result.success:
                profile = self.db.get(ConnectionProfile, snapshot.capability_id)
                if profile is None or profile.tenant_id != instance.tenant_id:
                    raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_PROFILE_NOT_FOUND")
                control.append_execution_event(
                    instance,
                    event_type="connection_profile_recovered",
                    causation_id=signal.id,
                    payload={
                        "profile_id": profile.id,
                        "profile_revision": profile.revision,
                        "secret_revision": profile.secret_revision,
                        "resume_signal_id": signal.id,
                    },
                )
            control.consume_signal(instance, signal, worker_id=worker_id)
            self.db.commit()
            if result.error is not None and result.error.code in {
                "DYNAMIC_REAUTH_REQUIRED",
                "DYNAMIC_RATE_LIMITED",
            }:
                return DynamicRunOutcome("blocked", instance.id, blocking_step_key=step.step_key)
        return self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
        )

    def resume_scheduled_start_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
    ) -> DynamicRunOutcome:
        """以持久 scheduled_start signal 推进新 Execution，结算或明确阻塞后才消费唤醒。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal else None
        if (
            signal is None
            or signal.signal_type != "scheduled_start"
            or instance is None
            or instance.kind != "dynamic_task"
            or instance.source_kind != "schedule"
            or str(signal.payload_json.get("scheduled_task_run_id") or "")
            != instance.source_ref
        ):
            raise DynamicTaskAgentError("DYNAMIC_SCHEDULE_SIGNAL_INVALID")
        if signal.status == "consumed":
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control = ExecutionControlService(self.db, self.store)
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        run = self.db.get(ScheduledTaskRun, instance.source_ref)
        actor = self.db.get(User, instance.initiator_user_id)
        agent = self.db.get(AgentProfile, instance.agent_id)
        authorized = (
            run is not None
            and run.tenant_id == instance.tenant_id
            and run.execution_id == instance.id
            and actor is not None
            and actor.tenant_id == instance.tenant_id
            and actor.membership_status == "active"
            and agent is not None
            and can_use_agent_in_chat(self.db, agent, actor)
        )
        if not authorized:
            with self.store.owned(instance, worker_id=worker_id):
                control.consume_signal(instance, signal, worker_id=worker_id)
                instance.terminal_reason_json = {"code": "DYNAMIC_SCHEDULE_ACCESS_DENIED"}
                self.db.add(instance)
                self.store.fail_instance(
                    instance,
                    context_patch={"failure_code": "DYNAMIC_SCHEDULE_ACCESS_DENIED"},
                )
            self.db.commit()
            return DynamicRunOutcome("failed", instance.id)
        outcome = self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
            resume_signal_id=signal.id,
            signal_worker_id=worker_id,
        )
        self.db.refresh(signal)
        self.db.refresh(instance)
        if signal.status == "claimed" and signal.lease_owner == worker_id:
            with self.store.owned(instance, worker_id=worker_id):
                control.consume_signal(instance, signal, worker_id=worker_id)
            self.db.commit()
        return outcome

    def resume_capacity_retry_signal(
        self,
        *,
        signal_id: str,
        model_config: ModelConfig,
        worker_id: str,
    ) -> DynamicRunOutcome:
        """认领容量退避信号并重试原 Execution；再次满载时由 worker 持久退避。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal else None
        if (
            signal is None
            or signal.signal_type != "capacity_retry"
            or instance is None
            or instance.kind != "dynamic_task"
        ):
            raise DynamicTaskAgentError("DYNAMIC_CAPACITY_SIGNAL_INVALID")
        if signal.status == "consumed":
            return self.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model_config,
                worker_id=worker_id,
                actor_user_id=instance.initiator_user_id,
            )
        control = ExecutionControlService(self.db, self.store)
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        self.db.commit()
        outcome = self.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model_config,
            worker_id=worker_id,
            actor_user_id=instance.initiator_user_id,
        )
        self.db.refresh(signal)
        self.db.refresh(instance)
        if signal.status == "claimed" and signal.lease_owner == worker_id:
            with self.store.owned(instance, worker_id=worker_id):
                control.consume_signal(instance, signal, worker_id=worker_id)
            self.db.commit()
        return outcome

    def _execute_connector_read(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
        arguments: dict[str, object],
        actor_user_id: str,
    ) -> ToolResult:
        """通过冻结 profile 身份和实时绑定执行 provider 读，reauth 时保留原 Operation。"""

        provider = str(snapshot.contract.get("provider") or "")
        if provider not in {"slack", "wecom"}:
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_PROVIDER_UNSUPPORTED")
        channel_id = str(arguments.get("channel_id") or "").strip()
        if provider == "slack" and (set(arguments) != {"channel_id"} or not channel_id):
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_ARGUMENT_INVALID")
        if provider == "wecom" and arguments:
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_ARGUMENT_INVALID")
        required_scope = str(snapshot.contract.get("required_scope") or "")
        required_action = str(snapshot.contract.get("required_action") or "")
        if not required_scope or not required_action:
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_CONTRACT_INVALID")
        try:
            self.connection_service.resolve(
                tenant_id=instance.tenant_id,
                profile_id=snapshot.capability_id,
                agent_id=instance.agent_id,
                required_scope=required_scope,
                required_action=required_action,
                actor_user_id=actor_user_id,
            )
        except ConnectionError as exc:
            if exc.code in _CONNECTION_REAUTH_CODES:
                self._offer_connector_reauth(
                    instance=instance,
                    step=step,
                    operation=operation,
                    snapshot=snapshot,
                    reason_code=exc.code,
                )
                self.db.commit()
                return ToolResult(
                    tool_name=snapshot.name,
                    success=False,
                    error=ToolError(
                        code="DYNAMIC_REAUTH_REQUIRED",
                        message="连接账号需要重新授权。",
                    ),
                )
            if operation.status == "prepared":
                self.store.cancel_prepared_operation(operation)
            self.store.fail_node(instance, step, error={"code": exc.code})
            self.db.commit()
            return ToolResult(
                tool_name=snapshot.name,
                success=False,
                error=ToolError(code=exc.code, message="连接读取失败。"),
            )
        if operation.status == "prepared":
            self._acquire_tool_quota(operation)
            self.store.start_operation(operation)
        self._consume_call_budget(instance, "tool_calls")
        self.db.commit()
        try:
            if provider == "slack":
                data = self.connection_service.read_slack_channel(
                    tenant_id=instance.tenant_id,
                    profile_id=snapshot.capability_id,
                    agent_id=instance.agent_id,
                    actor_user_id=actor_user_id,
                    channel_id=channel_id,
                )
            else:
                data = self.connection_service.read_wecom_application(
                    tenant_id=instance.tenant_id,
                    profile_id=snapshot.capability_id,
                    agent_id=instance.agent_id,
                    actor_user_id=actor_user_id,
                )
        except ConnectionError as exc:
            if exc.code in {"SLACK_RATE_LIMITED", "CONNECTION_RATE_LIMITED"}:
                self._schedule_connector_retry(
                    instance=instance,
                    step=step,
                    operation=operation,
                    snapshot=snapshot,
                )
                self.db.commit()
                return ToolResult(
                    tool_name=snapshot.name,
                    success=False,
                    error=ToolError(
                        code="DYNAMIC_RATE_LIMITED",
                        message="连接读取已按上游限流窗口安排重试。",
                    ),
                )
            if exc.code in _CONNECTION_REAUTH_CODES:
                self._offer_connector_reauth(
                    instance=instance,
                    step=step,
                    operation=operation,
                    snapshot=snapshot,
                    reason_code=exc.code,
                )
                self.db.commit()
                return ToolResult(
                    tool_name=snapshot.name,
                    success=False,
                    error=ToolError(
                        code="DYNAMIC_REAUTH_REQUIRED",
                        message="连接账号需要重新授权。",
                    ),
                )
            self.store.finish_operation(operation, succeeded=False, error={"code": exc.code})
            self.store.fail_node(instance, step, error={"code": exc.code})
            self.db.commit()
            return ToolResult(
                tool_name=snapshot.name,
                success=False,
                error=ToolError(code=exc.code, message="连接读取失败。"),
            )
        self.store.finish_operation(operation, succeeded=True, result={"data": data})
        self.store.complete_node(instance, step, output={"data": data})
        self.db.commit()
        return ToolResult(tool_name=snapshot.name, success=True, data=data)

    def _schedule_connector_retry(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
    ) -> ExecutionSignal:
        """按 provider Retry-After 写持久 timer signal，并暂停节点等待调度。"""

        profile = self.db.get(ConnectionProfile, snapshot.capability_id)
        if profile is None or profile.tenant_id != instance.tenant_id:
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_PROFILE_NOT_FOUND")
        control = ExecutionControlService(self.db, self.store)
        signal = control.enqueue_signal(
            instance,
            signal_type="timer",
            causation_type="connector_rate_limit",
            causation_id=f"{operation.id}:{profile.revision}",
            payload={
                "operation_id": operation.id,
                "profile_id": profile.id,
                "profile_revision": profile.revision,
            },
            available_at=profile.rate_limited_until,
        )
        control.append_execution_event(
            instance,
            event_type="connection_profile_unhealthy",
            causation_id=signal.id,
            payload={
                "profile_id": profile.id,
                "profile_revision": profile.revision,
                "reason_code": profile.health_error_code or "CONNECTION_RATE_LIMITED",
                "retry_signal_id": signal.id,
            },
        )
        if step.status == "running":
            self.store.wait_for_timer(instance, step, signal_id=signal.id)
        return signal

    def _offer_connector_reauth(
        self,
        *,
        instance: SopInstance,
        step: SopNodeExecution,
        operation: SopOperation,
        snapshot: CapabilitySnapshot,
        reason_code: str,
    ) -> SopWorkItem:
        """按 profile 密钥修订幂等创建 reauth Attention，并暂停当前运行节点。"""

        profile = self.db.get(ConnectionProfile, snapshot.capability_id)
        if profile is None or profile.tenant_id != instance.tenant_id:
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_PROFILE_NOT_FOUND")
        tenant_users = self.db.exec(
            select(User).where(
                User.tenant_id == instance.tenant_id,
                User.membership_status == "active",
            )
        ).all()
        manager_ids = [
            item.id
            for item in tenant_users
            if has_governance_permission(
                self.db,
                tenant_id=instance.tenant_id,
                user_id=item.id,
                permission_code="connection_profile.manage",
            )
        ]
        if not manager_ids:
            raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_MANAGER_UNAVAILABLE")
        control = ExecutionControlService(self.db, self.store)
        attention, created = control.offer_attention(
            instance,
            attention_kind="reauth",
            attention_key=(
                f"{step.step_key}:reauth:{profile.id}:{profile.secret_revision}"
            ),
            title=f"重新授权 {profile.display_name}",
            payload={
                "provider": profile.provider,
                "profile_id": profile.id,
                "account_id": profile.account_id,
                "secret_revision": profile.secret_revision,
                "profile_revision": profile.revision,
                "operation_id": operation.id,
                "reason_code": reason_code,
            },
            allowed_commands=["reauthorize"],
            candidate_user_ids=manager_ids,
            source_type="dynamic_task",
            source_ref=operation.id,
            node_execution=step,
        )
        if created:
            control.append_execution_event(
                instance,
                event_type="connection_profile_reauth_required",
                causation_id=attention.id,
                payload={
                    "profile_id": profile.id,
                    "profile_revision": profile.revision,
                    "secret_revision": profile.secret_revision,
                    "reason_code": reason_code,
                    "attention_id": attention.id,
                },
            )
        if step.status == "running":
            self.store.wait_for_work_item(instance, step, work_item_id=attention.id)
        return attention

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
            try:
                result = DynamicTaskResult.model_validate(completed_response.proposal.arguments)
            except ValidationError as exc:
                proposal.status = "superseded"
                proposal.superseded_at = self.store.database_now()
                self.db.add(proposal)
                self.db.flush()
                raise DynamicTaskAgentError("DYNAMIC_RESULT_SCHEMA_INVALID") from exc
            completed_keys = self._completed_step_keys(instance)
            verification = verify_dynamic_result(
                result,
                plan=plan,
                completed_step_keys=completed_keys,
                required_evidence_by_step=self._required_result_evidence(instance, plan),
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
            connector_thread = self.db.exec(
                select(ConnectorThreadBinding).where(
                    ConnectorThreadBinding.tenant_id == instance.tenant_id,
                    ConnectorThreadBinding.session_id == instance.session_id,
                    ConnectorThreadBinding.status == "active",
                )
            ).first()
            external_publication = None
            if instance.source_kind == "connector":
                if connector_thread is None:
                    raise DynamicTaskAgentError("DYNAMIC_CONNECTOR_THREAD_MISSING")
                external_publication, _ = control.ensure_external_publication(
                    instance,
                    result_row,
                    thread_binding_id=connector_thread.id,
                )
                ConnectorRuntimeService(self.db).enqueue_execution_publication(
                    external_publication,
                    thread_binding_id=connector_thread.id,
                    content=result.markdown,
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
                event_type=(
                    "execution_result_ready"
                    if external_publication is not None
                    else "execution_succeeded"
                ),
                causation_id=result_row.id,
                payload={
                    "result_id": result_row.id,
                    "publication_id": publication.id,
                    "external_publication_id": (
                        external_publication.id if external_publication is not None else None
                    ),
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
            if external_publication is None:
                self.store.complete_instance(instance)
            else:
                self.store.wait_for_publication(instance)
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
        tools = raw_catalog.get("tools") if isinstance(raw_catalog, dict) else None
        connectors = raw_catalog.get("connectors") if isinstance(raw_catalog, dict) else None
        if connectors is None:
            connectors = []
        if not isinstance(tools, list) or not isinstance(connectors, list):
            raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID")
        for value in [*tools, *connectors]:
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

    def _frozen_write_snapshot(
        self,
        instance: SopInstance,
        capability_ref: str,
    ) -> CapabilitySnapshot:
        """从冻结目录解析唯一 external-write，并拒绝非连接器或可变目标。"""

        connectors = (instance.capability_snapshot_json or {}).get("connectors")
        if not isinstance(connectors, list):
            raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID")
        for value in connectors:
            if not isinstance(value, dict) or value.get("name") != capability_ref:
                continue
            try:
                snapshot = CapabilitySnapshot.model_validate(value)
            except ValueError as exc:
                raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID") from exc
            if (
                snapshot.capability_type != "connector"
                or snapshot.agent_id != instance.agent_id
                or snapshot.tenant_id != instance.tenant_id
                or snapshot.contract.get("risk_class") != "external_write"
                or snapshot.contract.get("confirmation_policy") != "once"
                or not snapshot.contract.get("canonical_target")
                or not snapshot.contract.get("target_checksum")
            ):
                raise DynamicTaskAgentError("DYNAMIC_WRITE_CAPABILITY_INVALID")
            return snapshot
        raise DynamicTaskAgentError("DYNAMIC_WRITE_CAPABILITY_NOT_FROZEN")

    def _frozen_local_snapshot(
        self,
        instance: SopInstance,
        capability_ref: str,
        *,
        expected_risk: str,
    ) -> CapabilitySnapshot:
        """从冻结工具目录解析受管本地动作，并拒绝伪装连接器或缺少目标身份。"""

        tools = (instance.capability_snapshot_json or {}).get("tools")
        if not isinstance(tools, list):
            raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID")
        for value in tools:
            if not isinstance(value, dict) or value.get("name") != capability_ref:
                continue
            try:
                snapshot = CapabilitySnapshot.model_validate(value)
            except ValueError as exc:
                raise DynamicTaskAgentError("DYNAMIC_CAPABILITY_SNAPSHOT_INVALID") from exc
            managed = snapshot.audit_view.get("managed_workspace")
            if (
                snapshot.capability_type != "tool"
                or snapshot.agent_id != instance.agent_id
                or snapshot.tenant_id != instance.tenant_id
                or snapshot.contract.get("risk_class") != expected_risk
                or snapshot.contract.get("confirmation_policy") != "once"
                or not isinstance(managed, Mapping)
                or not managed.get("workspace_id")
                or not managed.get("handler")
            ):
                raise DynamicTaskAgentError("DYNAMIC_LOCAL_CAPABILITY_INVALID")
            return snapshot
        raise DynamicTaskAgentError("DYNAMIC_LOCAL_CAPABILITY_NOT_FROZEN")

    def _planned_step_risk(self, instance: SopInstance, step: PlanStep) -> str:
        """解析计划步骤唯一能力的冻结风险类别，用于区分本地写和外部写。"""

        if len(step.capability_refs) != 1:
            raise DynamicTaskAgentError("DYNAMIC_STEP_CAPABILITY_INVALID")
        ref = step.capability_refs[0]
        frozen = instance.capability_snapshot_json or {}
        for group in ("tools", "connectors"):
            values = frozen.get(group)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, Mapping) and item.get("name") == ref:
                    contract = item.get("contract")
                    if isinstance(contract, Mapping):
                        return str(contract.get("risk_class") or "")
        raise DynamicTaskAgentError("DYNAMIC_STEP_CAPABILITY_NOT_FROZEN")

    @staticmethod
    def _validate_workspace_arguments(
        snapshot: CapabilitySnapshot,
        arguments: Mapping[str, object],
    ) -> None:
        """在创建审批前按固定 handler 校验精确参数集合和关键大小边界。"""

        managed = snapshot.audit_view.get("managed_workspace")
        handler = str(managed.get("handler") or "") if isinstance(managed, Mapping) else ""
        expected = {
            "apply_file": {"path", "expected_sha256", "content"},
            "apply_files": {"changes"},
            "run_check": {"profile"},
            "commit": {"message", "paths"},
        }.get(handler)
        if expected is None or set(arguments) != expected:
            raise DynamicTaskAgentError("DYNAMIC_LOCAL_ARGUMENTS_INVALID")
        if handler == "apply_file" and (
            len(str(arguments.get("content") or "").encode("utf-8")) > 512 * 1024
            or not str(arguments.get("path") or "")
            or not str(arguments.get("expected_sha256") or "")
        ):
            raise DynamicTaskAgentError("DYNAMIC_LOCAL_ARGUMENTS_INVALID")
        if handler == "apply_files":
            changes = arguments.get("changes")
            if (
                not isinstance(changes, list)
                or not changes
                or len(changes) > 50
                or any(
                    not isinstance(change, Mapping)
                    or set(change) != {"path", "expected_sha256", "content"}
                    for change in changes
                )
            ):
                raise DynamicTaskAgentError("DYNAMIC_LOCAL_ARGUMENTS_INVALID")
        if handler == "run_check":
            names = managed.get("check_profile_names") if isinstance(managed, Mapping) else None
            if not isinstance(names, list) or str(arguments.get("profile") or "") not in names:
                raise DynamicTaskAgentError("DYNAMIC_LOCAL_ARGUMENTS_INVALID")
        if handler == "commit":
            paths = arguments.get("paths")
            if (
                not str(arguments.get("message") or "").strip()
                or not isinstance(paths, list)
                or not paths
                or not all(isinstance(path, str) and path for path in paths)
            ):
                raise DynamicTaskAgentError("DYNAMIC_LOCAL_ARGUMENTS_INVALID")

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

    def _required_result_evidence(
        self,
        instance: SopInstance,
        plan: NormalizedPlan,
    ) -> dict[str, dict[str, object]]:
        """从能力声明与成功 Operation 提取必须出现在最终交付物中的字段值。"""

        frozen = instance.capability_snapshot_json or {}
        snapshots: dict[str, Mapping[str, object]] = {}
        for group in ("tools", "connectors", "knowledge"):
            values = frozen.get(group)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                    snapshots[str(item["name"])] = item
        node_by_step = {
            item.step_key: item
            for item in self.db.exec(
                select(SopNodeExecution).where(
                    SopNodeExecution.tenant_id == instance.tenant_id,
                    SopNodeExecution.instance_id == instance.id,
                    SopNodeExecution.status == "succeeded",
                )
            ).all()
        }
        required: dict[str, dict[str, object]] = {}
        for step in plan.steps:
            if (
                step.kind not in {"tool.read", "tool.write", "tool.execute"}
                or len(step.capability_refs) != 1
            ):
                continue
            snapshot = snapshots.get(step.capability_refs[0])
            contract = snapshot.get("contract") if isinstance(snapshot, Mapping) else None
            paths = (
                contract.get("required_result_evidence_paths")
                if isinstance(contract, Mapping)
                else None
            )
            node = node_by_step.get(step.step_key)
            if not isinstance(paths, list) or node is None:
                continue
            operation = self.db.exec(
                select(SopOperation).where(
                    SopOperation.tenant_id == instance.tenant_id,
                    SopOperation.instance_id == instance.id,
                    SopOperation.node_execution_id == node.id,
                    SopOperation.status == "succeeded",
                )
            ).first()
            data = (operation.result_json or {}).get("data") if operation is not None else None
            if not isinstance(data, Mapping):
                continue
            values_by_path: dict[str, object] = {}
            for raw_path in paths:
                path = str(raw_path or "").strip()
                found, value = _mapping_path_value(data, path)
                if found:
                    values_by_path[path] = value
            if values_by_path:
                required[step.step_key] = values_by_path
        return required

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
            skill_guidance = self._step_guidance(instance, step)
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
                        "role": "system",
                        "content": {
                            "general_skill_guidance": skill_guidance,
                        },
                    },
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

    def _step_guidance(
        self,
        instance: SopInstance,
        step: PlanStep,
    ) -> list[dict[str, object]]:
        """在每次模型动作前重授权固定 Use，并投影不具有越权能力的指导块。"""

        if not step.guidance_skill_use_ids:
            return []
        actor = self.db.get(User, instance.initiator_user_id)
        if actor is None or actor.tenant_id != instance.tenant_id:
            raise DynamicTaskAgentError("DYNAMIC_SKILL_ACTOR_NOT_FOUND")
        try:
            return [
                GeneralSkillRuntimeService(self.db)
                .project_use_for_execution(
                    actor,
                    use_id=use_id,
                    session_id=instance.session_id,
                    agent_id=instance.agent_id,
                    execution_id=instance.id,
                )
                .prompt_block()
                for use_id in step.guidance_skill_use_ids
            ]
        except GeneralSkillRuntimeError as exc:
            raise DynamicTaskAgentError(exc.code) from exc

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


def _mapping_path_value(source: Mapping[str, object], path: str) -> tuple[bool, object]:
    """按点分隔路径读取结构化回执，并区分字段缺失与显式空值。"""

    if not path:
        return False, None
    current: object = source
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def canonical_result_checksum(result: DynamicTaskResult) -> str:
    """复用规划严格 JSON checksum 记录 answer Step 的结果引用。"""

    from app.dynamic_tasks.planning import canonical_checksum

    return canonical_checksum(result)
