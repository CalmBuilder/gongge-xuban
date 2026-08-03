"""
@Time       : 2026/07/22 12:45
@Author     : zhanglp8181
@File       : execution_store.py
@CallChain  : Agent Loop/Runtime Scheduler → SopExecutionStore → SQLModel 执行聚合
@Description: 持久化 SOP 实例、节点 attempt 和具有幂等语义的外部操作回执。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from sqlalchemy import func
from sqlmodel import Session, select

from app.db.models import SopInstance, SopNodeExecution, SopOperation, utc_now
from app.sop_runtime.contracts import (
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


class SopExecutionStore:
    """以统一状态机和租户边界维护可恢复 SOP 执行聚合。"""

    def __init__(self, db: Session) -> None:
        """绑定当前数据库事务；提交和回滚仍由应用服务控制。"""

        self.db = db

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

    def enter_node(
        self,
        instance: SopInstance,
        node_id: str,
        *,
        input_snapshot: Mapping[str, object] | None = None,
    ) -> SopNodeExecution:
        """为节点创建新的 attempt 并从 scheduled 确定性推进到 running。"""

        self._assert_instance_tenant(instance)
        attempt = self._next_node_attempt(instance.tenant_id, instance.id, node_id)
        execution = SopNodeExecution(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_id=node_id,
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

    def wait_for_input(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        expected_inputs: tuple[str, ...],
    ) -> None:
        """将输入节点和实例一起暂停，并冻结当前待补字段。"""

        self._assert_execution_owner(instance, execution)
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

    def resume_waiting_node(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        slots: Mapping[str, object],
    ) -> None:
        """用新的槽位快照恢复暂停节点，不创建重复 attempt。"""

        self._assert_execution_owner(instance, execution)
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

    def complete_node(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        *,
        output: Mapping[str, object] | None = None,
    ) -> None:
        """成功结束当前节点 attempt，并保存不可变语义的输出快照。"""

        self._assert_execution_owner(instance, execution)
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
    ) -> tuple[SopOperation, bool]:
        """以稳定命令摘要准备工具操作，相同请求只返回同一条操作记录。"""

        self._assert_execution_owner(instance, execution)
        idempotency_key = self.operation_idempotency_key(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=execution.id,
            operation_name=operation_name,
            request=request,
        )
        existing = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.idempotency_key == idempotency_key,
            )
        ).first()
        if existing is not None:
            return existing, False
        operation = SopOperation(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=execution.id,
            operation_name=operation_name,
            idempotency_key=idempotency_key,
            request_json=dict(request),
        )
        self.db.add(operation)
        self.db.flush()
        return operation, True

    def start_operation(self, operation: SopOperation) -> None:
        """在真正调用外部工具前把 prepared 操作推进为 running。"""

        transition = transition_operation(
            OperationStatus(operation.status),
            OperationStatus.RUNNING,
            actual_revision=operation.revision,
        )
        self._apply_operation_transition(operation, transition)
        operation.started_at = utc_now()
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
        self.db.add(operation)
        self.db.flush()

    def complete_instance(
        self,
        instance: SopInstance,
        *,
        slots: Mapping[str, object] | None = None,
    ) -> None:
        """成功结束 SOP 实例并保留最终槽位快照和结束时间。"""

        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.SUCCEEDED,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        if slots is not None:
            instance.slots_json = dict(slots)
        instance.completed_at = utc_now()
        self.db.add(instance)
        self.db.flush()

    def fail_instance(
        self,
        instance: SopInstance,
        *,
        context_patch: Mapping[str, object] | None = None,
    ) -> None:
        """失败结束 SOP 实例，并把确定性失败上下文合并到实例快照。"""

        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.FAILED,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        if context_patch:
            instance.context_json = {
                **(instance.context_json or {}),
                **dict(context_patch),
            }
        instance.completed_at = utc_now()
        self.db.add(instance)
        self.db.flush()

    def timeout_instance(
        self,
        instance: SopInstance,
        *,
        context_patch: Mapping[str, object] | None = None,
    ) -> None:
        """把活动 SOP 实例推进为 timed_out，并冻结超时上下文。"""

        transition = transition_instance(
            SopInstanceStatus(instance.status),
            SopInstanceStatus.TIMED_OUT,
            actual_revision=instance.revision,
        )
        self._apply_instance_transition(instance, transition)
        if context_patch:
            instance.context_json = {
                **(instance.context_json or {}),
                **dict(context_patch),
            }
        instance.completed_at = utc_now()
        self.db.add(instance)
        self.db.flush()

    @staticmethod
    def operation_idempotency_key(
        *,
        tenant_id: str,
        instance_id: str,
        node_execution_id: str,
        operation_name: str,
        request: Mapping[str, object],
    ) -> str:
        """对操作身份和规范请求 JSON 计算跨进程稳定的 SHA-256 幂等键。"""

        payload = {
            "tenant_id": tenant_id,
            "instance_id": instance_id,
            "node_execution_id": node_execution_id,
            "operation_name": operation_name,
            "request": dict(request),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

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

    def _next_node_attempt(self, tenant_id: str, instance_id: str, node_id: str) -> int:
        """计算节点重试的新 attempt 序号，历史执行记录不会被覆盖。"""

        latest = self.db.exec(
            select(func.max(SopNodeExecution.attempt)).where(
                SopNodeExecution.tenant_id == tenant_id,
                SopNodeExecution.instance_id == instance_id,
                SopNodeExecution.node_id == node_id,
            )
        ).one()
        return int(latest or 0) + 1

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
