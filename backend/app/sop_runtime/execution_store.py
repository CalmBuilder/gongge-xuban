"""
@Time       : 2026/07/22 12:45
@Author     : zhanglp8181
@File       : execution_store.py
@CallChain  : Agent Loop/Runtime Scheduler → SopExecutionStore → SQLModel 执行聚合
@Description: 持久化统一执行聚合，以数据库租约和 fencing guard 保护全部权威状态写。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, update
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session, select

from app.db.models import (
    ExecutionMutationRejection,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    utc_now,
)
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

    def enter_node(
        self,
        instance: SopInstance,
        node_id: str,
        *,
        input_snapshot: Mapping[str, object] | None = None,
    ) -> SopNodeExecution:
        """为节点创建新的 attempt 并从 scheduled 确定性推进到 running。"""

        self._assert_instance_tenant(instance)
        self._guard_mutation(instance, "node.enter")
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
    ) -> tuple[SopOperation, bool]:
        """以稳定命令摘要准备工具操作，相同请求只返回同一条操作记录。"""

        self._assert_execution_owner(instance, execution)
        self._guard_mutation(instance, "operation.prepare")
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

        self._guard_operation_mutation(operation, "operation.start")
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
        self.db.add(operation)
        self.db.flush()

    def complete_instance(
        self,
        instance: SopInstance,
        *,
        slots: Mapping[str, object] | None = None,
    ) -> None:
        """成功结束 SOP 实例并保留最终槽位快照和结束时间。"""

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
        self.db.flush()

    def fail_instance(
        self,
        instance: SopInstance,
        *,
        context_patch: Mapping[str, object] | None = None,
    ) -> None:
        """失败结束 SOP 实例，并把确定性失败上下文合并到实例快照。"""

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
        self.db.flush()

    def timeout_instance(
        self,
        instance: SopInstance,
        *,
        context_patch: Mapping[str, object] | None = None,
    ) -> None:
        """把活动 SOP 实例推进为 timed_out，并冻结超时上下文。"""

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
