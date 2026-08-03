"""
@Time       : 2026/08/01 20:40
@Author     : zhanglp8181
@File       : sessions.py
@CallChain  : 员工档案/对话日志 → FastAPI → 会话与反馈事实
@Description: 提供本人会话的兼容列表、服务端分页、轻量概览和按需详情接口。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, select
from sqlmodel.sql.expression import SelectOfScalar

from app.api.chat import message_read, session_read
from app.db import get_session
from app.db.models import AgentEvent, ChatSession, Message, MessageFeedback, User, utc_now
from app.feedback import FEEDBACK_BUCKET_LABELS, feedback_analysis_read
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/sessions", tags=["enterprise:sessions"])

SessionFeedbackFilter = Literal[
    "all", "up", "down", "unrated", "ability", "tool", "knowledge", "sop"
]


class ConversationSessionPageRead(BaseModel):
    """返回筛选后的会话页及当前员工范围内的会话总数。"""

    items: list[dict[str, Any]]
    total: int
    session_total: int
    page: int
    page_size: int


class SessionOverviewRead(BaseModel):
    """返回员工档案所需的会话总数和最近一条摘要。"""

    total: int
    latest: dict[str, Any] | None


@router.get("")
def list_sessions(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    """兼容旧调用，返回当前用户在指定员工范围内的全部会话。"""

    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    conditions = [ChatSession.tenant_id == tenant_id, ChatSession.user_id == current_user.id]
    if agent_id:
        conditions.append(ChatSession.agent_id == agent_id)
    rows = db.exec(
        select(ChatSession).where(*conditions).order_by(ChatSession.updated_at.desc())
    ).all()
    return [session_read(row).model_dump() for row in rows]


@router.get("/page", response_model=ConversationSessionPageRead)
def page_sessions(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    feedback_filter: SessionFeedbackFilter = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ConversationSessionPageRead:
    """按本人、员工和反馈归因过滤后分页，并批量投影当前页反馈摘要。"""

    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    base = _owned_session_query(tenant_id, current_user.id, agent_id)
    filtered = _filter_session_query(base, tenant_id, feedback_filter)
    session_total = db.exec(
        select(func.count()).select_from(base.order_by(None).subquery())
    ).one()
    total = db.exec(
        select(func.count()).select_from(filtered.order_by(None).subquery())
    ).one()
    sessions = db.exec(
        filtered.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    feedback_by_session, messages_by_id = _page_feedback(db, tenant_id, sessions)
    items = [
        {
            **session_read(row).model_dump(),
            "down_feedback": _feedback_projection(
                row,
                [item for item in feedback_by_session.get(row.id, []) if item.rating == "down"],
                messages_by_id,
            ),
            "up_feedback": _feedback_projection(
                row,
                [item for item in feedback_by_session.get(row.id, []) if item.rating == "up"],
                messages_by_id,
            ),
        }
        for row in sessions
    ]
    return ConversationSessionPageRead(
        items=items,
        total=int(total),
        session_total=int(session_total),
        page=page,
        page_size=page_size,
    )


@router.get("/overview", response_model=SessionOverviewRead)
def get_session_overview(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> SessionOverviewRead:
    """以一次计数和一条限量查询支持员工档案会话摘要。"""

    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    query = _owned_session_query(tenant_id, current_user.id, agent_id)
    total = db.exec(
        select(func.count()).select_from(query.order_by(None).subquery())
    ).one()
    latest = db.exec(
        query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc()).limit(1)
    ).first()
    return SessionOverviewRead(
        total=int(total),
        latest=session_read(latest).model_dump() if latest else None,
    )


def _owned_session_query(
    tenant_id: str,
    user_id: str,
    agent_id: str | None,
) -> SelectOfScalar[ChatSession]:
    """构造限定 tenant、当前用户和可选数字员工的会话查询。"""

    conditions = [ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id]
    if agent_id:
        conditions.append(ChatSession.agent_id == agent_id)
    return select(ChatSession).where(*conditions)


def _filter_session_query(
    query: SelectOfScalar[ChatSession],
    tenant_id: str,
    feedback_filter: SessionFeedbackFilter,
) -> SelectOfScalar[ChatSession]:
    """把反馈状态或归因筛选转换为关联反馈存在性条件。"""

    any_feedback = _feedback_exists(tenant_id)
    if feedback_filter == "unrated":
        return query.where(~any_feedback)
    if feedback_filter in {"up", "down"}:
        return query.where(_feedback_exists(tenant_id, rating=feedback_filter))
    bucket_by_filter = {
        "ability": "model_issue",
        "tool": "tool_or_system_issue",
        "knowledge": "unknown",
        "sop": "skill_issue",
    }
    bucket = bucket_by_filter.get(feedback_filter)
    if bucket:
        return query.where(_feedback_exists(tenant_id, rating="down", bucket=bucket))
    return query


def _feedback_exists(
    tenant_id: str,
    *,
    rating: str | None = None,
    bucket: str | None = None,
) -> ColumnElement[bool]:
    """构造与当前会话关联的反馈 EXISTS 条件，供分页前筛选复用。"""

    conditions = [
        MessageFeedback.tenant_id == tenant_id,
        MessageFeedback.session_id == ChatSession.id,
    ]
    if rating:
        conditions.append(MessageFeedback.rating == rating)
    if bucket:
        conditions.append(MessageFeedback.analysis_bucket == bucket)
    return select(MessageFeedback.id).where(*conditions).exists()


def _page_feedback(
    db: Session,
    tenant_id: str,
    sessions: list[ChatSession],
) -> tuple[dict[str, list[MessageFeedback]], dict[str, Message]]:
    """批量读取当前页会话的反馈和关联消息，避免逐行查询。"""

    session_ids = [row.id for row in sessions]
    if not session_ids:
        return {}, {}
    feedback_rows = list(
        db.exec(
            select(MessageFeedback).where(
                MessageFeedback.tenant_id == tenant_id,
                MessageFeedback.session_id.in_(session_ids),
            )
        ).all()
    )
    grouped: dict[str, list[MessageFeedback]] = {}
    for row in feedback_rows:
        grouped.setdefault(row.session_id, []).append(row)
    message_ids = sorted({row.message_id for row in feedback_rows})
    messages = (
        db.exec(
            select(Message).where(
                Message.tenant_id == tenant_id,
                Message.id.in_(message_ids),
            )
        ).all()
        if message_ids
        else []
    )
    return grouped, {row.id: row for row in messages}


def _feedback_projection(
    session: ChatSession,
    rows: list[MessageFeedback],
    messages_by_id: dict[str, Message],
) -> dict[str, Any] | None:
    """把单一评价方向的反馈集合压缩为列表行需要的摘要。"""

    if not rows:
        return None
    latest = max(rows, key=lambda item: (item.updated_at, item.id))
    latest_analysis = feedback_analysis_read(latest)
    bucket_counts: dict[str, int] = {}
    if latest.rating == "down":
        for item in rows:
            bucket = item.analysis_bucket or "unknown"
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    primary_bucket = (
        max(bucket_counts.items(), key=lambda item: item[1])[0] if bucket_counts else None
    )
    latest_message = messages_by_id.get(latest.message_id)
    return {
        "session_id": session.id,
        "tenant_id": session.tenant_id,
        "agent_id": session.agent_id,
        "user_id": session.user_id,
        "title": session.title,
        "summary": session.summary,
        "status": session.status,
        "feedback_count": len(rows),
        "latest_feedback_at": latest.updated_at.isoformat(),
        "latest_message_id": latest.message_id,
        "latest_message": latest_message.content if latest_message else "",
        "analysis_status": latest_analysis["status"],
        "analysis_bucket": latest_analysis["bucket"],
        "analysis_bucket_label": latest_analysis["bucket_label"],
        "analysis_summary": latest_analysis["summary"],
        "primary_bucket": primary_bucket,
        "primary_bucket_label": FEEDBACK_BUCKET_LABELS.get(
            primary_bucket or "unknown", primary_bucket or "unknown"
        ),
        "bucket_counts": bucket_counts,
        "updated_at": session.updated_at.isoformat(),
    }


@router.get("/{session_id}")
def get_session_detail(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    _ensure_request_tenant(tenant_id, current_user)
    row = _get_chat_session(db, tenant_id, current_user.id, session_id)
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    return {
        "session": session_read(row).model_dump(),
        "messages": [message_read(message).model_dump() for message in messages],
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "payload": event.payload_json,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
    }


@router.post("/{session_id}/reset")
def reset_session(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    _ensure_request_tenant(tenant_id, current_user)
    row = _get_chat_session(db, tenant_id, current_user.id, session_id)
    row.active_skill_id = None
    row.active_step_id = None
    row.slots_json = {}
    row.skill_stack_json = []
    row.pending_tasks_json = []
    row.resume_after_answer_json = None
    row.summary = None
    row.last_agent_question = None
    row.status = "active"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return session_read(row).model_dump()


def _get_chat_session(db: Session, tenant_id: str, user_id: str, session_id: str) -> ChatSession:
    ensure_tenant(db, tenant_id)
    row = db.get(ChatSession, session_id)
    if not row or row.tenant_id != tenant_id or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
