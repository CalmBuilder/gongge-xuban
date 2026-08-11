"""
@Time       : 2026/07/22 17:45
@Author     : zhanglp8181
@File       : traces.py
@CallChain  : Trace API → 会话鉴权 → SOP 实例/节点/操作执行事实
@Description: 查询当前账号会话轨迹，并附带可核验的统一 SOP Runtime 落库记录。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.api.sessions import get_session_detail
from app.db import get_session
from app.db.models import (
    AgentEvent,
    ChatSession,
    Message,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    User,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/traces",
    tags=["enterprise:traces"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
def list_traces(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    """列出当前账号拥有的会话轨迹摘要。"""

    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    sessions = db.exec(
        select(ChatSession)
        .where(ChatSession.tenant_id == tenant_id, ChatSession.user_id == current_user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    traces: list[dict] = []
    for chat_session in sessions:
        events = db.exec(
            select(AgentEvent)
            .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == chat_session.id)
            .order_by(AgentEvent.created_at.desc())
        ).all()
        messages = db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == chat_session.id)
            .order_by(Message.created_at.desc())
        ).all()
        last_decision = next(
            (event.payload_json for event in events if event.event_type == "router_decision_created"),
            None,
        )
        tool_calls = len([event for event in events if event.event_type == "tool_call_finished"])
        traces.append(
            {
                "session_id": chat_session.id,
                "user_id": chat_session.user_id,
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "last_decision": last_decision,
                "last_message": messages[0].content if messages else None,
                "last_message_time": messages[0].created_at.isoformat() if messages else None,
                "tool_call_count": tool_calls,
                "status": chat_session.status,
                "updated_at": chat_session.updated_at.isoformat(),
            }
        )
    return traces


@router.get("/{session_id}")
def get_trace(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """返回会话详情及该会话关联的 SOP 持久化执行事实。"""

    detail = get_session_detail(
        session_id=session_id,
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )
    detail["sop_runtime"] = _sop_runtime_trace(db, tenant_id, session_id)
    return detail


def _sop_runtime_trace(
    db: Session, tenant_id: str, session_id: str
) -> list[dict[str, object]]:
    """按运行序号汇总实例、节点 attempt、工具操作和身份审计上下文。"""

    instances = db.exec(
        select(SopInstance)
        .where(
            SopInstance.tenant_id == tenant_id,
            SopInstance.session_id == session_id,
        )
        .order_by(SopInstance.run_number)
    ).all()
    result: list[dict[str, object]] = []
    for instance in instances:
        executions = db.exec(
            select(SopNodeExecution)
            .where(
                SopNodeExecution.tenant_id == tenant_id,
                SopNodeExecution.instance_id == instance.id,
            )
            .order_by(SopNodeExecution.created_at, SopNodeExecution.attempt)
        ).all()
        operations = db.exec(
            select(SopOperation)
            .where(
                SopOperation.tenant_id == tenant_id,
                SopOperation.instance_id == instance.id,
            )
            .order_by(SopOperation.created_at)
        ).all()
        work_items = db.exec(
            select(SopWorkItem)
            .where(
                SopWorkItem.tenant_id == tenant_id,
                SopWorkItem.instance_id == instance.id,
            )
            .order_by(SopWorkItem.created_at)
        ).all()
        result.append(
            {
                "instance_id": instance.id,
                "skill_id": instance.skill_id,
                "skill_version": instance.skill_version,
                "definition_checksum": instance.definition_checksum,
                "run_number": instance.run_number,
                "status": instance.status,
                "current_node_id": instance.current_node_id,
                "slots": instance.slots_json,
                "identity": (instance.context_json or {}).get("identity"),
                "node_executions": [
                    {
                        "execution_id": execution.id,
                        "node_id": execution.node_id,
                        "attempt": execution.attempt,
                        "status": execution.status,
                        "input": execution.input_json,
                        "output": execution.output_json,
                        "error": execution.error_json,
                    }
                    for execution in executions
                ],
                "operations": [
                    {
                        "operation_id": operation.id,
                        "operation_name": operation.operation_name,
                        "idempotency_key": operation.idempotency_key,
                        "caused_by_skill_use_id": operation.caused_by_skill_use_id,
                        "caused_by_skill_use_ids": operation.caused_by_skill_use_ids_json,
                        "status": operation.status,
                        "request": operation.request_json,
                        "result": operation.result_json,
                        "error": operation.error_json,
                    }
                    for operation in operations
                ],
                "work_items": [
                    {
                        "id": work_item.id,
                        "node_id": work_item.node_id,
                        "status": work_item.status,
                        "assignee_user_id": work_item.assignee_user_id,
                        "outcome": work_item.outcome,
                        "comment": work_item.comment,
                        "revision": work_item.revision,
                        "candidates": work_item.candidate_snapshot_json,
                    }
                    for work_item in work_items
                ],
            }
        )
    return result
