"""
@Time       : 2026/08/04 06:10
@Author     : zhanglp8181
@File       : worker.py
@CallChain  : scheduled worker → pending ExecutionSignal → DynamicTaskAgent durable resume
@Description: 扫描并消费动态任务持久恢复信号，按 signal lease 与 Execution lease 处理重启和退避。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import or_
from sqlmodel import Session, select

from app.db.models import ExecutionSignal, ModelConfig, SopInstance, SopWorkItem
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgent
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore


DynamicAgentFactory = Callable[[Session], DynamicTaskAgent]


def due_dynamic_task_signals(db: Session, *, limit: int = 50) -> list[ExecutionSignal]:
    """按数据库时间返回可认领的动态 Attention signal，不在扫描阶段取得执行权。"""

    if limit < 1 or limit > 500:
        raise ValueError("动态 signal 扫描批量必须位于 1..500。")
    now = SopExecutionStore(db).database_now()
    candidates = db.exec(
        select(ExecutionSignal)
        .where(
            ExecutionSignal.signal_type == "attention_decided",
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
    """从持久 Attention 决定解析模型与 actor，失败时退避原 signal 而不丢失唤醒。"""

    instance = db.get(SopInstance, signal.execution_id)
    if instance is None or instance.kind != "dynamic_task":
        return None
    model_snapshot = (instance.capability_snapshot_json or {}).get("model", {})
    model_id = str(model_snapshot.get("model_config_id") or "") if isinstance(
        model_snapshot, dict
    ) else ""
    model = db.get(ModelConfig, model_id) if model_id else None
    attention_id = str(signal.payload_json.get("attention_id") or "")
    attention = db.get(SopWorkItem, attention_id) if attention_id else None
    actor_user_id = (
        str(attention.resolution_json.get("actor_user_id") or "")
        if attention is not None
        else ""
    )
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
        return agent_factory(db).resume_clarification_signal(
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
                    error={"code": type(exc).__name__[:128]},
                )
            db.commit()
        return None


def _retry_unprocessable_signal(
    db: Session,
    instance: SopInstance,
    signal: ExecutionSignal,
    *,
    worker_id: str,
    code: str,
) -> None:
    """对缺少模型或 Attention 上下文的 signal 完成一次受租约保护的退避。"""

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
