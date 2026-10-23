"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : external_tasks.py
@CallChain  : 外部任务扫描 → provider 查询 → Operation/Attempt 事实更新
@Description: 实现原 Operation 上 provider_task 的安全轮询，不创建第二套业务状态机。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import and_, or_, update
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import (
    ExecutionSignal,
    ExternalTaskEventInbox,
    SopInstance,
    SopOperation,
    SopOperationAttempt,
    utc_now,
)
from app.security.outbound import (
    OutboundTargetError,
    allowed_hosts_from_settings,
    prepare_outbound_request,
)
from app.sop_runtime.contracts import OperationStatus
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.external_identity import matches_external_task_callback_token


class ExternalTaskEventRejected(ValueError):
    """表示回执的租户、Operation、provider task 或凭据绑定不可信。"""

    code = "EXTERNAL_EVENT_REJECTED"


class ExternalTaskEventConflict(ValueError):
    """表示同一 provider event ID 被不同回执内容重用。"""

    code = "EXTERNAL_EVENT_CONFLICT"


@dataclass(frozen=True, slots=True)
class ExternalTaskEventReceipt:
    """返回可信 inbox 的幂等落盘身份，不暴露 provider 凭据。"""

    id: str
    status: str


@dataclass(frozen=True, slots=True)
class ProviderPollTarget:
    """保存提交扫描事务前冻结的轮询输入，避免外呼读取过期ORM对象。"""

    tenant_id: str
    operation_id: str
    observation_revision: int
    task_id: str
    status_url: str
    contract: Mapping[str, Any]


def poll_due_external_operations(db: Session, *, limit: int = 20) -> int:
    """轮询到期 provider task；网络调用不持有数据库事务，结果回到可信 inbox。"""

    if limit < 1 or limit > 200:
        raise ValueError("外部任务轮询批量必须位于 1..200。")
    store = SopExecutionStore(db)
    now = store.database_now()
    operations = db.exec(
        select(SopOperation)
        .where(
            SopOperation.status == OperationStatus.WAITING_EXTERNAL.value,
            SopOperation.external_task_id.is_not(None),
            or_(
                SopOperation.external_next_poll_at.is_(None),
                SopOperation.external_next_poll_at <= now,
            ),
        )
        .order_by(SopOperation.external_next_poll_at, SopOperation.created_at, SopOperation.id)
        .limit(limit)
    ).all()
    # 释放只读扫描事务后再进入 provider 网络调用；每条观察回到 inbox/CAS
    # 的短事务，避免常驻调度线程把数据库快照跨网络持有。
    db.commit()
    targets: list[ProviderPollTarget] = []
    for operation in operations:
        task_id = str(operation.external_task_id or "").strip()
        status_url = str(operation.external_status_url or "").strip()
        instance = db.get(SopInstance, operation.instance_id)
        if instance is None or instance.tenant_id != operation.tenant_id or not task_id or not status_url:
            continue
        targets.append(
            ProviderPollTarget(
                tenant_id=operation.tenant_id,
                operation_id=operation.id,
                observation_revision=operation.external_observation_revision,
                task_id=task_id,
                status_url=status_url,
                contract=deepcopy(_operation_contract(operation)),
            )
        )

    # 外呼循环只访问普通值，不得通过已过期的 ORM 属性隐式重开扫描事务。
    db.commit()
    processed = 0
    for target in targets:
        observation = _poll_provider_task(
            target.contract,
            task_id=target.task_id,
            status_url=target.status_url,
        )
        payload = dict(observation.get("payload") or {})
        result = observation.get("result")
        if result is not None:
            payload.setdefault("result", result)
        if observation.get("error"):
            payload["error"] = dict(observation["error"])
        next_revision = target.observation_revision + 1
        ingest_external_task_event(
            db,
            tenant_id=target.tenant_id,
            operation_id=target.operation_id,
            provider_task_id=target.task_id,
            provider_event_id=f"poll:{target.operation_id}:{next_revision}",
            provider_status=str(observation.get("status") or "working"),
            observation_revision=next_revision,
            payload=payload,
            callback_token="",
            event_type="poll",
            trusted_internal=True,
        )
        db.flush()
        apply_pending_external_task_events(db, limit=1)
        db.commit()
        processed += 1
    return processed


def _poll_provider_task(
    contract: Mapping[str, Any],
    *,
    task_id: str,
    status_url: str,
) -> dict[str, Any]:
    """使用 Operation 冻结的查询地址读取一次 provider 观察，不访问可变工具配置。"""

    status_field = str(contract.get("status_field") or "status")
    result_field = str(contract.get("result_field") or "result")
    status_mapping = {
        str(key): str(value)
        for key, value in (contract.get("status_mapping") or {}).items()
    }
    request_url = status_url.replace("{taskId}", task_id).replace("{task_id}", task_id)
    try:
        settings = get_settings()
        target = prepare_outbound_request(
            request_url,
            allowed_hosts=allowed_hosts_from_settings(settings),
        )
        response = httpx.get(
            target.request_url,
            headers=target.headers,
            timeout=settings.tool_timeout_seconds,
            **({"extensions": target.extensions} if target.extensions else {}),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("provider status response must be a JSON object")
        raw_status = _json_path(payload, status_field)
        status = str(status_mapping.get(str(raw_status), raw_status) or "working").lower()
        result = _json_path(payload, result_field)
        return {
            "status": status,
            "payload": dict(payload),
            "result": result,
            "observed_at": utc_now().isoformat(),
        }
    except (httpx.HTTPError, OutboundTargetError, ValueError) as exc:
        return {
            "status": "poll_error",
            "payload": {},
            "error": {"code": "EXTERNAL_POLL_ERROR", "message": str(exc)[:256]},
            "observed_at": utc_now().isoformat(),
        }


def _apply_observation(
    store: SopExecutionStore,
    operation: SopOperation,
    observation: Mapping[str, Any],
    *,
    expected_revision: int,
) -> None:
    """在当前 Execution lease 内按单向事实更新 Operation/Attempt。"""

    if operation.external_observation_revision != expected_revision:
        return
    status = str(observation.get("status") or "working").lower()
    payload = dict(observation.get("payload") or {})
    result = observation.get("result")
    now = utc_now()
    if status in {"completed", "succeeded", "success"}:
        store.finish_operation(
            operation,
            succeeded=True,
            result={"data": result} if result is not None else payload,
            external_reference=operation.external_task_id,
        )
        return
    if status in {"failed", "cancelled", "canceled", "expired"}:
        if operation.effect_kind in {"external_write", "destructive"}:
            store.mark_operation_unknown(
                operation,
                error={
                    "code": "PROVIDER_TERMINAL_REQUIRES_RECONCILIATION",
                    "provider_status": status,
                    "provider_task_id": operation.external_task_id,
                },
            )
            return
        store.finish_operation(
            operation,
            succeeded=False,
            error={"code": "PROVIDER_TASK_FAILED", "provider_status": status},
            external_reference=operation.external_task_id,
        )
        return
    operation.external_status = status
    operation.external_observation_revision += 1
    operation.external_last_event_id = f"poll:{operation.external_observation_revision}"
    operation.external_receipt_json = {
        **dict(operation.external_receipt_json or {}),
        "last_observation": dict(observation),
    }
    interval = _poll_interval_seconds(operation)
    operation.external_next_poll_at = now + timedelta(seconds=interval)
    attempt = store._latest_operation_attempt(operation)
    if attempt is not None:
        attempt.external_status = status
        attempt.external_observation_revision = operation.external_observation_revision
        attempt.external_receipt_json = dict(observation)
        attempt.updated_at = now
        store.db.add(attempt)
    store.db.add(operation)
    store.db.flush()


def _operation_contract(operation: SopOperation) -> dict[str, Any]:
    """读取已冻结能力快照中的异步查询字段。"""

    snapshot = operation.capability_snapshot_json or {}
    contract = snapshot.get("contract") if isinstance(snapshot, dict) else None
    return dict(contract) if isinstance(contract, dict) else {}


def _poll_interval_seconds(operation: SopOperation) -> int:
    """从冻结合同读取下一次查询间隔，非法历史值回到保守默认值。"""

    value = _operation_contract(operation).get("poll_interval_seconds", 5)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(parsed, 3600))


def _json_path(value: Any, path: str) -> Any:
    """按发布合同读取 provider 回执字段。"""

    current = value
    for part in (item for item in path.split(".") if item):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def ingest_external_task_event(
    db: Session,
    *,
    tenant_id: str,
    provider_task_id: str,
    provider_event_id: str,
    provider_status: str,
    observation_revision: int,
    payload: Mapping[str, object],
    callback_token: str,
    operation_id: str | None = None,
    external_submission_id: str | None = None,
    event_type: str = "status",
    trusted_internal: bool = False,
) -> ExternalTaskEventReceipt:
    """校验并 append-once 写入 provider 回执，不在回调事务中直接推进业务节点。"""

    normalized_task_id = str(provider_task_id or "").strip()
    normalized_event_id = str(provider_event_id or "").strip()
    normalized_status = str(provider_status or "").strip().lower()
    if (
        not tenant_id.strip()
        or not normalized_task_id
        or not normalized_event_id
        or not normalized_status
        or observation_revision < 1
    ):
        raise ExternalTaskEventRejected("外部回执身份或 revision 无效。")
    operation = None
    if operation_id:
        operation = db.get(SopOperation, operation_id)
        if operation is None or operation.tenant_id != tenant_id:
            raise ExternalTaskEventRejected("回执 Operation 不属于当前租户。")
    elif external_submission_id:
        operation = db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == tenant_id,
                SopOperation.external_submission_id == external_submission_id,
            )
        ).first()
    if operation is None:
        raise ExternalTaskEventRejected("找不到回执对应的持久 Operation。")
    if not trusted_internal and not matches_external_task_callback_token(
        callback_token,
        operation.external_callback_token_hash or "",
    ):
        raise ExternalTaskEventRejected("外部回执凭据无效。")
    if (
        operation.external_task_id
        and operation.external_task_id != normalized_task_id
    ):
        raise ExternalTaskEventRejected("provider task 身份与 Operation 不一致。")
    if trusted_internal and operation.external_task_id != normalized_task_id:
        raise ExternalTaskEventRejected("内部轮询 task 身份与 Operation 不一致。")
    if (
        external_submission_id
        and operation.external_submission_id
        and operation.external_submission_id != external_submission_id
    ):
        raise ExternalTaskEventRejected("provider submission 身份与 Operation 不一致。")
    payload_value = dict(payload)
    payload_checksum = _external_event_checksum(
        normalized_task_id,
        normalized_status,
        observation_revision,
        payload_value,
    )
    existing = db.exec(
        select(ExternalTaskEventInbox).where(
            ExternalTaskEventInbox.tenant_id == tenant_id,
            ExternalTaskEventInbox.provider_event_id == normalized_event_id,
        )
    ).first()
    if existing is not None:
        if existing.payload_checksum != payload_checksum:
            raise ExternalTaskEventConflict("同一 provider event ID 的回执内容不一致。")
        return ExternalTaskEventReceipt(existing.id, existing.status)
    now = SopExecutionStore(db).database_now()
    inbox = ExternalTaskEventInbox(
        tenant_id=tenant_id,
        operation_id=operation.id,
        external_submission_id=operation.external_submission_id,
        provider_task_id=normalized_task_id,
        provider_event_id=normalized_event_id,
        event_type=str(event_type or "status").strip() or "status",
        provider_status=normalized_status,
        observation_revision=observation_revision,
        payload_checksum=payload_checksum,
        payload_json=payload_value,
        available_at=now,
        received_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(inbox)
    db.flush()
    return ExternalTaskEventReceipt(inbox.id, inbox.status)


def apply_pending_external_task_events(db: Session, *, limit: int = 50) -> int:
    """以 inbox CAS 和 Execution lease 应用回执，保证观察与恢复 Signal 同事务。"""

    if limit < 1 or limit > 500:
        raise ValueError("外部回执应用批量必须位于 1..500。")
    now = SopExecutionStore(db).database_now()
    candidates = db.exec(
        select(ExternalTaskEventInbox)
        .where(
            ExternalTaskEventInbox.available_at <= now,
            or_(
                ExternalTaskEventInbox.status == "pending",
                and_(
                    ExternalTaskEventInbox.status == "claimed",
                    ExternalTaskEventInbox.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(ExternalTaskEventInbox.created_at, ExternalTaskEventInbox.id)
        .limit(limit)
    ).all()
    processed = 0
    for candidate in candidates:
        candidate_id = candidate.id
        worker_id = f"external-event:{candidate.id}"
        claim = db.exec(
            update(ExternalTaskEventInbox)
            .where(
                ExternalTaskEventInbox.id == candidate.id,
                or_(
                    ExternalTaskEventInbox.status == "pending",
                    and_(
                        ExternalTaskEventInbox.status == "claimed",
                        ExternalTaskEventInbox.lease_expires_at <= now,
                    ),
                ),
            )
            .values(
                status="claimed",
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=60),
                attempt_count=ExternalTaskEventInbox.attempt_count + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            continue
        db.expire(candidate)
        db.refresh(candidate)
        operation = db.get(SopOperation, candidate.operation_id)
        if operation is None:
            _reject_external_event(candidate, now, "EXTERNAL_OPERATION_MISSING")
            db.commit()
            processed += 1
            continue
        instance = db.get(SopInstance, operation.instance_id)
        if (
            instance is None
            or instance.tenant_id != operation.tenant_id
            or operation.tenant_id != candidate.tenant_id
        ):
            _reject_external_event(candidate, now, "EXTERNAL_OPERATION_BINDING_INVALID")
            db.commit()
            processed += 1
            continue
        if operation.status in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.FAILED.value,
            OperationStatus.UNKNOWN.value,
            OperationStatus.CANCELLED.value,
        }:
            _ignore_external_event(candidate, now, "EXTERNAL_OPERATION_ALREADY_TERMINAL")
            db.commit()
            processed += 1
            continue
        if operation.status != OperationStatus.WAITING_EXTERNAL.value:
            _release_external_event(candidate, now, "EXTERNAL_OPERATION_NOT_WAITING")
            db.commit()
            continue
        store = SopExecutionStore(db)
        if not store.agent_is_available(instance):
            _ignore_external_event(candidate, now, "EXTERNAL_AGENT_UNAVAILABLE")
            db.commit()
            processed += 1
            continue
        try:
            with store.owned(instance, worker_id=worker_id):
                current = db.get(SopOperation, operation.id)
                if current is None or current.status != OperationStatus.WAITING_EXTERNAL.value:
                    _ignore_external_event(candidate, now, "EXTERNAL_OPERATION_ALREADY_SETTLED")
                elif candidate.observation_revision <= current.external_observation_revision:
                    _ignore_external_event(candidate, now, "EXTERNAL_EVENT_OUT_OF_ORDER")
                else:
                    _apply_inbox_observation(store, current, candidate, instance)
            db.commit()
        except Exception as exc:
            db.rollback()
            candidate = db.get(ExternalTaskEventInbox, candidate_id)
            if candidate is not None:
                _release_external_event(
                    candidate,
                    SopExecutionStore(db).database_now(),
                    str(getattr(exc, "code", "EXTERNAL_EVENT_APPLY_RETRY")),
                )
                db.commit()
        processed += 1
    return processed


def _apply_inbox_observation(
    store: SopExecutionStore,
    operation: SopOperation,
    inbox: ExternalTaskEventInbox,
    instance: SopInstance,
) -> None:
    """在当前 Execution lease 内单向应用一条可信 provider 观察。"""

    db = store.db
    status = inbox.provider_status
    payload = dict(inbox.payload_json or {})
    operation.external_status = status
    operation.external_observation_revision = inbox.observation_revision
    operation.external_last_event_id = inbox.provider_event_id
    operation.external_receipt_json = payload
    operation.external_next_poll_at = None
    attempt = db.exec(
        select(SopOperationAttempt)
        .where(SopOperationAttempt.operation_id == operation.id)
        .order_by(SopOperationAttempt.attempt_number.desc())
    ).first()
    if attempt is not None:
        attempt.external_status = status
        attempt.external_observation_revision = inbox.observation_revision
        attempt.external_receipt_json = payload
        attempt.updated_at = utc_now()
        db.add(attempt)
    if status in {"completed", "succeeded", "success"}:
        result = payload.get("result")
        store.finish_operation(
            operation,
            succeeded=True,
            result={"data": result} if result is not None else payload,
            external_reference=str(payload.get("reference") or operation.external_task_id or "")
            or None,
        )
    elif status in {"failed", "cancelled", "canceled", "expired"}:
        if operation.effect_kind in {"external_write", "destructive"}:
            store.mark_operation_unknown(
                operation,
                error={
                    "code": "PROVIDER_TERMINAL_REQUIRES_RECONCILIATION",
                    "provider_status": status,
                    "provider_task_id": operation.external_task_id,
                },
            )
        else:
            store.finish_operation(
                operation,
                succeeded=False,
                error={"code": "PROVIDER_TASK_FAILED", "provider_status": status},
                external_reference=operation.external_task_id,
            )
    else:
        operation.external_next_poll_at = utc_now() + timedelta(
            seconds=_poll_interval_seconds(operation)
        )
        db.add(operation)
        db.flush()
        _apply_external_inbox_status(inbox, "applied", utc_now())
        return
    control = ExecutionControlService(db, store)
    signal = control.enqueue_signal(
        instance,
        signal_type="external_event",
        causation_type="external_task_event",
        causation_id=inbox.id,
        payload={
            "kind": "external_task",
            "operation_id": operation.id,
            "node_execution_id": operation.node_execution_id,
            "inbox_id": inbox.id,
            "provider_status": status,
        },
        priority=30,
    )
    inbox.signal_id = signal.id
    _apply_external_inbox_status(inbox, "applied", utc_now())
    db.add(operation)
    db.add(inbox)
    db.flush()


def _external_event_checksum(
    provider_task_id: str,
    provider_status: str,
    observation_revision: int,
    payload: Mapping[str, object],
) -> str:
    """计算 provider event 身份和回执内容的稳定摘要。"""

    encoded = json.dumps(
        {
            "provider_task_id": provider_task_id,
            "provider_status": provider_status,
            "observation_revision": observation_revision,
            "payload": dict(payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _apply_external_inbox_status(
    inbox: ExternalTaskEventInbox,
    status: str,
    now: Any,
    *,
    error_code: str | None = None,
) -> None:
    """统一清理 inbox 租约并记录应用或忽略事实。"""

    inbox.status = status
    inbox.lease_owner = None
    inbox.lease_expires_at = None
    inbox.applied_at = now if status in {"applied", "ignored", "rejected"} else None
    inbox.last_error_json = {"code": error_code} if error_code else {}
    inbox.updated_at = now


def _ignore_external_event(inbox: ExternalTaskEventInbox, now: Any, code: str) -> None:
    """标记不再可推进业务的迟到或乱序回执，但保留其可信事实。"""

    _apply_external_inbox_status(inbox, "ignored", now, error_code=code)


def _reject_external_event(inbox: ExternalTaskEventInbox, now: Any, code: str) -> None:
    """标记内部绑定损坏的 inbox，阻止 worker 无限重试同一条坏事实。"""

    _apply_external_inbox_status(inbox, "rejected", now, error_code=code)


def _release_external_event(inbox: ExternalTaskEventInbox, now: Any, code: str) -> None:
    """释放暂时还不能应用的早到回执，等待 Operation 进入 waiting_external。"""

    inbox.status = "pending"
    inbox.available_at = now + timedelta(seconds=1)
    inbox.lease_owner = None
    inbox.lease_expires_at = None
    inbox.last_error_json = {"code": code}
    inbox.updated_at = now


def due_sop_external_task_signals(
    db: Session,
    *,
    limit: int = 50,
) -> list[ExecutionSignal]:
    """返回只属于 SOP 分支的外部回执 Signal，不把它交给 DTA worker。"""

    if limit < 1 or limit > 500:
        raise ValueError("SOP 外部回执 Signal 扫描批量必须位于 1..500。")
    now = SopExecutionStore(db).database_now()
    candidates = db.exec(
        select(ExecutionSignal)
        .where(
            ExecutionSignal.signal_type == "external_event",
            ExecutionSignal.available_at <= now,
            or_(
                ExecutionSignal.status == "pending",
                and_(
                    ExecutionSignal.status == "claimed",
                    ExecutionSignal.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(ExecutionSignal.priority.desc(), ExecutionSignal.created_at, ExecutionSignal.id)
        .limit(limit)
    ).all()
    instances = {
        instance.id
        for instance in db.exec(
            select(SopInstance).where(
                SopInstance.id.in_({item.execution_id for item in candidates}),
                SopInstance.kind == "sop",
                SopInstance.status.in_(("running", "waiting")),
            )
        ).all()
    }
    return [item for item in candidates if item.execution_id in instances]


def process_sop_external_task_signal(db: Session, signal: ExecutionSignal) -> object | None:
    """原CAS认领或接管过期Signal后恢复；失去认领者不得重试他人的Signal。"""

    instance = db.get(SopInstance, signal.execution_id)
    if instance is None or instance.kind != "sop":
        return None
    worker_id = f"sop-external-event:{signal.id}:{signal.attempt_count + 1}"
    control = ExecutionControlService(db)
    claimed_here = False
    try:
        control.claim_signal(signal, worker_id=worker_id, ttl_seconds=300)
        claimed_here = True
        db.commit()
        from app.sop_runtime.coordinator import DeterministicSopCoordinator

        plan = DeterministicSopCoordinator(db).resume_external_task_signal(
            signal_id=signal.id,
            worker_id=worker_id,
        )
        db.commit()
        return plan
    except Exception as exc:
        db.rollback()
        if not claimed_here:
            return None
        signal = db.get(ExecutionSignal, signal.id)
        instance = db.get(SopInstance, signal.execution_id) if signal is not None else None
        if (
            signal is not None
            and instance is not None
            and signal.status == "claimed"
            and signal.lease_owner == worker_id
        ):
            store = SopExecutionStore(db)
            with store.owned(instance, worker_id=worker_id):
                control = ExecutionControlService(db, store)
                control.retry_signal(
                    instance,
                    signal,
                    worker_id=worker_id,
                    error={"code": str(getattr(exc, "code", "SOP_EXTERNAL_EVENT_RETRY"))},
                )
            db.commit()
        return None
