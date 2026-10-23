"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : memories.py
@CallChain  : 管理端员工记忆页 → FastAPI → MemoryRecord/MemoryService
@Description: 提供租户与员工隔离的记忆查询、用户分组分页和个人记忆清理接口。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import (
    AgentProfile,
    ChatSession,
    MemoryCaptureScope,
    MemoryCaptureJob,
    MemoryKeyState,
    MemoryRecord,
    User,
)
from app.memory.capture import advance_memory_clear_generation
from app.memory.service import (
    memory_agent_id,
    memory_effective_agent_id,
    memory_matches_agent,
    memory_read,
    memory_rows_for_read,
)
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import agent_owned_by_user, is_admin_user
from app.security.tenant import ensure_tenant


router = APIRouter(prefix="/api/enterprise/memories", tags=["enterprise:memories"])


class MemoryUserPageRead(BaseModel):
    """员工记忆页响应，其中 total 表示匹配的用户组数量。"""

    items: list[dict]
    total: int
    page: int
    page_size: int


MemoryCaptureReason = Literal[
    "capture_contract_changed", "source_changed", "invalid_output", "deadline_exceeded",
    "attempts_exhausted", "retry_scheduled", "processing_failed", "superseded",
]


class MemoryCaptureJobRead(BaseModel):
    """对外展示的记忆提取任务安全状态，不包含来源正文、租约或模型细节。"""

    job_id: str
    status: str
    created_at: datetime
    finished_at: datetime | None
    attempt_count: int
    max_attempts: int
    model_dispatch_count: int
    applied_count: int
    discarded_count: int
    reason_code: MemoryCaptureReason | None = None
    next_attempt_at: datetime | None = None


class MemoryCaptureJobPageRead(BaseModel):
    """记忆提取任务状态的独立分页响应。"""

    items: list[MemoryCaptureJobRead]
    total: int
    page: int
    page_size: int


@router.get("")
def list_memories(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    username: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_current_tenant),
    db: Session = Depends(get_session),
) -> list[dict]:
    ensure_tenant(db, tenant_id)
    normalized_agent_id = _normalize_requested_agent_id(agent_id)
    if agent_id is not None and normalized_agent_id is None:
        return []
    can_view_all = _can_view_all_memories(db, tenant_id, normalized_agent_id, current_user)
    if not can_view_all and user_id and user_id != current_user.id:
        return []
    if not can_view_all and username and username != current_user.username:
        return []
    statement = select(MemoryRecord).where(
        MemoryRecord.tenant_id == tenant_id,
        MemoryRecord.kind != "conversation",
    )
    if can_view_all and user_id:
        statement = statement.where(MemoryRecord.user_id == user_id)
    elif not can_view_all:
        statement = statement.where(MemoryRecord.user_id == current_user.id)
    if can_view_all and username:
        statement = statement.where(MemoryRecord.username == username)
    rows = list(
        db.exec(
            statement.order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
        ).all()
    )
    session_agents = _session_agent_map(db, rows)
    if normalized_agent_id:
        rows = [
            row
            for row in rows
            if _memory_matches_agent(row, normalized_agent_id, session_agents)
        ]
    rows = memory_rows_for_read(rows, session_agents)
    if q:
        needle = q.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.content.lower()
            or needle in (row.username or "").lower()
        ]
    rows = rows[:limit]
    return [_memory_read_with_inferred_agent(row, session_agents) for row in rows]


@router.get("/page", response_model=MemoryUserPageRead)
def page_memories(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    username: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    current_user: User = Depends(require_current_tenant),
    db: Session = Depends(get_session),
) -> MemoryUserPageRead:
    """先按可见记忆筛选用户组，再稳定分页并返回当前页用户的全部匹配记忆。"""

    ensure_tenant(db, tenant_id)
    normalized_agent_id = _normalize_requested_agent_id(agent_id)
    if agent_id is not None and normalized_agent_id is None:
        return MemoryUserPageRead(items=[], total=0, page=page, page_size=page_size)
    can_view_all = _can_view_all_memories(db, tenant_id, normalized_agent_id, current_user)
    if not can_view_all and user_id and user_id != current_user.id:
        return MemoryUserPageRead(items=[], total=0, page=page, page_size=page_size)
    if not can_view_all and username and username != current_user.username:
        return MemoryUserPageRead(items=[], total=0, page=page, page_size=page_size)

    conditions = [
        MemoryRecord.tenant_id == tenant_id,
        MemoryRecord.kind != "conversation",
    ]
    if can_view_all and user_id:
        conditions.append(MemoryRecord.user_id == user_id)
    elif not can_view_all:
        conditions.append(MemoryRecord.user_id == current_user.id)
    if can_view_all and username:
        conditions.append(MemoryRecord.username == username)
    rows = list(
        db.exec(
            select(MemoryRecord)
            .where(*conditions)
            .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
        ).all()
    )
    session_agents = _session_agent_map(db, rows)
    if normalized_agent_id:
        rows = [
            row
            for row in rows
            if _memory_matches_agent(row, normalized_agent_id, session_agents)
        ]
    rows = memory_rows_for_read(rows, session_agents)
    needle = (q or "").strip()
    if needle:
        matching_ids = _page_memory_matching_ids(db, rows, needle)
        rows = [row for row in rows if row.id in matching_ids]
    grouped: dict[str, list[MemoryRecord]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)
    ordered_users = sorted(grouped)
    ordered_users.sort(
        key=lambda user_id: grouped[user_id][0].updated_at,
        reverse=True,
    )
    total = len(ordered_users)
    page_user_ids = ordered_users[(page - 1) * page_size : page * page_size]
    if not page_user_ids:
        return MemoryUserPageRead(items=[], total=total, page=page, page_size=page_size)

    page_rows = [row for row in rows if row.user_id in set(page_user_ids)]
    page_rows.sort(key=lambda row: (row.updated_at, row.id), reverse=True)
    return MemoryUserPageRead(
        items=[_memory_read_with_inferred_agent(row, session_agents) for row in page_rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/jobs", response_model=MemoryCaptureJobPageRead)
def list_memory_capture_jobs(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_current_tenant),
    db: Session = Depends(get_session),
) -> MemoryCaptureJobPageRead:
    """按记忆页相同的租户、用户和 Agent 规则分页返回安全提取状态。"""

    ensure_tenant(db, tenant_id)
    normalized_agent_id = _normalize_requested_agent_id(agent_id)
    if agent_id is not None and normalized_agent_id is None:
        return MemoryCaptureJobPageRead(items=[], total=0, page=page, page_size=page_size)
    can_view_all = _can_view_all_memories(db, tenant_id, normalized_agent_id, current_user)
    if not can_view_all and user_id and user_id != current_user.id:
        return MemoryCaptureJobPageRead(items=[], total=0, page=page, page_size=page_size)

    status_aliases = {
        "succeeded": "completed",
        "retry_wait": "retry",
    }
    normalized_status = status_aliases.get(status, status) if status else None
    if normalized_status is not None and normalized_status not in {
        "pending",
        "running",
        "retry",
        "failed",
        "discarded",
        "completed",
    }:
        raise HTTPException(status_code=422, detail="Unsupported memory capture status")

    conditions = [MemoryCaptureJob.tenant_id == tenant_id]
    if normalized_agent_id:
        conditions.append(MemoryCaptureJob.agent_scope_key == normalized_agent_id)
    if can_view_all and user_id:
        conditions.append(MemoryCaptureJob.user_id == user_id)
    elif not can_view_all:
        conditions.append(MemoryCaptureJob.user_id == current_user.id)
    if normalized_status:
        conditions.append(MemoryCaptureJob.status == normalized_status)

    total = int(db.exec(select(func.count(MemoryCaptureJob.id)).where(*conditions)).one())
    rows = db.exec(
        select(MemoryCaptureJob)
        .where(*conditions)
        .order_by(MemoryCaptureJob.created_at.desc(), MemoryCaptureJob.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return MemoryCaptureJobPageRead(
        items=[_memory_capture_job_read(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete("/me")
def clear_my_memories(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """在 scope 写权内推进清空代数并删除当前有效归属事实，阻止迟到复活。"""

    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    normalized_agent_id = _normalize_requested_agent_id(agent_id)
    if agent_id is not None and normalized_agent_id is None:
        return {"deleted": 0}
    rows = list(
        db.exec(
            select(MemoryRecord)
            .where(
                MemoryRecord.tenant_id == tenant_id,
                MemoryRecord.user_id == current_user.id,
                MemoryRecord.kind != "conversation",
            )
            .order_by(MemoryRecord.updated_at.desc())
        ).all()
    )
    session_agents = _session_agent_map(db, rows)
    if normalized_agent_id:
        rows = [
            row
            for row in rows
            if _memory_matches_agent(row, normalized_agent_id, session_agents)
        ]
        agent = db.get(AgentProfile, normalized_agent_id)
        if (agent is None or agent.tenant_id != tenant_id) and not rows:
            return {"deleted": 0}
    user_scope, agent_scope = advance_memory_clear_generation(
        db,
        tenant_id=tenant_id,
        user_id=current_user.id,
        agent_id=normalized_agent_id,
    )
    # scope 已取得写权；重新读取清空集合，包含先于本事务提交的 T3 内容。
    rows = list(db.exec(select(MemoryRecord).where(
        MemoryRecord.tenant_id == tenant_id, MemoryRecord.user_id == current_user.id,
        MemoryRecord.kind != "conversation",
    ).with_for_update().execution_options(populate_existing=True)).all())
    session_agents = _session_agent_map(db, rows)
    if normalized_agent_id:
        rows = [row for row in rows if _memory_matches_agent(
            row, normalized_agent_id, session_agents
        )]
    deleted = len(rows)
    deleted_ids = {row.id for row in rows}
    scope_rows = db.exec(
        select(MemoryCaptureScope).where(
            MemoryCaptureScope.tenant_id == tenant_id,
            MemoryCaptureScope.user_id == current_user.id,
            MemoryCaptureScope.scope_kind == "agent",
        )
    ).all()
    scopes_by_agent = {row.agent_scope_key: row for row in scope_rows}
    key_states = db.exec(
        select(MemoryKeyState).where(
            MemoryKeyState.tenant_id == tenant_id,
            MemoryKeyState.user_id == current_user.id,
        )
    ).all()
    for state in key_states:
        if normalized_agent_id and state.agent_scope_key != normalized_agent_id:
            continue
        state.record_id = None
        state.tombstoned = True
        state.user_generation = user_scope.clear_generation
        target_agent_scope = scopes_by_agent.get(state.agent_scope_key)
        state.agent_generation = (
            agent_scope.clear_generation
            if agent_scope is not None and state.agent_scope_key == agent_scope.agent_scope_key
            else target_agent_scope.clear_generation if target_agent_scope else 0
        )
        state.user_permission_generation = user_scope.permission_generation
        state.agent_permission_generation = (
            target_agent_scope.permission_generation if target_agent_scope else 0
        )
        state.clear_generation = max(state.user_generation, state.agent_generation)
        state.permission_generation = max(
            state.user_permission_generation,
            state.agent_permission_generation,
        )
        state.row_version += 1
        state.updated_at = user_scope.updated_at
        db.add(state)
    for row in rows:
        if row.id not in deleted_ids:
            continue
        db.delete(row)
    db.commit()
    return {"deleted": deleted}


def _session_agent_map(db: Session, rows: list[MemoryRecord]) -> dict[str, ChatSession]:
    """批量加载候选历史会话，并把租户边界交给统一归属解析器校验。"""

    session_ids = sorted(
        {
            row.session_id
            for row in rows
            if row.session_id and not memory_agent_id(row)
        }
    )
    if not session_ids:
        return {}
    tenant_ids = sorted({row.tenant_id for row in rows})
    sessions = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id.in_(tenant_ids),
            ChatSession.id.in_(session_ids),
        )
    ).all()
    return {session.id: session for session in sessions}


def _memory_capture_job_read(job: MemoryCaptureJob) -> MemoryCaptureJobRead:
    """把内部任务状态映射成页面可见字段，并统一完成/重试状态名称。"""

    status = {
        "completed": "succeeded",
        "retry": "retry_wait",
    }.get(job.status, job.status)
    reasons = {
        "capture_contract_changed": "capture_contract_changed",
        "source_changed": "source_changed",
        "invalid_delta": "invalid_output",
        "deadline_exceeded": "deadline_exceeded",
        "max_attempts_exceeded": "attempts_exhausted",
        "retryable_failure": "retry_scheduled",
        "worker_failed": "processing_failed",
    }
    reason = reasons.get(job.terminal_reason or "")
    if job.status == "completed" and job.discarded_count and not job.applied_count:
        reason = "superseded"
    if reason is None and job.status in {"discarded", "failed"}:
        reason = "capture_contract_changed" if job.status == "discarded" else "processing_failed"
    return MemoryCaptureJobRead(
        job_id=job.id,
        status=status,
        created_at=job.created_at,
        finished_at=job.finished_at,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        model_dispatch_count=job.model_dispatch_count,
        applied_count=job.applied_count,
        discarded_count=job.discarded_count,
        reason_code=reason,
        next_attempt_at=job.next_attempt_at if job.status == "retry" else None,
    )


def _can_view_all_memories(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> bool:
    if is_admin_user(current_user):
        return True
    if not agent_id:
        return False
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_owned_by_user(agent, current_user)


def _normalize_requested_agent_id(agent_id: str | None) -> str | None:
    """保留缺省筛选与显式无效筛选的区别，防止空白参数扩大查询或清空范围。"""

    if agent_id is None:
        return None
    normalized = agent_id.strip()
    return normalized or None


def _page_memory_matching_ids(
    db: Session,
    rows: list[MemoryRecord],
    needle: str,
) -> set[str]:
    """用数据库原生 ILIKE 查询已确定有效的记忆行，保持各方言比较语义。"""

    if not rows:
        return set()
    pattern = f"%{needle}%"
    matching_ids: set[str] = set()
    row_ids = [row.id for row in rows]
    for start in range(0, len(row_ids), 500):
        chunk = row_ids[start : start + 500]
        statement = select(MemoryRecord.id).where(
            MemoryRecord.id.in_(chunk),
            or_(
                MemoryRecord.username.ilike(pattern),
                MemoryRecord.user_id.ilike(pattern),
                MemoryRecord.content.ilike(pattern),
            ),
        )
        matching_ids.update(db.exec(statement).all())
    return matching_ids


def _memory_matches_agent(
    row: MemoryRecord,
    agent_id: str,
    session_agents: Mapping[str, ChatSession],
) -> bool:
    """使用 service 层同一有效归属规则过滤 API 行。"""

    return memory_matches_agent(row, agent_id, session_agents)


def _memory_read_with_inferred_agent(
    row: MemoryRecord,
    session_agents: Mapping[str, ChatSession],
) -> dict:
    """对外展示规范列、metadata 或会话推断出的唯一有效员工归属。"""

    payload = memory_read(row)
    inferred_agent_id = memory_effective_agent_id(row, session_agents)
    if not inferred_agent_id:
        return payload
    metadata = dict(payload.get("metadata") or {})
    metadata["agent_id"] = inferred_agent_id
    if not memory_agent_id(row):
        metadata["agent_id_source"] = "session"
    elif row.agent_id and row.agent_id.strip():
        metadata["agent_id_source"] = "column"
    else:
        metadata["agent_id_source"] = "metadata"
    payload["metadata"] = metadata
    return payload
