"""
@Time       : 2026/08/04 01:45
@Author     : zhanglp8181
@File       : agent.py
@CallChain  : Agent Loop/signal worker → DynamicTaskAgent → Execution Store/ToolExecutor
@Description: 以统一 Execution 账本串行推进只读动态动作，并支持崩溃后的安全恢复。
"""

from __future__ import annotations

from typing import Protocol

from sqlmodel import Session, select

from app.db.models import SopInstance, SopNodeExecution, SopOperation
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, DynamicCapabilityCatalog
from app.dynamic_tasks.planning import CompletedProviderProposal
from app.dynamic_tasks.provider_view import require_dynamic_preflight
from app.sop_runtime.execution_store import SopExecutionStore
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


class DynamicTaskAgentError(RuntimeError):
    """表示只读动态推进在 provider、能力或状态边界被确定性拒绝。"""


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
    ) -> None:
        """绑定统一事务、能力目录和既有工具执行器，禁止创建第二套 Runtime。"""

        self.db = db
        self.store = SopExecutionStore(db)
        self.catalog = catalog or DynamicCapabilityCatalog(db)
        self.tool_executor = tool_executor or ToolExecutor(db)

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

    def _step_definition(self, instance: SopInstance, step_key: str) -> dict[str, object]:
        """从活动 PlanRevision 读取服务端稳定步骤，不接受调用方临时定义。"""

        from app.db.models import ExecutionPlanRevision

        revision = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
        steps = revision.plan_json.get("steps") if revision is not None else None
        if not isinstance(steps, list):
            raise DynamicTaskAgentError("DYNAMIC_PLAN_INVALID")
        for step in steps:
            if isinstance(step, dict) and step.get("step_key") == step_key:
                return dict(step)
        raise DynamicTaskAgentError("DYNAMIC_STEP_NOT_DECLARED")

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
