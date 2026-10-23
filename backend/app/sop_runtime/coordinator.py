"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : coordinator.py
@CallChain  : Agent Loop → DeterministicSopCoordinator → Scheduler/ExecutionStore/服务执行器
@Description: 连接确定性计划、冻结版本和Operation回执，原子交付可恢复且不重复副作用的结果。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from uuid import uuid4

from sqlmodel import Session, select

from app.approvals import ApprovalRequestService
from app.config import get_settings
from app.core.time_context import normalize_persisted_time_context
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    ExecutionArtifact,
    ExecutionResult,
    ExecutionSignal,
    InputDocumentElement,
    InputResourceSnapshot,
    Message,
    ManagedInputResource,
    MessageInputBindingLink,
    MessageInputResourceLink,
    SelectedResourceExtraction,
    Skill,
    SkillVersion,
    ScheduledTaskRun,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    SopWorkItem,
    Tool,
    User,
    utc_now,
)
from app.dynamic_tasks.artifact_renderer import ArtifactRenderError, ArtifactRendererService
from app.dynamic_tasks.capability_catalog import (
    ToolReliabilityContract,
    published_tool_snapshot,
)
from app.session.helpers import public_session
from app.session.input_bindings import InputBindingError
from app.session.input_runtime import TurnInputRuntimeService
from app.session.session_schema import KnowledgeQuery, StepAgentResult
from app.memory.capture import memory_source_anchor_is_current, source_anchor_from_message
from app.sop_runtime.capabilities import DEFAULT_CAPABILITY_REGISTRY
from app.sop_runtime.definition import CollectInputNode, HumanTaskNode
from app.sop_runtime.execution_store import (
    ACTIVE_INSTANCE_STATUSES,
    ExecutionLease,
    SopExecutionConflictError,
    SopExecutionStore,
)
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.contracts import IdempotencyPolicy, IdempotencyScope
from app.sop_runtime.explicit_confirmation import resolve_explicit_confirmation_slots
from app.sop_runtime.identity_context import (
    SopIdentityContextError,
    resolve_identity_inputs,
    sanitize_identity_slots_after_failure,
)
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, RuntimePlan, plan_next_action
from app.sop_runtime.slot_values import canonicalize_slot_keys, normalize_slot_values
from app.sop_runtime.work_items import SopWorkItemService, WorkItemError
from app.sop_runtime.versioning import skill_content_checksum
from app.tools import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


KNOWLEDGE_RECEIPT_REFERENCE_LIMIT = 8


def _compact_knowledge_receipt(result: Mapping[str, object]) -> dict[str, object]:
    """压缩确定性 Runtime 的知识回执，保留分支事实、计数和稳定引用而移除大段正文。"""

    receipt: dict[str, object] = {}
    for key in (
        "outcome",
        "degraded",
        "evidence_sufficiency",
        "knowledge_base_version_ids",
    ):
        if key in result:
            receipt[key] = result[key]

    evidence_pack = result.get("evidence_pack")
    evidence_rows = (
        [item for item in evidence_pack if isinstance(item, Mapping)]
        if isinstance(evidence_pack, list)
        else []
    )
    evidence_refs = [
        {
            key: item[key]
            for key in (
                "chunk_id",
                "document_id",
                "bucket_id",
                "source_path",
                "section_path",
                "relevance_score",
                "evidence_alignment_score",
            )
            if item.get(key) is not None
        }
        for item in evidence_rows[:KNOWLEDGE_RECEIPT_REFERENCE_LIMIT]
    ]
    if evidence_refs:
        receipt["evidence_refs"] = evidence_refs

    selected_documents = result.get("selected_documents")
    selected_buckets = result.get("selected_buckets")
    selected_concepts = result.get("selected_concepts")
    chunks = result.get("chunks")
    receipt["counts"] = {
        "selected_documents": len(selected_documents)
        if isinstance(selected_documents, list)
        else 0,
        "selected_buckets": len(selected_buckets)
        if isinstance(selected_buckets, list)
        else 0,
        "selected_concepts": len(selected_concepts)
        if isinstance(selected_concepts, list)
        else 0,
        "chunks": len(chunks) if isinstance(chunks, list) else 0,
        "evidence": len(evidence_rows),
    }
    return receipt


class DeterministicSopCoordinator:
    """协调首批确定性 SOP 的持久化节点推进和工具回执路由。"""

    def __init__(self, db: Session) -> None:
        """绑定数据库会话并复用统一执行存储。"""

        self.db = db
        self.store = SopExecutionStore(db)
        self.work_items = SopWorkItemService(db)
        self.worker_id = f"sop-coordinator:{uuid4().hex}"

    def _owned(self, instance: SopInstance) -> AbstractContextManager[ExecutionLease]:
        """为一次 Runtime 命令取得并在结束时释放 execution 推进所有权。"""

        return self.store.owned(instance, worker_id=self.worker_id)

    @staticmethod
    def is_enabled(skill: Skill | None) -> bool:
        """仅对定义显式声明 deterministic 的 SOP 启用新 Runtime。"""

        return bool(skill and (skill.content_json or {}).get("execution_mode") == "deterministic")

    def synchronize_active_instance(self, chat_session: ChatSession) -> SopInstance | None:
        """用活动实例的冻结技能、节点和槽位修复可能被并发旧事务覆盖的会话游标。"""

        instance = self._active_instance(chat_session)
        if instance is None:
            return None
        chat_session.active_skill_id = instance.skill_id
        chat_session.active_step_id = instance.current_node_id
        source_message = self.db.get(Message, instance.source_ref) if instance.source_ref else None
        anchor = source_anchor_from_message(source_message) if source_message is not None else None
        if (
            chat_session.user_id
            and source_message is not None
            and anchor is not None
            and not memory_source_anchor_is_current(
                self.db,
                tenant_id=chat_session.tenant_id,
                user_id=chat_session.user_id,
                anchor=anchor,
            )
        ):
            # 记忆清空/撤权已经使原来源失效；保留实例版本和游标供审计，
            # 但清除会话槽位，禁止旧事实穿过确认轮进入真实消费者。
            chat_session.slots_json = {}
        else:
            chat_session.slots_json = dict(instance.slots_json or {})
        return instance

    def normalize_model_slot_updates(
        self,
        chat_session: ChatSession,
        skill: Skill,
        updates: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """按实例冻结定义归一模型更新，并丢弃所有未声明或非法的槽位。"""

        return self._normalize_declared_slots(chat_session, skill, updates)

    def normalize_runtime_slots(
        self,
        chat_session: ChatSession,
        skill: Skill,
    ) -> dict[str, object]:
        """统一清洗 Router、模型和旧会话汇入 Runtime 的全部槽位。"""

        normalized_slots = self._normalize_declared_slots(
            chat_session,
            skill,
            chat_session.slots_json,
        )
        chat_session.slots_json = normalized_slots
        return normalized_slots

    def _normalize_declared_slots(
        self,
        chat_session: ChatSession,
        skill: Skill,
        slots: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """依据活动实例或当前发布版本执行同一套键、值和声明白名单校验。"""

        active_instance = self._active_instance(chat_session)
        version, definition = (
            self._definition_for_instance(active_instance)
            if active_instance is not None
            else self._published_definition(skill)
        )
        declared_slots = {
            slot_name
            for node in definition.nodes
            if isinstance(node, CollectInputNode)
            for slot_name in node.config.required_inputs
        }
        declared_slots.update(self._schema_declared_slots(version.content_json))
        canonical_slots = canonicalize_slot_keys(version.content_json, slots or {})
        normalized_slots = normalize_slot_values(definition, canonical_slots)
        return {
            key: value
            for key, value in normalized_slots.items()
            if key in declared_slots and not self._blank_slot_value(value)
        }

    def current_slot_repair_contract(
        self,
        chat_session: ChatSession,
        skill: Skill,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """返回当前收集节点仍缺失的规范键及其唯一允许枚举值。"""

        active_instance = self._active_instance(chat_session)
        _, definition = (
            self._definition_for_instance(active_instance)
            if active_instance is not None
            else self._published_definition(skill)
        )
        current_node_id = (
            (active_instance.current_node_id if active_instance is not None else None)
            or chat_session.active_step_id
            or definition.start_node_id
        )
        node = next(
            (
                item
                for item in definition.nodes
                if item.node_id == current_node_id and isinstance(item, CollectInputNode)
            ),
            None,
        )
        if node is None:
            return [], {}
        slots = chat_session.slots_json or {}
        missing_fields = [
            slot_name
            for slot_name in node.config.required_inputs
            if self._blank_slot_value(slots.get(slot_name))
        ]
        contract = {
            slot_name: sorted(
                {
                    canonical
                    for canonical in node.config.value_aliases.get(slot_name, {}).values()
                    if canonical
                }
            )
            for slot_name in missing_fields
            if node.config.value_aliases.get(slot_name)
        }
        return missing_fields, contract

    @staticmethod
    def _blank_slot_value(value: object) -> bool:
        """统一判断模型槽位是否缺失，保留零值和显式布尔值。"""

        return value is None or (isinstance(value, str) and not value.strip())

    @staticmethod
    def _schema_declared_slots(version_content: Mapping[str, object]) -> set[str]:
        """汇总条件 schema 和槽位策略声明，覆盖无输入节点但依赖上下文的流程。"""

        declared: set[str] = set()
        condition_schemas = version_content.get("condition_schemas")
        slots_schema = (
            condition_schemas.get("slots")
            if isinstance(condition_schemas, Mapping)
            else None
        )
        properties = slots_schema.get("properties") if isinstance(slots_schema, Mapping) else None
        if isinstance(properties, Mapping):
            declared.update(
                str(slot_name).strip()
                for slot_name in properties
                if str(slot_name).strip()
            )
        slot_policy = version_content.get("slot_filling_policy")
        target_info = (
            slot_policy.get("target_info")
            if isinstance(slot_policy, Mapping)
            else None
        )
        if isinstance(target_info, (list, tuple)):
            declared.update(
                str(slot_name).strip()
                for slot_name in target_info
                if str(slot_name).strip()
            )
        required_info = version_content.get("required_info")
        if isinstance(required_info, (list, tuple)):
            declared.update(
                str(slot_name).strip()
                for slot_name in required_info
                if str(slot_name).strip()
            )
        return declared

    def prepare_step(
        self,
        chat_session: ChatSession,
        skill: Skill,
        model_result: StepAgentResult,
        *,
        user_message: str = "",
        execution_org_unit_id: str | None = None,
        time_context: Mapping[str, object] | None = None,
    ) -> StepAgentResult:
        """接收模型抽取的槽位后，由 Runtime 唯一决定等待、工具调用或完成。"""

        active_instance = self._active_instance(chat_session)
        source_message_id = self._latest_user_message_id(chat_session)
        (
            incoming_time_context,
            incoming_source_kind,
            incoming_source_ref,
        ) = self._resolve_input_time_context(
            chat_session,
            source_message_id=source_message_id,
            time_context=time_context,
        )
        persisted_time_context = incoming_time_context or self._instance_time_context(
            active_instance
        )
        version, definition = (
            self._definition_for_instance(active_instance)
            if active_instance is not None
            else self._published_definition(skill)
        )
        existing_identity = (
            (active_instance.context_json or {}).get("identity")
            if active_instance is not None
            else None
        )
        current_node_id = (
            (active_instance.current_node_id if active_instance is not None else None)
            or chat_session.active_step_id
            or definition.start_node_id
        )
        normalized_slots = self._normalize_declared_slots(
            chat_session,
            skill,
            chat_session.slots_json,
        )
        normalized_slots = resolve_explicit_confirmation_slots(
            definition,
            current_node_id=current_node_id,
            slots=normalized_slots,
            user_message=user_message,
        )
        chat_session.slots_json = normalized_slots
        try:
            identity_resolution = resolve_identity_inputs(
                self.db,
                definition=definition,
                tenant_id=chat_session.tenant_id,
                actor_user_id=chat_session.user_id,
                slots=normalized_slots,
                user_message=user_message,
                existing_identity_context=(
                    existing_identity if isinstance(existing_identity, Mapping) else None
                ),
            )
        except SopIdentityContextError as error:
            return self._record_identity_failure(
                chat_session,
                skill,
                version,
                definition,
                model_result,
                error,
                time_context=persisted_time_context,
            )
        chat_session.slots_json = identity_resolution.slots
        model_result = model_result.model_copy(
            update={
                "slot_updates": {
                    **identity_resolution.slots,
                }
            }
        )
        instance, instance_created = self.store.start_instance(
            tenant_id=chat_session.tenant_id,
            session_id=chat_session.id,
            skill_id=skill.skill_id,
            skill_version_id=version.id,
            skill_version=version.version,
            definition_checksum=definition.checksum,
            start_node_id=definition.start_node_id,
            initiator_user_id=chat_session.user_id,
            agent_id=chat_session.agent_id,
            slots=identity_resolution.slots,
            context=self._instance_context(
                identity=identity_resolution.audit_context,
                time_context=persisted_time_context,
            ),
            # 历史无 Agent 绑定的会话仍由旧 SOP 兼容路径托管；一旦会话带有
            # Agent 身份，则必须经过墓碑/租户/状态门禁，避免删除并发穿透。
            enforce_agent_lifecycle=chat_session.agent_id is not None,
        )
        with self._owned(instance):
            if instance_created:
                if incoming_source_kind is not None and incoming_source_ref is not None:
                    instance.source_kind = incoming_source_kind
                    instance.source_ref = incoming_source_ref
                elif source_message_id is not None:
                    instance.source_kind = "chat"
                    instance.source_ref = source_message_id
                self.db.add(instance)
                self.db.add(
                    AgentEvent(
                        tenant_id=instance.tenant_id,
                        session_id=instance.session_id,
                        event_type="sop_execution_started",
                        payload_json={
                            "execution_id": instance.id,
                            "skill_id": instance.skill_id,
                            "skill_version_id": instance.skill_version_id,
                            "definition_checksum": instance.definition_checksum,
                            "source_message_id": source_message_id,
                        },
                    )
                )
            instance.slots_json = dict(identity_resolution.slots)
            self._bind_attachment_slots(
                instance,
                definition,
                source_message_id=source_message_id,
            )
            if instance.slots_json != (chat_session.slots_json or {}):
                # 等待节点恢复会从会话槽位生成下一次输入快照；附件绑定先写入
                # Execution 实例后必须同步回会话，否则恢复路径会立即用空槽位覆盖它。
                chat_session.slots_json = dict(instance.slots_json or {})
                self.db.add(chat_session)
            if identity_resolution.audit_context:
                instance.context_json = {
                    **(instance.context_json or {}),
                    "identity": identity_resolution.audit_context,
                }
            if persisted_time_context is not None:
                instance.context_json = {
                    **(instance.context_json or {}),
                    "time_context": persisted_time_context,
                }
            current_node_id = instance.current_node_id or chat_session.active_step_id
            if not current_node_id:
                current_node_id = definition.start_node_id
            execution = self._current_execution(instance, current_node_id)
            if execution is None:
                execution = self.store.enter_node(
                    instance,
                    current_node_id,
                    input_snapshot=self._node_input_snapshot(instance),
                )
            elif execution.status == "waiting":
                self.store.resume_waiting_node(
                    instance,
                    execution,
                    slots=chat_session.slots_json or {},
                    resume_time_context=incoming_time_context,
                )
            plan = self._advance_until_external_action(
                chat_session,
                instance,
                execution,
                definition,
                execution_org_unit_id=execution_org_unit_id,
            )
            return self.merge_plan(model_result.mark_runtime_execution(instance.id), plan)

    def record_tool_result(
        self,
        chat_session: ChatSession,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> RuntimePlan | None:
        """记录工具回执；外部写超时进入 unknown 对账，确定结果才驱动后续路由。"""

        instance = self._active_instance(chat_session)
        if instance is None:
            return None
        execution = self._current_execution(instance, instance.current_node_id or "")
        if execution is None:
            return None
        try:
            operation = self.current_tool_operation(chat_session, tool_call)
        except SopExecutionConflictError as exc:
            return RuntimePlan(
                action=RuntimeAction.FAIL,
                node_id=execution.node_id,
                operation_name=tool_call.name,
                error_code=exc.code,
                control_reply="当前工具请求与已持久化 Operation 不一致，系统拒绝继续派发。",
            )
        if operation is None:
            return None
        if operation.status == "waiting_external":
            return self._wait_for_operation_plan(
                RuntimePlan(
                    action=RuntimeAction.CALL_TOOL,
                    node_id=execution.node_id,
                    operation_name=operation.operation_name,
                    operation_arguments=dict(operation.request_json or {}),
                ),
                operation,
            )
        if not tool_result.success and tool_result.error is not None and operation.status in {
            "succeeded",
            "failed",
        }:
            return RuntimePlan(
                action=RuntimeAction.FAIL,
                node_id=execution.node_id,
                operation_name=operation.operation_name,
                error_code=tool_result.error.code,
                control_reply=tool_result.error.message,
            )
        if operation.status in {"unknown", "retry_wait"}:
            if operation.status == "unknown":
                with self.store.owned_for_reconciliation(instance, worker_id=self.worker_id):
                    self.store._ensure_effect_reconciliation_attention(
                        instance, operation, actor_user_id=instance.initiator_user_id or "",
                    )
            if (
                not tool_result.success
                and tool_result.error is not None
                and tool_result.error.code
                not in {
                    "TOOL_CALL_OUTCOME_UNKNOWN",
                    "RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
                    "RUNTIME_OPERATION_RETRY_WAIT",
                }
            ):
                return RuntimePlan(
                    action=RuntimeAction.FAIL,
                    node_id=execution.node_id,
                    operation_name=operation.operation_name,
                    error_code=tool_result.error.code,
                    control_reply=tool_result.error.message,
                )
            return self._wait_for_operation_plan(
                RuntimePlan(
                    action=RuntimeAction.CALL_TOOL,
                    node_id=execution.node_id,
                    operation_name=operation.operation_name,
                    operation_arguments=dict(operation.request_json or {}),
                ),
                operation,
            )
        if operation.status in {"succeeded", "failed"}:
            with self._owned(instance):
                if execution.status == "running":
                    self._restore_operation_receipt(instance, execution, operation)
                    self.store.complete_node(
                        instance,
                        execution,
                        output={"operation_id": operation.id, "restored": True},
                    )
                _, definition = self._definition_for_instance(instance)
                return self._advance_until_external_action(
                    chat_session,
                    instance,
                    execution,
                    definition,
                    current_already_completed=True,
                )
        with self._owned(instance):
            if operation.status == "prepared":
                self.store.start_operation(operation)
            error_payload = tool_result.error.model_dump(mode="json") if tool_result.error else {}
            if tool_result.disposition == "accepted":
                data = tool_result.data if isinstance(tool_result.data, Mapping) else {}
                contract = (operation.capability_snapshot_json or {}).get("contract", {})
                provider_task_id = str(data.get("provider_task_id") or "").strip()
                status_url = str(contract.get("status_url") or "").strip()
                if not provider_task_id or not status_url:
                    self.store.mark_operation_unknown(
                        operation,
                        error={
                            "code": "PROVIDER_TASK_RECEIPT_INVALID",
                            "message": "provider_task 受理回执缺少任务身份或查询地址。",
                        },
                    )
                    self.store._ensure_effect_reconciliation_attention(
                        instance, operation, actor_user_id=instance.initiator_user_id or "",
                    )
                    return RuntimePlan(
                        action=RuntimeAction.WAIT_OPERATION,
                        node_id=execution.node_id,
                        operation_name=operation.operation_name,
                        error_code="RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
                        control_reply="外部任务受理回执不完整，系统已停止重复提交并等待对账。",
                    )
                self.store.mark_operation_waiting_external(
                    operation,
                    provider_task_id=provider_task_id,
                    provider_status=str(data.get("provider_status") or "accepted"),
                    receipt=data,
                    status_url=status_url,
                    tracking_seconds=int(contract.get("max_tracking_seconds") or 86400),
                    poll_interval_seconds=int(contract.get("poll_interval_seconds") or 5),
                )
                return self._wait_for_operation_plan(
                    RuntimePlan(
                        action=RuntimeAction.CALL_TOOL,
                        node_id=execution.node_id,
                        operation_name=operation.operation_name,
                        operation_arguments=dict(operation.request_json or {}),
                    ),
                    operation,
                )
            if tool_result.disposition == "unknown" and operation.effect_kind in {
                "external_write",
                "destructive",
            }:
                self.store.mark_operation_unknown(operation, error=error_payload)
                self.store._ensure_effect_reconciliation_attention(
                    instance, operation, actor_user_id=instance.initiator_user_id or "",
                )
                return RuntimePlan(
                    action=RuntimeAction.WAIT_OPERATION,
                    node_id=execution.node_id,
                    operation_name=operation.operation_name,
                    error_code="RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
                    control_reply="外部任务结果仍需对账，系统已停止重复提交。",
                )
            if (
                not tool_result.success
                and operation.effect_kind == "external_write"
                and tool_result.error is not None
                and self._is_ambiguous_external_failure(
                    tool_result.error.code,
                    tool_result.error.message,
                )
            ):
                self.store.mark_operation_unknown(operation, error=error_payload)
                self.store._ensure_effect_reconciliation_attention(
                    instance, operation, actor_user_id=instance.initiator_user_id or "",
                )
                return RuntimePlan(
                    action=RuntimeAction.WAIT_OPERATION,
                    node_id=execution.node_id,
                    operation_name=operation.operation_name,
                    error_code="RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
                    control_reply="外部操作结果尚未确认，系统已停止重复提交并等待对账。",
                )
            self.store.finish_operation(
                operation,
                succeeded=tool_result.success,
                result=tool_result.data or {},
                error=error_payload,
            )
            result_key = self._operation_result_key(instance, execution.node_id)
            if not result_key:
                return None
            tool_results = self._tool_results(instance)
            tool_results[result_key] = {
                "status": "succeeded" if tool_result.success else "failed",
                "data": tool_result.data or {},
                "error": error_payload or None,
                "operation_id": operation.id,
            }
            instance.context_json = {
                **(instance.context_json or {}),
                "tool_results": tool_results,
            }
            self.store.complete_node(
                instance,
                execution,
                output={"tool_result": tool_results[result_key]},
            )
            _, definition = self._definition_for_instance(instance)
            plan = self._advance_until_external_action(
                chat_session,
                instance,
                execution,
                definition,
                current_already_completed=True,
            )
            return plan

    def record_knowledge_result(
        self,
        chat_session: ChatSession,
        knowledge_query: KnowledgeQuery,
        result: Mapping[str, object],
        *,
        succeeded: bool = True,
        error: Mapping[str, object] | None = None,
    ) -> RuntimePlan | None:
        """持久化知识检索回执，并由冻结定义唯一决定后续路由。"""

        instance = self._active_instance(chat_session)
        if instance is None:
            return None
        execution = self._current_execution(instance, instance.current_node_id or "")
        if execution is None:
            return None
        operation = self.db.exec(
            select(SopOperation)
            .join(
                SopOperationAttempt,
                SopOperationAttempt.operation_id == SopOperation.id,
            )
            .where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperationAttempt.tenant_id == instance.tenant_id,
                SopOperationAttempt.node_execution_id == execution.id,
                SopOperation.operation_name == "knowledge.search",
                SopOperation.status.in_(("prepared", "running")),
            )
            .order_by(SopOperation.created_at.desc())
        ).first()
        if operation is None:
            return None
        with self._owned(instance):
            if operation.status == "prepared":
                self.store.start_operation(operation)
            error_payload = dict(error or {})
            persisted_result = _compact_knowledge_receipt(result)
            self.store.finish_operation(
                operation,
                succeeded=succeeded,
                result=persisted_result,
                error=error_payload,
            )
            result_key = self._operation_result_key(instance, execution.node_id)
            if not result_key:
                return None
            node_outputs = self._node_outputs(instance)
            node_outputs[result_key] = {
                "status": "succeeded" if succeeded else "failed",
                "query": knowledge_query.model_dump(mode="json"),
                "data": persisted_result,
                "error": error_payload or None,
                "operation_id": operation.id,
            }
            instance.context_json = {
                **(instance.context_json or {}),
                "node_outputs": node_outputs,
            }
            self.store.complete_node(
                instance,
                execution,
                output={"node_output": node_outputs[result_key]},
            )
            _, definition = self._definition_for_instance(instance)
            return self._advance_until_external_action(
                chat_session,
                instance,
                execution,
                definition,
                current_already_completed=True,
            )

    def resume_completed_work_item(self, work_item: SopWorkItem) -> RuntimePlan:
        """消费已完成工作项回执，恢复等待节点并按结构化 outcome 继续唯一分支。"""

        if work_item.status != "completed" or not work_item.outcome:
            raise WorkItemError(
                "WORK_ITEM_NOT_COMPLETED",
                "只有已经形成结构化结果的工作项可以恢复 SOP。",
            )
        instance = self.db.get(SopInstance, work_item.instance_id)
        if instance is None or instance.tenant_id != work_item.tenant_id:
            raise WorkItemError("WORK_ITEM_INSTANCE_NOT_FOUND", "工作项所属 SOP 实例不存在。")
        execution = self.db.get(SopNodeExecution, work_item.node_execution_id)
        if (
            execution is None
            or execution.tenant_id != work_item.tenant_id
            or execution.instance_id != instance.id
        ):
            raise WorkItemError("WORK_ITEM_EXECUTION_NOT_FOUND", "工作项所属节点执行不存在。")
        if self._work_item_was_resumed(instance.session_id, work_item.id):
            return RuntimePlan(
                action=(
                    RuntimeAction.COMPLETE
                    if instance.status == "succeeded"
                    else RuntimeAction.ADVANCE
                ),
                node_id=execution.node_id,
                outcome=work_item.outcome,
            )
        if instance.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return RuntimePlan(
                action=(
                    RuntimeAction.COMPLETE if instance.status == "succeeded" else RuntimeAction.FAIL
                ),
                node_id=execution.node_id,
                outcome=instance.status if instance.status == "succeeded" else None,
                error_code=(
                    None if instance.status == "succeeded" else "RUNTIME_INSTANCE_ALREADY_TERMINAL"
                ),
            )
        with self._owned(instance):
            return self._resume_completed_work_item_owned(work_item, instance, execution)

    def resume_reconciliation_attention(
        self,
        attention: SopWorkItem,
        *,
        worker_id: str,
    ) -> RuntimePlan:
        """在原API对账lease事务消费唯一signal后结算原动作；取消仅记账，正常走冻结定义。

        先消费信号以保持原取消终态闭合；已生效只生成原对账身份元数据，comment留私密证据。
        不新建Operation/Attempt，不重新提交原动作，也不从comment或失败回执拼造业务结果。
        """

        instance = self.db.get(SopInstance, attention.instance_id)
        control = ExecutionControlService(self.db, self.store)
        if (
            instance is None or instance.kind != "sop" or attention.status != "completed"
            or not control.is_external_reconciliation_attention(instance, attention)
        ):
            raise SopExecutionConflictError("SOP 对账工作项未绑定当前unknown外部效果。")
        self.store.authorize_mutation(instance, "operation.reconciliation_resume")
        operation = self.db.get(SopOperation, str(attention.payload_json.get("operation_id") or ""))
        execution = self.db.get(
            SopNodeExecution, str(attention.payload_json.get("node_execution_id") or ""),
        )
        if (
            operation is None or execution is None or attention.source_ref != operation.id
            or execution.tenant_id != instance.tenant_id or execution.instance_id != instance.id
            or operation.node_execution_id != execution.id or execution.status != "waiting"
        ):
            raise SopExecutionConflictError("SOP 对账未绑定同一Operation及等待节点。")
        resolution = attention.resolution_json or {}
        command = str(resolution.get("command") or "")
        actor_user_id = str(resolution.get("actor_user_id") or "")
        if (
            command not in {"confirm_applied", "confirm_not_applied"} or not actor_user_id
            or resolution.get("revision") != attention.revision or not (attention.comment or "").strip()
        ):
            raise SopExecutionConflictError("SOP 对账缺少原完成revision、命令或证据。")
        signal = self.db.exec(select(ExecutionSignal).where(
            ExecutionSignal.tenant_id == instance.tenant_id,
            ExecutionSignal.execution_id == instance.id,
            ExecutionSignal.signal_type == "attention_decided",
            ExecutionSignal.causation_id == f"{attention.id}:{attention.revision}",
        )).first()
        if (
            signal is None or signal.status != "pending"
            or signal.payload_json.get("attention_id") != attention.id
            or signal.payload_json.get("command") != command
            or signal.payload_json.get("revision") != attention.revision
        ):
            raise SopExecutionConflictError("SOP 对账缺少唯一且尚未消费的原恢复信号。")
        cancellation_requested = instance.cancellation_requested_at is not None
        chat_session = self.db.get(ChatSession, instance.session_id)
        if chat_session is None or chat_session.tenant_id != instance.tenant_id:
            raise SopExecutionConflictError("SOP 对账所属原会话不存在。")
        if not cancellation_requested:
            self.store.resume_waiting_node(instance, execution, slots=instance.slots_json or {})
        applied = command == "confirm_applied"
        evidence = {
            "code": "MANUAL_RECONCILIATION", "actor_user_id": actor_user_id,
            "attention_id": attention.id, "comment_present": bool(attention.comment),
        }
        data = {
            "effect_status": "manually_confirmed", "operation_id": operation.id,
            "external_reference": operation.external_reference or "",
        }
        control.claim_signal(signal, worker_id=worker_id, allow_archived_agent=True)
        control.consume_signal(instance, signal, worker_id=worker_id)
        settled = self.store.reconcile_operation(
            instance, operation, succeeded=applied,
            result={"data": data} if applied else None,
            error=None if applied else evidence, effect_confirmed=applied,
        )
        if cancellation_requested or settled:
            return RuntimePlan(action=RuntimeAction.FAIL, node_id=execution.node_id,
                               error_code="RUNTIME_INSTANCE_CANCELLED")
        chat_session.active_skill_id = instance.skill_id
        chat_session.active_step_id = instance.current_node_id
        chat_session.slots_json = dict(instance.slots_json or {})
        if not applied:
            self.store.fail_node(instance, execution, error=evidence)
            self.store.fail_instance(instance, context_patch={"failure_code": "SOP_WRITE_NOT_APPLIED"})
            self.db.add(chat_session)
            return RuntimePlan(action=RuntimeAction.FAIL, node_id=execution.node_id,
                               error_code="SOP_WRITE_NOT_APPLIED")
        unavailable = self._current_runtime_actor_unavailable_code(instance)
        if unavailable is not None:
            # 治理人已确认的效果是独立事实。来源关闭只阻止普通推进，不得借最终消息
            # 权限拒绝回滚该对账，也不能虚构一项原本不存在的用户取消请求。
            self.store.fail_node(instance, execution, error={"code": unavailable})
            instance.terminal_reason_json = {"code": unavailable}
            self.db.add(instance)
            self.store.fail_instance(instance, context_patch={"failure_code": unavailable})
            self.db.add(chat_session)
            return RuntimePlan(
                action=RuntimeAction.FAIL, node_id=execution.node_id, error_code=unavailable,
            )
        self._restore_operation_receipt(instance, execution, operation)
        self.store.complete_node(instance, execution, output={"operation_id": operation.id,
                                                            "reconciled": True})
        try:
            _, definition = self._definition_for_instance(instance)
        except ValueError as error:
            raise SopExecutionConflictError("SOP 对账的冻结定义身份已失效。") from error
        plan = self._advance_until_external_action(
            chat_session, instance, execution, definition, current_already_completed=True,
        )
        plan = self._execute_resumed_tool_plans(
            chat_session, instance, plan, execution_org_unit_id=None,
            source="external_reconciliation_resume", durable_dispatch=True,
        )
        if plan.action is RuntimeAction.COMPLETE and instance.status == "succeeded":
            self._deliver_external_completion(chat_session, instance, execution, operation)
        self.db.add(chat_session)
        return plan

    def _current_runtime_actor_unavailable_code(self, instance: SopInstance) -> str | None:
        """锁读原员工和发起人当前使用资格；旧无完整聊天身份保持原工具授权边界。"""

        if not instance.agent_id:
            return None
        agent = self.db.get(
            AgentProfile, instance.agent_id, populate_existing=True, with_for_update=True,
        )
        if agent is None or agent.tenant_id != instance.tenant_id or agent.status != "active":
            return "AGENT_NOT_AVAILABLE"
        if not instance.initiator_user_id:
            return None
        actor = self.db.get(
            User, instance.initiator_user_id, populate_existing=True, with_for_update=True,
        )
        if (actor is None or actor.tenant_id != instance.tenant_id
                or actor.membership_status != "active"):
            return "SOP_ACTOR_NOT_ACTIVE"
        from app.security.permissions import can_use_agent_in_chat

        if not can_use_agent_in_chat(self.db, agent, actor, current=True):
            return "SOP_ACTOR_PERMISSION_REVOKED"
        return None

    def resume_external_task_signal(
        self,
        *,
        signal_id: str,
        worker_id: str,
    ) -> RuntimePlan:
        """恢复原Operation并派发后继，成功后原子交付真实回执，保持旧动作和Turn幂等。"""

        signal = self.db.get(ExecutionSignal, signal_id)
        instance = self.db.get(SopInstance, signal.execution_id) if signal is not None else None
        if (
            signal is None
            or signal.signal_type != "external_event"
            or signal.payload_json.get("kind") != "external_task"
            or instance is None
            or instance.kind != "sop"
            or signal.status != "claimed"
            or signal.lease_owner != worker_id
        ):
            raise SopExecutionConflictError("SOP 外部回执 Signal 身份无效。")
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
            raise SopExecutionConflictError("SOP 外部回执未绑定同一 Operation/节点。")
        if operation.status == "unknown":
            with self._owned(instance):
                self.store._ensure_effect_reconciliation_attention(
                    instance,
                    operation,
                    actor_user_id=instance.initiator_user_id or "",
                )
                ExecutionControlService(self.db, self.store).consume_signal(
                    instance,
                    signal,
                    worker_id=worker_id,
                )
            return RuntimePlan(
                action=RuntimeAction.WAIT_OPERATION,
                node_id=execution.node_id,
                operation_name=operation.operation_name,
                error_code="RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
                control_reply="外部结果需要对账，系统不会重新提交原动作。",
            )
        if operation.status not in {"succeeded", "failed"}:
            raise SopExecutionConflictError("SOP 外部回执尚未形成终态 Operation。")
        chat_session = self.db.get(ChatSession, instance.session_id)
        if chat_session is None or chat_session.tenant_id != instance.tenant_id:
            raise SopExecutionConflictError("SOP 外部回执所属会话不存在。")
        with self._owned(instance):
            chat_session.active_skill_id = instance.skill_id
            chat_session.active_step_id = instance.current_node_id
            chat_session.slots_json = dict(instance.slots_json or {})
            if execution.status == "waiting" and instance.status == "waiting":
                self.store.resume_waiting_node(
                    instance,
                    execution,
                    slots=instance.slots_json or {},
                )
            self._restore_operation_receipt(instance, execution, operation)
            if execution.status == "running":
                self.store.complete_node(
                    instance,
                    execution,
                    output={"operation_id": operation.id, "restored": True},
                )
            # 先在同一事务消费回执 Signal，再进入可能闭合实例的推进；否则终态门禁会把
            # 当前已处理的 Signal 误当成活动阻塞事实。后继派发身份提交前异常可回滚重试；
            # 提交后 Signal 已 consumed，外呼异常保留 running Operation 供原超时入口对账。
            ExecutionControlService(self.db, self.store).consume_signal(
                instance,
                signal,
                worker_id=worker_id,
            )
            _, definition = self._definition_for_instance(instance)
            plan = self._advance_until_external_action(
                chat_session,
                instance,
                execution,
                definition,
                current_already_completed=True,
            )
            plan = self._execute_resumed_tool_plans(
                chat_session,
                instance,
                plan,
                execution_org_unit_id=None,
                source="external_task_resume",
                durable_dispatch=True,
            )
            if plan.action is RuntimeAction.COMPLETE and instance.status == "succeeded":
                self._deliver_external_completion(chat_session, instance, execution, operation)
            self.db.add(chat_session)
            return plan

    def _external_completion_source(
        self,
        chat_session: ChatSession,
        instance: SopInstance,
        execution: SopNodeExecution,
    ) -> tuple[Message, dict[str, str] | None] | None:
        """从原工具节点输入绑定确认致因，并锁读本人及当前权限；不从最新消息猜来源。"""

        if instance.source_kind != "chat" or not instance.agent_id or not instance.initiator_user_id:
            # 历史无完整聊天身份的SOP仍沿原恢复合同推进，不捏造可反馈的用户来源。
            return None
        if (chat_session.tenant_id != instance.tenant_id
                or chat_session.id != instance.session_id
                or chat_session.user_id != instance.initiator_user_id
                or chat_session.agent_id != instance.agent_id
                or execution.tenant_id != instance.tenant_id
                or execution.instance_id != instance.id):
            raise SopExecutionConflictError("SOP 最终交付的会话身份已失效。")
        raw_time = (execution.input_json or {}).get("time_context")
        time_context = normalize_persisted_time_context(raw_time)
        if raw_time is not None and time_context is None:
            raise SopExecutionConflictError("SOP 最终交付的致因时间已损坏。")
        if raw_time is None and instance.source_ref == instance.session_id:
            # start_instance 的旧默认来源是会话ID，并非致因消息。保留原恢复合同，
            # 不能从现存消息猜来源，也不为缺失聊天交付身份回滚已验证的流程终态。
            return None
        source_id = time_context["source_input_id"] if time_context else instance.source_ref
        if not source_id:
            return None
        source = self.db.get(Message, source_id, populate_existing=True, with_for_update=True)
        if (source is None or source.role != "user"
                or source.tenant_id != instance.tenant_id
                or source.session_id != instance.session_id):
            raise SopExecutionConflictError("SOP 最终交付的原始输入已失效。")
        if time_context is not None and normalize_persisted_time_context(
            (source.metadata_json or {}).get("time_context"),
            expected_source_input_id=source.id,
        ) != time_context:
            raise SopExecutionConflictError("SOP 最终交付的时间与原始输入不一致。")
        actor = self.db.get(
            User, instance.initiator_user_id, populate_existing=True, with_for_update=True,
        )
        agent = self.db.get(
            AgentProfile, instance.agent_id, populate_existing=True, with_for_update=True,
        )
        from app.security.permissions import can_use_agent_in_chat

        if (actor is None or actor.tenant_id != instance.tenant_id
                or actor.membership_status != "active" or agent is None
                or not can_use_agent_in_chat(self.db, agent, actor, current=True)):
            raise SopExecutionConflictError("SOP 最终交付的本人权限已失效。")
        version = self.db.get(
            SkillVersion, instance.skill_version_id, populate_existing=True, with_for_update=True,
        )
        if (version is None or version.tenant_id != instance.tenant_id
                or version.skill_id != instance.skill_id
                or version.version != instance.skill_version
                or version.compiled_definition_checksum != instance.definition_checksum):
            raise SopExecutionConflictError("SOP 最终交付的冻结版本身份已失效。")
        return source, time_context

    def _deliver_external_completion(
        self,
        chat_session: ChatSession,
        instance: SopInstance,
        execution: SopNodeExecution,
        operation: SopOperation,
    ) -> Message | None:
        """在原恢复事务和owner屏障内交付实际成功回执，保留旧Turn封口及一次消息语义。"""

        if operation.status != "succeeded":
            return None
        self.db.flush()
        binding = self._external_completion_source(chat_session, instance, execution)
        if binding is None:
            return None
        self.store.authorize_mutation(instance, "result.external_chat_delivery")
        source, time_context = binding
        from app.feedback.source import feedback_source, freeze_execution_source
        from app.observability.event_log import EventLog

        client_turn_id = str((source.metadata_json or {}).get("client_turn_id") or "").strip()
        events = EventLog(self.db)
        events.bind_turn(source.id, client_turn_id or None)
        turn_payload = {"turn_id": source.id, "user_message_id": source.id}
        terminals = events.terminal_event_types(instance.tenant_id, instance.session_id, turn_payload)
        if terminals - {"complete"}:
            return None
        result = self.db.get(ExecutionResult, instance.current_result_id)
        if (result is None or result.tenant_id != instance.tenant_id
                or result.execution_id != instance.id or result.status != "verified"):
            raise SopExecutionConflictError("SOP 最终交付缺少同一实例的验证结果。")
        for existing in self.db.exec(select(Message).where(
            Message.tenant_id == instance.tenant_id, Message.session_id == instance.session_id,
            Message.role == "assistant",
        )).all():
            metadata = existing.metadata_json or {}
            if (metadata.get("execution_id") == instance.id
                    and metadata.get("result_id") == result.id
                    and (metadata.get("feedback_source") or {}).get("execution_status")
                    == "succeeded"):
                return existing
        completed = self.db.exec(select(SopOperation).where(
            SopOperation.tenant_id == instance.tenant_id,
            SopOperation.instance_id == instance.id, SopOperation.status == "succeeded",
        ).order_by(SopOperation.created_at, SopOperation.id)).all()
        receipts = [row for row in completed if row.id == operation.id or row.external_task_id
                    or row.effect_kind in {"external_write", "destructive"}]
        reply = "流程已完成。以下是已确认的任务回执：\n\n" + json.dumps(
            [dict(operation_name=row.operation_name,
                  result=self._user_visible_operation_result(row)) for row in receipts],
            ensure_ascii=False, sort_keys=True, indent=2,
        )
        metadata = {
            "source": "runtime_control", "response_source": "runtime_control",
            "render_policy": "verbatim", "execution_id": instance.id,
            "result_id": result.id, "result_checksum": result.checksum,
            "operation_ids": [row.id for row in receipts],
            "user_message_id": source.id, "turn_id": source.id,
            "feedback_source": freeze_execution_source(
                self.db, chat_session, source.id, instance.id,
            ),
        }
        if time_context is not None:
            metadata["time_context"] = time_context
        if client_turn_id:
            metadata["client_turn_id"] = client_turn_id
        message = Message(
            tenant_id=instance.tenant_id, session_id=instance.session_id,
            role="assistant", content=reply, metadata_json=metadata,
        )
        self.db.add(message)
        self.db.flush()
        if feedback_source(self.db, message).status != "verified":
            raise SopExecutionConflictError("SOP 最终交付来源未通过现有反馈来源验证。")
        chat_session.status = "active"
        chat_session.summary = f"最近回复：{reply[:120]}"
        chat_session.updated_at = utc_now()
        self.db.add(chat_session)
        payload = {
            "message_id": message.id, "assistant_message_id": message.id, "reply": reply,
            "execution_id": instance.id, "result_id": result.id,
            "response_source": "runtime_control", "render_policy": "verbatim",
        }
        if time_context is not None:
            payload["time_context"] = time_context
        if not terminals:
            events.record(instance.tenant_id, instance.session_id, "assistant_message_created", payload)
            events.record_terminal_once(instance.tenant_id, instance.session_id, "complete", {
                **payload, "status": "completed",
            })
        # 原waiting回复可能已结束同步Turn。最终业务消息供正常会话读回，不重开旧Turn；
        # session_state_changed继续保持原会话状态投影语义，并携带实际交付身份。
        events.record(instance.tenant_id, instance.session_id, "session_state_changed", {
            **public_session(chat_session).model_dump(mode="json"), **payload,
        })
        return message

    @staticmethod
    def _user_visible_operation_result(operation: SopOperation) -> object | None:
        """按原Operation冻结的用户schema投影实际data，缺schema失效关闭且不裁剪审计原件。"""

        from app.dynamic_tasks.execution_context import project_result_for_model

        view = (operation.capability_snapshot_json or {}).get("user_view")
        schema = view.get("output_schema") if isinstance(view, Mapping) else None
        data = (operation.result_json or {}).get("data")
        if (not isinstance(schema, Mapping) or schema.get("type") != "object"
                or not isinstance(data, dict)):
            # 原已发布三视图的根schema为object；未知/缺失视图和不匹配原文均不扩张披露。
            return None
        return project_result_for_model(data, schema)

    def _resume_completed_work_item_owned(
        self,
        work_item: SopWorkItem,
        instance: SopInstance,
        execution: SopNodeExecution,
    ) -> RuntimePlan:
        """在已取得 execution 所有权后消费人工任务结果并完成后继推进。"""

        chat_session = self.db.get(ChatSession, instance.session_id)
        if chat_session is None or chat_session.tenant_id != instance.tenant_id:
            raise WorkItemError("WORK_ITEM_SESSION_NOT_FOUND", "工作项所属会话不存在。")
        chat_session.active_skill_id = instance.skill_id
        chat_session.active_step_id = instance.current_node_id
        chat_session.slots_json = dict(instance.slots_json or {})
        if execution.status == "waiting" and instance.status == "waiting":
            self.store.resume_waiting_node(
                instance,
                execution,
                slots=instance.slots_json or {},
            )
        if execution.status == "running":
            self.store.complete_node(
                instance,
                execution,
                output={
                    "work_item_id": work_item.id,
                    "status": work_item.status,
                    "outcome": work_item.outcome,
                },
            )
        _, definition = self._definition_for_instance(instance)
        plan = self._advance_until_external_action(
            chat_session,
            instance,
            execution,
            definition,
            current_already_completed=True,
        )
        scope_snapshot = work_item.participant_scope_snapshot_json or {}
        execution_org_unit_id = str(scope_snapshot.get("root_org_unit_id") or "") or None
        plan = self._execute_resumed_tool_plans(
            chat_session,
            instance,
            plan,
            execution_org_unit_id=execution_org_unit_id,
        )
        self.db.add(
            AgentEvent(
                tenant_id=instance.tenant_id,
                session_id=instance.session_id,
                event_type="sop_work_item_resumed",
                payload_json={
                    "instance_id": instance.id,
                    "node_execution_id": execution.id,
                    "work_item_id": work_item.id,
                    "outcome": work_item.outcome,
                    "next_action": plan.action.value,
                    "next_node_id": plan.next_node_id,
                },
            )
        )
        self.db.add(
            Message(
                tenant_id=instance.tenant_id,
                session_id=instance.session_id,
                role="assistant",
                content=self._work_item_completion_message(work_item, plan, instance),
                metadata_json={
                    "source": "runtime_control",
                    "render_policy": "verbatim",
                    "event_type": "sop_work_item_completed",
                    "instance_id": instance.id,
                    "work_item_id": work_item.id,
                    "outcome": work_item.outcome,
                    "decision_comment": work_item.comment,
                },
            )
        )
        return plan

    def _execute_resumed_tool_plans(
        self,
        chat_session: ChatSession,
        instance: SopInstance,
        plan: RuntimePlan,
        *,
        execution_org_unit_id: str | None,
        source: str = "work_item_resume",
        durable_dispatch: bool = False,
    ) -> RuntimePlan:
        """执行后继工具；外部回执先提交派发身份，故障回滚不能抹去已发送的动作。"""

        current_plan = plan
        for _ in range(16):
            if current_plan.action is not RuntimeAction.CALL_TOOL or not current_plan.operation_name:
                return current_plan
            tool_call = ToolCall(
                name=current_plan.operation_name,
                arguments=current_plan.operation_arguments,
            )
            operation = self._running_operation(instance, tool_call.name)
            if operation is not None and operation.status in {
                "retry_wait",
                "waiting_external",
                "unknown",
            }:
                if operation.status == "unknown":
                    self.store._ensure_effect_reconciliation_attention(
                        instance,
                        operation,
                        actor_user_id=instance.initiator_user_id or "",
                    )
                return self._wait_for_operation_plan(current_plan, operation)
            started_payload = tool_call.model_dump(mode="json")
            if operation is not None:
                started_payload["idempotency_key"] = operation.idempotency_key
            started_payload["source"] = source
            self.db.add(
                AgentEvent(
                    tenant_id=instance.tenant_id,
                    session_id=instance.session_id,
                    event_type="tool_call_started",
                    payload_json=started_payload,
                )
            )
            if durable_dispatch:
                self.db.commit()
                self.store.authorize_mutation(instance, "operation.resume_dispatch")

            def authorize_http_dispatch() -> None:
                """在原HTTP运输前复核同owner、当前来源及冻结工具；由原执行器提交后释放事务。"""

                self.db.expire_all()
                self.store.authorize_mutation(instance, "operation.resume_http_dispatch")
                unavailable = self._current_runtime_actor_unavailable_code(instance)
                if unavailable is not None:
                    raise SopExecutionConflictError(f"SOP 后继当前来源已失效：{unavailable}。")
                if (
                    chat_session.tenant_id != instance.tenant_id
                    or chat_session.id != instance.session_id
                    or (instance.agent_id and chat_session.agent_id != instance.agent_id)
                    or (instance.initiator_user_id
                        and chat_session.user_id != instance.initiator_user_id)
                ):
                    raise SopExecutionConflictError("SOP 后继的原会话与发起人身份已失效。")
                if operation is not None and not self.operation_capability_is_current(
                    chat_session, operation,
                ):
                    raise SopExecutionConflictError("SOP 后继冻结工具合同已失效。")
                authorization_error = ToolExecutor(self.db).authorize(
                    instance.tenant_id, tool_call, instance.skill_id,
                    chat_session.agent_id, chat_session.user_id,
                    execution_org_unit_id=execution_org_unit_id,
                    remote_idempotency_key=(
                        operation.remote_idempotency_key if operation is not None else None
                    ),
                )
                if authorization_error is not None:
                    raise SopExecutionConflictError("SOP 后继当前工具授权已失效。")

            tool_result = ToolExecutor(self.db).execute(
                instance.tenant_id,
                tool_call,
                instance.skill_id,
                chat_session.agent_id,
                chat_session.user_id,
                execution_org_unit_id=execution_org_unit_id,
                remote_idempotency_key=(
                    operation.remote_idempotency_key if operation is not None else None
                ),
                **({"before_http_dispatch": authorize_http_dispatch} if durable_dispatch else {}),
            )
            finished_payload = tool_result.model_dump(mode="json")
            finished_payload["tool_call"] = tool_call.model_dump(mode="json")
            finished_payload["source"] = source
            if operation is not None:
                finished_payload["idempotency_key"] = operation.idempotency_key
            self.db.add(
                AgentEvent(
                    tenant_id=instance.tenant_id,
                    session_id=instance.session_id,
                    event_type="tool_call_finished",
                    payload_json=finished_payload,
                )
            )
            next_plan = self.record_tool_result(chat_session, tool_call, tool_result)
            if next_plan is None:
                return RuntimePlan(
                    action=RuntimeAction.FAIL,
                    node_id=current_plan.node_id,
                    error_code="RUNTIME_RESUMED_TOOL_RESULT_NOT_RECORDED",
                )
            current_plan = next_plan
        return RuntimePlan(
            action=RuntimeAction.FAIL,
            node_id=current_plan.node_id,
            error_code="RUNTIME_RESUMED_TOOL_CHAIN_LIMIT_EXCEEDED",
        )

    def _running_operation(
        self,
        instance: SopInstance,
        operation_name: str,
    ) -> SopOperation | None:
        """读取当前恢复链已准备的工具操作，用于关联事件和稳定幂等键。"""

        execution = self._current_execution(instance, instance.current_node_id or "")
        if execution is None:
            return None
        return self.db.exec(
            select(SopOperation)
            .join(
                SopOperationAttempt,
                SopOperationAttempt.operation_id == SopOperation.id,
            )
            .where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperationAttempt.tenant_id == instance.tenant_id,
                SopOperationAttempt.node_execution_id == execution.id,
                SopOperation.operation_name == operation_name,
                SopOperation.status.in_(
                    ("prepared", "running", "retry_wait", "waiting_external", "unknown")
                ),
            )
            .order_by(SopOperation.created_at.desc())
        ).first()

    def timeout_expired_work_item(self, work_item: SopWorkItem) -> None:
        """消费已过期工作项，以统一状态机结束等待节点和实例并通知申请人。"""

        if work_item.status != "expired":
            raise WorkItemError(
                "WORK_ITEM_NOT_EXPIRED",
                "只有已经到期的人工工作项可以执行超时处置。",
            )
        if work_item.timeout_action != "fail":
            raise WorkItemError(
                "WORK_ITEM_TIMEOUT_ACTION_UNSUPPORTED",
                "当前 Runtime 只执行 fail 超时动作。",
            )
        instance = self.db.get(SopInstance, work_item.instance_id)
        execution = self.db.get(SopNodeExecution, work_item.node_execution_id)
        if instance is None or instance.tenant_id != work_item.tenant_id:
            raise WorkItemError("WORK_ITEM_INSTANCE_NOT_FOUND", "工作项所属 SOP 实例不存在。")
        if (
            execution is None
            or execution.tenant_id != work_item.tenant_id
            or execution.instance_id != instance.id
        ):
            raise WorkItemError("WORK_ITEM_EXECUTION_NOT_FOUND", "工作项所属节点执行不存在。")
        if instance.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return
        with self._owned(instance):
            operation_id = str(work_item.payload_json.get("operation_id") or "")
            operation = self.db.get(SopOperation, operation_id) if operation_id else None
            if operation is not None:
                if (
                    operation.tenant_id != work_item.tenant_id
                    or operation.instance_id != instance.id
                    or operation.node_execution_id != execution.id
                ):
                    raise WorkItemError(
                        "WORK_ITEM_OPERATION_MISMATCH",
                        "工作项引用的操作不属于当前等待节点。",
                    )
                if operation.status == "prepared":
                    self.store.cancel_prepared_operation(operation)
                elif operation.status in {"running", "retry_wait", "unknown"}:
                    raise WorkItemError(
                        "WORK_ITEM_OPERATION_EFFECT_UNSETTLED",
                        "工作项到期时关联操作的效果尚未收敛。",
                    )
            timeout_error = {
                "code": "WORK_ITEM_TIMED_OUT",
                "work_item_id": work_item.id,
                "timeout_action": work_item.timeout_action,
            }
            self.store.timeout_node(instance, execution, error=timeout_error)
            self.store.timeout_instance(
                instance,
                context_patch={"work_item_timeout": timeout_error},
            )
            expired_request_ids = ApprovalRequestService(
                self.db
            ).expire_pending_for_work_item(work_item)
            self.db.add(
                AgentEvent(
                    tenant_id=instance.tenant_id,
                    session_id=instance.session_id,
                    event_type="sop_work_item_timed_out",
                    payload_json={
                        "instance_id": instance.id,
                        "node_execution_id": execution.id,
                        "work_item_id": work_item.id,
                        "timeout_action": work_item.timeout_action,
                        "expired_approval_request_ids": expired_request_ids,
                    },
                )
            )
            self.db.add(
                Message(
                    tenant_id=instance.tenant_id,
                    session_id=instance.session_id,
                    role="assistant",
                    content="您的申请因超过处理时限未完成，流程已终止，请重新发起或联系负责人。",
                    metadata_json={
                        "source": "runtime_control",
                        "render_policy": "verbatim",
                        "event_type": "sop_work_item_timed_out",
                        "instance_id": instance.id,
                        "work_item_id": work_item.id,
                        "error_code": "WORK_ITEM_TIMED_OUT",
                    },
                )
            )

    def _work_item_was_resumed(self, session_id: str, work_item_id: str) -> bool:
        """按会话事件确认完成信号是否已消费，防止重复命令再次推进和重复通知。"""

        events = self.db.exec(
            select(AgentEvent).where(
                AgentEvent.tenant_id == self._tenant_id_for_session(session_id),
                AgentEvent.session_id == session_id,
                AgentEvent.event_type == "sop_work_item_resumed",
            )
        ).all()
        return any(
            (event.payload_json or {}).get("work_item_id") == work_item_id for event in events
        )

    def _tenant_id_for_session(self, session_id: str) -> str:
        """为内部恢复幂等查询读取会话租户，并在数据损坏时确定性失败。"""

        chat_session = self.db.get(ChatSession, session_id)
        if chat_session is None:
            raise WorkItemError("WORK_ITEM_SESSION_NOT_FOUND", "工作项所属会话不存在。")
        return chat_session.tenant_id

    def _work_item_completion_message(
        self,
        work_item: SopWorkItem,
        plan: RuntimePlan,
        instance: SopInstance,
    ) -> str:
        """根据结构化结果生成不经过大模型改写的申请人状态通知。"""

        comment = (work_item.comment or "").strip()
        for raw_option in work_item.outcome_options_json or []:
            if not isinstance(raw_option, Mapping):
                continue
            if raw_option.get("value") != work_item.outcome:
                continue
            template = str(raw_option.get("completion_message") or "").strip()
            if template:
                return self._render_completion_template(template, comment, instance)
        if work_item.outcome == "rejected":
            return f"您的申请未通过审批。处理意见：{comment}" if comment else "您的申请未通过审批。"
        if work_item.outcome == "approved":
            return (
                "您的申请已审批通过，流程已完成。"
                if plan.action is RuntimeAction.COMPLETE
                else "您的申请已审批通过，流程已继续执行。"
            )
        if plan.action is RuntimeAction.COMPLETE:
            return "人工任务已处理，流程已完成。"
        return "人工任务已处理，流程已继续执行。"

    def _render_completion_template(
        self,
        template: str,
        comment: str,
        instance: SopInstance,
    ) -> str:
        """用人工意见和结构化工具回执标量填充版本冻结的完成通知模板。"""

        values: dict[str, str] = {
            "comment": comment,
            "business_status": "failed",
            "cert_id": "无",
            "grant_id": "无",
        }
        for tool_result in self._tool_results(instance).values():
            data = tool_result.get("data") if isinstance(tool_result, Mapping) else None
            if not isinstance(data, Mapping):
                continue
            values["business_status"] = str(
                data.get("status") or tool_result.get("status") or "unknown"
            )
            for key, value in data.items():
                if isinstance(value, str | int | float | bool):
                    values[str(key)] = str(value)
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

    def _advance_until_external_action(
        self,
        chat_session: ChatSession,
        instance: SopInstance,
        execution: SopNodeExecution,
        definition,
        *,
        current_already_completed: bool = False,
        execution_org_unit_id: str | None = None,
    ) -> RuntimePlan:
        """连续执行无副作用推进，直到等待、调用工具、完成或失败。"""

        current_execution = execution
        already_completed = current_already_completed
        while True:
            plan = plan_next_action(
                definition,
                current_node_id=current_execution.node_id,
                slots=instance.slots_json or {},
                tool_results=self._tool_results(instance),
                node_outputs=self._node_outputs(instance),
                work_items=self._work_item_result(instance, current_execution.node_id),
            )
            if plan.action is RuntimeAction.ADVANCE and plan.next_node_id:
                if not already_completed:
                    self.store.complete_node(
                        instance,
                        current_execution,
                        output={"next_node_id": plan.next_node_id},
                    )
                current_execution = self.store.enter_node(
                    instance,
                    plan.next_node_id,
                    input_snapshot=self._node_input_snapshot(instance),
                )
                chat_session.active_step_id = plan.next_node_id
                already_completed = False
                continue
            if plan.action is RuntimeAction.WAIT_INPUT:
                if current_execution.status == "running":
                    self.store.wait_for_input(
                        instance,
                        current_execution,
                        expected_inputs=plan.expected_inputs,
                    )
                return plan
            if plan.action is RuntimeAction.WAIT_WORK_ITEM:
                node = next(
                    (
                        item
                        for item in definition.nodes
                        if item.node_id == current_execution.node_id
                    ),
                    None,
                )
                if not isinstance(node, HumanTaskNode):
                    return RuntimePlan(
                        action=RuntimeAction.FAIL,
                        node_id=current_execution.node_id,
                        error_code="RUNTIME_HUMAN_TASK_DEFINITION_INVALID",
                    )
                try:
                    work_item, created = self.work_items.offer(
                        instance,
                        current_execution,
                        node.config,
                        initiator_user_id=chat_session.user_id,
                    )
                except WorkItemError as error:
                    self.store.fail_node(
                        instance,
                        current_execution,
                        error={"code": error.code, "message": str(error)},
                    )
                    self.store.fail_instance(
                        instance,
                        context_patch={
                            "work_item_error": {
                                "code": error.code,
                                "message": str(error),
                            }
                        },
                    )
                    return RuntimePlan(
                        action=RuntimeAction.FAIL,
                        node_id=current_execution.node_id,
                        error_code=error.code,
                    )
                if current_execution.status == "running":
                    self.store.wait_for_work_item(
                        instance,
                        current_execution,
                        work_item_id=work_item.id,
                    )
                if created:
                    self.db.add(
                        AgentEvent(
                            tenant_id=instance.tenant_id,
                            session_id=instance.session_id,
                            event_type="sop_work_item_offered",
                            payload_json={
                                "instance_id": instance.id,
                                "node_execution_id": current_execution.id,
                                "work_item_id": work_item.id,
                                "candidate_count": len(work_item.candidate_snapshot_json or []),
                                "completion_mode": work_item.completion_mode,
                            },
                        )
                    )
                if plan.control_reply:
                    plan = plan.model_copy(
                        update={
                            "control_reply": self._render_completion_template(
                                plan.control_reply,
                                "",
                                instance,
                            )
                        }
                    )
                return plan
            if plan.action is RuntimeAction.CALL_BUILTIN_INPUT and plan.operation_name:
                return self._execute_builtin_input(
                    chat_session,
                    instance,
                    current_execution,
                    definition,
                    plan,
                )
            if plan.action is RuntimeAction.CALL_TOOL and plan.operation_name:
                capability_snapshot, capability_snapshot_checksum, idempotency_policy = (
                    self._operation_capability_contract(
                        instance.tenant_id,
                        plan.operation_name,
                        chat_session.agent_id,
                    )
                )
                operation, _created = self.store.prepare_operation(
                    instance,
                    current_execution,
                    operation_name=plan.operation_name,
                    request=plan.operation_arguments,
                    effect_kind=self._operation_effect_kind(
                        instance.tenant_id,
                        plan.operation_name,
                    ),
                    idempotency_policy=idempotency_policy,
                    capability_snapshot=capability_snapshot,
                    capability_snapshot_checksum=capability_snapshot_checksum,
                )
                if operation.status == "prepared":
                    self.store.start_operation(operation)
                    return plan
                if operation.status in {"running", "retry_wait", "waiting_external", "unknown"}:
                    if operation.status == "running":
                        self.store.mark_stale_running_operation_unknown(
                            operation,
                            timeout_seconds=float(get_settings().tool_timeout_seconds),
                        )
                    elif operation.status == "unknown":
                        self.store._ensure_effect_reconciliation_attention(
                            instance,
                            operation,
                            actor_user_id=instance.initiator_user_id or "",
                        )
                    return self._wait_for_operation_plan(plan, operation)
                if operation.status in {"succeeded", "failed"}:
                    authorization_error = ToolExecutor(self.db).authorize(
                        tenant_id=instance.tenant_id,
                        tool_call=ToolCall(
                            name=operation.operation_name,
                            arguments=dict(operation.request_json or {}),
                        ),
                        active_skill_id=instance.skill_id,
                        agent_id=chat_session.agent_id,
                        actor_user_id=chat_session.user_id,
                        execution_org_unit_id=execution_org_unit_id,
                        remote_idempotency_key=operation.remote_idempotency_key,
                    )
                    if authorization_error is not None:
                        error = authorization_error.error
                        return RuntimePlan(
                            action=RuntimeAction.FAIL,
                            node_id=current_execution.node_id,
                            operation_name=operation.operation_name,
                            error_code=(
                                error.code if error is not None else "TOOL_AUTHORIZATION_REQUIRED"
                            ),
                            control_reply=(
                                error.message
                                if error is not None
                                else "当前执行人无权恢复该工具操作。"
                            ),
                        )
                    self._restore_operation_receipt(instance, current_execution, operation)
                    if current_execution.status == "running":
                        self.store.complete_node(
                            instance,
                            current_execution,
                            output={"operation_id": operation.id, "restored": True},
                        )
                    already_completed = True
                    continue
                return RuntimePlan(
                    action=RuntimeAction.FAIL,
                    node_id=current_execution.node_id,
                    error_code="RUNTIME_OPERATION_CANCELLED",
                )
            if plan.action is RuntimeAction.QUERY_KNOWLEDGE and plan.operation_name:
                operation, _created = self.store.prepare_operation(
                    instance,
                    current_execution,
                    operation_name=plan.operation_name,
                    request=plan.operation_arguments,
                    effect_kind="read",
                )
                if operation.status == "prepared":
                    self.store.start_operation(operation)
                    return plan
                if operation.status in {"running", "retry_wait", "waiting_external", "unknown"}:
                    return self._wait_for_operation_plan(plan, operation)
                if operation.status in {"succeeded", "failed"}:
                    self._restore_operation_receipt(instance, current_execution, operation)
                    if current_execution.status == "running":
                        self.store.complete_node(
                            instance,
                            current_execution,
                            output={"operation_id": operation.id, "restored": True},
                        )
                    already_completed = True
                    continue
                return RuntimePlan(
                    action=RuntimeAction.FAIL,
                    node_id=current_execution.node_id,
                    error_code="RUNTIME_OPERATION_CANCELLED",
                )
            if plan.action is RuntimeAction.COMPLETE:
                self._render_declared_artifacts(instance, current_execution)
                if not already_completed and current_execution.status == "running":
                    self.store.complete_node(
                        instance,
                        current_execution,
                        output={"outcome": plan.outcome or "completed"},
                    )
                if instance.status in ACTIVE_INSTANCE_STATUSES:
                    self.store.complete_instance(instance, slots=instance.slots_json)
                return plan
            return plan

    def _render_declared_artifacts(
        self,
        instance: SopInstance,
        source_node: SopNodeExecution,
    ) -> list[ExecutionArtifact]:
        """为正式SOP声明的交付物冻结结果并生成可恢复Artifact，重放保持幂等。"""

        version = self.db.get(SkillVersion, instance.skill_version_id)
        declarations = (
            (version.content_json or {}).get("expected_artifacts")
            if version is not None and version.tenant_id == instance.tenant_id
            else None
        )
        if not isinstance(declarations, list) or not declarations:
            return []
        from app.sop_runtime.execution_control import ExecutionControlService

        markdown = self._formal_sop_report_markdown(instance)
        result_body = {
            "status": "succeeded",
            "slots": dict(instance.slots_json or {}),
            "node_outputs": self._node_outputs(instance),
            "markdown": markdown,
        }
        result_row, _ = ExecutionControlService(self.db, self.store).ensure_terminal_result(
            instance,
            target_status="succeeded",
            result=result_body,
            verification={"passed": True, "source": "formal_sop_runtime"},
        )
        input_snapshot_ids = tuple(
            row.id
            for row in self.db.exec(
                select(InputResourceSnapshot).where(
                    InputResourceSnapshot.tenant_id == instance.tenant_id,
                    InputResourceSnapshot.execution_id == instance.id,
                )
            ).all()
        )
        renderer = ArtifactRendererService(self.db)
        worker_id = f"sop-renderer:{instance.id}"
        artifacts: list[ExecutionArtifact] = []
        for raw in declarations:
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("content_source") or "result.markdown") != "result.markdown":
                if raw.get("required", True) is True:
                    raise ArtifactRenderError("ARTIFACT_RENDER_SOURCE_UNSUPPORTED")
                continue
            job, _ = renderer.ensure_job(
                instance=instance,
                result_id=result_row.id,
                result_checksum=result_row.checksum,
                source_node=source_node,
                artifact_key=str(raw.get("artifact_key") or "").strip(),
                filename=str(raw.get("filename") or "").strip(),
                mime_type=str(raw.get("mime_type") or "").strip(),
                required=raw.get("required", True) is True,
            )
            if job.status != "ready":
                renderer.claim(job, worker_id=worker_id)
                artifact = renderer.render_and_publish(
                    job,
                    markdown=markdown,
                    worker_id=worker_id,
                    fencing_token=job.fencing_token,
                    input_snapshot_ids=input_snapshot_ids,
                )
            elif job.artifact_id:
                artifact = self.db.get(ExecutionArtifact, job.artifact_id)
                if artifact is None:
                    raise ArtifactRenderError("ARTIFACT_RENDER_ARTIFACT_MISSING")
            else:
                raise ArtifactRenderError("ARTIFACT_RENDER_JOB_INVALID")
            artifacts.append(artifact)
        return artifacts

    def _formal_sop_report_markdown(self, instance: SopInstance) -> str:
        """把确定性节点回执投影为不含公式和外链的稳定报告正文。"""

        lines = ["# 附件分析报告", "", f"执行编号：{instance.id}"]
        for result_key, raw in sorted(self._node_outputs(instance).items()):
            lines.extend(("", f"## {result_key}", str(raw)))
        return "\n".join(lines)

    @staticmethod
    def merge_plan(model_result: StepAgentResult, plan: RuntimePlan) -> StepAgentResult:
        """保留模型的输入理解和文案，但覆盖所有流程控制字段。"""

        if plan.action is RuntimeAction.CALL_TOOL and plan.operation_name:
            return model_result.model_copy(
                update={
                    "action": "call_tool",
                    "tool_call": ToolCall(
                        name=plan.operation_name, arguments=plan.operation_arguments
                    ),
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            )
        if plan.action is RuntimeAction.QUERY_KNOWLEDGE:
            arguments = plan.operation_arguments
            return model_result.model_copy(
                update={
                    "action": "query_knowledge",
                    "knowledge_query": KnowledgeQuery(
                        query=str(arguments.get("query") or ""),
                        query_type=str(arguments.get("query_type") or "answer"),
                        desired_evidence=(
                            str(arguments["desired_evidence"])
                            if arguments.get("desired_evidence") is not None
                            else None
                        ),
                        max_chunks=int(arguments.get("max_chunks") or 6),
                        max_depth=int(arguments.get("max_depth") or 2),
                    ),
                    "tool_call": None,
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            )
        if plan.action is RuntimeAction.WAIT_INPUT:
            result = model_result.model_copy(
                update={
                    "action": "ask_user",
                    "reply": plan.control_reply or model_result.reply,
                    "tool_call": None,
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            )
            return (
                result.mark_runtime_control_reply("EXPLICIT_CONFIRMATION_REQUIRED")
                if plan.control_reply
                else result
            )
        if plan.action is RuntimeAction.CALL_BUILTIN_INPUT:
            return model_result.model_copy(
                update={
                    "action": "reply",
                    "reply": "正在按已发布SOP读取并核验附件。",
                    "tool_call": None,
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            ).mark_runtime_control_reply("BUILTIN_INPUT_RUNNING")
        if plan.action is RuntimeAction.WAIT_WORK_ITEM:
            result = model_result.model_copy(
                update={
                    "action": "reply",
                    "reply": plan.control_reply or "当前流程正在等待有权限的处理人。",
                    "tool_call": None,
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            )
            return result.mark_runtime_control_reply("WORK_ITEM_WAITING")
        if plan.action is RuntimeAction.WAIT_OPERATION:
            result = model_result.model_copy(
                update={
                    "action": "reply",
                    "reply": plan.control_reply or "外部操作仍在处理中，请勿重复提交。",
                    "tool_call": None,
                    "knowledge_query": None,
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            )
            return result.mark_runtime_control_reply("OPERATION_RECONCILIATION_REQUIRED")
        if plan.action is RuntimeAction.COMPLETE:
            return model_result.model_copy(
                update={
                    "action": "reply",
                    "tool_call": None,
                    "next_step_id": None,
                    "is_step_completed": True,
                }
            )
        if plan.action is RuntimeAction.FAIL:
            return model_result.model_copy(
                update={
                    "action": "reply",
                    "reply": plan.control_reply
                    or f"流程无法继续（{plan.error_code or 'RUNTIME_ERROR'}）。",
                    "tool_call": None,
                    "next_step_id": None,
                    "is_step_completed": False,
                }
            ).mark_runtime_control_reply(plan.error_code or "RUNTIME_ERROR")
        return model_result

    def _latest_user_message_id(self, chat_session: ChatSession) -> str | None:
        """返回当前会话最新权威用户消息，SOP附件只能从其MessageLink绑定。"""

        message = self.db.exec(
            select(Message)
            .where(
                Message.tenant_id == chat_session.tenant_id,
                Message.session_id == chat_session.id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
        ).first()
        return message.id if message is not None else None

    def _resolve_input_time_context(
        self,
        chat_session: ChatSession,
        *,
        source_message_id: str | None,
        time_context: Mapping[str, object] | None,
    ) -> tuple[dict[str, str] | None, str | None, str | None]:
        """按 Message 或经归属校验的 ScheduledTaskRun 解析致因时间来源。"""

        normalized = normalize_persisted_time_context(time_context)
        if normalized is None:
            return None, None, None
        if source_message_id and normalized["source_input_id"] == source_message_id:
            return normalized, "chat", source_message_id
        run = self.db.get(ScheduledTaskRun, normalized["source_input_id"])
        if run is None or not self._scheduled_run_matches_session(run, chat_session):
            return None, None, None
        return normalized, "schedule", run.id

    @staticmethod
    def _scheduled_run_matches_session(
        run: ScheduledTaskRun,
        chat_session: ChatSession,
    ) -> bool:
        """拒绝跨租户、跨会话、跨 Agent/用户或非活动调度 Run 伪造时间来源。"""

        if run.source_kind not in {"schedule", "manual"}:
            return False
        if run.status not in {"queued", "running", "waiting"}:
            return False
        if (
            run.tenant_id != chat_session.tenant_id
            or run.session_id != chat_session.id
            or not chat_session.agent_id
            or not chat_session.user_id
            or run.agent_id != chat_session.agent_id
            or run.user_id != chat_session.user_id
        ):
            return False
        snapshot = run.source_snapshot_json if isinstance(run.source_snapshot_json, Mapping) else {}
        return all(
            snapshot.get(key) == value
            for key, value in (
                ("tenant_id", run.tenant_id),
                ("agent_id", run.agent_id),
                ("initiator_user_id", run.user_id),
            )
        )

    def _bind_attachment_slots(self, instance: SopInstance, definition, *, source_message_id: str | None) -> None:
        """按发布槽位格式和顺序冻结消息附件，缺失槽位保持现有WAIT_INPUT语义。"""

        slots = [
            slot
            for node in definition.nodes
            if isinstance(node, CollectInputNode)
            for slot in node.config.attachment_slots
        ]
        if not slots or source_message_id is None:
            return
        links = list(
            self.db.exec(
                select(MessageInputResourceLink)
                .where(
                    MessageInputResourceLink.tenant_id == instance.tenant_id,
                    MessageInputResourceLink.session_id == instance.session_id,
                    MessageInputResourceLink.message_id == source_message_id,
                )
                .order_by(MessageInputResourceLink.ordinal)
            ).all()
        )
        available = list(links)
        resolved_slots = dict(instance.slots_json or {})
        for slot in slots:
            existing = self.db.exec(
                select(MessageInputBindingLink)
                .where(
                    MessageInputBindingLink.tenant_id == instance.tenant_id,
                    MessageInputBindingLink.execution_id == instance.id,
                    MessageInputBindingLink.slot_key == slot.slot_key,
                )
                .order_by(MessageInputBindingLink.ordinal)
            ).all()
            if existing:
                resolved_slots[slot.slot_key] = [item.input_snapshot_id for item in existing]
                continue
            matching = []
            for link in list(available):
                resource = self.db.get(ManagedInputResource, link.resource_id)
                file_format = _attachment_format(resource.filename if resource else "")
                if (
                    resource is not None
                    and file_format in slot.allowed_formats
                    and self._resource_matches_required_columns(resource, slot.required_columns)
                ):
                    matching.append((link, resource))
                    available.remove(link)
                    if len(matching) >= slot.max_count:
                        break
            if len(matching) < slot.min_count:
                continue
            snapshot_ids: list[str] = []
            snapshot_handles: list[str] = []
            for ordinal, (link, resource) in enumerate(matching):
                snapshot, _ = self.store.snapshot_input_resource(
                    instance,
                    resource,
                    source_message_id=source_message_id,
                )
                self.db.add(
                    MessageInputBindingLink(
                        tenant_id=instance.tenant_id,
                        execution_id=instance.id,
                        definition_checksum=instance.definition_checksum or "",
                        slot_key=slot.slot_key,
                        ordinal=ordinal,
                        message_resource_link_id=link.id,
                        input_snapshot_id=snapshot.id,
                    )
                )
                snapshot_ids.append(snapshot.id)
                if snapshot.opaque_handle:
                    snapshot_handles.append(snapshot.opaque_handle)
            resolved_slots[slot.slot_key] = snapshot_handles
        instance.slots_json = resolved_slots
        self.db.add(instance)
        self.db.flush()

    def _resource_matches_required_columns(
        self,
        resource: ManagedInputResource,
        required_columns: tuple[str, ...],
    ) -> bool:
        """按当前发布Extraction表头验证typed slot关键列，缺失时保持WAIT_INPUT。"""

        if not required_columns:
            return True
        selected = self.db.exec(
            select(SelectedResourceExtraction).where(
                SelectedResourceExtraction.tenant_id == resource.tenant_id,
                SelectedResourceExtraction.resource_id == resource.id,
                SelectedResourceExtraction.resource_version == resource.version,
                SelectedResourceExtraction.profile_key == "default",
            )
        ).first()
        if selected is None:
            return False
        required = {column.strip().casefold() for column in required_columns}
        elements = self.db.exec(
            select(InputDocumentElement).where(
                InputDocumentElement.tenant_id == resource.tenant_id,
                InputDocumentElement.extraction_id == selected.extraction_id,
                InputDocumentElement.element_type == "table",
            )
        ).all()
        return any(
            required
            <= {
                str(column).strip().casefold()
                for column in (element.table_json or {}).get("columns", [])
            }
            for element in elements
        )

    def _execute_builtin_input(
        self,
        chat_session: ChatSession,
        instance: SopInstance,
        execution: SopNodeExecution,
        definition,
        plan: RuntimePlan,
    ) -> RuntimePlan:
        """在同一Execution内执行本地input能力并持久化Operation/Attempt，零模型外发。"""

        operation, _ = self.store.prepare_operation(
            instance,
            execution,
            operation_name=plan.operation_name or "input.read",
            request=plan.operation_arguments,
            effect_kind="read",
            idempotency_policy=IdempotencyPolicy(),
            capability_snapshot={"type": "builtin_input", "name": plan.operation_name},
            capability_snapshot_checksum=None,
        )
        if operation.status == "prepared":
            self.store.start_operation(operation)
        try:
            result = self._invoke_builtin_input(instance, plan)
        except InputBindingError as exc:
            self.store.finish_operation(operation, succeeded=False, error={"code": exc.code})
            self.store.fail_node(instance, execution, error={"code": exc.code})
            self.store.fail_instance(instance, context_patch={"input_error": exc.code})
            return RuntimePlan(action=RuntimeAction.FAIL, node_id=execution.node_id, error_code=exc.code)
        self.store.finish_operation(operation, succeeded=True, result=result)
        outputs = self._node_outputs(instance)
        outputs[str(plan.result_key)] = {
            "status": "succeeded",
            "data": result,
            "operation_id": operation.id,
        }
        instance.context_json = {**(instance.context_json or {}), "node_outputs": outputs}
        self.store.complete_node(instance, execution, output={"node_output": outputs[str(plan.result_key)]})
        return self._advance_until_external_action(
            chat_session,
            instance,
            execution,
            definition,
            current_already_completed=True,
        )

    def _invoke_builtin_input(self, instance: SopInstance, plan: RuntimePlan) -> dict[str, object]:
        """把已冻结SOP snapshot映射为不透明handle并调用共享Runtime。"""

        handles = plan.operation_arguments.get("snapshot_handles")
        if isinstance(handles, str):
            handles = [handles]
        if not isinstance(handles, list) or not handles:
            raise InputBindingError("ATTACHMENT_HANDLE_REQUIRED")
        runtime = TurnInputRuntimeService(self.db)
        payloads = []
        for handle in handles:
            operation_name = plan.operation_name or "input.read"
            if operation_name == "input.read":
                payload = runtime.read_execution(
                    str(handle),
                    tenant_id=instance.tenant_id,
                    execution_id=instance.id,
                )
            elif operation_name == "input.search":
                payload = runtime.search_execution(
                    str(handle),
                    tenant_id=instance.tenant_id,
                    execution_id=instance.id,
                    query=str(plan.operation_arguments.get("query") or ""),
                )
            elif operation_name == "input.table_profile":
                payload = runtime.table_profile_execution(
                    str(handle),
                    tenant_id=instance.tenant_id,
                    execution_id=instance.id,
                )
            elif operation_name == "table.compute":
                compute_ast = plan.operation_arguments.get("operation")
                if not isinstance(compute_ast, dict):
                    raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
                if compute_ast.get("op") == "verify_formula" and "element_id" not in compute_ast:
                    payload = runtime.table_compute_published_sop(
                        str(handle),
                        tenant_id=instance.tenant_id,
                        execution_id=instance.id,
                        operation=compute_ast,
                    )
                else:
                    payload = runtime.table_compute_execution(
                        str(handle),
                        tenant_id=instance.tenant_id,
                        execution_id=instance.id,
                        operation=compute_ast,
                    )
            else:
                raise InputBindingError("ATTACHMENT_BUILTIN_OPERATION_INVALID")
            payloads.append(payload)
        return {"items": payloads, "provider_dispatch_receipts": 0}
    def remote_idempotency_key_for(
        self,
        chat_session: ChatSession,
        operation_name: str,
    ) -> str | None:
        """读取当前确定性执行的远端幂等键，供 HTTP 适配器发送而不污染业务参数。"""

        instance = self._active_instance(chat_session)
        if instance is None:
            return None
        operation = self._running_operation(instance, operation_name)
        return operation.remote_idempotency_key if operation is not None else None

    def current_tool_operation(
        self,
        chat_session: ChatSession,
        tool_call: ToolCall,
    ) -> SopOperation | None:
        """按当前节点、动作名称和严格请求指纹读取唯一 Operation。"""

        instance = self._active_instance(chat_session)
        if instance is None:
            return None
        execution = self._current_execution(instance, instance.current_node_id or "")
        if execution is None:
            return None
        operations = self.db.exec(
            select(SopOperation)
            .join(
                SopOperationAttempt,
                SopOperationAttempt.operation_id == SopOperation.id,
            )
            .where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperationAttempt.tenant_id == instance.tenant_id,
                SopOperationAttempt.node_execution_id == execution.id,
                SopOperation.operation_name == tool_call.name,
            )
            .order_by(SopOperation.created_at.desc(), SopOperation.id.desc())
        ).all()
        if not operations:
            return None
        fingerprint = self.store.request_fingerprint(tool_call.arguments)
        exact = next(
            (operation for operation in operations if operation.request_fingerprint == fingerprint),
            None,
        )
        if exact is not None:
            return exact
        raise SopExecutionConflictError(
            "当前 SOP 节点已有同名 Operation，但请求指纹不一致，拒绝复用或重新派发。"
        )

    def operation_capability_is_current(
        self,
        chat_session: ChatSession,
        operation: SopOperation,
    ) -> bool:
        """比较 Operation 冻结能力快照与当前发布快照，防止能力漂移绕过再授权。"""

        if not operation.capability_checksum:
            return True
        snapshot, checksum, _policy = self._operation_capability_contract(
            operation.tenant_id,
            operation.operation_name,
            chat_session.agent_id,
        )
        return (
            checksum == operation.capability_checksum
            and snapshot == dict(operation.capability_snapshot_json or {})
        )

    def _operation_effect_kind(self, tenant_id: str, operation_name: str) -> str:
        """优先采用已发布风险契约，无契约 SOP 仍保持旧 GET/保守写兼容。"""

        if operation_name == "knowledge.search":
            return "read"
        tool = self.db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == operation_name)
        ).first()
        if tool is not None and tool.reliability_contract_json:
            try:
                contract = ToolReliabilityContract.model_validate(
                    tool.reliability_contract_json
                )
                return "read" if contract.risk_class == "read" else "external_write"
            except (TypeError, ValueError):
                return "external_write"
        if tool is not None and (tool.tool_type or "http") == "http" and tool.method.upper() == "GET":
            return "read"
        return "external_write"

    def _operation_capability_contract(
        self,
        tenant_id: str,
        operation_name: str,
        agent_id: str | None,
    ) -> tuple[dict[str, object], str | None, IdempotencyPolicy | None]:
        """冻结已发布工具契约，并将其远端幂等语义映射到可靠 Operation。"""

        tool = self.db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == operation_name)
        ).first()
        if tool is None:
            return {}, None, None
        snapshot = published_tool_snapshot(tool, agent_id or "")
        if snapshot is None:
            return {}, None, None
        contract = ToolReliabilityContract.model_validate(tool.reliability_contract_json)
        snapshot_payload = snapshot.model_dump(
            mode="json", exclude={"checksum", "agent_id"}
        )
        mode = contract.idempotency.mode
        if mode == "none":
            policy = IdempotencyPolicy(required=False)
        elif mode == "business_key":
            policy = IdempotencyPolicy(
                required=True,
                scope=IdempotencyScope.BUSINESS,
                key_fields=(str(contract.idempotency.argument),),
            )
        else:
            policy = IdempotencyPolicy(required=True, scope=IdempotencyScope.INSTANCE)
        return snapshot_payload, snapshot.checksum, policy

    @staticmethod
    def _is_ambiguous_external_failure(code: str, message: str) -> bool:
        """保守识别请求可能已到达远端但回执不确定的传输错误。"""

        if code in {
            "TIMEOUT",
            "EXECUTION_ERROR",
            "MCP_ERROR",
            "MCP_EXECUTION_ERROR",
            "TOOL_CALL_OUTCOME_UNKNOWN",
            "RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
        }:
            return True
        if code != "HTTP_ERROR":
            return False
        return any(f"{status}" in message for status in range(500, 600))

    @staticmethod
    def _wait_for_operation_plan(plan: RuntimePlan, operation: SopOperation) -> RuntimePlan:
        """把活动或效果未决逻辑动作转换为禁止重复 dispatch 的显式等待计划。"""

        unknown = operation.status == "unknown"
        retry_wait = operation.status == "retry_wait"
        waiting_external = operation.status == "waiting_external"
        return RuntimePlan(
            action=RuntimeAction.WAIT_OPERATION,
            node_id=plan.node_id,
            operation_name=operation.operation_name,
            error_code=(
                "RUNTIME_OPERATION_RECONCILIATION_REQUIRED"
                if unknown
                else "RUNTIME_OPERATION_RETRY_WAIT"
                if retry_wait
                else "RUNTIME_OPERATION_WAITING_EXTERNAL"
                if waiting_external
                else "RUNTIME_OPERATION_IN_PROGRESS"
            ),
            control_reply=(
                "外部操作结果尚未确认，系统已停止重复提交并等待对账。"
                if unknown
                else "操作正在等待下一次重试，系统不会重复提交。"
                if retry_wait
                else "外部任务已受理，系统将查询同一任务，不会重复提交。"
                if waiting_external
                else "外部操作仍在处理中，系统不会重复提交。"
            ),
        )

    def _restore_operation_receipt(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        operation: SopOperation,
    ) -> None:
        """从终态 Operation 重建崩溃前未写入上下文的回执，禁止再次调用适配器。"""

        result_key = self._operation_result_key(instance, execution.node_id)
        if not result_key:
            raise ValueError("终态 Operation 所在节点缺少 result_key，无法恢复。")
        receipt = {
            "status": operation.status,
            "data": dict(operation.result_json or {}),
            "error": dict(operation.error_json or {}) or None,
            "operation_id": operation.id,
        }
        if operation.operation_name == "knowledge.search":
            node_outputs = self._node_outputs(instance)
            node_outputs[result_key] = receipt
            instance.context_json = {
                **(instance.context_json or {}),
                "node_outputs": node_outputs,
            }
        else:
            tool_results = self._tool_results(instance)
            tool_results[result_key] = receipt
            instance.context_json = {
                **(instance.context_json or {}),
                "tool_results": tool_results,
            }
        self.db.add(instance)

    def _published_definition(self, skill: Skill):
        """按服务端可见投影的精确范围读取发布身份，禁止跨员工或整体猜测。"""

        branch_meta = getattr(skill, "agent_branch_meta", {})
        scope_agent_id = branch_meta.get("agent_id") or ""
        version = self.db.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == skill.tenant_id,
                SkillVersion.skill_id == skill.skill_id,
                SkillVersion.scope_agent_id == scope_agent_id,
                SkillVersion.version == skill.version,
                SkillVersion.status == "published",
            )
        ).first()
        if version is None:
            raise ValueError("deterministic SOP 缺少对应的不可变发布版本")
        if skill_content_checksum(version.content_json) != skill_content_checksum(skill.content_json):
            raise ValueError("deterministic SOP 的活动头与精确发布快照不一致")
        definition = compile_legacy_skill_card(version.content_json)
        if (
            version.compiled_definition_checksum
            and version.compiled_definition_checksum != definition.checksum
        ):
            raise ValueError("deterministic SOP 的发布版本 checksum 不一致")
        if definition.diagnostics:
            raise ValueError("deterministic SOP 仍包含兼容警告，禁止进入新 Runtime")
        unsupported = DEFAULT_CAPABILITY_REGISTRY.non_executable_nodes(definition)
        if unsupported:
            raise ValueError(f"deterministic SOP 包含不可执行能力：{unsupported}")
        return version, definition

    def _record_identity_failure(
        self,
        chat_session: ChatSession,
        skill: Skill,
        version: SkillVersion,
        definition,
        model_result: StepAgentResult,
        error: SopIdentityContextError,
        *,
        time_context: Mapping[str, object] | None = None,
    ) -> StepAgentResult:
        """把身份校验失败写入实例和节点事实，并返回稳定用户提示。"""

        identity_error = {
            **error.audit_context,
            "error_code": error.code,
        }
        sanitized_slots = sanitize_identity_slots_after_failure(
            definition,
            chat_session.slots_json or {},
            error.audit_context,
        )
        rejected_identity_slots = {
            key
            for key in (chat_session.slots_json or {})
            if sanitized_slots.get(key) != (chat_session.slots_json or {}).get(key)
        }
        chat_session.slots_json = sanitized_slots
        instance, _ = self.store.start_instance(
            tenant_id=chat_session.tenant_id,
            session_id=chat_session.id,
            skill_id=skill.skill_id,
            skill_version_id=version.id,
            skill_version=version.version,
            definition_checksum=definition.checksum,
            start_node_id=definition.start_node_id,
            agent_id=chat_session.agent_id,
            slots=sanitized_slots,
            context=self._instance_context(
                identity=identity_error,
                time_context=time_context,
            ),
            # 身份失败也要沿用同一条兼容边界：Agent-scoped 会话受生命周期
            # 门禁保护，历史无 Agent 会话不因新增门禁而无法收口失败事实。
            enforce_agent_lifecycle=chat_session.agent_id is not None,
        )
        with self._owned(instance):
            instance.context_json = {
                **(instance.context_json or {}),
                "identity": identity_error,
            }
            if time_context is not None:
                instance.context_json = {
                    **(instance.context_json or {}),
                    "time_context": dict(time_context),
                }
            execution = self._current_execution(
                instance,
                instance.current_node_id or definition.start_node_id,
            )
            if execution is None:
                execution = self.store.enter_node(
                    instance,
                    instance.current_node_id or definition.start_node_id,
                    input_snapshot=self._node_input_snapshot(instance),
                )
            self.store.fail_node(
                instance,
                execution,
                error={"code": error.code, "context": error.audit_context},
            )
            self.store.fail_instance(
                instance,
                context_patch={"identity": identity_error},
            )
        result = model_result.model_copy(
            update={
                "action": "reply",
                "reply": error.user_message,
                "slot_updates": {
                    key: value
                    for key, value in dict(model_result.slot_updates or {}).items()
                    if key not in rejected_identity_slots
                },
                "tool_call": None,
                "next_step_id": None,
                "is_step_completed": False,
                "handoff": False,
            }
        )
        return result.mark_runtime_execution(instance.id).mark_runtime_control_reply(error.code)

    def _definition_for_instance(self, instance: SopInstance):
        """按实例绑定的版本 ID 读取定义，防止运行中漂移到最新版本。"""

        version = self.db.get(SkillVersion, instance.skill_version_id)
        if version is None or version.tenant_id != instance.tenant_id:
            raise ValueError("SOP 实例绑定的技能版本不存在")
        definition = compile_legacy_skill_card(version.content_json)
        if definition.checksum != instance.definition_checksum:
            raise ValueError("SOP 实例绑定的定义 checksum 已漂移")
        return version, definition

    def _active_instance(self, chat_session: ChatSession) -> SopInstance | None:
        """在当前租户和会话内查询活动确定性实例。"""

        return self.db.exec(
            select(SopInstance).where(
                SopInstance.tenant_id == chat_session.tenant_id,
                SopInstance.session_id == chat_session.id,
                SopInstance.status.in_(ACTIVE_INSTANCE_STATUSES),
            )
        ).first()

    def _current_execution(self, instance: SopInstance, node_id: str) -> SopNodeExecution | None:
        """读取节点最近一次尚未终结的 attempt。"""

        return self.db.exec(
            select(SopNodeExecution)
            .where(
                SopNodeExecution.tenant_id == instance.tenant_id,
                SopNodeExecution.instance_id == instance.id,
                SopNodeExecution.node_id == node_id,
                SopNodeExecution.status.in_(("scheduled", "running", "waiting")),
            )
            .order_by(SopNodeExecution.attempt.desc())
        ).first()

    def _operation_result_key(self, instance: SopInstance, node_id: str) -> str | None:
        """从实例冻结定义读取工具节点的稳定回执字段名。"""

        _, definition = self._definition_for_instance(instance)
        node = next((item for item in definition.nodes if item.node_id == node_id), None)
        config = getattr(node, "config", None)
        return getattr(config, "result_key", None)

    @staticmethod
    def _tool_results(instance: SopInstance) -> dict[str, object]:
        """复制实例上下文中的工具回执，避免原位修改 JSON 脏检查失效。"""

        value = (instance.context_json or {}).get("tool_results")
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _node_outputs(instance: SopInstance) -> dict[str, object]:
        """复制实例中的通用节点回执，供知识任务和确定性条件安全消费。"""

        value = (instance.context_json or {}).get("node_outputs")
        return dict(value) if isinstance(value, Mapping) else {}

    def _work_item_result(
        self,
        instance: SopInstance,
        node_id: str,
    ) -> dict[str, object]:
        """读取当前节点工作项的结构化回执，供调度器确定性等待或选择分支。"""

        work_item = self.db.exec(
            select(SopWorkItem)
            .where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.node_id == node_id,
            )
            .order_by(SopWorkItem.created_at.desc())
        ).first()
        if work_item is None:
            return {}
        return {
            "work_item_id": work_item.id,
            "status": work_item.status,
            "outcome": work_item.outcome,
            "revision": work_item.revision,
        }

    @staticmethod
    def _instance_time_context(instance: SopInstance | None) -> dict[str, str] | None:
        """从已持久执行实例读取并校验其致因输入时间，拒绝损坏侧带。"""

        if instance is None:
            return None
        context = instance.context_json or {}
        return normalize_persisted_time_context(context.get("time_context"))

    @staticmethod
    def _instance_context(
        *,
        identity: Mapping[str, object] | None = None,
        time_context: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        """构造 SOP 实例初始上下文，只写入已验证的身份与输入时间事实。"""

        context: dict[str, object] = {}
        if identity:
            context["identity"] = dict(identity)
        normalized_time_context = normalize_persisted_time_context(time_context)
        if normalized_time_context is not None:
            context["time_context"] = normalized_time_context
        return context or None

    @staticmethod
    def _node_input_snapshot(instance: SopInstance) -> dict[str, object]:
        """构造同时包含业务槽位和身份来源的节点输入审计快照。"""

        snapshot: dict[str, object] = {"slots": dict(instance.slots_json or {})}
        identity = (instance.context_json or {}).get("identity")
        if isinstance(identity, Mapping) and identity:
            snapshot["identity"] = dict(identity)
        time_context = normalize_persisted_time_context(
            (instance.context_json or {}).get("time_context")
        )
        if time_context is not None:
            snapshot["time_context"] = time_context
        return snapshot


def _attachment_format(filename: str) -> str:
    """把受管文件扩展名映射为发布期附件格式枚举。"""

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return "image" if suffix in {"png", "jpg", "jpeg", "webp", "gif"} else suffix
