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
from app.db.models import (
    DynamicReadDispatchBatch,
    DynamicReadDispatchItem,
    DynamicReadDispatchResult,
    ExecutionCommand,
    ExecutionResult,
    ExecutionSignal,
    ChatSession,
    Message,
    ModelConfig,
    SopInstance,
    SopOperation,
    SopWorkItem,
)
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import CapabilityAccessDenied
from app.dynamic_tasks.quotas import (
    DynamicTaskQuotaError,
    DynamicTaskQuotaService,
    quota_limits_from_settings,
)
from app.observability.event_log import EventLog
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
                    (
                        "attention_decided",
                        "command",
                        "timer",
                        "scheduled_start",
                        "capacity_retry",
                    )
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


def reconcile_parallel_read_batches(db: Session, *, limit: int = 50) -> int:
    """接管崩溃遗留波次：结果齐备则结算，逾期则确定性失败并释放全部子事实。"""

    now = SopExecutionStore(db).database_now()
    batches = db.exec(
        select(DynamicReadDispatchBatch)
        .where(DynamicReadDispatchBatch.status.in_(("dispatched", "settling")))
        .order_by(DynamicReadDispatchBatch.created_at, DynamicReadDispatchBatch.id)
        .limit(limit)
    ).all()
    settled = 0
    for batch in batches:
        items = db.exec(
            select(DynamicReadDispatchItem)
            .where(DynamicReadDispatchItem.batch_id == batch.id)
            .order_by(DynamicReadDispatchItem.position)
        ).all()
        if not items:
            store = SopExecutionStore(db)
            if store.recover_parallel_read_batch(
                batch,
                instance=None,
                items=(),
                results={},
            ):
                settled += 1
            continue
        results = {
            row.dispatch_token: row
            for row in db.exec(
                select(DynamicReadDispatchResult).where(
                    DynamicReadDispatchResult.tenant_id == batch.tenant_id,
                    DynamicReadDispatchResult.dispatch_token.in_(
                        tuple(item.dispatch_token for item in items)
                    ),
                )
            ).all()
        }
        expired = batch.deadline_at is not None and batch.deadline_at <= now
        if not expired and any(item.dispatch_token not in results for item in items):
            continue
        instance = db.get(SopInstance, batch.execution_id)
        if instance is None or instance.status not in {"created", "running", "waiting"}:
            store = SopExecutionStore(db)
            if store.recover_parallel_read_batch(
                batch,
                instance=instance,
                items=items,
                results=results,
            ):
                settled += 1
            continue
        store = SopExecutionStore(db)
        operations = [db.get(SopOperation, item.operation_id) for item in items]
        if any(
            operation is not None and operation.status in {"retry_wait", "unknown"}
            for operation in operations
        ):
            # 本批不替 retry_wait 激活新的重试 writer；保留波次和 Operation 原状，等待原有
            # 合法入口或取消分类处理，不能把未决动作伪装成失败并终结父实例。
            continue
        worker_id = f"parallel-recovery:{batch.id}"
        with store.owned(instance, worker_id=worker_id):
            if store.recover_parallel_read_batch(
                batch,
                instance=instance,
                items=items,
                results=results,
            ):
                settled += 1
    db.commit()
    return settled


def _chat_turn_binding(
    db: Session,
    instance: SopInstance,
) -> tuple[str, str | None] | None:
    """校验聊天动态 Execution 的原始用户消息，并返回服务端与客户端 Turn 身份。"""

    if instance.source_kind != "chat":
        return None
    source_ref = str(instance.source_ref or "").strip()
    session = db.get(ChatSession, instance.session_id)
    message = db.get(Message, source_ref) if source_ref else None
    if (
        not source_ref
        or session is None
        or message is None
        or session.tenant_id != instance.tenant_id
        or session.user_id != instance.initiator_user_id
        or session.agent_id != instance.agent_id
        or message.tenant_id != instance.tenant_id
        or message.session_id != instance.session_id
        or message.role != "user"
        or message.id != source_ref
    ):
        raise DynamicTaskAgentError("DYNAMIC_CHAT_SOURCE_INVALID")
    metadata = message.metadata_json or {}
    client_turn_id = str(metadata.get("client_turn_id") or "").strip() or None
    return source_ref, client_turn_id


def _record_chat_dynamic_outcome(
    db: Session,
    instance: SopInstance,
    outcome: DynamicRunOutcome | None,
    *,
    turn_binding: tuple[str, str | None] | None,
    error_code: str | None = None,
) -> None:
    """把后台聊天动态任务的最终结果写回原 Turn，等待结果保持非终态。"""

    if turn_binding is None:
        return
    if outcome is not None:
        status = str(outcome.status or "").strip().lower()
        if str(outcome.execution_id or "").strip() != instance.id:
            raise DynamicTaskAgentError("DYNAMIC_CHAT_RESULT_OWNERSHIP_INVALID")
        if status == "succeeded":
            event_type = "complete"
        elif status in {"failed", "timed_out"}:
            event_type = "error_occurred"
            error_code = error_code or f"DYNAMIC_TASK_{status.upper()}"
        elif status == "cancelled":
            event_type = "stream_cancelled"
        else:
            return
    elif error_code:
        event_type = "error_occurred"
        status = "failed"
    else:
        return

    turn_id, client_turn_id = turn_binding
    payload: dict[str, object] = {
        "execution_id": instance.id,
        "status": status,
        "turn_id": turn_id,
        "user_message_id": turn_id,
        "client_turn_id": client_turn_id,
    }
    if error_code:
        payload["code"] = error_code
        payload["message"] = "后台动态任务未能完成。"
    if outcome is not None and status == "succeeded" and outcome.message is None:
        raise DynamicTaskAgentError("DYNAMIC_CHAT_RESULT_OWNERSHIP_INVALID")
    if outcome is not None and outcome.message is not None:
        message_id = str(outcome.message.id or "").strip()
        message = db.get(Message, message_id) if message_id else None
        metadata = message.metadata_json if message is not None else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        message_execution_id = str(metadata.get("execution_id") or "").strip()
        message_result_id = str(metadata.get("result_id") or "").strip()
        message_turn_id = str(
            metadata.get("turn_id") or metadata.get("user_message_id") or ""
        ).strip()
        message_client_turn_id = str(metadata.get("client_turn_id") or "").strip()
        result = db.get(ExecutionResult, message_result_id) if message_result_id else None
        if (
            message is not None
            and message.id == message_id
            and message_execution_id == instance.id
            and (
                not message_result_id
                or (
                    result is not None
                    and result.tenant_id == instance.tenant_id
                    and result.execution_id == instance.id
                )
            )
            and message.tenant_id == instance.tenant_id
            and message.session_id == instance.session_id
            and message.role == "assistant"
        ):
            bound_turn_ids = {
                value for value in (turn_id, client_turn_id or "") if str(value).strip()
            }
            published_turn_ids = {
                value
                for value in (message_turn_id, message_client_turn_id)
                if str(value).strip()
            }
            if published_turn_ids and not bound_turn_ids.intersection(published_turn_ids):
                # 前台 clarification 可能已经用新的回答 Turn 发布了结果；旧 signal
                # 恢复时只能认领 Execution 事实，不能把该结果再次投影到最初等待 Turn。
                return
            payload.update(
                {
                    "message_id": message.id,
                    "assistant_message_id": message.id,
                    "reply": message.content,
                }
            )
        elif status == "succeeded":
            raise DynamicTaskAgentError("DYNAMIC_CHAT_RESULT_OWNERSHIP_INVALID")
    event_log = EventLog(db)
    event_log.bind_turn(turn_id, client_turn_id)
    event_log.record_terminal_once(
        instance.tenant_id,
        instance.session_id,
        event_type,
        payload,
    )
    db.commit()


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
    agent: DynamicTaskAgent | None = None
    turn_binding: tuple[str, str | None] | None = None
    try:
        agent = agent_factory(db)
        turn_binding = _chat_turn_binding(db, instance)
        if turn_binding is not None:
            bind_turn = getattr(agent, "bind_turn", None)
            if not callable(bind_turn):
                raise DynamicTaskAgentError("DYNAMIC_CHAT_TURN_BINDING_UNSUPPORTED")
            bind_turn(*turn_binding)
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
            if command is not None and command.command_type == "add_skill":
                outcome = agent.resume_add_skill_signal(
                    signal_id=signal.id,
                    model_config=model,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                    skill_loading_enabled=get_settings().dynamic_task_skill_loading_enabled,
                )
            else:
                outcome = agent.resume_steer_signal(
                    signal_id=signal.id,
                    model_config=model,
                    worker_id=worker_id,
                    actor_user_id=actor_user_id,
                    steering_enabled=get_settings().dynamic_task_steering_enabled,
                )
        elif signal.signal_type == "scheduled_start":
            outcome = agent.resume_scheduled_start_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        elif signal.signal_type == "timer":
            outcome = agent.resume_connector_timer_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        elif signal.signal_type == "operation_settled":
            outcome = agent.resume_standing_dispatch_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        elif signal.signal_type == "capacity_retry":
            outcome = agent.resume_capacity_retry_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
            )
        elif attention is not None and attention.attention_kind == "reauth":
            outcome = agent.resume_reauth_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        elif attention is not None and attention.attention_kind in {
            "tool_approval",
            "publication",
        }:
            outcome = agent.resume_tool_approval_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        elif attention is not None and attention.attention_kind == "exception":
            outcome = agent.resume_write_reconciliation_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        else:
            outcome = agent.resume_clarification_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id=worker_id,
                actor_user_id=actor_user_id,
            )
        _record_chat_dynamic_outcome(
            db,
            instance,
            outcome,
            turn_binding=turn_binding,
        )
        return outcome
    except Exception as exc:
        db.rollback()
        db.refresh(signal)
        db.refresh(instance)
        error_code = _stable_signal_error_code(exc)
        if (
            error_code
            in {
                "DYNAMIC_CHAT_SOURCE_INVALID",
                "DYNAMIC_CHAT_TURN_BINDING_UNSUPPORTED",
            }
            and signal.status != "claimed"
        ):
            try:
                ExecutionControlService(db).claim_signal(signal, worker_id=worker_id)
                db.commit()
                db.refresh(signal)
            except Exception:
                db.rollback()
                return None
        if signal.status == "claimed" and signal.lease_owner == worker_id:
            store = SopExecutionStore(db)
            control = ExecutionControlService(db, store)
            error = {"code": error_code}
            if instance.status in {"succeeded", "failed", "timed_out", "cancelled"}:
                control.settle_claimed_signal_for_terminal_execution(
                    instance,
                    signal,
                    worker_id=worker_id,
                    error=error,
                )
            elif _is_terminal_signal_error(str(error["code"])):
                with store.owned(instance, worker_id=worker_id):
                    control.dead_letter_claimed_signal(
                        instance,
                        signal,
                        worker_id=worker_id,
                        error=error,
                    )
                    if signal.signal_type == "command":
                        terminal_command = db.get(ExecutionCommand, signal.causation_id)
                        if terminal_command is not None and terminal_command.status in {
                            "pending",
                            "claimed",
                        }:
                            terminal_command.status = "rejected"
                            terminal_command.reason_code = str(error["code"])
                            terminal_command.consumed_at = store.database_now()
                            terminal_command.updated_at = terminal_command.consumed_at
                            db.add(terminal_command)
                db.commit()
                # 来源身份损坏时，Execution 可能仍持有等待中的 Operation；强行调用
                # fail_execution 会违反统一终态闭合约束。先将唤醒信号死信，保留执行
                # 账本供管理员按数据修复流程处理。
                if agent is not None and error_code not in {
                    "DYNAMIC_CHAT_SOURCE_INVALID",
                    "DYNAMIC_CHAT_TURN_BINDING_UNSUPPORTED",
                }:
                    agent.fail_execution(
                        execution_id=instance.id,
                        worker_id=f"{worker_id}:terminal",
                        error_code=str(error["code"]),
                    )
                _record_chat_dynamic_outcome(
                    db,
                    instance,
                    None,
                    turn_binding=turn_binding,
                    error_code=error_code,
                )
            else:
                with store.owned(instance, worker_id=worker_id):
                    retry_status = control.retry_signal(
                        instance,
                        signal,
                        worker_id=worker_id,
                        error=error,
                    )
                    if retry_status == "dead_letter" and signal.signal_type == "command":
                        exhausted_command = db.get(ExecutionCommand, signal.causation_id)
                        if exhausted_command is not None and exhausted_command.status in {
                            "pending",
                            "claimed",
                        }:
                            exhausted_command.status = "rejected"
                            exhausted_command.reason_code = "DYNAMIC_SIGNAL_RETRY_EXHAUSTED"
                            exhausted_command.consumed_at = store.database_now()
                            exhausted_command.updated_at = exhausted_command.consumed_at
                            db.add(exhausted_command)
                if retry_status == "dead_letter":
                    db.commit()
                    if agent is not None:
                        agent.fail_execution(
                            execution_id=instance.id,
                            worker_id=f"{worker_id}:exhausted",
                            error_code="DYNAMIC_SIGNAL_RETRY_EXHAUSTED",
                        )
                    _record_chat_dynamic_outcome(
                        db,
                        instance,
                        None,
                        turn_binding=turn_binding,
                        error_code="DYNAMIC_SIGNAL_RETRY_EXHAUSTED",
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


def _is_terminal_signal_error(code: str) -> bool:
    """区分不可通过相同 signal 重试修复的领域错误，未知/基础设施错误继续退避。"""

    return code in {
        "GENERAL_SKILL_COUNTERMANDED",
        "DYNAMIC_RESULT_REPAIR_EXHAUSTED",
        # 最终 answer 的 schema/verification 在同一执行内只允许一次有界 repair；
        # repair 仍失败时必须像预算错误一样终结 signal，不能把模型自相矛盾
        # 的结果当作瞬时网络故障无限退避，导致 Execution 长时间停留 running。
        "DYNAMIC_RESULT_SCHEMA_INVALID",
        "DYNAMIC_RESULT_VERIFICATION_FAILED",
        "DYNAMIC_ACTION_PROPOSAL_INVALID",
        "DYNAMIC_STEP_BUDGET_EXHAUSTED",
        "DYNAMIC_RUNTIME_BUDGET_EXCEEDED",
        "DYNAMIC_INPUT_BUDGET_EXCEEDED",
        "DYNAMIC_TOOL_CALLS_BUDGET_EXCEEDED",
        "DYNAMIC_MODEL_CALLS_BUDGET_EXCEEDED",
        "DYNAMIC_EXPLORE_TOOL_BUDGET_EXCEEDED",
        "DYNAMIC_EXPLORE_MODEL_BUDGET_EXCEEDED",
        "DYNAMIC_CHAT_SOURCE_INVALID",
        "DYNAMIC_CHAT_TURN_BINDING_UNSUPPORTED",
        "DYNAMIC_CHAT_RESULT_OWNERSHIP_INVALID",
    }


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
