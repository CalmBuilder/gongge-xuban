"""
@Time       : 2026/08/03 18:30
@Author     : zhanglp8181
@File       : execution_control.py
@CallChain  : Attention/Execution API/Workers → ExecutionControlService → Execution Store/SQLModel
@Description: 管理统一 Attention、命令、信号、事件、结果、发布和终态闭合契约。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlmodel import Session, select

from app.db.models import (
    AgentEvent,
    EventOutbox,
    ExecutionCommand,
    ExecutionPlanRevision,
    ExecutionPublication,
    ExecutionResult,
    ExecutionSignal,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    SopWorkItemCandidate,
    SopWorkItemCommandReceipt,
    utc_now,
)
from app.sop_runtime.contracts import NodeExecutionStatus, SopInstanceStatus, WorkItemStatus
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore


ACTIVE_ATTENTION_STATUSES = (WorkItemStatus.OFFERED.value, WorkItemStatus.CLAIMED.value)
ACTIVE_OPERATION_STATUSES = ("prepared", "running", "unknown")
ACTIVE_COMMAND_STATUSES = ("pending", "claimed")
TERMINAL_SIGNAL_STATUSES = ("consumed", "dead_letter", "discarded")


class ExecutionControlError(ValueError):
    """表示统一控制平面的稳定业务拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        """保存适合 API 映射和回归断言的错误码。"""

        self.code = code
        super().__init__(message)


def canonical_checksum(value: object) -> str:
    """对严格 JSON 值生成稳定 SHA-256，作为命令、事件和结果身份。"""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionControlError("CONTROL_PAYLOAD_INVALID", "控制载荷必须是严格 JSON。") from exc
    return hashlib.sha256(encoded).hexdigest()


def attention_identity(*, tenant_id: str, execution_id: str, attention_key: str) -> str:
    """从 tenant、Execution 和独立等待步骤键派生不可猜测的稳定 Attention 身份。"""

    if not attention_key.strip():
        raise ExecutionControlError("ATTENTION_KEY_REQUIRED", "Attention key 不能为空。")
    return canonical_checksum(
        {
            "tenant_id": tenant_id,
            "execution_id": execution_id,
            "attention_key": attention_key,
        }
    )


class ExecutionControlService:
    """在调用方事务中原子维护控制命令、恢复信号、领域事件和结果发布。"""

    def __init__(self, db: Session, store: SopExecutionStore | None = None) -> None:
        """绑定数据库事务，并复用或创建统一 Execution Store。"""

        self.db = db
        self.store = store or SopExecutionStore(db)

    def offer_attention(
        self,
        instance: SopInstance,
        *,
        attention_kind: str,
        attention_key: str,
        title: str,
        payload: Mapping[str, object],
        allowed_commands: Sequence[str],
        candidate_user_ids: Sequence[str],
        source_type: str = "runtime",
        source_ref: str | None = None,
        required: bool = True,
        node_execution: SopNodeExecution | None = None,
    ) -> tuple[SopWorkItem, bool]:
        """在 execution 写屏障内按稳定身份幂等创建一条 typed Attention。"""

        self._assert_instance(instance)
        identity = attention_identity(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            attention_key=attention_key,
        )
        existing = self.db.exec(
            select(SopWorkItem).where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.attention_identity == identity,
            )
        ).first()
        if existing is not None:
            if existing.attention_kind != attention_kind or existing.payload_json != dict(payload):
                raise ExecutionControlError(
                    "ATTENTION_IDENTITY_CONFLICT",
                    "相同 Attention 身份不能改写 kind 或 payload。",
                )
            return existing, False
        if attention_kind == "sop_human_task":
            raise ExecutionControlError(
                "ATTENTION_FORMAL_SOP_SERVICE_REQUIRED",
                "正式 SOP 人工任务必须通过 SopWorkItemService 创建。",
            )
        candidates = sorted({item.strip() for item in candidate_user_ids if item.strip()})
        if not candidates:
            raise ExecutionControlError("ATTENTION_NO_CANDIDATE", "Attention 必须有服务端候选人。")
        commands = list(dict.fromkeys(item.strip() for item in allowed_commands if item.strip()))
        if not commands:
            raise ExecutionControlError("ATTENTION_NO_COMMAND", "Attention 必须声明允许命令。")
        self.store.authorize_mutation(instance, "attention.offer")
        now = self.store.database_now()
        attention = SopWorkItem(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=node_execution.id if node_execution else None,
            skill_version_id=instance.skill_version_id if node_execution else None,
            node_id=node_execution.node_id if node_execution else None,
            attention_kind=attention_kind,
            attention_key=attention_key,
            attention_identity=identity,
            title=title,
            source_type=source_type,
            source_ref=source_ref,
            payload_json=dict(payload),
            allowed_commands_json=commands,
            candidate_snapshot_json=[{"user_id": item} for item in candidates],
            required=required,
            allowed_outcomes_json=commands,
            initiator_user_id=instance.initiator_user_id,
            exclude_initiator=False,
            created_at=now,
            updated_at=now,
        )
        self.db.add(attention)
        self.db.flush()
        for user_id in candidates:
            self.db.add(
                SopWorkItemCandidate(
                    tenant_id=instance.tenant_id,
                    work_item_id=attention.id,
                    user_id=user_id,
                    source_types_json=["attention_contract"],
                )
            )
        self._append_event(
            instance,
            event_type="attention_offered",
            causation_id=attention.id,
            payload={
                "attention_id": attention.id,
                "attention_kind": attention.attention_kind,
                "attention_identity": attention.attention_identity,
            },
        )
        self.db.flush()
        return attention, True

    def issue_command(
        self,
        instance: SopInstance,
        *,
        command_id: str,
        command_type: str,
        actor_user_id: str,
        expected_execution_revision: int,
        payload: Mapping[str, object] | None = None,
        source_type: str = "api",
        source_message_id: str | None = None,
    ) -> tuple[ExecutionCommand, bool]:
        """以 tenant+command_id 幂等登记命令，并在同一事务写领域事件和 signal。"""

        self._assert_instance(instance)
        if command_type not in {"cancel", "steer"}:
            raise ExecutionControlError("COMMAND_TYPE_INVALID", "仅支持 cancel 或 steer 命令。")
        body = dict(payload or {})
        checksum = canonical_checksum(body)
        existing = self.db.exec(
            select(ExecutionCommand).where(
                ExecutionCommand.tenant_id == instance.tenant_id,
                ExecutionCommand.command_id == command_id,
            )
        ).first()
        if existing is not None:
            if (
                existing.execution_id != instance.id
                or existing.command_type != command_type
                or existing.actor_user_id != actor_user_id
                or existing.payload_checksum != checksum
            ):
                raise ExecutionControlError(
                    "COMMAND_IDEMPOTENCY_CONFLICT",
                    "command_id 已绑定不同命令语义。",
                )
            return existing, False
        if instance.revision != expected_execution_revision:
            raise ExecutionControlError(
                "EXECUTION_REVISION_CONFLICT",
                f"Execution revision 已从 {expected_execution_revision} 变为 {instance.revision}。",
            )
        command = ExecutionCommand(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            command_id=command_id,
            command_type=command_type,
            actor_user_id=actor_user_id,
            source_type=source_type,
            source_message_id=source_message_id,
            expected_execution_revision=expected_execution_revision,
            payload_json=body,
            payload_checksum=checksum,
        )
        self.db.add(command)
        self.db.flush()
        signal = self.enqueue_signal(
            instance,
            signal_type="command",
            causation_type="execution_command",
            causation_id=command.id,
            payload={"command_id": command.command_id, "command_type": command.command_type},
        )
        self._append_event(
            instance,
            event_type="execution_command_issued",
            causation_id=command.id,
            payload={
                "command_id": command.command_id,
                "command_type": command.command_type,
                "signal_id": signal.id,
            },
        )
        self.db.flush()
        return command, True

    def resolve_attention(
        self,
        instance: SopInstance,
        attention: SopWorkItem,
        *,
        actor_user_id: str,
        command_id: str,
        command: str,
        expected_revision: int,
        comment: str | None = None,
    ) -> tuple[SopWorkItem, bool]:
        """在 execution 写屏障内办理 Attention，并原子追加领域事件与 signal。"""

        if attention.instance_id != instance.id or attention.tenant_id != instance.tenant_id:
            raise ExecutionControlError("ATTENTION_EXECUTION_MISMATCH", "Attention 不属于当前 Execution。")
        allowed_commands = set(attention.allowed_commands_json or [])
        command_allowed = command in allowed_commands or (
            attention.attention_kind == "sop_human_task"
            and "complete" in allowed_commands
            and command in set(attention.allowed_outcomes_json or [])
        )
        if not command_allowed:
            raise ExecutionControlError("ATTENTION_COMMAND_FORBIDDEN", "命令不在 Attention 允许集合中。")
        from app.sop_runtime.work_items import SopWorkItemService

        self.store.authorize_mutation(instance, "attention.resolve")
        resolved, completed = SopWorkItemService(self.db).complete(
            attention,
            actor_user_id=actor_user_id,
            command_id=command_id,
            outcome=command,
            comment=comment,
            expected_revision=expected_revision,
        )
        if completed:
            resolved.resolution_json = {
                "command": command,
                "actor_user_id": actor_user_id,
                "comment": comment,
                "revision": resolved.revision,
            }
            self.db.add(resolved)
            causation_id = f"{resolved.id}:{resolved.revision}"
            signal = self.enqueue_signal(
                instance,
                signal_type="attention_decided",
                causation_type="attention_resolution",
                causation_id=causation_id,
                payload={
                    "attention_id": resolved.id,
                    "attention_kind": resolved.attention_kind,
                    "command": command,
                    "revision": resolved.revision,
                },
            )
            self._append_event(
                instance,
                event_type="attention_decided",
                causation_id=causation_id,
                payload={
                    "attention_id": resolved.id,
                    "command": command,
                    "revision": resolved.revision,
                    "signal_id": signal.id,
                },
            )
        self.db.flush()
        return resolved, completed

    def replayed_attention_resolution(
        self,
        attention: SopWorkItem,
        *,
        actor_user_id: str,
        command_id: str,
        command: str,
    ) -> tuple[SopWorkItem, bool] | None:
        """在抢 Execution lease 前识别稳定回执，使终态后的网络重试仍然幂等。"""

        receipt = self.db.exec(
            select(SopWorkItemCommandReceipt).where(
                SopWorkItemCommandReceipt.tenant_id == attention.tenant_id,
                SopWorkItemCommandReceipt.command_id == command_id,
            )
        ).first()
        if receipt is None:
            return None
        recorded_outcome = str(receipt.result_json.get("outcome") or "")
        if (
            receipt.work_item_id != attention.id
            or receipt.command_type != "complete"
            or receipt.actor_user_id != actor_user_id
            or recorded_outcome != command
        ):
            raise ExecutionControlError(
                "ATTENTION_COMMAND_ID_REUSED",
                "command_id 已绑定不同 Attention、actor 或 resolution。",
            )
        return attention, bool(receipt.result_json.get("completed"))

    def enqueue_signal(
        self,
        instance: SopInstance,
        *,
        signal_type: str,
        causation_type: str,
        causation_id: str,
        payload: Mapping[str, object] | None = None,
        priority: int = 0,
        max_attempts: int = 8,
    ) -> ExecutionSignal:
        """按因果事实去重写入恢复信号；signal 本身不授予 execution 推进权。"""

        self._assert_instance(instance)
        body = dict(payload or {})
        dedupe_key = canonical_checksum(
            {
                "tenant_id": instance.tenant_id,
                "execution_id": instance.id,
                "signal_type": signal_type,
                "causation_type": causation_type,
                "causation_id": causation_id,
            }
        )
        existing = self.db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.tenant_id == instance.tenant_id,
                ExecutionSignal.dedupe_key == dedupe_key,
            )
        ).first()
        if existing is not None:
            if existing.payload_checksum != canonical_checksum(body):
                raise ExecutionControlError(
                    "SIGNAL_DEDUPE_CONFLICT",
                    "相同 signal 因果键不能改写 payload。",
                )
            return existing
        now = self.store.database_now()
        signal = ExecutionSignal(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            signal_type=signal_type,
            dedupe_key=dedupe_key,
            causation_type=causation_type,
            causation_id=causation_id,
            payload_json=body,
            payload_checksum=canonical_checksum(body),
            priority=priority,
            max_attempts=max_attempts,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        self.db.add(signal)
        self.db.flush()
        return signal

    def claim_signal(
        self,
        signal: ExecutionSignal,
        *,
        worker_id: str,
        ttl_seconds: int = 30,
    ) -> ExecutionSignal:
        """以数据库时间 CAS 认领待处理或租约过期信号，但不触碰 Execution lease。"""

        if ttl_seconds < 1 or not worker_id.strip():
            raise ExecutionControlError("SIGNAL_LEASE_INVALID", "signal worker 和 TTL 必须有效。")
        now = self.store.database_now()
        result = self.db.exec(
            update(ExecutionSignal)
            .where(
                ExecutionSignal.id == signal.id,
                ExecutionSignal.tenant_id == signal.tenant_id,
                ExecutionSignal.available_at <= now,
                or_(
                    ExecutionSignal.status == "pending",
                    (ExecutionSignal.status == "claimed")
                    & (ExecutionSignal.lease_expires_at <= now),
                ),
            )
            .values(
                status="claimed",
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                claimed_at=now,
                attempt_count=ExecutionSignal.attempt_count + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.expire(signal)
        self.db.refresh(signal)
        if result.rowcount != 1:
            raise ExecutionControlError("SIGNAL_ALREADY_CLAIMED", "signal 已被其他 worker 处理。")
        return signal

    def consume_signal(
        self,
        instance: SopInstance,
        signal: ExecutionSignal,
        *,
        worker_id: str,
    ) -> str:
        """在 execution lease 内消费 signal；取消请求后普通 signal 只会被丢弃。"""

        self._assert_signal_owner(instance, signal, worker_id)
        self.store.authorize_mutation(instance, "signal.consume")
        now = self.store.database_now()
        if instance.cancellation_requested_at is not None and signal.signal_type != "command":
            status = "discarded"
        else:
            status = "consumed"
        signal.status = status
        signal.lease_owner = None
        signal.lease_expires_at = None
        signal.consumed_at = now
        signal.updated_at = now
        self.db.add(signal)
        self.db.flush()
        return status

    def retry_signal(
        self,
        instance: SopInstance,
        signal: ExecutionSignal,
        *,
        worker_id: str,
        error: Mapping[str, object],
        base_delay_seconds: int = 2,
    ) -> str:
        """在 execution 写屏障内指数退避或死信，保证崩溃恢复不会无限热循环。"""

        self._assert_signal_owner(instance, signal, worker_id)
        self.store.authorize_mutation(instance, "signal.retry")
        now = self.store.database_now()
        dead = signal.attempt_count >= signal.max_attempts
        signal.status = "dead_letter" if dead else "pending"
        signal.available_at = now + timedelta(
            seconds=0 if dead else min(base_delay_seconds * 2 ** (signal.attempt_count - 1), 3600)
        )
        signal.lease_owner = None
        signal.lease_expires_at = None
        signal.last_error_json = dict(error)
        signal.updated_at = now
        self.db.add(signal)
        self.db.flush()
        return signal.status

    def claim_outbox(
        self,
        outbox: EventOutbox,
        *,
        worker_id: str,
        ttl_seconds: int = 30,
    ) -> EventOutbox:
        """以数据库时间认领待投递或崩溃过期 outbox，并保留同一 publication key。"""

        if ttl_seconds < 1 or not worker_id.strip():
            raise ExecutionControlError("OUTBOX_LEASE_INVALID", "outbox worker 和 TTL 必须有效。")
        now = self.store.database_now()
        result = self.db.exec(
            update(EventOutbox)
            .where(
                EventOutbox.id == outbox.id,
                EventOutbox.tenant_id == outbox.tenant_id,
                EventOutbox.available_at <= now,
                or_(
                    EventOutbox.status == "pending",
                    (EventOutbox.status == "delivering")
                    & (EventOutbox.lease_expires_at <= now),
                ),
            )
            .values(
                status="delivering",
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                attempt_count=EventOutbox.attempt_count + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.expire(outbox)
        self.db.refresh(outbox)
        if result.rowcount != 1:
            raise ExecutionControlError("OUTBOX_ALREADY_CLAIMED", "outbox 已被其他 worker 处理。")
        return outbox

    def acknowledge_outbox(self, outbox: EventOutbox, *, worker_id: str) -> None:
        """仅允许当前未过期投递者确认完成；重复确认已完成行保持幂等。"""

        if outbox.status == "delivered":
            return
        now = self.store.database_now()
        self._assert_outbox_owner(outbox, worker_id, now=now)
        outbox.status = "delivered"
        outbox.lease_owner = None
        outbox.lease_expires_at = None
        outbox.delivered_at = now
        outbox.updated_at = now
        self.db.add(outbox)
        self.db.flush()

    def retry_outbox(
        self,
        outbox: EventOutbox,
        *,
        worker_id: str,
        error: Mapping[str, object],
        base_delay_seconds: int = 2,
    ) -> str:
        """投递失败时按 attempt 退避或死信，且不改写 payload 与 publication key。"""

        now = self.store.database_now()
        self._assert_outbox_owner(outbox, worker_id, now=now)
        dead = outbox.attempt_count >= outbox.max_attempts
        outbox.status = "dead_letter" if dead else "pending"
        outbox.available_at = now + timedelta(
            seconds=0 if dead else min(base_delay_seconds * 2 ** (outbox.attempt_count - 1), 3600)
        )
        outbox.lease_owner = None
        outbox.lease_expires_at = None
        outbox.last_error_json = dict(error)
        outbox.updated_at = now
        self.db.add(outbox)
        self.db.flush()
        return outbox.status

    def apply_cancel_command(
        self,
        instance: SopInstance,
        command: ExecutionCommand,
        *,
        worker_id: str,
    ) -> bool:
        """在 execution lease 内应用取消命令；steer 在 B1.2 前只保持 pending。"""

        if command.command_type != "cancel":
            raise ExecutionControlError("COMMAND_NOT_CONSUMABLE", "steer 命令将在 B1.2 才消费。")
        if command.execution_id != instance.id or command.tenant_id != instance.tenant_id:
            raise ExecutionControlError("COMMAND_EXECUTION_MISMATCH", "命令不属于当前 Execution。")
        if command.status == "applied":
            return instance.status == SopInstanceStatus.CANCELLED.value
        if command.status != "pending":
            raise ExecutionControlError("COMMAND_NOT_PENDING", "命令当前状态不可应用。")
        command.status = "claimed"
        command.claimed_by = worker_id
        command.claimed_fencing_token = instance.fencing_token
        command.claimed_at = self.store.database_now()
        command.status = "applied"
        command.consumed_at = command.claimed_at
        command.updated_at = command.claimed_at
        self.db.add(command)
        self.db.flush()
        settled = self.store.request_cancellation(
            instance,
            actor_user_id=command.actor_user_id or "system",
            reason=str(command.payload_json.get("reason") or "user_requested"),
        )
        command.result_json = {"cancelled": settled, "execution_status": instance.status}
        command.updated_at = self.store.database_now()
        self.db.add(command)
        self._append_event(
            instance,
            event_type="execution_cancellation_settled" if settled else "execution_cancelling",
            causation_id=command.id,
            payload={
                "command_id": command.command_id,
                "execution_status": instance.status,
                "cancellation_disposition": instance.cancellation_disposition,
                "effect_state": instance.effect_state,
            },
        )
        self.db.flush()
        return settled

    def freeze_result(
        self,
        instance: SopInstance,
        *,
        result: Mapping[str, object],
        verification: Mapping[str, object],
        created_by_step_key: str | None = None,
        application_publication_status: str = "pending",
    ) -> tuple[ExecutionResult, ExecutionPublication, bool]:
        """在 execution 写屏障内冻结不可变结果和唯一应用内 required publication。"""

        self._assert_instance(instance)
        body = dict(result)
        proof = dict(verification)
        result_status = "verified" if proof.get("passed") is True else "rejected"
        checksum = canonical_checksum({"result": body, "verification": proof})
        existing = self.db.exec(
            select(ExecutionResult).where(
                ExecutionResult.tenant_id == instance.tenant_id,
                ExecutionResult.execution_id == instance.id,
                ExecutionResult.checksum == checksum,
            )
        ).first()
        if existing is not None:
            publication = self._application_publication(existing)
            return existing, publication, False
        if instance.current_result_id is not None:
            raise ExecutionControlError("RESULT_ALREADY_FROZEN", "Execution 结果已经冻结。")
        self.store.authorize_mutation(instance, "result.freeze")
        result_row = ExecutionResult(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            status=result_status,
            result_json=body,
            verification_json=proof,
            checksum=checksum,
            created_by_step_key=created_by_step_key,
        )
        self.db.add(result_row)
        self.db.flush()
        publication = ExecutionPublication(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            result_id=result_row.id,
            publication_key=canonical_checksum(
                {
                    "tenant_id": instance.tenant_id,
                    "execution_id": instance.id,
                    "result_id": result_row.id,
                    "target_type": "application",
                }
            ),
            target_type="application",
            target_ref=instance.session_id,
            required=True,
            status=application_publication_status,
            settled_at=utc_now() if application_publication_status == "settled" else None,
        )
        self.db.add(publication)
        instance.current_result_id = result_row.id
        self.db.add(instance)
        self._append_event(
            instance,
            event_type="execution_result_frozen",
            causation_id=result_row.id,
            payload={
                "result_id": result_row.id,
                "result_checksum": result_row.checksum,
                "publication_id": publication.id,
            },
        )
        self.db.flush()
        return result_row, publication, True

    def ensure_terminal_result(
        self,
        instance: SopInstance,
        *,
        target_status: str,
        result: Mapping[str, object],
        verification: Mapping[str, object],
    ) -> tuple[ExecutionResult, ExecutionPublication]:
        """为正式 SOP 或非成功终态冻结应用内结果投影；动态成功必须显式验证结果。"""

        if instance.current_result_id is not None:
            existing = self.db.get(ExecutionResult, instance.current_result_id)
            if existing is None or existing.tenant_id != instance.tenant_id:
                raise ExecutionControlError("RESULT_REFERENCE_INVALID", "Execution 结果引用已损坏。")
            return existing, self._application_publication(existing)
        if target_status == "succeeded" and instance.kind == "dynamic_task":
            raise ExecutionControlError(
                "DYNAMIC_RESULT_REQUIRED",
                "动态任务成功前必须显式冻结并验证结果。",
            )
        result_row, publication, _ = self.freeze_result(
            instance,
            result=result,
            verification=verification,
            application_publication_status="settled",
        )
        publication.receipt_json = {
            "projection": "execution_result",
            "execution_id": instance.id,
            "result_id": result_row.id,
        }
        self.db.add(publication)
        self.db.flush()
        return result_row, publication

    def settle_application_publication(
        self,
        instance: SopInstance,
        publication: ExecutionPublication,
        *,
        message_id: str,
    ) -> None:
        """以持久消息标识确认应用内发布，同一 publication 重放不得绑定另一条消息。"""

        if publication.execution_id != instance.id or publication.tenant_id != instance.tenant_id:
            raise ExecutionControlError("PUBLICATION_EXECUTION_MISMATCH", "发布不属于当前 Execution。")
        if publication.status == "settled":
            if publication.receipt_json.get("message_id") != message_id:
                raise ExecutionControlError(
                    "PUBLICATION_RECEIPT_CONFLICT",
                    "已完成 publication 不能改绑另一条消息。",
                )
            return
        self.store.authorize_mutation(instance, "publication.settle")
        publication.status = "settled"
        publication.receipt_json = {"message_id": message_id}
        publication.settled_at = self.store.database_now()
        publication.updated_at = publication.settled_at
        self.db.add(publication)
        self.db.flush()

    def assert_terminal_closure(self, instance: SopInstance, target_status: str) -> None:
        """在当前 execution lease 事务内验证全部权威子事实闭合，拒绝先查后写终态。"""

        if target_status not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise ExecutionControlError("TERMINAL_STATUS_INVALID", "目标状态不是 Execution 终态。")
        self.store.authorize_mutation(instance, f"terminal.guard.{target_status}")
        blockers = self.terminal_blockers(instance, target_status)
        if blockers:
            raise ExecutionControlError(
                "TERMINAL_CLOSURE_BLOCKED",
                "Execution 尚未闭合：" + ", ".join(blockers),
            )

    def terminal_blockers(self, instance: SopInstance, target_status: str) -> list[str]:
        """从权威表计算可解释阻塞项；调用方不得据此绕过写屏障直接改终态。"""

        self._assert_instance(instance)
        blockers: list[str] = []
        step_query = select(SopNodeExecution).where(
            SopNodeExecution.tenant_id == instance.tenant_id,
            SopNodeExecution.instance_id == instance.id,
            SopNodeExecution.required.is_(True),
            SopNodeExecution.superseded_by_step_key.is_(None),
        )
        if instance.current_plan_revision_id is not None:
            step_query = step_query.where(
                SopNodeExecution.plan_revision_id == instance.current_plan_revision_id
            )
        steps = self.db.exec(step_query).all()
        if target_status == "succeeded":
            incomplete_steps = [
                item.id
                for item in steps
                if item.status
                not in {NodeExecutionStatus.SUCCEEDED.value, NodeExecutionStatus.SKIPPED.value}
            ]
            if incomplete_steps:
                blockers.append("required_steps")
            if instance.kind == "dynamic_task":
                revision = self.db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
                plan_steps = (
                    revision.plan_json.get("steps")
                    if revision is not None and isinstance(revision.plan_json, dict)
                    else None
                )
                required_keys = {
                    str(item.get("step_key"))
                    for item in plan_steps or []
                    if isinstance(item, dict) and item.get("required", True) is True
                }
                completed_keys = {
                    item.step_key
                    for item in steps
                    if item.status
                    in {NodeExecutionStatus.SUCCEEDED.value, NodeExecutionStatus.SKIPPED.value}
                }
                if revision is None or not isinstance(plan_steps, list) or not (
                    required_keys <= completed_keys
                ):
                    blockers.append("missing_required_steps")
        active_operation = self.db.exec(
            select(SopOperation.id).where(
                SopOperation.tenant_id == instance.tenant_id,
                SopOperation.instance_id == instance.id,
                SopOperation.status.in_(ACTIVE_OPERATION_STATUSES),
            )
        ).first()
        if active_operation is not None:
            blockers.append("active_operations")
        active_attention = self.db.exec(
            select(SopWorkItem.id).where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.status.in_(ACTIVE_ATTENTION_STATUSES),
            )
        ).first()
        if active_attention is not None:
            blockers.append("active_attentions")
        active_command = self.db.exec(
            select(ExecutionCommand.id).where(
                ExecutionCommand.tenant_id == instance.tenant_id,
                ExecutionCommand.execution_id == instance.id,
                ExecutionCommand.status.in_(ACTIVE_COMMAND_STATUSES),
            )
        ).first()
        if active_command is not None:
            blockers.append("active_commands")
        ordinary_signal = self.db.exec(
            select(ExecutionSignal.id).where(
                ExecutionSignal.tenant_id == instance.tenant_id,
                ExecutionSignal.execution_id == instance.id,
                ~ExecutionSignal.status.in_(TERMINAL_SIGNAL_STATUSES),
            )
        ).first()
        if ordinary_signal is not None:
            blockers.append("active_signals")
        result = None
        if instance.current_result_id is not None:
            result = self.db.get(ExecutionResult, instance.current_result_id)
        if result is None or result.tenant_id != instance.tenant_id or result.status != "verified":
            blockers.append("verified_result")
        else:
            required_publications = self.db.exec(
                select(ExecutionPublication).where(
                    ExecutionPublication.tenant_id == instance.tenant_id,
                    ExecutionPublication.execution_id == instance.id,
                    ExecutionPublication.result_id == result.id,
                    ExecutionPublication.required.is_(True),
                )
            ).all()
            if not required_publications or any(
                item.status != "settled" for item in required_publications
            ):
                blockers.append("required_publications")
        if instance.effect_state == "unknown":
            blockers.append("unknown_effect")
        if target_status == "cancelled" and instance.cancellation_requested_at is None:
            blockers.append("cancellation_request")
        return blockers

    def enqueue_event_delivery(
        self,
        event: AgentEvent,
        *,
        destination: str,
        destination_ref: str,
    ) -> tuple[EventOutbox, bool]:
        """仅为已配置的真实外部目标幂等登记事件投递，不伪造应用内 event bus。"""

        if destination not in {"external_thread", "webhook"}:
            raise ExecutionControlError(
                "OUTBOX_DESTINATION_INVALID",
                "Outbox 仅接受已定义投递语义的 external_thread 或 webhook。",
            )
        normalized_ref = destination_ref.strip()
        if not normalized_ref:
            raise ExecutionControlError("OUTBOX_DESTINATION_REF_REQUIRED", "外部投递目标不能为空。")
        publication_key = canonical_checksum(
            {
                "destination": destination,
                "destination_ref": normalized_ref,
                "event_id": event.id,
            }
        )
        existing = self.db.exec(
            select(EventOutbox).where(
                EventOutbox.tenant_id == event.tenant_id,
                EventOutbox.publication_key == publication_key,
            )
        ).first()
        if existing is not None:
            return existing, False
        outbox_payload = {
            "event_id": event.id,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "tenant_id": event.tenant_id,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "aggregate_revision": event.aggregate_revision,
            "destination_ref": normalized_ref,
            "payload": dict(event.payload_json),
        }
        now = self.store.database_now()
        outbox = EventOutbox(
            tenant_id=event.tenant_id,
            event_id=event.id,
            publication_key=publication_key,
            destination=destination,
            payload_json=outbox_payload,
            payload_checksum=canonical_checksum(outbox_payload),
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        self.db.add(outbox)
        self.db.flush()
        return outbox, True

    def _append_event(
        self,
        instance: SopInstance,
        *,
        event_type: str,
        causation_id: str,
        payload: Mapping[str, object],
    ) -> AgentEvent:
        """把可重放领域事件追加到当前事务；外部投递必须按真实目标显式登记。"""

        existing = self.db.exec(
            select(AgentEvent).where(
                AgentEvent.tenant_id == instance.tenant_id,
                AgentEvent.aggregate_type == "execution",
                AgentEvent.aggregate_id == instance.id,
                AgentEvent.event_type == event_type,
                AgentEvent.causation_id == causation_id,
            )
        ).first()
        if existing is not None:
            return existing
        body = dict(payload)
        event = AgentEvent(
            tenant_id=instance.tenant_id,
            session_id=instance.session_id,
            event_type=event_type,
            schema_version=1,
            aggregate_type="execution",
            aggregate_id=instance.id,
            aggregate_revision=instance.revision,
            correlation_id=instance.id,
            causation_id=causation_id,
            payload_checksum=canonical_checksum(body),
            payload_json=body,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _application_publication(self, result: ExecutionResult) -> ExecutionPublication:
        """返回已冻结结果唯一的应用内 publication，缺失表示持久事实损坏。"""

        publication = self.db.exec(
            select(ExecutionPublication).where(
                ExecutionPublication.tenant_id == result.tenant_id,
                ExecutionPublication.result_id == result.id,
                ExecutionPublication.target_type == "application",
            )
        ).first()
        if publication is None:
            raise ExecutionControlError(
                "RESULT_PUBLICATION_MISSING",
                "不可变结果缺少应用内 publication。",
            )
        return publication

    @staticmethod
    def _assert_instance(instance: SopInstance) -> None:
        """拒绝缺少统一租户或 execution 身份的脱离聚合调用。"""

        if not instance.id or not instance.tenant_id:
            raise SopExecutionConflictError("Execution 身份不完整。")

    def _assert_signal_owner(
        self,
        instance: SopInstance,
        signal: ExecutionSignal,
        worker_id: str,
    ) -> None:
        """校验 signal 租约归属；该校验仍不能代替 execution lease。"""

        if signal.execution_id != instance.id or signal.tenant_id != instance.tenant_id:
            raise ExecutionControlError("SIGNAL_EXECUTION_MISMATCH", "signal 不属于当前 Execution。")
        now = self.store.database_now()
        if (
            signal.status != "claimed"
            or signal.lease_owner != worker_id
            or signal.lease_expires_at is None
            or signal.lease_expires_at <= now
        ):
            raise ExecutionControlError("SIGNAL_FENCED", "signal 租约无效或已经过期。")

    @staticmethod
    def _assert_outbox_owner(
        outbox: EventOutbox,
        worker_id: str,
        *,
        now: datetime,
    ) -> None:
        """拒绝非当前或已过期 outbox worker 的迟到确认。"""

        if (
            outbox.status != "delivering"
            or outbox.lease_owner != worker_id
            or outbox.lease_expires_at is None
            or outbox.lease_expires_at <= now
        ):
            raise ExecutionControlError("OUTBOX_FENCED", "outbox 租约无效或已经过期。")
