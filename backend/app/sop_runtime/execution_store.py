"""
@Time       : 2026/08/10 19:55
@Author     : zhanglp8181
@File       : execution_store.py
@CallChain  : Agent Loop/Runtime Scheduler → SopExecutionStore → SQLModel 执行聚合
@Description: 持久化统一执行聚合，以租约/fencing、逻辑动作幂等和效果账本保护权威状态写。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, update
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session, select

from app.db.models import (
    ActionProposalRecord,
    ExecutionCommand,
    ExecutionPlanRevision,
    ExecutionMutationRejection,
    ExecutionSignal,
    GeneralSkillUse,
    InputResourceSnapshot,
    ManagedInputResource,
    Message,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    SopOperationEffect,
    SopWorkItem,
    User,
    utc_now,
)
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.dynamic_tasks.planning import (
    CompletedProviderProposal,
    NormalizedPlan,
    PlanReason,
    canonical_checksum,
)
from app.dynamic_tasks.quotas import DynamicTaskQuotaService
from app.session.managed_resources import (
    InputResourceAccessDenied,
    assert_input_resource_access,
)
from app.sop_runtime.contracts import (
    IdempotencyPolicy,
    IdempotencyScope,
    NodeExecutionStatus,
    OperationStatus,
    SopInstanceStatus,
)
from app.sop_runtime.state_machine import (
    TransitionResult,
    transition_instance,
    transition_node,
    transition_operation,
)


ACTIVE_INSTANCE_STATUSES = (
    SopInstanceStatus.CREATED.value,
    SopInstanceStatus.RUNNING.value,
    SopInstanceStatus.WAITING.value,
)


class SopExecutionConflictError(ValueError):
    """同一会话存在不兼容的活动实例或聚合归属不一致。"""

    code = "SOP_EXECUTION_CONFLICT"


class SopExecutionFencedError(SopExecutionConflictError):
    """执行租约失效、过期或 fencing token 落后时拒绝权威写。"""

    code = "SOP_EXECUTION_FENCED"

    def __init__(
        self,
        message: str,
        *,
        tenant_id: str,
        instance_id: str,
        worker_id: str,
        fencing_token: int,
        action: str,
    ) -> None:
        """保存不含业务载荷的拒绝证据，供事务边界写入隔离审计。"""

        super().__init__(message)
        self.tenant_id = tenant_id
        self.instance_id = instance_id
        self.worker_id = worker_id
        self.fencing_token = fencing_token
        self.action = action


class SopExecutionSkillAuthorizationError(SopExecutionConflictError):
    """表示带 Skill 因果的 Operation 未通过实时父链 allowlist。"""

    code = "GENERAL_SKILL_TOOL_NOT_AUTHORIZED"

    def __init__(self, message: str, *, authorization_code: str) -> None:
        """保留稳定授权错误码，供 Runtime 转为失败回执或重新规划。"""

        super().__init__(message)
        self.authorization_code = authorization_code


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    """表示一次由数据库时间裁决的 execution 推进所有权。"""

    tenant_id: str
    instance_id: str
    worker_id: str
    fencing_token: int
    expires_at: datetime


class SopExecutionStore:
    """以统一状态机和租户边界维护可恢复 SOP 执行聚合。"""

    def __init__(self, db: Session) -> None:
        """绑定事务；正常提交由服务控制，fencing 拒绝会独立提交脱敏审计。"""

        self.db = db
        self._lease: ExecutionLease | None = None

    def authorize_mutation(self, instance: SopInstance, action: str) -> None:
        """供同一 Runtime 的扩展聚合复用 execution revision、租约和 fencing 写屏障。"""

        self._guard_mutation(instance, action)

    def database_now(self) -> datetime:
        """向信号和投递 worker 暴露数据库权威时间，不允许调用方使用本机时钟裁决。"""

        return self._database_now()

    def active_instance(self, tenant_id: str, session_id: str) -> SopInstance | None:
        """按 tenant/session 返回统一活动槽中的 Execution，供入口做幂等结果标记。"""

        return self._active_instance(tenant_id, session_id)

    @contextmanager
    def owned(
        self,
        instance: SopInstance,
        *,
        worker_id: str,
        ttl_seconds: int = 30,
    ) -> Iterator[ExecutionLease]:
        """原子取得 execution 所有权，并在作用域结束时仅释放本 token 的租约。"""

        if ttl_seconds < 1:
            raise ValueError("execution lease TTL 必须大于零。")
        if self._lease is not None:
            if self._lease.instance_id != instance.id:
                raise SopExecutionConflictError("同一 Store 不能同时推进两个 SOP 实例。")
            yield self._lease
            return
        lease = self.claim(instance, worker_id=worker_id, ttl_seconds=ttl_seconds)
        self._lease = lease
        fenced = False
        try:
            yield lease
        except SopExecutionFencedError:
            fenced = True
            raise
        finally:
            try:
                if not fenced:
                    self.release(lease)
            finally:
                self._lease = None

    def claim(
        self,
        instance: SopInstance,
        *,
        worker_id: str,
        ttl_seconds: int = 30,
    ) -> ExecutionLease:
        """以数据库当前时间原子抢占空闲或过期租约，并单调增加 fencing token。"""

        self._assert_instance_tenant(instance)
        if not worker_id.strip():
            raise ValueError("execution lease worker_id 不能为空。")
        if ttl_seconds < 1:
            raise ValueError("execution lease TTL 必须大于零。")
        database_now = self._database_now()
        expires_at = database_now + timedelta(seconds=ttl_seconds)
        with self.db.no_autoflush:
            result = self.db.exec(
                update(SopInstance)
                .where(
                    SopInstance.id == instance.id,
                    SopInstance.tenant_id == instance.tenant_id,
                    SopInstance.status.in_(ACTIVE_INSTANCE_STATUSES),
                    or_(
                        SopInstance.lease_owner.is_(None),
                        SopInstance.lease_expires_at <= database_now,
                    ),
                )
                .values(
                    lease_owner=worker_id,
                    lease_acquired_at=database_now,
                    lease_heartbeat_at=database_now,
                    lease_expires_at=expires_at,
                    fencing_token=SopInstance.fencing_token + 1,
                    revision=SopInstance.revision + 1,
                    updated_at=database_now,
                )
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            raise SopExecutionConflictError("SOP 实例正在由其他 worker 推进。")
        self.db.refresh(instance)
        return ExecutionLease(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            worker_id=worker_id,
            fencing_token=instance.fencing_token,
            expires_at=expires_at,
        )

    def renew(self, lease: ExecutionLease, *, ttl_seconds: int = 30) -> ExecutionLease:
        """仅允许当前未过期 token 续租，并继续使用数据库权威时间。"""

        if ttl_seconds < 1:
            raise ValueError("execution lease TTL 必须大于零。")
        database_now = self._database_now()
        expires_at = database_now + timedelta(seconds=ttl_seconds)
        with self.db.no_autoflush:
            result = self.db.exec(
                update(SopInstance)
                .where(*self._lease_predicates(lease, database_now, require_unexpired=True))
                .values(
                    lease_heartbeat_at=database_now,
                    lease_expires_at=expires_at,
                    revision=SopInstance.revision + 1,
                    updated_at=database_now,
                )
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            self._persist_fencing_rejection(lease, "lease.renew")
            raise self._fenced_error(lease, "lease.renew")
        instance = self.db.get(SopInstance, lease.instance_id)
        if instance is not None:
            self.db.refresh(instance)
        renewed = ExecutionLease(
            tenant_id=lease.tenant_id,
            instance_id=lease.instance_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            expires_at=expires_at,
        )
        if self._lease == lease:
            self._lease = renewed
        return renewed

    def release(self, lease: ExecutionLease) -> bool:
        """仅清除与 owner/token 同时匹配的租约，绝不释放后来 worker 的所有权。"""

        database_now = self._database_now()
        result = self.db.exec(
            update(SopInstance)
            .where(*self._lease_predicates(lease, database_now, require_unexpired=False))
            .values(
                lease_owner=None,
                lease_expires_at=None,
                lease_heartbeat_at=database_now,
                revision=SopInstance.revision + 1,
                updated_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def start_instance(
        self,
        *,
        tenant_id: str,
        session_id: str,
        skill_id: str,
        skill_version_id: str,
        skill_version: str,
        definition_checksum: str,
        start_node_id: str,
        slots: Mapping[str, object] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> tuple[SopInstance, bool]:
        """创建并启动实例；相同会话和技能版本的活动实例按幂等方式复用。"""

        active = self._active_instance(tenant_id, session_id)
        if active is not None:
            if active.skill_version_id != skill_version_id:
                raise SopExecutionConflictError(
                    "同一会话已存在绑定其他不可变技能版本的活动 SOP 实例。"
                )
            return active, False

        run_number = self._next_run_number(tenant_id, session_id, skill_version_id)
        instance = SopInstance(
            tenant_id=tenant_id,
            session_id=session_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            skill_version=skill_version,
            definition_checksum=definition_checksum,
            run_number=run_number,
            kind="sop",
            active_slot_key=f"foreground:{session_id}",
            source_kind="chat",
            source_ref=session_id,
            current_node_id=start_node_id,
            slots_json=dict(slots or {}),
            context_json=dict(context or {}),
        )
        transition = transition_instance(
            SopInstanceStatus.CREATED,
            SopInstanceStatus.RUNNING,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.started_at = utc_now()
        self.db.add(instance)
        self.db.flush()
        return instance, True

    def start_dynamic_instance(
        self,
        *,
        tenant_id: str,
        session_id: str,
        agent_id: str,
        initiator_user_id: str,
        plan: NormalizedPlan,
        capability_snapshot: Mapping[str, object],
        source_kind: str = "chat",
        source_ref: str | None = None,
    ) -> tuple[SopInstance, ExecutionPlanRevision]:
        """原子创建动态 Execution 与首个活动计划，不伪造 SkillVersion 身份。"""

        if not agent_id.strip() or not initiator_user_id.strip():
            raise SopExecutionConflictError("动态 Execution 必须绑定 Agent 和发起人。")
        capability_payload = dict(capability_snapshot)
        if not capability_payload:
            raise SopExecutionConflictError("动态 Execution 必须冻结非空能力快照。")
        self._assert_plan_capabilities_available(plan, capability_payload)
        plan_payload = plan.model_dump(mode="json")
        plan_checksum = canonical_checksum(plan)
        capability_digest = capability_checksum(capability_payload)
        resolved_source_ref = source_ref or session_id
        active = self._active_instance(tenant_id, session_id)
        if active is not None:
            if (
                active.kind == "dynamic_task"
                and active.agent_id == agent_id
                and active.initiator_user_id == initiator_user_id
                and active.current_plan_checksum == plan_checksum
                and active.capability_checksum == capability_digest
                and active.source_kind == source_kind
                and active.source_ref == resolved_source_ref
            ):
                revision = self.db.get(ExecutionPlanRevision, active.current_plan_revision_id)
                if (
                    revision is not None
                    and revision.execution_id == active.id
                    and revision.status == "active"
                ):
                    return active, revision
            raise SopExecutionConflictError("同一会话已存在语义不同的活动 Execution。")
        instance = SopInstance(
            tenant_id=tenant_id,
            session_id=session_id,
            run_number=self._next_execution_run_number(tenant_id, session_id),
            kind="dynamic_task",
            active_slot_key=f"foreground:{session_id}",
            initiator_user_id=initiator_user_id,
            source_kind=source_kind,
            source_ref=resolved_source_ref,
            agent_id=agent_id,
            goal_snapshot_json={
                "goal": plan.goal,
                "success_criteria": [
                    criterion.model_dump(mode="json") for criterion in plan.success_criteria
                ],
            },
            current_node_id=plan.steps[0].step_key,
            current_plan_checksum=plan_checksum,
            capability_snapshot_json=capability_payload,
            capability_checksum=capability_digest,
            budget_snapshot_json=dict(plan.budget),
        )
        revision = ExecutionPlanRevision(
            tenant_id=tenant_id,
            execution_id=instance.id,
            revision_number=1,
            reason=PlanReason.INITIAL.value,
            status="active",
            plan_json=plan_payload,
            checksum=plan_checksum,
            capability_snapshot_json=capability_payload,
            capability_checksum=capability_digest,
            activated_at=utc_now(),
        )
        instance.current_plan_revision_id = revision.id
        transition = transition_instance(
            SopInstanceStatus.CREATED,
            SopInstanceStatus.RUNNING,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.started_at = utc_now()
        self.db.add(instance)
        self.db.add(revision)
        self.db.flush()
        return instance, revision

    def append_plan_revision(
        self,
        instance: SopInstance,
        *,
        plan: NormalizedPlan,
        reason: PlanReason,
        capability_snapshot: Mapping[str, object],
        created_by_proposal_id: str | None = None,
    ) -> tuple[ExecutionPlanRevision, bool]:
        """追加并激活动态计划；历史 step key 的语义及已完成步骤均不可改写。"""

        self._assert_dynamic_instance(instance)
        plan_payload = plan.model_dump(mode="json")
        checksum = canonical_checksum(plan)
        current = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
        if current is None or current.execution_id != instance.id:
            raise SopExecutionConflictError("动态 Execution 当前计划不存在或归属错误。")
        if current.checksum == checksum:
            return current, False
        capability_payload = dict(capability_snapshot)
        if not capability_payload:
            raise SopExecutionConflictError("计划修订必须冻结非空能力快照。")
        self._assert_plan_capabilities_available(plan, capability_payload)
        causal_proposal = None
        if created_by_proposal_id is not None:
            causal_proposal = self.db.get(ActionProposalRecord, created_by_proposal_id)
            if (
                causal_proposal is None
                or causal_proposal.tenant_id != instance.tenant_id
                or causal_proposal.execution_id != instance.id
                or causal_proposal.status != "validated"
                or causal_proposal.normalized_proposal_json.get("action_kind") != "replan"
            ):
                raise SopExecutionConflictError("计划修订的因果提案不存在或不属于当前 Execution。")
        self._guard_mutation(instance, "plan.append")
        self._assert_plan_preserves_step_identity(instance, plan_payload)
        next_number = int(
            self.db.exec(
                select(func.max(ExecutionPlanRevision.revision_number)).where(
                    ExecutionPlanRevision.tenant_id == instance.tenant_id,
                    ExecutionPlanRevision.execution_id == instance.id,
                )
            ).one()
            or 0
        ) + 1
        revision = ExecutionPlanRevision(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            revision_number=next_number,
            parent_revision_id=current.id,
            reason=reason.value,
            status="active",
            plan_json=plan_payload,
            checksum=checksum,
            capability_snapshot_json=capability_payload,
            capability_checksum=capability_checksum(capability_payload),
            created_by_proposal_id=created_by_proposal_id,
            activated_at=utc_now(),
        )
        current.status = "superseded"
        current.superseded_at = utc_now()
        instance.current_plan_revision_id = revision.id
        instance.current_plan_checksum = revision.checksum
        instance.capability_snapshot_json = capability_payload
        instance.capability_checksum = revision.capability_checksum
        instance.budget_snapshot_json = dict(plan.budget)
        if causal_proposal is not None:
            causal_proposal.status = "consumed"
            causal_proposal.consumed_plan_revision_id = revision.id
            causal_proposal.consumed_at = utc_now()
            self.db.add(causal_proposal)
        self.db.add(current)
        self.db.add(revision)
        self.db.add(instance)
        self.db.flush()
        return revision, True

    def enter_node(
        self,
        instance: SopInstance,
        node_id: str,
        *,
        input_snapshot: Mapping[str, object] | None = None,
        step_key: str | None = None,
        plan_revision_id: str | None = None,
        step_kind: str = "sop_node",
        title: str | None = None,
        required: bool = True,
    ) -> SopNodeExecution:
        """为统一步骤创建新 attempt；SOP 默认以 node id 作为稳定 step key。"""

        self._assert_instance_tenant(instance)
        self._guard_mutation(instance, "node.enter")
        stable_step_key = step_key or node_id
        if instance.kind == "dynamic_task":
            if plan_revision_id != instance.current_plan_revision_id:
                raise SopExecutionConflictError("动态步骤必须绑定当前活动 PlanRevision。")
            self._assert_step_declared(instance, stable_step_key)
        elif plan_revision_id is not None:
            raise SopExecutionConflictError("正式 SOP 节点不得绑定动态 PlanRevision。")
        attempt = self._next_node_attempt(instance.tenant_id, instance.id, stable_step_key)
        execution = SopNodeExecution(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_id=node_id,
            step_key=stable_step_key,
            plan_revision_id=plan_revision_id,
            step_kind=step_kind,
            title=title,
            required=required,
            attempt=attempt,
            input_json=dict(input_snapshot or {}),
        )
        transition = transition_node(
            NodeExecutionStatus.SCHEDULED,
            NodeExecutionStatus.RUNNING,
            actual_revision=execution.revision,
        )
        self._apply_node_transition(execution, transition)
        execution.started_at = utc_now()
        instance.current_node_id = node_id
        instance.revision += 1
        instance.updated_at = utc_now()
        self.db.add(execution)
        self.db.add(instance)
        self.db.flush()
        return execution

    def record_action_proposal(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        provider: str,
        model: str,
        model_capability_snapshot: Mapping[str, object],
        completed_response: CompletedProviderProposal,
        causation_id: str | None = None,
    ) -> tuple[ActionProposalRecord, bool]:
        """仅将完整 provider 响应中的已解析规范提案写入不可变决策账本。"""

        self._assert_dynamic_instance(instance)
        self._assert_execution_owner(instance, execution)
        if execution.plan_revision_id != instance.current_plan_revision_id:
            raise SopExecutionConflictError("只能为当前活动计划的步骤记录动作提案。")
        self._assert_proposal_declared_by_step(instance, execution, completed_response)
        if not provider.strip() or len(provider) > 64 or not model.strip() or len(model) > 191:
            raise SopExecutionConflictError("动作提案的 provider/model 身份无效。")
        capability_payload = dict(model_capability_snapshot)
        if not capability_payload:
            raise SopExecutionConflictError("动作提案必须冻结模型能力快照。")
        normalized = completed_response.proposal.model_dump(mode="json")
        identity = {
            "execution_id": instance.id,
            "plan_revision_id": execution.plan_revision_id,
            "step_key": execution.step_key,
            "step_attempt": execution.attempt,
            "provider": provider,
            "model": model,
            "model_capability_snapshot": capability_payload,
            "response_id": completed_response.response_id,
            "finish_reason": completed_response.finish_reason,
            "proposal": normalized,
        }
        checksum = canonical_checksum(identity)
        response_identity = capability_checksum(
            {"provider": provider, "response_id": completed_response.response_id}
        )
        existing = self.db.exec(
            select(ActionProposalRecord).where(
                ActionProposalRecord.tenant_id == instance.tenant_id,
                ActionProposalRecord.execution_id == instance.id,
                ActionProposalRecord.proposal_checksum == checksum,
            )
        ).first()
        if existing is not None:
            return existing, False
        response_record = self.db.exec(
            select(ActionProposalRecord).where(
                ActionProposalRecord.tenant_id == instance.tenant_id,
                ActionProposalRecord.execution_id == instance.id,
                ActionProposalRecord.provider_response_identity == response_identity,
            )
        ).first()
        if response_record is not None:
            raise SopExecutionConflictError("同一 provider response 不得映射为不同动作提案。")
        self._guard_mutation(instance, "proposal.record")
        proposal = ActionProposalRecord(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            plan_revision_id=execution.plan_revision_id or "",
            step_key=execution.step_key,
            step_attempt=execution.attempt,
            provider=provider,
            model=model,
            provider_response_id=completed_response.response_id,
            provider_response_identity=response_identity,
            finish_reason=completed_response.finish_reason,
            model_capability_snapshot_json=capability_payload,
            normalized_proposal_json=normalized,
            validation_json={
                "provider_response_complete": True,
                "schema_validated": True,
                "current_plan_step_validated": True,
                "capability_scope_validated": True,
            },
            proposal_checksum=checksum,
            usage_json=dict(completed_response.usage),
            causation_id=causation_id,
        )
        self.db.add(proposal)
        self.db.flush()
        return proposal, True

    def consume_action_proposal(
        self,
        instance: SopInstance,
        proposal: ActionProposalRecord,
        *,
        operation_id: str,
    ) -> bool:
        """幂等消费 validated proposal；已消费记录不得改绑另一 Operation。"""

        self._assert_dynamic_instance(instance)
        if proposal.execution_id != instance.id or proposal.tenant_id != instance.tenant_id:
            raise SopExecutionConflictError("动作提案与 Execution 归属不一致。")
        step_execution = self.db.exec(
            select(SopNodeExecution).where(
                SopNodeExecution.tenant_id == instance.tenant_id,
                SopNodeExecution.instance_id == instance.id,
                SopNodeExecution.plan_revision_id == proposal.plan_revision_id,
                SopNodeExecution.step_key == proposal.step_key,
                SopNodeExecution.attempt == proposal.step_attempt,
            )
        ).first()
        operation = self.db.get(SopOperation, operation_id)
        if (
            step_execution is None
            or operation is None
            or operation.tenant_id != instance.tenant_id
            or operation.instance_id != instance.id
            or operation.node_execution_id != step_execution.id
        ):
            raise SopExecutionConflictError("动作提案只能绑定同一 Execution 的已准备 Operation。")
        if proposal.status == "consumed":
            if proposal.consumed_operation_id != operation_id:
                raise SopExecutionConflictError("已消费提案不得改绑另一 Operation。")
            return False
        if proposal.status != "validated":
            raise SopExecutionConflictError("只有 validated 动作提案可以消费。")
        self._guard_mutation(instance, "proposal.consume")
        proposal.status = "consumed"
        proposal.consumed_operation_id = operation_id
        proposal.consumed_at = utc_now()
        self.db.add(proposal)
        self.db.flush()
        return True

    def consume_result_proposal(
        self,
        instance: SopInstance,
        proposal: ActionProposalRecord,
    ) -> bool:
        """将 answer/complete 提案标记为最终结果已消费，不伪造 Operation 或 PlanRevision。"""

        if proposal.tenant_id != instance.tenant_id or proposal.execution_id != instance.id:
            raise SopExecutionConflictError("结果提案与 Execution 归属不一致。")
        if proposal.status == "consumed":
            return False
        if proposal.status != "validated" or proposal.normalized_proposal_json.get(
            "action_kind"
        ) not in {"answer", "complete"}:
            raise SopExecutionConflictError("只有 validated answer/complete 提案可消费为结果。")
        self._guard_mutation(instance, "proposal.consume_result")
        proposal.status = "consumed"
        proposal.consumed_at = utc_now()
        self.db.add(proposal)
        self.db.flush()
        return True

    def consume_attention_proposal(
        self,
        instance: SopInstance,
        proposal: ActionProposalRecord,
        *,
        attention_id: str,
    ) -> bool:
        """将等待输入提案绑定到同 Execution 的 Attention，不伪造工具 Operation。"""

        if proposal.tenant_id != instance.tenant_id or proposal.execution_id != instance.id:
            raise SopExecutionConflictError("等待提案与 Execution 归属不一致。")
        attention = self.db.get(SopWorkItem, attention_id)
        if (
            attention is None
            or attention.tenant_id != instance.tenant_id
            or attention.instance_id != instance.id
        ):
            raise SopExecutionConflictError("等待提案只能绑定同一 Execution 的 Attention。")
        if proposal.status == "consumed":
            if proposal.causation_id != attention_id:
                raise SopExecutionConflictError("已消费等待提案不得改绑另一 Attention。")
            return False
        if proposal.status != "validated" or proposal.normalized_proposal_json.get(
            "action_kind"
        ) not in {"wait_input", "wait_attention"}:
            raise SopExecutionConflictError("只有 validated 等待提案可消费为 Attention。")
        self._guard_mutation(instance, "proposal.consume_attention")
        proposal.status = "consumed"
        proposal.causation_id = attention_id
        proposal.consumed_at = utc_now()
        self.db.add(proposal)
        self.db.flush()
        return True

    def snapshot_input_resource(
        self,
        instance: SopInstance,
        resource: ManagedInputResource,
        *,
        source_message_id: str | None = None,
    ) -> tuple[InputResourceSnapshot, bool]:
        """在当前所有权下追加冻结 ready 输入身份；ACL 证据由服务端事实机械生成。"""

        self._assert_dynamic_instance(instance)
        try:
            assert_input_resource_access(self.db, resource, instance=instance)
        except InputResourceAccessDenied as exc:
            raise SopExecutionConflictError("输入资源不可用。") from exc
        if resource.ingestion_status != "ready" or resource.revoked_at is not None:
            raise SopExecutionConflictError("只有当前 ready 输入可以形成 Execution snapshot。")
        message_id = source_message_id or resource.source_message_id
        if resource.source_type == "chat_upload":
            message = self.db.get(Message, message_id) if message_id else None
            if (
                message is None
                or message.tenant_id != instance.tenant_id
                or message.session_id != instance.session_id
                or message.role != "user"
                or not self._message_references_resource(message, resource)
            ):
                raise SopExecutionConflictError("聊天输入必须绑定同会话的权威用户消息引用。")
        existing = self.db.exec(
            select(InputResourceSnapshot).where(
                InputResourceSnapshot.tenant_id == instance.tenant_id,
                InputResourceSnapshot.execution_id == instance.id,
                InputResourceSnapshot.source_resource_id == resource.id,
                InputResourceSnapshot.source_version == resource.version,
                InputResourceSnapshot.content_checksum == resource.content_checksum,
            )
        ).first()
        if existing is not None:
            return existing, False
        self._guard_mutation(instance, "input.snapshot")
        identity_checksum = capability_checksum(
            {
                "source_type": resource.source_type,
                "source_resource_id": resource.id,
                "source_version": resource.version,
                "content_checksum": resource.content_checksum,
            }
        )
        snapshot = InputResourceSnapshot(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            source_type=resource.source_type,
            source_resource_id=resource.id,
            source_version=resource.version,
            source_message_id=message_id,
            filename=resource.filename,
            mime_type=resource.mime_type,
            size_bytes=resource.size_bytes,
            content_checksum=resource.content_checksum,
            extraction_checksum=resource.extraction_checksum,
            ingestion_status=resource.ingestion_status,
            identity_checksum=identity_checksum,
            storage_locator_digest=hashlib.sha256(
                resource.storage_locator.encode("utf-8")
            ).hexdigest(),
            captured_acl_json={
                "owner_user_id": resource.owner_user_id,
                "agent_id": resource.agent_id,
                "acl_revision": resource.acl_revision,
                "captured_for_initiator": instance.initiator_user_id,
            },
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot, True

    def wait_for_input(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        expected_inputs: tuple[str, ...],
    ) -> None:
        """将输入节点和实例一起暂停，并冻结当前待补字段。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.wait_input")
        node_transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.WAITING,
            actual_revision=execution.revision,
        )
        instance_transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.WAITING,
            actual_revision=instance.revision,
        )
        self._apply_node_transition(execution, node_transition)
        execution.output_json = {"expected_inputs": list(expected_inputs)}
        self._apply_instance_transition(instance, instance_transition)
        self.db.add(execution)
        self.db.add(instance)
        self.db.flush()

    def wait_for_work_item(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        work_item_id: str,
    ) -> None:
        """将人工节点和实例一起暂停，并保存本次等待的结构化工作项标识。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.wait_work_item")
        node_transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.WAITING,
            actual_revision=execution.revision,
        )
        instance_transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.WAITING,
            actual_revision=instance.revision,
        )
        self._apply_node_transition(execution, node_transition)
        execution.output_json = {"work_item_id": work_item_id}
        self._apply_instance_transition(instance, instance_transition)
        self.db.add(execution)
        self.db.add(instance)
        self.db.flush()

    def wait_for_timer(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        signal_id: str,
    ) -> None:
        """将节点和实例暂停到持久 timer signal，不借用人工 Attention 语义。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.wait_timer")
        node_transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.WAITING,
            actual_revision=execution.revision,
        )
        instance_transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.WAITING,
            actual_revision=instance.revision,
        )
        self._apply_node_transition(execution, node_transition)
        execution.output_json = {"timer_signal_id": signal_id}
        self._apply_instance_transition(instance, instance_transition)
        self.db.add(execution)
        self.db.add(instance)
        self.db.flush()

    def wait_for_publication(self, instance: SopInstance) -> None:
        """在结果步骤完成后把实例暂停到 required 外部 publication 结算。"""

        self._guard_mutation(instance, "instance.wait_publication")
        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.WAITING,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.current_node_id = None
        self.db.add(instance)
        self.db.flush()

    def resume_waiting_node(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        slots: Mapping[str, object],
    ) -> None:
        """用新的槽位快照恢复暂停节点，不创建重复 attempt。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.resume")
        node_transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.RUNNING,
            actual_revision=execution.revision,
        )
        instance_transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.RUNNING,
            actual_revision=instance.revision,
        )
        self._apply_node_transition(execution, node_transition)
        self._apply_instance_transition(instance, instance_transition)
        instance.slots_json = dict(slots)
        self.db.add(execution)
        self.db.add(instance)
        self.db.flush()

    def retarget_waiting_work_item(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        work_item_id: str,
    ) -> None:
        """保持 waiting 状态，仅把节点的当前阻塞依据切换到新的持久工作项。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.retarget_work_item")
        if execution.status != "waiting" or instance.status != "waiting":
            raise SopExecutionConflictError("只有 waiting 节点可以切换阻塞工作项。")
        execution.output_json = {"work_item_id": work_item_id}
        execution.revision += 1
        execution.updated_at = self._database_now()
        instance.revision += 1
        instance.updated_at = execution.updated_at
        self.db.add(execution)
        self.db.add(instance)
        self.db.flush()

    def complete_node(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        output: Mapping[str, object] | None = None,
    ) -> None:
        """成功结束当前节点 attempt，并保存不可变语义的输出快照。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.complete")
        transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.SUCCEEDED,
            actual_revision=execution.revision,
        )
        self._apply_node_transition(execution, transition)
        execution.output_json = dict(output or {})
        execution.completed_at = utc_now()
        self.db.add(execution)
        self.db.flush()

    def fail_node(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        error: Mapping[str, object],
    ) -> None:
        """失败结束当前节点 attempt，并保存结构化错误供审计和恢复判断。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.fail")
        transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.FAILED,
            actual_revision=execution.revision,
        )
        self._apply_node_transition(execution, transition)
        execution.error_json = dict(error)
        execution.completed_at = utc_now()
        self.db.add(execution)
        self.db.flush()

    def timeout_node(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        error: Mapping[str, object],
    ) -> None:
        """把等待或运行节点推进为 timed_out，并保存结构化超时原因。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "node.timeout")
        transition = transition_node(
            NodeExecutionStatus(execution.status),
            NodeExecutionStatus.TIMED_OUT,
            actual_revision=execution.revision,
        )
        self._apply_node_transition(execution, transition)
        execution.error_json = dict(error)
        execution.completed_at = utc_now()
        self.db.add(execution)
        self.db.flush()

    def prepare_operation(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        operation_name: str,
        request: Mapping[str, object],
        logical_action_id: str | None = None,
        idempotency_policy: IdempotencyPolicy | None = None,
        effect_kind: str = "read",
        compensates_operation_id: str | None = None,
        caused_by_skill_use_id: str | None = None,
        caused_by_skill_use_ids: Sequence[str] = (),
        capability_snapshot: Mapping[str, object] | None = None,
        capability_snapshot_checksum: str | None = None,
    ) -> tuple[SopOperation, bool]:
        """准备稳定逻辑动作，并保留触发它的全部固定 Skill Use 因果归属。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "operation.prepare")
        if effect_kind not in {"read", "external_write"}:
            raise ValueError("effect_kind 必须是 read 或 external_write。")
        policy = idempotency_policy or IdempotencyPolicy()
        frozen_capability = dict(capability_snapshot or {})
        frozen_checksum = capability_checksum(frozen_capability) if frozen_capability else None
        if capability_snapshot_checksum is not None and capability_snapshot_checksum != frozen_checksum:
            raise ValueError("capability snapshot checksum 与规范快照不一致。")
        fingerprint = self.request_fingerprint(request)
        normalized_skill_use_ids = tuple(
            dict.fromkeys(
                value
                for value in (
                    str(caused_by_skill_use_id or "").strip(),
                    *(str(item).strip() for item in caused_by_skill_use_ids),
                )
                if value
            )
        )
        primary_skill_use_id = normalized_skill_use_ids[0] if normalized_skill_use_ids else None
        action_id = logical_action_id or self._default_logical_action_id(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_id=execution.node_id,
            operation_name=operation_name,
        )
        if not action_id.strip():
            raise ValueError("logical_action_id 不能为空。")
        if compensates_operation_id is not None:
            self._validate_compensation_target(
                instance,
                compensates_operation_id=compensates_operation_id,
            )
        existing = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.logical_action_id == action_id,
            )
        ).first()
        if existing is not None:
            if (
                existing.instance_id != instance.id
                or existing.operation_name != operation_name
                or existing.request_fingerprint != fingerprint
                or existing.effect_kind != effect_kind
                or existing.idempotency_required is not policy.required
                or existing.idempotency_scope != policy.scope.value
                or tuple(existing.idempotency_key_fields_json or ()) != policy.key_fields
                or existing.compensates_operation_id != compensates_operation_id
                or existing.caused_by_skill_use_id != primary_skill_use_id
                or tuple(existing.caused_by_skill_use_ids_json or ())
                != normalized_skill_use_ids
                or dict(existing.capability_snapshot_json or {}) != frozen_capability
                or existing.capability_checksum != frozen_checksum
            ):
                raise SopExecutionConflictError(
                    "logical action 的 fingerprint、策略或效果契约与已持久化命令不一致。"
                )
            self._ensure_operation_attempt(existing, execution)
            return existing, False
        idempotency_key = self.operation_idempotency_key(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            logical_action_id=action_id,
            operation_name=operation_name,
        )
        remote_key = (
            self.remote_idempotency_key(
                tenant_id=instance.tenant_id,
                instance_id=instance.id,
                logical_action_id=action_id,
                operation_name=operation_name,
                request=request,
                policy=policy,
            )
            if effect_kind == "external_write"
            else None
        )
        operation = SopOperation(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=execution.id,
            operation_name=operation_name,
            idempotency_key=idempotency_key,
            logical_action_id=action_id,
            request_fingerprint=fingerprint,
            remote_idempotency_key=remote_key,
            idempotency_required=policy.required,
            idempotency_scope=policy.scope.value,
            idempotency_key_fields_json=list(policy.key_fields),
            effect_kind=effect_kind,
            compensates_operation_id=compensates_operation_id,
            caused_by_skill_use_id=primary_skill_use_id,
            caused_by_skill_use_ids_json=list(normalized_skill_use_ids),
            request_json=dict(request),
            capability_snapshot_json=frozen_capability,
            capability_checksum=frozen_checksum,
        )
        self.db.add(operation)
        self.db.flush()
        self._ensure_operation_attempt(operation, execution)
        return operation, True

    def prepare_operation_from_proposal(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        proposal: ActionProposalRecord,
        *,
        operation_name: str,
        request: Mapping[str, object],
        idempotency_policy: IdempotencyPolicy | None = None,
        effect_kind: str = "read",
        caused_by_skill_use_ids: Sequence[str] = (),
        capability_snapshot: Mapping[str, object] | None = None,
        capability_snapshot_checksum: str | None = None,
    ) -> tuple[SopOperation, bool]:
        """在同一事务把 validated proposal 冻结为稳定 Operation 并标记已消费。"""

        if (
            proposal.tenant_id != instance.tenant_id
            or proposal.execution_id != instance.id
            or proposal.plan_revision_id != execution.plan_revision_id
            or proposal.step_key != execution.step_key
            or proposal.step_attempt != execution.attempt
        ):
            raise SopExecutionConflictError("动作提案与当前 Execution/Step 归属不一致。")
        if proposal.status == "consumed":
            operation = self.db.get(SopOperation, proposal.consumed_operation_id)
            if operation is None or operation.node_execution_id != execution.id:
                raise SopExecutionConflictError("已消费提案的 Operation 归属已损坏。")
        elif proposal.status != "validated":
            raise SopExecutionConflictError("只有 validated 动作提案可以准备 Operation。")
        normalized = proposal.normalized_proposal_json
        if normalized.get("action_kind") not in {"call_tool", "query_knowledge"}:
            raise SopExecutionConflictError("只有能力调用提案可以准备 Operation。")
        if normalized.get("capability_ref") != operation_name:
            raise SopExecutionConflictError("Operation 能力与已验证提案不一致。")
        arguments = normalized.get("arguments")
        if not isinstance(arguments, dict) or self.request_fingerprint(arguments) != self.request_fingerprint(
            request
        ):
            raise SopExecutionConflictError("Operation 参数与已验证提案不一致。")
        operation, created = self.prepare_operation(
            instance,
            execution,
            operation_name=operation_name,
            request=request,
            logical_action_id=f"proposal:{proposal.id}",
            idempotency_policy=idempotency_policy,
            effect_kind=effect_kind,
            caused_by_skill_use_ids=caused_by_skill_use_ids,
            capability_snapshot=capability_snapshot,
            capability_snapshot_checksum=capability_snapshot_checksum,
        )
        self.consume_action_proposal(instance, proposal, operation_id=operation.id)
        return operation, created

    def start_operation(self, operation: SopOperation) -> None:
        """在真正调用外部工具前把 prepared 操作推进为 running。"""

        self._authorize_skill_caused_operation(operation)
        self._guard_operation_mutation(operation, "operation.start")
        transition = transition_operation(
            OperationStatus(operation.status),
            OperationStatus.RUNNING,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.started_at = utc_now()
        attempt = self._latest_operation_attempt(operation)
        if attempt is not None and attempt.status == OperationStatus.PREPARED.value:
            attempt.status = OperationStatus.RUNNING.value
            attempt.started_at = operation.started_at
            attempt.updated_at = operation.started_at
            self.db.add(attempt)
        self.db.add(operation)
        self.db.flush()

    def _authorize_skill_caused_operation(self, operation: SopOperation) -> None:
        """按 Operation 快照、执行归属和全部 Use 父链执行最后一道收窄授权。"""

        explicit_use_ids = tuple(operation.caused_by_skill_use_ids_json or ())
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
        if not use_ids:
            return
        instance = self.db.get(SopInstance, operation.instance_id)
        snapshot = dict(operation.capability_snapshot_json or {})
        snapshot_type = str(snapshot.get("capability_type") or "")
        if snapshot_type == "tool":
            action = str(snapshot.get("name") or "").strip()
        elif snapshot_type == "connector":
            audit_view = snapshot.get("audit_view")
            action = (
                str(audit_view.get("required_action") or "").strip()
                if isinstance(audit_view, dict)
                else ""
            )
        elif snapshot_type == "knowledge":
            action = str(snapshot.get("name") or "").strip()
        else:
            action = ""
        if (
            instance is None
            or instance.tenant_id != operation.tenant_id
            or not action
        ):
            raise SopExecutionSkillAuthorizationError(
                "Skill 因果链、执行归属或能力快照不完整。",
                authorization_code="GENERAL_SKILL_TOOL_CAUSE_INVALID",
            )
        from app.general_skills.runtime import (  # 避免执行存储初始化时形成循环依赖
            GeneralSkillRuntimeError,
            GeneralSkillRuntimeService,
        )

        for use_id in use_ids:
            use = self.db.get(GeneralSkillUse, use_id)
            actor = self.db.get(User, use.user_id) if use is not None else None
            if (
                use is None
                or actor is None
                or use.tenant_id != operation.tenant_id
                or use.session_id != instance.session_id
                or (bool(explicit_use_ids) and use.execution_id != instance.id)
                or use.agent_id != instance.agent_id
                or use.user_id != instance.initiator_user_id
            ):
                raise SopExecutionSkillAuthorizationError(
                    "Skill 因果链或执行归属不完整。",
                    authorization_code="GENERAL_SKILL_TOOL_CAUSE_INVALID",
                )
            try:
                GeneralSkillRuntimeService(self.db).authorize_tool_for_use(
                    actor,
                    use_id=use.id,
                    tool_name=action,
                    baseline_tools={action},
                )
            except GeneralSkillRuntimeError as exc:
                raise SopExecutionSkillAuthorizationError(
                    "Skill 未授权本次工具动作。",
                    authorization_code=exc.code,
                ) from exc

    def authorize_external_operation_dispatch(
        self,
        operation: SopOperation,
        *,
        approval_work_item_id: str | None,
        approval_fingerprint: str,
        approved_by_user_id: str,
        authorization_evidence: Mapping[str, object],
        authorization_source_type: str = "attention",
        authorization_source_ref: str | None = None,
    ) -> None:
        """在同一事务冻结 Attention 或长期规则授权证据，提交后方可外呼。"""

        if operation.effect_kind != "external_write" or operation.status != "prepared":
            raise SopExecutionConflictError("只有 prepared 外部写可以绑定批准并派发。")
        self._authorize_skill_caused_operation(operation)
        if not approval_fingerprint.strip() or not approved_by_user_id.strip() or not authorization_evidence:
            raise SopExecutionConflictError("外部写批准和再授权证据不能为空。")
        if authorization_source_type == "attention":
            if not approval_work_item_id or not approval_work_item_id.strip():
                raise SopExecutionConflictError("一次性批准必须绑定 Attention。")
            reused = self.db.exec(
                select(SopOperation).where(
                    SopOperation.tenant_id == operation.tenant_id,
                    SopOperation.approval_work_item_id == approval_work_item_id,
                    SopOperation.id != operation.id,
                )
            ).first()
            if reused is not None:
                raise SopExecutionConflictError("一次性批准已绑定其他 Operation。")
            source_ref = authorization_source_ref or approval_work_item_id
        elif authorization_source_type == "standing_rule":
            if approval_work_item_id is not None or not (authorization_source_ref or "").strip():
                raise SopExecutionConflictError("长期批准必须绑定规则且不能伪装 Attention。")
            source_ref = authorization_source_ref
        else:
            raise SopExecutionConflictError("外部写授权来源类型无效。")
        operation.approval_work_item_id = approval_work_item_id
        operation.approval_fingerprint = approval_fingerprint
        operation.approved_by_user_id = approved_by_user_id
        operation.approved_at = utc_now()
        operation.authorization_evidence_json = dict(authorization_evidence)
        operation.authorization_source_type = authorization_source_type
        operation.authorization_source_ref = source_ref
        self.start_operation(operation)
        operation.dispatched_at = operation.started_at
        self.db.add(operation)
        self.db.flush()

    def finish_operation(
        self,
        operation: SopOperation,
        *,
        succeeded: bool,
        result: Mapping[str, object] | None = None,
        error: Mapping[str, object] | None = None,
        external_reference: str | None = None,
    ) -> None:
        """将工具结果写成成功或失败回执，供条件 DSL 和恢复逻辑消费。"""

        if operation.status != OperationStatus.RUNNING.value:
            raise SopExecutionConflictError("只有 running 操作可以直接写入工具回执。")
        self._guard_operation_mutation(operation, "operation.finish")
        target = OperationStatus.SUCCEEDED if succeeded else OperationStatus.FAILED
        transition = transition_operation(
            OperationStatus(operation.status),
            target,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.result_json = dict(result or {})
        operation.error_json = dict(error or {})
        operation.external_reference = external_reference
        operation.completed_at = utc_now()
        attempt = self._latest_operation_attempt(operation)
        if attempt is not None and attempt.status in {
            OperationStatus.PREPARED.value,
            OperationStatus.RUNNING.value,
        }:
            attempt.status = target.value
            attempt.error_json = dict(error or {})
            attempt.completed_at = operation.completed_at
            attempt.updated_at = operation.completed_at
            self.db.add(attempt)
        if operation.effect_kind == "external_write":
            operation.effect_state = "complete" if succeeded else "none"
            self._append_operation_effect(
                operation,
                event_type="dispatch_succeeded" if succeeded else "dispatch_failed",
                effect_state=operation.effect_state,
                evidence=dict(result or error or {}),
            )
            if succeeded and operation.compensates_operation_id is not None:
                self._mark_compensation_applied(operation)
        self.db.add(operation)
        instance = self._operation_instance(operation)
        self.aggregate_effect_state(instance)
        DynamicTaskQuotaService(self.db).release_tool_operation(operation)
        self.db.flush()

    def cancel_prepared_operation(self, operation: SopOperation) -> None:
        """取消尚未 dispatch 的操作，并证明该动作没有触发任何外部调用。"""

        self._guard_operation_mutation(operation, "operation.cancel_prepared")
        transition = transition_operation(
            OperationStatus(operation.status),
            OperationStatus.CANCELLED,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.cancellation_disposition = "not_dispatched"
        operation.completed_at = utc_now()
        attempt = self._latest_operation_attempt(operation)
        if attempt is not None and attempt.status == OperationStatus.PREPARED.value:
            attempt.status = OperationStatus.CANCELLED.value
            attempt.completed_at = operation.completed_at
            attempt.updated_at = operation.completed_at
            self.db.add(attempt)
        self.db.add(operation)
        self.aggregate_effect_state(self._operation_instance(operation))
        DynamicTaskQuotaService(self.db).release_tool_operation(operation)
        self.db.flush()

    def mark_operation_unknown(
        self,
        operation: SopOperation,
        *,
        error: Mapping[str, object],
    ) -> None:
        """将已 dispatch 外部写标为效果未知，强制后续进入对账而非盲目重试。"""

        if operation.effect_kind != "external_write":
            raise SopExecutionConflictError("只有外部写操作可以进入 unknown 效果状态。")
        self._guard_operation_mutation(operation, "operation.mark_unknown")
        transition = transition_operation(
            OperationStatus(operation.status),
            OperationStatus.UNKNOWN,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.effect_state = "unknown"
        operation.error_json = dict(error)
        operation.cancellation_disposition = "awaiting_reconciliation"
        attempt = self._latest_operation_attempt(operation)
        if attempt is not None and attempt.status == OperationStatus.RUNNING.value:
            attempt.status = OperationStatus.UNKNOWN.value
            attempt.error_json = dict(error)
            attempt.updated_at = utc_now()
            self.db.add(attempt)
        self._append_operation_effect(
            operation,
            event_type="dispatch_outcome_unknown",
            effect_state="unknown",
            evidence=dict(error),
        )
        self.db.add(operation)
        self.aggregate_effect_state(self._operation_instance(operation))
        self.db.flush()

    def mark_stale_running_operation_unknown(
        self,
        operation: SopOperation,
        *,
        timeout_seconds: float,
    ) -> bool:
        """在同步调用已超过上限且无 worker 回执时收敛为 unknown，供崩溃后安全恢复。"""

        if (
            operation.status != OperationStatus.RUNNING.value
            or operation.effect_kind != "external_write"
            or operation.started_at is None
        ):
            return False
        if timeout_seconds <= 0:
            raise ValueError("operation timeout 必须大于零。")
        cutoff = self._database_now() - timedelta(seconds=timeout_seconds)
        if operation.started_at > cutoff:
            return False
        self.mark_operation_unknown(
            operation,
            error={"code": "WORKER_RESULT_MISSING_AFTER_TIMEOUT"},
        )
        return True

    def reconcile_operation(
        self,
        instance: SopInstance,
        operation: SopOperation,
        *,
        succeeded: bool,
        result: Mapping[str, object] | None = None,
        error: Mapping[str, object] | None = None,
        effect_confirmed: bool,
    ) -> bool:
        """以远端证据收敛 unknown 操作，并在取消请求已无悬而未决动作时终结实例。"""

        if operation.instance_id != instance.id or operation.tenant_id != instance.tenant_id:
            raise SopExecutionConflictError("待对账操作不属于指定执行实例。")
        if operation.status != OperationStatus.UNKNOWN.value:
            raise SopExecutionConflictError("只有 unknown 操作可以进入对账。")
        if succeeded != effect_confirmed:
            raise SopExecutionConflictError("对账生命周期结果必须与外部效果证据一致。")
        self._guard_mutation(instance, "operation.reconcile")
        transition = transition_operation(
            OperationStatus(operation.status),
            OperationStatus.SUCCEEDED if succeeded else OperationStatus.FAILED,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.result_json = dict(result or {})
        operation.error_json = dict(error or {})
        operation.effect_state = "complete" if effect_confirmed else "none"
        operation.cancellation_disposition = "reconciled"
        operation.reconciled_at = utc_now()
        operation.completed_at = operation.reconciled_at
        attempt = self._latest_operation_attempt(operation)
        if attempt is not None and attempt.status == OperationStatus.UNKNOWN.value:
            attempt.status = "succeeded" if succeeded else "failed"
            attempt.error_json = dict(error or {})
            attempt.completed_at = operation.reconciled_at
            attempt.updated_at = operation.reconciled_at
            self.db.add(attempt)
        self._append_operation_effect(
            operation,
            event_type="reconciled_applied" if effect_confirmed else "reconciled_not_applied",
            effect_state=operation.effect_state,
            evidence=dict(result or error or {}),
        )
        self.db.add(operation)
        self.aggregate_effect_state(instance)
        DynamicTaskQuotaService(self.db).release_tool_operation(operation)
        return self._settle_requested_cancellation(instance)

    def request_cancellation(
        self,
        instance: SopInstance,
        *,
        actor_user_id: str,
        reason: str,
    ) -> bool:
        """登记取消请求，零调用动作直接取消，已发外部写转入 unknown 等待对账。"""

        self._guard_mutation(instance, "instance.request_cancellation")
        now = utc_now()
        instance.cancellation_requested_at = now
        instance.cancellation_requested_by = actor_user_id
        instance.cancellation_reason = reason
        instance.cancellation_disposition = "requested"
        operations = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperation.status.in_(("prepared", "running", "unknown")),
            )
        ).all()
        for operation in operations:
            if operation.status == OperationStatus.PREPARED.value:
                self.cancel_prepared_operation(operation)
            elif operation.status == OperationStatus.RUNNING.value:
                if operation.effect_kind == "external_write":
                    self.mark_operation_unknown(
                        operation,
                        error={"code": "CANCELLED_WHILE_IN_FLIGHT"},
                    )
                else:
                    self._cancel_running_read(operation)
        active_attentions = self.db.exec(
            select(SopWorkItem).where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.status.in_(("offered", "claimed")),
            )
        ).all()
        for attention in active_attentions:
            attention.status = "cancelled"
            attention.assignee_user_id = None
            attention.resolution_json = {
                "command": "cancel_execution",
                "actor_user_id": actor_user_id,
                "reason": reason,
            }
            attention.revision += 1
            attention.updated_at = now
            self.db.add(attention)
        active_signals = self.db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.tenant_id == instance.tenant_id,
                ExecutionSignal.execution_id == instance.id,
                ExecutionSignal.status.in_(("pending", "claimed")),
            )
        ).all()
        for signal in active_signals:
            signal.status = "discarded"
            signal.lease_owner = None
            signal.lease_expires_at = None
            signal.consumed_at = now
            signal.updated_at = now
            self.db.add(signal)
        pending_commands = self.db.exec(
            select(ExecutionCommand).where(
                ExecutionCommand.tenant_id == instance.tenant_id,
                ExecutionCommand.execution_id == instance.id,
                ExecutionCommand.status.in_(("pending", "claimed")),
            )
        ).all()
        for pending_command in pending_commands:
            pending_command.status = "rejected"
            pending_command.reason_code = "EXECUTION_CANCELLED"
            pending_command.result_json = {"execution_status": "cancelling"}
            pending_command.consumed_at = now
            pending_command.updated_at = now
            self.db.add(pending_command)
        instance.cancellation_disposition = "awaiting_reconciliation"
        self.db.add(instance)
        self.aggregate_effect_state(instance)
        return self._settle_requested_cancellation(instance)

    def aggregate_effect_state(self, instance: SopInstance) -> str:
        """聚合外部写效果事实；unknown 优先，其次区分部分完成、全部完成与无效果。"""

        operations = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperation.effect_kind == "external_write",
            )
        ).all()
        states = [operation.effect_state for operation in operations]
        if "unknown" in states:
            aggregate = "unknown"
        else:
            completed = sum(state in {"complete", "compensated"} for state in states)
            if completed == 0:
                aggregate = "none"
            elif completed == len(states):
                aggregate = "complete"
            else:
                aggregate = "partial"
        instance.effect_state = aggregate
        self.db.add(instance)
        return aggregate

    def complete_instance(
        self,
        instance: SopInstance,
        *,
        slots: Mapping[str, object] | None = None,
    ) -> None:
        """成功结束 SOP 实例并保留最终槽位快照和结束时间。"""

        self._guard_mutation(instance, "instance.complete")
        from app.sop_runtime.execution_control import ExecutionControlService

        control = ExecutionControlService(self.db, self)
        control.ensure_terminal_result(
            instance,
            target_status="succeeded",
            result={"status": "succeeded", "slots": dict(slots or instance.slots_json or {})},
            verification={"passed": True, "source": "formal_sop_runtime"},
        )
        control.assert_terminal_closure(instance, "succeeded")
        self._guard_mutation(instance, "instance.complete")
        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.SUCCEEDED,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.active_slot_key = None
        if slots is not None:
            instance.slots_json = dict(slots)
        instance.completed_at = utc_now()
        self.db.add(instance)
        if instance.kind == "dynamic_task":
            DynamicTaskQuotaService(self.db).release_execution(instance)
        self.db.flush()

    def fail_instance(
        self,
        instance: SopInstance,
        *,
        context_patch: Mapping[str, object] | None = None,
    ) -> None:
        """失败结束 SOP 实例，并把确定性失败上下文合并到实例快照。"""

        self._guard_mutation(instance, "instance.fail")
        from app.sop_runtime.execution_control import ExecutionControlService

        control = ExecutionControlService(self.db, self)
        control.ensure_terminal_result(
            instance,
            target_status="failed",
            result={"status": "failed", "context": dict(context_patch or {})},
            verification={"passed": True, "source": "runtime_failure"},
        )
        control.assert_terminal_closure(instance, "failed")
        self._guard_mutation(instance, "instance.fail")
        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.FAILED,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.active_slot_key = None
        if context_patch:
            instance.context_json = {
                **(instance.context_json or {}),
                **dict(context_patch),
            }
        instance.completed_at = utc_now()
        self.db.add(instance)
        if instance.kind == "dynamic_task":
            DynamicTaskQuotaService(self.db).release_execution(instance)
        self.db.flush()

    def timeout_instance(
        self,
        instance: SopInstance,
        *,
        context_patch: Mapping[str, object] | None = None,
    ) -> None:
        """把活动 SOP 实例推进为 timed_out，并冻结超时上下文。"""

        self._guard_mutation(instance, "instance.timeout")
        from app.sop_runtime.execution_control import ExecutionControlService

        control = ExecutionControlService(self.db, self)
        control.ensure_terminal_result(
            instance,
            target_status="timed_out",
            result={"status": "timed_out", "context": dict(context_patch or {})},
            verification={"passed": True, "source": "runtime_timeout"},
        )
        control.assert_terminal_closure(instance, "timed_out")
        self._guard_mutation(instance, "instance.timeout")
        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.TIMED_OUT,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.active_slot_key = None
        if context_patch:
            instance.context_json = {
                **(instance.context_json or {}),
                **dict(context_patch),
            }
        instance.completed_at = utc_now()
        self.db.add(instance)
        if instance.kind == "dynamic_task":
            DynamicTaskQuotaService(self.db).release_execution(instance)
        self.db.flush()

    @staticmethod
    def operation_idempotency_key(
        *,
        tenant_id: str,
        instance_id: str,
        logical_action_id: str,
        operation_name: str,
    ) -> str:
        """计算本地逻辑动作键，使节点 attempt 变化不改变命令身份。"""

        payload = {
            "tenant_id": tenant_id,
            "instance_id": instance_id,
            "logical_action_id": logical_action_id,
            "operation_name": operation_name,
        }
        return SopExecutionStore._hash_json(payload)

    @staticmethod
    def request_fingerprint(request: Mapping[str, object]) -> str:
        """按严格 RFC 8259 JSON 语义生成请求指纹，拒绝隐式字符串化和非有限数。"""

        return SopExecutionStore._hash_json(dict(request))

    @staticmethod
    def remote_idempotency_key(
        *,
        tenant_id: str,
        instance_id: str,
        logical_action_id: str,
        operation_name: str,
        request: Mapping[str, object],
        policy: IdempotencyPolicy,
    ) -> str | None:
        """按实例、业务或租户契约生成可发送给远端系统的稳定幂等键。"""

        fingerprint = SopExecutionStore.request_fingerprint(request)
        if not policy.required:
            return None
        if policy.scope is IdempotencyScope.BUSINESS:
            missing = [field for field in policy.key_fields if field not in request]
            if missing:
                raise ValueError("业务幂等字段缺失: " + ",".join(missing))
            identity: Mapping[str, object] = {
                "tenant_id": tenant_id,
                "operation_name": operation_name,
                "business_key": {field: request[field] for field in policy.key_fields},
            }
        elif policy.scope is IdempotencyScope.TENANT:
            identity = {
                "tenant_id": tenant_id,
                "operation_name": operation_name,
                "request_fingerprint": fingerprint,
            }
        else:
            identity = {
                "tenant_id": tenant_id,
                "instance_id": instance_id,
                "logical_action_id": logical_action_id,
                "operation_name": operation_name,
            }
        return SopExecutionStore._hash_json(identity)

    @staticmethod
    def _hash_json(value: object) -> str:
        """严格规范化 JSON 并返回 SHA-256；类型或数值越界时统一给出明确错误。"""

        SopExecutionStore._validate_json_value(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("请求必须是严格 JSON，且数字必须为有限值。") from error
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_json_value(value: object) -> None:
        """递归拒绝 JSON 数据模型之外的键、容器、对象和非有限浮点数。"""

        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if math.isfinite(value):
                return
            raise ValueError("请求必须是严格 JSON，且数字必须为有限值。")
        if isinstance(value, list):
            for item in value:
                SopExecutionStore._validate_json_value(item)
            return
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("请求必须是严格 JSON，且对象键必须是字符串。")
            for item in value.values():
                SopExecutionStore._validate_json_value(item)
            return
        raise ValueError("请求必须是严格 JSON，禁止隐式字符串化对象。")

    @staticmethod
    def _default_logical_action_id(
        *,
        tenant_id: str,
        instance_id: str,
        node_id: str,
        operation_name: str,
    ) -> str:
        """从稳定节点身份派生默认逻辑动作，不纳入会随重试变化的 execution id。"""

        digest = SopExecutionStore._hash_json(
            {
                "tenant_id": tenant_id,
                "instance_id": instance_id,
                "node_id": node_id,
                "operation_name": operation_name,
            }
        )
        return f"action:{digest}"

    def _ensure_operation_attempt(
        self,
        operation: SopOperation,
        execution: SopNodeExecution,
    ) -> SopOperationAttempt:
        """为新的节点执行追加一次 dispatch attempt，同一 execution 重入时保持幂等。"""

        existing = self.db.exec(
            select(SopOperationAttempt).where(
                SopOperationAttempt.tenant_id == operation.tenant_id,
                SopOperationAttempt.operation_id == operation.id,
                SopOperationAttempt.node_execution_id == execution.id,
            )
        ).first()
        if existing is not None:
            return existing
        latest = self.db.exec(
            select(func.max(SopOperationAttempt.attempt_number)).where(
                SopOperationAttempt.tenant_id == operation.tenant_id,
                SopOperationAttempt.operation_id == operation.id,
            )
        ).one()
        status = (
            OperationStatus.PREPARED.value
            if operation.status == OperationStatus.PREPARED.value
            else "reused"
        )
        attempt = SopOperationAttempt(
            tenant_id=operation.tenant_id,
            instance_id=operation.instance_id,
            operation_id=operation.id,
            node_execution_id=execution.id,
            attempt_number=int(latest or 0) + 1,
            status=status,
            completed_at=utc_now() if status == "reused" else None,
        )
        self.db.add(attempt)
        self.db.flush()
        return attempt

    def _latest_operation_attempt(self, operation: SopOperation) -> SopOperationAttempt | None:
        """返回逻辑动作最近一次本地 attempt，供状态推进保持历史一致。"""

        return self.db.exec(
            select(SopOperationAttempt)
            .where(
                SopOperationAttempt.tenant_id == operation.tenant_id,
                SopOperationAttempt.operation_id == operation.id,
            )
            .order_by(SopOperationAttempt.attempt_number.desc())
        ).first()

    def _append_operation_effect(
        self,
        operation: SopOperation,
        *,
        event_type: str,
        effect_state: str,
        evidence: Mapping[str, object],
        compensation_operation_id: str | None = None,
    ) -> SopOperationEffect:
        """追加不可覆盖的效果事实，为超时对账和补偿 lineage 保留证据。"""

        latest = self.db.exec(
            select(func.max(SopOperationEffect.sequence)).where(
                SopOperationEffect.tenant_id == operation.tenant_id,
                SopOperationEffect.operation_id == operation.id,
            )
        ).one()
        effect = SopOperationEffect(
            tenant_id=operation.tenant_id,
            instance_id=operation.instance_id,
            operation_id=operation.id,
            logical_action_id=operation.logical_action_id,
            sequence=int(latest or 0) + 1,
            event_type=event_type,
            effect_state=effect_state,
            external_reference=operation.external_reference,
            evidence_json=dict(evidence),
            compensation_operation_id=compensation_operation_id,
        )
        self.db.add(effect)
        return effect

    def _operation_instance(self, operation: SopOperation) -> SopInstance:
        """解析操作父实例并验证租户，供效果聚合避免跨租户污染。"""

        instance = self.db.get(SopInstance, operation.instance_id)
        if instance is None or instance.tenant_id != operation.tenant_id:
            raise SopExecutionConflictError("工具操作所属执行实例不存在或租户不匹配。")
        return instance

    def _validate_compensation_target(
        self,
        instance: SopInstance,
        *,
        compensates_operation_id: str,
    ) -> SopOperation:
        """确保补偿只指向同一执行内已确认生效且尚未补偿的外部动作。"""

        target = self.db.get(SopOperation, compensates_operation_id)
        if (
            target is None
            or target.tenant_id != instance.tenant_id
            or target.instance_id != instance.id
        ):
            raise SopExecutionConflictError("补偿目标不属于当前执行实例。")
        if target.effect_kind != "external_write" or target.effect_state != "complete":
            raise SopExecutionConflictError("补偿目标必须是已确认生效的外部写。")
        return target

    def _mark_compensation_applied(self, compensation: SopOperation) -> None:
        """补偿命令成功后标记原效果已补偿，同时保留原操作和补偿操作两条历史。"""

        target_id = compensation.compensates_operation_id
        if target_id is None:
            return
        target = self.db.get(SopOperation, target_id)
        if target is None or target.tenant_id != compensation.tenant_id:
            raise SopExecutionConflictError("补偿完成时找不到同租户原操作。")
        target.effect_state = "compensated"
        target.updated_at = utc_now()
        self._append_operation_effect(
            target,
            event_type="compensated",
            effect_state="compensated",
            evidence={"compensation_operation_id": compensation.id},
            compensation_operation_id=compensation.id,
        )
        self.db.add(target)

    def _cancel_running_read(self, operation: SopOperation) -> None:
        """取消无外部写效果的运行中读取，并终结其当前 attempt。"""

        self._guard_operation_mutation(operation, "operation.cancel_running_read")
        transition = transition_operation(
            OperationStatus(operation.status),
            OperationStatus.CANCELLED,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.cancellation_disposition = "cancelled_no_effect"
        operation.completed_at = utc_now()
        attempt = self._latest_operation_attempt(operation)
        if attempt is not None and attempt.status == OperationStatus.RUNNING.value:
            attempt.status = OperationStatus.CANCELLED.value
            attempt.completed_at = operation.completed_at
            attempt.updated_at = operation.completed_at
            self.db.add(attempt)
        self.db.add(operation)
        DynamicTaskQuotaService(self.db).release_tool_operation(operation)

    def _settle_requested_cancellation(self, instance: SopInstance) -> bool:
        """仅在没有 running/unknown 动作时把已请求取消的实例推进到终态。"""

        if instance.cancellation_requested_at is None:
            return False
        unresolved = self.db.exec(
            select(SopOperation.id).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperation.status.in_(("running", "unknown")),
            )
        ).first()
        if unresolved is not None:
            instance.cancellation_disposition = "awaiting_reconciliation"
            self.db.add(instance)
            self.db.flush()
            return False
        if instance.status not in ACTIVE_INSTANCE_STATUSES:
            return instance.status == SopInstanceStatus.CANCELLED.value
        from app.sop_runtime.execution_control import ExecutionControlService

        control = ExecutionControlService(self.db, self)
        control.ensure_terminal_result(
            instance,
            target_status="cancelled",
            result={
                "status": "cancelled",
                "reason": instance.cancellation_reason,
                "effect_state": instance.effect_state,
            },
            verification={"passed": True, "source": "runtime_cancellation"},
        )
        control.assert_terminal_closure(instance, "cancelled")
        self._guard_mutation(instance, "instance.cancel")
        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.CANCELLED,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        instance.active_slot_key = None
        instance.cancellation_disposition = "cancelled"
        instance.completed_at = utc_now()
        self.db.add(instance)
        if instance.kind == "dynamic_task":
            DynamicTaskQuotaService(self.db).release_execution(instance)
        self.db.flush()
        return True

    def _active_instance(self, tenant_id: str, session_id: str) -> SopInstance | None:
        """在租户和会话范围内查找唯一活动实例。"""

        return self.db.exec(
            select(SopInstance).where(
                SopInstance.tenant_id == tenant_id,
                SopInstance.session_id == session_id,
                SopInstance.status.in_(ACTIVE_INSTANCE_STATUSES),
            )
        ).first()

    def _next_run_number(self, tenant_id: str, session_id: str, skill_version_id: str) -> int:
        """计算同一会话重复执行同一不可变版本时的下一运行序号。"""

        latest = self.db.exec(
            select(func.max(SopInstance.run_number)).where(
                SopInstance.tenant_id == tenant_id,
                SopInstance.session_id == session_id,
                SopInstance.skill_version_id == skill_version_id,
            )
        ).one()
        return int(latest or 0) + 1

    def _next_execution_run_number(self, tenant_id: str, session_id: str) -> int:
        """计算统一会话中下一 Execution 序号，动态任务不依赖虚假的 SkillVersion。"""

        latest = self.db.exec(
            select(func.max(SopInstance.run_number)).where(
                SopInstance.tenant_id == tenant_id,
                SopInstance.session_id == session_id,
            )
        ).one()
        return int(latest or 0) + 1

    def _next_node_attempt(self, tenant_id: str, instance_id: str, step_key: str) -> int:
        """按 execution 内稳定 step key 计算新 attempt，历史执行记录不会被覆盖。"""

        latest = self.db.exec(
            select(func.max(SopNodeExecution.attempt)).where(
                SopNodeExecution.tenant_id == tenant_id,
                SopNodeExecution.instance_id == instance_id,
                SopNodeExecution.step_key == step_key,
            )
        ).one()
        return int(latest or 0) + 1

    def _assert_dynamic_instance(self, instance: SopInstance) -> None:
        """拒绝把动态计划或提案写入正式 SOP 或其他租户的 Execution。"""

        self._assert_instance_tenant(instance)
        if instance.kind != "dynamic_task" or not instance.current_plan_revision_id:
            raise SopExecutionConflictError("该写入仅适用于已绑定活动计划的动态 Execution。")

    def _assert_step_declared(self, instance: SopInstance, step_key: str) -> None:
        """确认动态步骤由当前活动计划声明，而不是模型临时伪造数据库身份。"""

        revision = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
        if revision is None or revision.execution_id != instance.id:
            raise SopExecutionConflictError("动态 Execution 当前计划不存在或归属错误。")
        steps = revision.plan_json.get("steps") if isinstance(revision.plan_json, dict) else None
        if not isinstance(steps, list) or not any(
            isinstance(step, dict) and step.get("step_key") == step_key for step in steps
        ):
            raise SopExecutionConflictError("动态步骤未由当前活动计划声明。")

    def _assert_proposal_declared_by_step(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        response: CompletedProviderProposal,
    ) -> None:
        """确认模型提案使用的能力已由当前计划步骤冻结，不能临时扩大目录。"""

        revision = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
        steps = revision.plan_json.get("steps") if revision and isinstance(revision.plan_json, dict) else []
        step = next(
            (
                item
                for item in steps
                if isinstance(item, dict) and item.get("step_key") == execution.step_key
            ),
            None,
        )
        if step is None:
            raise SopExecutionConflictError("动作提案步骤未由当前计划声明。")
        capability_ref = response.proposal.capability_ref
        declared = step.get("capability_refs")
        if capability_ref is not None and (
            not isinstance(declared, list) or capability_ref not in declared
        ):
            raise SopExecutionConflictError("动作提案能力未由当前计划步骤冻结。")

    @staticmethod
    def _assert_plan_capabilities_available(
        plan: NormalizedPlan,
        capability_snapshot: Mapping[str, object],
    ) -> None:
        """拒绝计划引用不在冻结目录中的 Tool/GeneralSkill/Knowledge 能力。"""

        available: set[str] = set()
        for value in capability_snapshot.values():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                for key in ("name", "slug", "id", "capability_id"):
                    identity = item.get(key)
                    if isinstance(identity, str) and identity.strip():
                        available.add(identity)
        missing = sorted(
            {
                capability_ref
                for step in plan.steps
                for capability_ref in step.capability_refs
                if capability_ref not in available
            }
        )
        if missing:
            raise SopExecutionConflictError(
                "计划引用了未冻结的动态能力：" + ",".join(missing)
            )

    @staticmethod
    def _message_references_resource(
        message: Message,
        resource: ManagedInputResource,
    ) -> bool:
        """核对用户消息 metadata 中由上传 API 生成的资源身份、版本和内容摘要。"""

        attachments = message.metadata_json.get("attachments")
        if not isinstance(attachments, list):
            return False
        return any(
            isinstance(item, dict)
            and item.get("resource_id") == resource.id
            and item.get("resource_version") == resource.version
            and item.get("content_checksum") == resource.content_checksum
            for item in attachments
        )

    def _assert_plan_preserves_step_identity(
        self,
        instance: SopInstance,
        next_plan: Mapping[str, object],
    ) -> None:
        """禁止任何 PlanRevision 复用历史 step key 表达不同语义或移除已完成步骤。"""

        raw_steps = next_plan.get("steps")
        if not isinstance(raw_steps, list):
            raise SopExecutionConflictError("计划 steps 必须是完整数组。")
        next_steps = {
            str(step.get("step_key")): step
            for step in raw_steps
            if isinstance(step, dict) and step.get("step_key")
        }
        historical = self.db.exec(
            select(ExecutionPlanRevision)
            .where(
                ExecutionPlanRevision.tenant_id == instance.tenant_id,
                ExecutionPlanRevision.execution_id == instance.id,
            )
            .order_by(ExecutionPlanRevision.revision_number)
        ).all()
        historical_steps: dict[str, dict[str, object]] = {}
        for revision in historical:
            steps = revision.plan_json.get("steps") if isinstance(revision.plan_json, dict) else []
            if not isinstance(steps, list):
                raise SopExecutionConflictError("历史计划 steps 已损坏。")
            for step in steps:
                if not isinstance(step, dict) or not step.get("step_key"):
                    raise SopExecutionConflictError("历史计划包含非法 step identity。")
                key = str(step["step_key"])
                previous = historical_steps.get(key)
                if previous is not None and previous != step:
                    raise SopExecutionConflictError("历史计划已存在 step key 语义漂移。")
                historical_steps[key] = step
        for key, step in next_steps.items():
            previous = historical_steps.get(key)
            if previous is not None and previous != step:
                raise SopExecutionConflictError("修改步骤必须分配新的 step key。")
        completed = self.db.exec(
            select(SopNodeExecution).where(
                SopNodeExecution.tenant_id == instance.tenant_id,
                SopNodeExecution.instance_id == instance.id,
                SopNodeExecution.status == NodeExecutionStatus.SUCCEEDED.value,
            )
        ).all()
        for execution in completed:
            if execution.step_key not in next_steps:
                raise SopExecutionConflictError("PlanRevision 不得移除已完成步骤。")
            if historical_steps.get(execution.step_key) != next_steps[execution.step_key]:
                raise SopExecutionConflictError("PlanRevision 不得改写已完成步骤。")

    def _guard_operation_mutation(self, operation: SopOperation, action: str) -> None:
        """解析 Operation 的父实例并应用同一 execution mutation guard。"""

        with self.db.no_autoflush:
            instance = self.db.get(SopInstance, operation.instance_id)
        if instance is None or instance.tenant_id != operation.tenant_id:
            raise SopExecutionConflictError("工具操作所属 SOP 实例不存在或租户不匹配。")
        self._guard_mutation(instance, action)

    def _guard_mutation(self, instance: SopInstance, action: str) -> None:
        """以 revision、未过期 lease 和 fencing token 的单条 CAS 授权一次权威写。"""

        lease = self._lease
        if (
            lease is None
            or lease.instance_id != instance.id
            or lease.tenant_id != instance.tenant_id
        ):
            raise SopExecutionConflictError("权威执行写入必须位于 execution lease 作用域内。")
        database_now = self._database_now()
        expected_revision = instance.revision
        with self.db.no_autoflush:
            result = self.db.exec(
                update(SopInstance)
                .where(
                    *self._lease_predicates(lease, database_now, require_unexpired=True),
                    SopInstance.revision == expected_revision,
                )
                .values(
                    revision=SopInstance.revision + 1,
                    updated_at=database_now,
                )
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            self._persist_fencing_rejection(lease, action)
            raise self._fenced_error(lease, action)
        set_committed_value(instance, "revision", expected_revision + 1)
        set_committed_value(instance, "updated_at", database_now)

    def _database_now(self) -> datetime:
        """读取数据库当前时间，禁止以 worker 本地时钟裁决跨进程所有权。"""

        with self.db.no_autoflush:
            value = self.db.exec(select(func.current_timestamp())).one()
        if not isinstance(value, datetime):
            raise SopExecutionConflictError("数据库未返回可用的权威时间。")
        return value

    def _persist_fencing_rejection(self, lease: ExecutionLease, action: str) -> None:
        """回滚旧 worker 的全部待写后，独立提交不含业务载荷的拒绝证据。"""

        with self.db.no_autoflush:
            current = self.db.exec(
                select(SopInstance.lease_owner, SopInstance.fencing_token).where(
                    SopInstance.id == lease.instance_id,
                    SopInstance.tenant_id == lease.tenant_id,
                )
            ).first()
            current_owner = current[0] if current is not None else None
            current_token = int(current[1]) if current is not None else 0
        self.db.rollback()
        rejection = ExecutionMutationRejection(
            tenant_id=lease.tenant_id,
            instance_id=lease.instance_id,
            worker_id=lease.worker_id,
            rejected_fencing_token=lease.fencing_token,
            current_lease_owner=current_owner,
            current_fencing_token=current_token,
            action=action,
        )
        self.db.add(rejection)
        self.db.commit()

    @staticmethod
    def _lease_predicates(
        lease: ExecutionLease,
        database_now: datetime,
        *,
        require_unexpired: bool,
    ) -> tuple[object, ...]:
        """构造所有权 CAS 的 tenant、execution、owner、token 和可选过期条件。"""

        predicates: list[object] = [
            SopInstance.id == lease.instance_id,
            SopInstance.tenant_id == lease.tenant_id,
            SopInstance.lease_owner == lease.worker_id,
            SopInstance.fencing_token == lease.fencing_token,
        ]
        if require_unexpired:
            predicates.append(SopInstance.lease_expires_at > database_now)
        return tuple(predicates)

    @staticmethod
    def _fenced_error(lease: ExecutionLease, action: str) -> SopExecutionFencedError:
        """创建仅携带执行身份、不携带业务输入输出的 fencing 拒绝。"""

        return SopExecutionFencedError(
            "execution lease 已过期或 fencing token 已失效，拒绝迟到写入。",
            tenant_id=lease.tenant_id,
            instance_id=lease.instance_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            action=action,
        )

    @staticmethod
    def _apply_instance_transition(instance: SopInstance, transition: TransitionResult) -> None:
        """把已校验实例迁移结果写回 ORM 聚合。"""

        instance.status = str(transition.status.value)
        instance.revision = transition.revision
        instance.updated_at = utc_now()

    @staticmethod
    def _apply_node_transition(execution: SopNodeExecution, transition: TransitionResult) -> None:
        """把已校验节点迁移结果写回 ORM 聚合。"""

        execution.status = str(transition.status.value)
        execution.revision = transition.revision
        execution.updated_at = utc_now()

    @staticmethod
    def _apply_operation_transition(operation: SopOperation, transition: TransitionResult) -> None:
        """把已校验操作迁移结果写回 ORM 聚合。"""

        operation.status = str(transition.status.value)
        operation.revision = transition.revision
        operation.updated_at = utc_now()

    @staticmethod
    def _assert_execution_owner(instance: SopInstance, execution: SopNodeExecution) -> None:
        """拒绝跨租户或跨实例组合节点执行记录。"""

        if instance.tenant_id != execution.tenant_id or instance.id != execution.instance_id:
            raise SopExecutionConflictError("节点执行记录不属于指定 SOP 实例。")

    @staticmethod
    def _assert_instance_tenant(instance: SopInstance) -> None:
        """确保实例持有非空租户边界，防止无租户执行进入持久化。"""

        if not instance.tenant_id:
            raise SopExecutionConflictError("SOP 实例缺少租户标识。")
