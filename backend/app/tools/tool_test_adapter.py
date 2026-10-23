"""
@Time       : 2026/09/28 18:10
@Author     : zhanglp8181
@File       : tool_test_adapter.py
@CallChain  : Tools API/工具测试消费者 → ToolTestExecutionAdapter → Operation/ExecutionControl/ToolExecutor
@Description: 将管理端单次工具测试接入统一账本，隔离聊天槽位并禁止模型或追加动作。
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.branching import get_agent, get_overall_agent
from app.db.models import (
    ExecutionSignal,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    Tool,
)
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.sop_runtime.contracts import OperationStatus
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


class ToolTestExecutionAdapter:
    """为工具管理页提供无模型、单动作、可恢复的测试执行适配器。"""

    def __init__(self, db: Session, *, executor: ToolExecutor | None = None) -> None:
        """绑定账本会话和可注入的工具执行器，生产默认使用统一 ToolExecutor。"""

        self.db = db
        self.store = SopExecutionStore(db)
        self.executor = executor or ToolExecutor(db)

    def execute(
        self,
        *,
        tenant_id: str,
        tool: Tool,
        arguments: Mapping[str, object],
        actor_user_id: str,
        agent_id: str | None,
        submission_id: str | None = None,
        active_skill_id: str | None = None,
        execution_org_unit_id: str | None = None,
    ) -> ToolResult:
        """提交或读取一条工具测试动作；同一提交身份永远不重新执行。"""

        resolved_agent_id = self._resolve_agent_id(tenant_id, agent_id)
        if resolved_agent_id is None:
            return self._error(
                tool.name,
                "TOOL_TEST_AGENT_REQUIRED",
                "工具测试必须绑定当前租户的活动数字员工。",
            )
        logical_action_id, session_id, active_slot_key = self._logical_identity(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            agent_id=resolved_agent_id,
            tool=tool,
            arguments=arguments,
            submission_id=submission_id,
        )
        fingerprint = self.store.request_fingerprint(arguments)
        existing = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == tenant_id,
                SopOperation.logical_action_id == logical_action_id,
            )
        ).first()
        if existing is not None:
            if existing.request_fingerprint != fingerprint or existing.operation_name != tool.name:
                return self._error(
                    tool.name,
                    "TOOL_TEST_IDENTITY_CONFLICT",
                    "同一工具测试提交身份不能修改动作参数或工具。",
                )
            return self._result_from_operation(existing, tool.name)

        try:
            instance, execution, operation = self._start_execution(
                tenant_id=tenant_id,
                session_id=session_id,
                active_slot_key=active_slot_key,
                agent_id=resolved_agent_id,
                actor_user_id=actor_user_id,
                tool=tool,
                arguments=arguments,
                logical_action_id=logical_action_id,
            )
        except IntegrityError:
            # 双击并发时两个请求可能同时通过上面的读取；唯一活动槽位是
            # 数据库最终仲裁点，冲突后重新读取同一业务身份而不是返回 500。
            self.db.rollback()
            existing = self.db.exec(
                select(SopOperation).where(
                    SopOperation.tenant_id == tenant_id,
                    SopOperation.logical_action_id == logical_action_id,
                )
            ).first()
            if existing is None:
                return self._error(
                    tool.name,
                    "TOOL_TEST_CONCURRENCY_CONFLICT",
                    "工具测试提交正在并发建立，请重试同一提交。",
                )
            if existing.request_fingerprint != fingerprint or existing.operation_name != tool.name:
                return self._error(
                    tool.name,
                    "TOOL_TEST_IDENTITY_CONFLICT",
                    "同一工具测试提交身份不能修改动作参数或工具。",
                )
            return self._result_from_operation(existing, tool.name)
        self.db.commit()
        try:
            result = self.executor.execute(
                tenant_id,
                ToolCall(name=tool.name, arguments=dict(arguments)),
                active_skill_id=active_skill_id,
                agent_id=resolved_agent_id,
                actor_user_id=actor_user_id,
                execution_org_unit_id=execution_org_unit_id,
                remote_idempotency_key=operation.remote_idempotency_key,
                execution_id=instance.id,
            )
        except Exception as exc:  # pragma: no cover - ToolExecutor 已规范化常见异常
            result = self._error(tool.name, "TOOL_TEST_EXECUTION_ERROR", str(exc))
        return self._settle_result(
            instance=instance,
            execution=execution,
            operation=operation,
            tool=tool,
            result=result,
        )

    def resume_external_task_signal(
        self,
        *,
        signal_id: str,
        worker_id: str,
    ) -> object:
        """无模型消费工具测试的外部回执，完成原节点并发布已核验结果。"""

        from app.dynamic_tasks.agent import DynamicRunOutcome

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal is not None else None
        if (
            signal is None
            or instance is None
            or instance.kind != "dynamic_task"
            or instance.source_kind != "tool_test"
            or signal.signal_type != "external_event"
            or signal.payload_json.get("kind") != "external_task"
        ):
            raise SopExecutionConflictError("工具测试外部回执 Signal 身份无效。")
        control = ExecutionControlService(self.db, self.store)
        if signal.status != "claimed":
            control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
            self.db.commit()
        operation = self.db.get(
            SopOperation,
            str(signal.payload_json.get("operation_id") or ""),
        )
        execution = self.db.get(
            SopNodeExecution,
            str(signal.payload_json.get("node_execution_id") or ""),
        )
        if (
            operation is None
            or execution is None
            or operation.tenant_id != instance.tenant_id
            or operation.instance_id != instance.id
            or operation.node_execution_id != execution.id
        ):
            raise SopExecutionConflictError("工具测试外部回执未绑定原 Operation/节点。")
        if operation.status == OperationStatus.UNKNOWN.value:
            with self.store.owned(instance, worker_id=worker_id):
                control.consume_signal(instance, signal, worker_id=worker_id)
            self.db.commit()
            return DynamicRunOutcome("unknown", instance.id, blocking_step_key=execution.step_key)
        if operation.status not in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.FAILED.value,
        }:
            raise SopExecutionConflictError("工具测试外部回执尚未形成终态 Operation。")
        with self.store.owned(instance, worker_id=worker_id):
            if execution.status == "waiting" and instance.status == "waiting":
                self.store.resume_waiting_node(
                    instance,
                    execution,
                    slots=instance.slots_json or {},
                )
            if operation.status == OperationStatus.SUCCEEDED.value:
                data = self._operation_data(operation)
                verification_error = self._verify_output(data, self._output_schema(operation))
                if verification_error is not None:
                    control.consume_signal(instance, signal, worker_id=worker_id)
                    self.store.fail_node(
                        instance,
                        execution,
                        error={"code": "TOOL_TEST_RESULT_INVALID", "message": verification_error},
                    )
                    instance.terminal_reason_json = {"code": "TOOL_TEST_RESULT_INVALID"}
                    self.db.add(instance)
                    self.store.fail_instance(
                        instance,
                        context_patch={"failure_code": "TOOL_TEST_RESULT_INVALID"},
                    )
                else:
                    self.store.complete_node(
                        instance,
                        execution,
                        output={"operation_id": operation.id, "data": data},
                    )
                    control.consume_signal(instance, signal, worker_id=worker_id)
                    control.freeze_result(
                        instance,
                        result={
                            "tool_name": operation.operation_name,
                            "data": data,
                        },
                        verification={"passed": True, "source": "tool_test_output_schema"},
                        created_by_step_key=execution.step_key,
                        application_publication_status="settled",
                    )
                    self.store.complete_instance(instance, slots=instance.slots_json)
            else:
                self.store.fail_node(
                    instance,
                    execution,
                    error=dict(operation.error_json or {"code": "TOOL_TEST_FAILED"}),
                )
                instance.terminal_reason_json = dict(operation.error_json or {})
                self.db.add(instance)
                control.consume_signal(instance, signal, worker_id=worker_id)
                self.store.fail_instance(instance, context_patch={"failure_code": "TOOL_TEST_FAILED"})
        self.db.commit()
        return DynamicRunOutcome(
            "succeeded" if operation.status == OperationStatus.SUCCEEDED.value else "failed",
            instance.id,
        )

    def _start_execution(
        self,
        *,
        tenant_id: str,
        session_id: str,
        active_slot_key: str,
        agent_id: str,
        actor_user_id: str,
        tool: Tool,
        arguments: Mapping[str, object],
        logical_action_id: str,
    ) -> tuple[SopInstance, SopNodeExecution, SopOperation]:
        """创建不占用聊天活动槽位的单步骤动态账本。"""

        effect_kind = self._effect_kind(tool)
        plan = NormalizedPlan(
            goal=f"工具测试：{tool.name}",
            success_criteria=(
                SuccessCriterion(
                    id="tool_result_verified",
                    type="schema",
                    spec={"tool_name": tool.name},
                ),
            ),
            steps=(
                PlanStep(
                    step_key="tool_test_action",
                    title=f"测试 {tool.name}",
                    kind="tool.read" if effect_kind == "read" else "tool.write",
                    expected_output_schema=dict(tool.output_schema or {}),
                ),
            ),
            budget={"max_steps": 1},
        )
        snapshot = {
            "tool_test": {
                "tool_id": tool.id,
                "tool_name": tool.name,
                "output_schema": dict(tool.output_schema or {}),
            },
            "tools": [],
            "contract": dict(tool.reliability_contract_json or {}),
        }
        instance, revision = self.store.start_dynamic_instance(
            tenant_id=tenant_id,
            session_id=session_id,
            agent_id=agent_id,
            initiator_user_id=actor_user_id,
            plan=plan,
            capability_snapshot=snapshot,
            source_kind="tool_test",
            source_ref=logical_action_id,
            active_slot_key=active_slot_key,
        )
        with self.store.owned(instance, worker_id=f"tool-test-start:{logical_action_id[-12:]}"):
            execution = self.store.enter_node(
                instance,
                "tool_test_action",
                step_key="tool_test_action",
                plan_revision_id=revision.id,
                step_kind="tool_test_action",
                title=f"测试 {tool.name}",
            )
            operation, created = self.store.prepare_operation(
                instance,
                execution,
                operation_name=tool.name,
                request=arguments,
                logical_action_id=logical_action_id,
                effect_kind=effect_kind,
                capability_snapshot=snapshot,
            )
            if not created:
                raise SopExecutionConflictError("工具测试 Operation 在创建过程中发生身份冲突。")
            self.store.start_operation(operation)
        return instance, execution, operation

    def _settle_result(
        self,
        *,
        instance: SopInstance,
        execution: SopNodeExecution,
        operation: SopOperation,
        tool: Tool,
        result: ToolResult,
    ) -> ToolResult:
        """在第二个短事务中写入工具结果、等待事实或核验后的 Publication。"""

        self.db.refresh(instance)
        self.db.refresh(execution)
        self.db.refresh(operation)
        if result.disposition == "accepted":
            data = dict(result.data) if isinstance(result.data, Mapping) else {}
            task_id = str(data.get("provider_task_id") or "").strip()
            contract = dict((operation.capability_snapshot_json or {}).get("contract") or {})
            status_url = str(contract.get("status_url") or "").strip()
            if not task_id or not status_url:
                return self._settle_failure(
                    instance,
                    execution,
                    operation,
                    tool.name,
                    "PROVIDER_RESPONSE_INVALID",
                    "外部任务受理回执缺少任务身份或查询地址。",
                )
            with self.store.owned(instance, worker_id=f"tool-test-accept:{operation.id}"):
                self.store.mark_operation_waiting_external(
                    operation,
                    provider_task_id=task_id,
                    provider_status=str(data.get("provider_status") or "accepted"),
                    receipt=data,
                    status_url=status_url,
                    tracking_seconds=int(contract.get("max_tracking_seconds") or 300),
                    poll_interval_seconds=int(contract.get("poll_interval_seconds") or 5),
                )
            self.db.commit()
            return self._with_identity(result.model_copy(update={"disposition": "waiting"}), operation)
        if result.disposition == "unknown":
            with self.store.owned(instance, worker_id=f"tool-test-unknown:{operation.id}"):
                self.store.mark_operation_unknown(
                    operation,
                    error={
                        "code": result.error.code if result.error else "TOOL_TEST_UNKNOWN",
                        "message": result.error.message if result.error else "工具测试效果需要对账。",
                    },
                )
            self.db.commit()
            return self._with_identity(result, operation)
        if result.success:
            verification_error = self._verify_output(result.data, tool.output_schema or {})
            if verification_error is not None:
                return self._settle_failure(
                    instance,
                    execution,
                    operation,
                    tool.name,
                    "TOOL_TEST_RESULT_INVALID",
                    verification_error,
                )
            with self.store.owned(instance, worker_id=f"tool-test-finish:{operation.id}"):
                self.store.finish_operation(
                    operation,
                    succeeded=True,
                    result={"data": result.data},
                )
                self.store.complete_node(
                    instance,
                    execution,
                    output={"operation_id": operation.id, "data": result.data},
                )
                ExecutionControlService(self.db, self.store).freeze_result(
                    instance,
                    result={"tool_name": tool.name, "data": result.data},
                    verification={"passed": True, "source": "tool_test_output_schema"},
                    created_by_step_key=execution.step_key,
                    application_publication_status="settled",
                )
                self.store.complete_instance(instance, slots=instance.slots_json)
            self.db.commit()
            return self._with_identity(result, operation)
        return self._settle_failure(
            instance,
            execution,
            operation,
            tool.name,
            result.error.code if result.error else "TOOL_TEST_FAILED",
            result.error.message if result.error else "工具测试执行失败。",
        )

    def _settle_failure(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        operation: SopOperation,
        tool_name: str,
        code: str,
        message: str,
    ) -> ToolResult:
        """以失败终态关闭单动作测试，确保失败也有可读取的 Publication。"""

        error = {"code": code, "message": message[:512]}
        with self.store.owned(instance, worker_id=f"tool-test-failed:{operation.id}"):
            if operation.status in {
                OperationStatus.RUNNING.value,
                OperationStatus.WAITING_EXTERNAL.value,
            }:
                self.store.finish_operation(operation, succeeded=False, error=error)
            self.store.fail_node(instance, execution, error=error)
            instance.terminal_reason_json = error
            self.db.add(instance)
            self.store.fail_instance(instance, context_patch={"failure_code": code})
        self.db.commit()
        return self._with_identity(
            ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(code=code, message=message),
                disposition="failed",
            ),
            operation,
        )

    def _resolve_agent_id(self, tenant_id: str, agent_id: str | None) -> str | None:
        """选择显式 Agent 或租户整体 Agent，拒绝使用用户或临时调用 ID 冒充 Agent。"""

        if agent_id:
            agent = get_agent(self.db, tenant_id, agent_id)
            return agent.id if agent is not None and agent.status == "active" else None
        overall = get_overall_agent(self.db, tenant_id)
        return overall.id if overall is not None and overall.status == "active" else None

    @staticmethod
    def _logical_identity(
        *,
        tenant_id: str,
        actor_user_id: str,
        agent_id: str,
        tool: Tool,
        arguments: Mapping[str, object],
        submission_id: str | None,
    ) -> tuple[str, str, str]:
        """按租户、用户、Agent、工具和显式提交号隔离一次工具测试。"""

        supplied_submission_id = str(submission_id or "").strip()
        if not supplied_submission_id:
            supplied_submission_id = SopExecutionStore.request_fingerprint(arguments)
        identity = {
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "agent_id": agent_id,
            "tool_id": tool.id,
            "submission_id": supplied_submission_id,
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        logical_action_id = f"tool-test:{digest}"
        return logical_action_id, f"tool-test-session:{digest}", f"tool_test:{digest}"

    @staticmethod
    def _effect_kind(tool: Tool) -> str:
        """把工具发布契约归一成测试 Operation 的读或外部写效果类别。"""

        contract = tool.reliability_contract_json or {}
        if contract.get("risk_class") == "read" or tool.method.upper() == "GET":
            return "read"
        return "external_write"

    @staticmethod
    def _result_from_operation(operation: SopOperation, tool_name: str) -> ToolResult:
        """把同身份历史 Operation 投影为工具测试响应，不触发任何外呼。"""

        status = operation.status
        if status == OperationStatus.SUCCEEDED.value:
            return ToolResult(
                tool_name=tool_name,
                success=True,
                data=ToolTestExecutionAdapter._operation_data(operation),
                operation_id=operation.id,
                execution_id=operation.instance_id,
                source_kind="tool_test",
            )
        if status == OperationStatus.UNKNOWN.value:
            error = dict(operation.error_json or {})
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(
                    code=str(error.get("code") or "TOOL_TEST_RECONCILIATION_REQUIRED"),
                    message=str(error.get("message") or "工具测试效果需要对账。"),
                ),
                disposition="unknown",
                operation_id=operation.id,
                execution_id=operation.instance_id,
                source_kind="tool_test",
            )
        if status == OperationStatus.FAILED.value:
            error = dict(operation.error_json or {})
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=ToolError(
                    code=str(error.get("code") or "TOOL_TEST_FAILED"),
                    message=str(error.get("message") or "工具测试执行失败。"),
                ),
                disposition="failed",
                operation_id=operation.id,
                execution_id=operation.instance_id,
                source_kind="tool_test",
            )
        return ToolResult(
            tool_name=tool_name,
            success=False,
            error=ToolError(
                code="TOOL_TEST_WAITING",
                message="工具测试仍在等待外部结果，系统不会重复提交。",
            ),
            disposition="waiting",
            operation_id=operation.id,
            execution_id=operation.instance_id,
            source_kind="tool_test",
        )

    @staticmethod
    def _operation_data(operation: SopOperation) -> Any:
        """读取 Operation 中工具测试的原始业务 data，不返回凭据或内部快照。"""

        payload = operation.result_json or {}
        return payload.get("data") if isinstance(payload, Mapping) else payload

    @staticmethod
    def _output_schema(operation: SopOperation) -> dict[str, Any]:
        """从冻结工具测试快照读取结果 schema，防止结果验证随工具编辑漂移。"""

        snapshot = operation.capability_snapshot_json or {}
        tool_test = snapshot.get("tool_test") if isinstance(snapshot, Mapping) else None
        schema = tool_test.get("output_schema") if isinstance(tool_test, Mapping) else None
        return dict(schema) if isinstance(schema, Mapping) else {}

    @staticmethod
    def _verify_output(value: object, schema: Mapping[str, object]) -> str | None:
        """验证工具测试最小 JSON Schema 合同，失败时阻止 verified Publication。"""

        if not schema:
            return None
        schema_type = schema.get("type")
        type_matches = {
            "object": isinstance(value, Mapping),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "number": isinstance(value, int | float) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "null": value is None,
        }
        if isinstance(schema_type, str) and not type_matches.get(schema_type, True):
            return f"结果类型不符合 output schema：期望 {schema_type}。"
        if isinstance(value, Mapping):
            required = schema.get("required")
            missing = [str(item) for item in required or () if item not in value]
            if missing:
                return "结果缺少必填字段：" + ",".join(missing[:8])
            properties = schema.get("properties")
            if isinstance(properties, Mapping):
                for key, child in properties.items():
                    if key in value and isinstance(child, Mapping):
                        error = ToolTestExecutionAdapter._verify_output(value[key], child)
                        if error:
                            return f"字段 {key}：{error}"
        return None

    @staticmethod
    def _with_identity(result: ToolResult, operation: SopOperation) -> ToolResult:
        """在不改写工具业务 data 的前提下附加可恢复执行身份。"""

        return result.model_copy(
            update={
                "operation_id": operation.id,
                "execution_id": operation.instance_id,
                "source_kind": "tool_test",
            }
        )

    @staticmethod
    def _error(tool_name: str, code: str, message: str) -> ToolResult:
        """构造不泄露请求参数和凭据的工具测试错误。"""

        return ToolResult(
            tool_name=tool_name,
            success=False,
            error=ToolError(code=code, message=message),
            disposition="failed",
            source_kind="tool_test",
        )
