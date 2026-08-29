"""
@Time       : 2026/08/10 23:30
@Author     : zhanglp8181
@File       : worker.py
@CallChain  : FastAPI lifespan → Connector worker → inbox/Agent Loop/outbox
@Description: 独立轮询并消费 Connector 入站与出站事实，避免阻塞定时任务调度器。
"""

from __future__ import annotations

import threading
from time import sleep

from sqlalchemy import Engine, inspect
from sqlmodel import Session

from app.connectors.runtime import ConnectorRuntimeError, ConnectorRuntimeService
from app.core.agent_loop import AgentLoop
from app.db import engine
from app.db.models import ConnectorInboundEvent
from app.session.session_schema import ChatTurnRequest


CONNECTOR_WORKER_POLL_SECONDS = 2.0
_stopped = False
_background_thread: threading.Thread | None = None


def process_one_inbound(*, worker_id: str) -> bool:
    """抢占并完整处理一条 inbox；缺授权时可恢复停放，坏报文进入 dead letter。"""

    with Session(engine) as db:
        service = ConnectorRuntimeService(db)
        event = service.claim_due_event(worker_id=worker_id, lease_seconds=1200)
        if event is None:
            return False
        try:
            dispatch = service.prepare_dispatch(
                event,
                worker_id=worker_id,
                lease_seconds=1200,
            )
        except ConnectorRuntimeError as exc:
            terminal = exc.code in {
                "WECOM_INBOUND_EVENT_UNSUPPORTED",
                "WECOM_INBOUND_PAYLOAD_INVALID",
                "WECOM_CALLBACK_RECEIVE_ID_MISMATCH",
                "WECOM_CALLBACK_AGENT_ID_MISMATCH",
            }
            service.park_event(
                event,
                worker_id=worker_id,
                error_code=exc.code,
                terminal=terminal,
            )
            return True
        try:
            AgentLoop(db).handle_turn(
                ChatTurnRequest(
                    tenant_id=dispatch.tenant_id,
                    session_id=dispatch.session_id,
                    agent_id=dispatch.agent_id,
                    user_id=dispatch.user_id,
                    client_turn_id=event.external_event_id,
                    message=dispatch.content,
                    channel=event.provider,
                )
            )
            service.complete_dispatch(dispatch, worker_id=worker_id)
        except Exception as exc:  # noqa: BLE001 - worker 必须把异常收敛为可恢复 inbox 状态。
            db.rollback()
            refreshed = db.get(ConnectorInboundEvent, event.id)
            if refreshed is not None and refreshed.lease_owner == worker_id:
                service.park_event(
                    refreshed,
                    worker_id=worker_id,
                    error_code=_safe_error_code(exc),
                    retry_seconds=60,
                )
        return True


def process_one_outbound(*, worker_id: str) -> bool:
    """抢占并投递一条 outbox，超时/断线后的未知效果不会被下一轮重发。"""

    with Session(engine) as db:
        service = ConnectorRuntimeService(db)
        delivery = service.claim_due_delivery(worker_id=worker_id, lease_seconds=120)
        if delivery is None:
            return service.reconcile_one_execution_publication(worker_id=worker_id)
        delivery_id = delivery.id
        try:
            service.deliver_claimed(delivery, worker_id=worker_id)
        except ConnectorRuntimeError as exc:
            db.rollback()
            refreshed = service.db.get(type(delivery), delivery_id)
            if refreshed is not None and refreshed.lease_owner == worker_id:
                service.finish_delivery(
                    refreshed,
                    worker_id=worker_id,
                    status="dead_letter",
                    error_code=exc.code,
                )
        return True


def run_connector_worker(*, poll_seconds: float = CONNECTOR_WORKER_POLL_SECONDS) -> None:
    """循环处理有限批次入站和出站，空闲时等待并响应进程停止标志。"""

    worker_id = f"connector-worker:{threading.get_ident()}"
    while not _stopped:
        progressed = False
        for _item in range(5):
            progressed = process_one_inbound(worker_id=worker_id) or progressed
            progressed = process_one_outbound(worker_id=worker_id) or progressed
            if not progressed:
                break
        if not progressed:
            sleep(max(0.2, poll_seconds))


def start_connector_background_worker(
    *,
    poll_seconds: float = CONNECTOR_WORKER_POLL_SECONDS,
) -> None:
    """在 Web 进程内幂等启动独立 Connector worker。"""

    global _background_thread, _stopped
    if _background_thread and _background_thread.is_alive():
        return
    if not connector_runtime_schema_ready(engine):
        return
    _stopped = False
    _background_thread = threading.Thread(
        target=run_connector_worker,
        kwargs={"poll_seconds": poll_seconds},
        name="gongge-xuban-connector-worker",
        daemon=True,
    )
    _background_thread.start()


def stop_connector_background_worker() -> None:
    """请求 Connector worker 在当前有界工作完成后停止。"""

    global _stopped
    _stopped = True


def connector_runtime_schema_ready(db_engine: Engine) -> bool:
    """仅在 0044 必需表列完整时启动 worker，升级窗口内保持静默停用。"""

    inspector = inspect(db_engine)
    required_tables = {
        "connector_principal_bindings",
        "connector_inbound_routes",
        "connector_thread_bindings",
        "connector_outbound_deliveries",
    }
    if not required_tables <= set(inspector.get_table_names()):
        return False
    inbound_columns = {
        column["name"] for column in inspector.get_columns("connector_inbound_events")
    }
    return {
        "lease_owner",
        "lease_until",
        "thread_binding_id",
        "session_id",
        "message_id",
        "execution_id",
    } <= inbound_columns


def _safe_error_code(error: Exception) -> str:
    """把任意异常归一为不含消息正文和凭据的稳定类型代码。"""

    explicit = str(getattr(error, "code", "") or "").strip()
    return (explicit or f"CONNECTOR_TURN_{type(error).__name__.upper()}")[:128]
