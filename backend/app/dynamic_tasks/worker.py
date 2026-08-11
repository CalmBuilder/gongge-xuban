"""
@Time       : 2026/08/10 19:20
@Author     : zhanglp8181
@File       : worker.py
@CallChain  : scheduled worker → pending ExecutionSignal → DynamicTaskAgent durable resume
@Description: 扫描并消费动态任务持久恢复信号，按 signal lease 与 Execution lease 处理重启和退避。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import ExecutionCommand, ExecutionSignal, ModelConfig, SopInstance, SopWorkItem
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import CapabilityAccessDenied
from app.dynamic_tasks.quotas import (
    DynamicTaskQuotaError,
    DynamicTaskQuotaService,
    quota_limits_from_settings,
)
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore


DynamicAgentFactory = Callable[[Session], DynamicTaskAgent]
_settings = get_settings()
SIGNAL_DISPATCH_WORKERS = _settings.dynamic_task_signal_dispatch_workers
SIGNAL_DISPATCH_CAPACITY = _settings.dynamic_task_signal_dispatch_capacity
_signal_executor = ThreadPoolExecutor(
    max_workers=SIGNAL_DISPATCH_WORKERS,
    thread_name_prefix="gongge-xuban-dynamic-signal",
)
_signal_inflight: set[str] = set()
_signal_inflight_lock = Lock()


def start_dynamic_task_signal_async(signal_id: str) -> bool:
    """有界提交一个 Signal；同进程重复扫描不会重复排队，跨进程由数据库租约仲裁。"""

    with _signal_inflight_lock:
        if signal_id in _signal_inflight or len(_signal_inflight) >= SIGNAL_DISPATCH_CAPACITY:
            return False
        _signal_inflight.add(signal_id)
    try:
        _signal_executor.submit(_process_dynamic_task_signal_in_background, signal_id)
    except RuntimeError:
        with _signal_inflight_lock:
            _signal_inflight.discard(signal_id)
        raise
    return True


def _process_dynamic_task_signal_in_background(signal_id: str) -> None:
    """在独立数据库会话中消费 Signal，完成后解除进程内排队去重。"""

    try:
        with Session(engine) as db:
            signal = db.get(ExecutionSignal, signal_id)
            if signal is not None:
                process_dynamic_task_signal(db, signal)
    finally:
        with _signal_inflight_lock:
            _signal_inflight.discard(signal_id)


def due_dynamic_task_signals(db: Session, *, limit: int = 50) -> list[ExecutionSignal]:
    """按数据库时间返回可认领的动态恢复 signal，不在扫描阶段取得执行权。"""

    if limit < 1 or limit > 500:
        raise ValueError("动态 signal 扫描批量必须位于 1..500。")
    now = SopExecutionStore(db).database_now()
    candidates = db.exec(
        select(ExecutionSignal)
        .where(
            or_(
                ExecutionSignal.signal_type.in_(
                    ("attention_decided", "command", "timer", "scheduled_start")
                ),
                and_(
                    ExecutionSignal.signal_type == "operation_settled",
                    ExecutionSignal.causation_type == "standing_rule_dispatch",
                ),
            ),
            ExecutionSignal.available_at <= now,
            or_(
                ExecutionSignal.status == "pending",
                (ExecutionSignal.status == "claimed")
                & (ExecutionSignal.lease_expires_at <= now),
            ),
        )
        .order_by(
            ExecutionSignal.priority.desc(),
            ExecutionSignal.available_at,
            ExecutionSignal.id,
        )
        .limit(limit)
    ).all()
    execution_ids = {item.execution_id for item in candidates}
    dynamic_ids = {
        row.id
        for row in db.exec(
            select(SopInstance).where(
                SopInstance.id.in_(execution_ids),
                SopInstance.kind == "dynamic_task",
                SopInstance.status.in_(("running", "waiting")),
            )
        ).all()
    }
    return [item for item in candidates if item.execution_id in dynamic_ids]


def process_dynamic_task_signal(
    db: Session,
    signal: ExecutionSignal,
    *,
    agent_factory: DynamicAgentFactory = DynamicTaskAgent,
) -> DynamicRunOutcome | None:
    """解析持久信号的模型与 actor，失败时退避原 signal 而不丢失唤醒。"""

    instance = db.get(SopInstance, signal.execution_id)
    if instance is None or instance.kind != "dynamic_task":
        return None
    model_snapshot = (instance.capability_snapshot_json or {}).get("model", {})
    model_id = str(model_snapshot.get("model_config_id") or "") if isinstance(
        model_snapshot, dict
    ) else ""
    model = db.get(ModelConfig, model_id) if model_id else None
    if signal.signal_type == "command":
        command = db.get(ExecutionCommand, signal.causation_id)
        actor_user_id = str(command.actor_user_id or "") if command is not None else ""
    elif signal.signal_type == "attention_decided":
        attention_id = str(signal.payload_json.get("attention_id") or "")
        attention = db.get(SopWorkItem, attention_id) if attention_id else None
        actor_user_id = (
            str(attention.resolution_json.get("actor_user_id") or "")
            if attention is not None
            else ""
        )
    else:
        attention = None
        actor_user_id = instance.initiator_user_id
    worker_id = f"dynamic-signal:{signal.id}:{signal.attempt_count + 1}"
    if model is None or model.tenant_id != instance.tenant_id or not actor_user_id:
        return _retry_unprocessable_signal(
            db,
            instance,
            signal,
            worker_id=worker_id,
            code="DYNAMIC_SIGNAL_CONTEXT_INVALID",
        )
    try:
        agent = agent_factory(db)
        quota_limits = quota_limits_from_settings(get_settings())
        if quota_limits.configured:
            agent.quota_limits = quota_limits
            try:
                DynamicTaskQuotaService(db).acquire_execution(instance, limits=quota_limits)
                db.commit()
            except DynamicTaskQuotaError as exc:
                return _retry_unprocessable_signal(
                    db,
                    instance,
                    signal,
                    worker_id=worker_id,
                    code=exc.code,
                )
        if signal.signal_type == "command":
            return agent.resume_steer_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
                steering_enabled=get_settings().dynamic_task_steering_enabled,
            )
        if signal.signal_type == "scheduled_start":
            return agent.resume_scheduled_start_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        if signal.signal_type == "timer":
            return agent.resume_connector_timer_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        if signal.signal_type == "operation_settled":
            return agent.resume_standing_dispatch_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        if signal.signal_type == "capacity_retry":
            return agent.resume_capacity_retry_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        if attention is not None and attention.attention_kind == "reauth":
            return agent.resume_reauth_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        if attention is not None and attention.attention_kind in {
            "tool_approval",
            "publication",
        }:
            return agent.resume_tool_approval_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        if attention is not None and attention.attention_kind == "exception":
            return agent.resume_write_reconciliation_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        return agent.resume_clarification_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id=worker_id,
            actor_user_id=actor_user_id,
        )
    except Exception as exc:
        db.commit()
        db.refresh(signal)
        db.refresh(instance)
        if signal.status == "claimed" and signal.lease_owner == worker_id:
            store = SopExecutionStore(db)
            with store.owned(instance, worker_id=worker_id):
                ExecutionControlService(db, store).retry_signal(
                    instance,
                    signal,
                    worker_id=worker_id,
                    error={"code": _stable_signal_error_code(exc)},
                )
            db.commit()
        return None


def _stable_signal_error_code(exc: Exception) -> str:
    """保留受控领域错误码并对未知异常退回类型名，避免 signal 账本泄露敏感消息。"""

    explicit_code = getattr(exc, "code", None)
    if isinstance(explicit_code, str) and explicit_code.strip():
        return explicit_code.strip()[:128]
    if isinstance(exc, (DynamicTaskAgentError, CapabilityAccessDenied)) and exc.args:
        domain_code = str(exc.args[0]).strip()
        if domain_code:
            return domain_code[:128]
    return type(exc).__name__[:128]


def _retry_unprocessable_signal(
    db: Session,
    instance: SopInstance,
    signal: ExecutionSignal,
    *,
    worker_id: str,
    code: str,
) -> None:
    """对缺少模型或命令上下文的 signal 完成一次受租约保护的退避。"""

    control = ExecutionControlService(db)
    control.claim_signal(signal, worker_id=worker_id)
    db.commit()
    with control.store.owned(instance, worker_id=worker_id):
        control.retry_signal(
            instance,
            signal,
            worker_id=worker_id,
            error={"code": code},
        )
    db.commit()
    return None
