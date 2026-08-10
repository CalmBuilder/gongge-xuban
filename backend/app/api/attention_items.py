"""
@Time       : 2026/08/03 21:35
@Author     : zhanglp8181
@File       : attention_items.py
@CallChain  : 统一待我处理中心 → FastAPI → ExecutionControl/SOP Coordinator
@Description: 提供 typed Attention 的统一查询和带 tenant、actor、CAS、command id 的办理接口。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ExecutionSignal, SopInstance, SopWorkItem, SopWorkItemCandidate, User
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.execution_control import ExecutionControlError, ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore
from app.sop_runtime.state_machine import RevisionConflictError
from app.sop_runtime.work_items import (
    ACTIVE_WORK_ITEM_STATUSES,
    SopWorkItemService,
    WorkItemError,
)


router = APIRouter(prefix="/api/attention-items", tags=["attention-items"])


class AttentionResolveRequest(BaseModel):
    """办理 Attention 的严格幂等命令信封。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)
    comment: str | None = Field(default=None, max_length=10000)


class AttentionRead(BaseModel):
    """返回统一待办中心需要的类型、来源、载荷、命令和处理状态。"""

    id: str
    execution_id: str
    session_id: str
    kind: str
    key: str | None
    title: str | None
    source_type: str
    source_ref: str | None
    payload: dict[str, object]
    allowed_commands: list[str]
    available_commands: list[str]
    resolution: dict[str, object]
    required: bool
    status: str
    revision: int
    assignee_user_id: str | None
    created_at: str
    updated_at: str


class AttentionPageRead(BaseModel):
    """返回稳定分页后的 Attention 列表及总量。"""

    items: list[AttentionRead]
    total: int
    page: int
    page_size: int


@router.get("", response_model=AttentionPageRead)
def list_attention_items(
    tenant_id: str = Query(...),
    view: Literal["active", "resolved", "all"] = Query("active"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AttentionPageRead:
    """按当前用户真实关系过滤全部 Attention kind，并执行稳定服务端分页。"""

    statement = _visible_query(db, tenant_id, view, current_user)
    total = int(
        db.exec(select(func.count()).select_from(statement.order_by(None).subquery())).one()
    )
    items = db.exec(
        statement
        .order_by(SopWorkItem.created_at.desc(), SopWorkItem.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return AttentionPageRead(
        items=[_attention_read(db, item, current_user.id) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{attention_id}", response_model=AttentionRead)
def get_attention_item(
    attention_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AttentionRead:
    """读取当前用户有实际关系的 Attention，不提供平台角色业务旁路。"""

    ensure_current_user_tenant(tenant_id, current_user)
    item = _tenant_attention(db, tenant_id, attention_id)
    if not _can_view(db, item, current_user.id):
        raise HTTPException(status_code=403, detail="ATTENTION_VIEW_FORBIDDEN")
    return _attention_read(db, item, current_user.id)


@router.post("/{attention_id}/resolve", response_model=AttentionRead)
def resolve_attention_item(
    attention_id: str,
    request: AttentionResolveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AttentionRead:
    """以首个 CAS 胜者办理 Attention，并为异步恢复原子生成唯一 signal。"""

    ensure_current_user_tenant(request.tenant_id, current_user)
    item = _tenant_attention(db, request.tenant_id, attention_id)
    instance = db.get(SopInstance, item.instance_id)
    if instance is None or instance.tenant_id != request.tenant_id:
        raise HTTPException(status_code=409, detail="ATTENTION_EXECUTION_MISSING")
    coordinator = DeterministicSopCoordinator(db) if item.attention_kind == "sop_human_task" else None
    store = coordinator.store if coordinator is not None else SopExecutionStore(db)
    control = ExecutionControlService(db, store)
    worker_id = f"attention-{item.id[-16:]}"
    was_completed = item.status == "completed"
    try:
        replay = control.replayed_attention_resolution(
            item,
            actor_user_id=current_user.id,
            command_id=request.command_id,
            command=request.command,
        )
        if replay is not None:
            return _attention_read(db, item, current_user.id)
        with store.owned(instance, worker_id=worker_id):
            _, completed = control.resolve_attention(
                instance,
                item,
                actor_user_id=current_user.id,
                command_id=request.command_id,
                command=request.command,
                expected_revision=request.expected_revision,
                comment=request.comment,
            )
            if completed and coordinator is not None:
                _consume_synchronous_attention_signal(
                    db,
                    control,
                    instance,
                    item,
                    worker_id=worker_id,
                )
                if not was_completed:
                    coordinator.resume_completed_work_item(item)
        db.commit()
        db.refresh(item)
    except (
        ExecutionControlError,
        SopExecutionConflictError,
        WorkItemError,
        RevisionConflictError,
    ) as error:
        db.rollback()
        raise _attention_error(error) from error
    return _attention_read(db, item, current_user.id)


def _visible_query(
    db: Session,
    tenant_id: str,
    view: Literal["active", "resolved", "all"],
    current_user: User,
):
    """构造租户、用户关系和状态共同约束的 Attention 查询。"""

    ensure_current_user_tenant(tenant_id, current_user)
    candidate_exists = (
        select(SopWorkItemCandidate.id)
        .where(
            SopWorkItemCandidate.tenant_id == tenant_id,
            SopWorkItemCandidate.work_item_id == SopWorkItem.id,
            SopWorkItemCandidate.user_id == current_user.id,
        )
        .exists()
    )
    statement = select(SopWorkItem).where(
        SopWorkItem.tenant_id == tenant_id,
        or_(
            SopWorkItem.initiator_user_id == current_user.id,
            SopWorkItem.owner_user_id == current_user.id,
            SopWorkItem.assignee_user_id == current_user.id,
            candidate_exists,
        ),
    )
    if view == "active":
        statement = statement.where(SopWorkItem.status.in_(ACTIVE_WORK_ITEM_STATUSES))
    elif view == "resolved":
        statement = statement.where(~SopWorkItem.status.in_(ACTIVE_WORK_ITEM_STATUSES))
    return statement


def _attention_read(db: Session, item: SopWorkItem, current_user_id: str) -> AttentionRead:
    """组合 Execution 和候选资格形成不由前端推导的 Attention 投影。"""

    instance = db.get(SopInstance, item.instance_id)
    if instance is None or instance.tenant_id != item.tenant_id:
        raise HTTPException(status_code=409, detail="ATTENTION_EXECUTION_MISSING")
    available: list[str] = []
    if (
        item.status in ACTIVE_WORK_ITEM_STATUSES
        and SopWorkItemService(db).is_current_candidate(item, current_user_id)
        and not (item.exclude_initiator and item.initiator_user_id == current_user_id)
    ):
        available = list(item.allowed_commands_json or [])
        if item.attention_kind == "sop_human_task" and "complete" in available:
            available = list(item.allowed_outcomes_json or [])
    return AttentionRead(
        id=item.id,
        execution_id=item.instance_id,
        session_id=instance.session_id,
        kind=item.attention_kind,
        key=item.attention_key,
        title=item.title,
        source_type=item.source_type,
        source_ref=item.source_ref,
        payload=dict(item.payload_json or {}),
        allowed_commands=list(item.allowed_commands_json or []),
        available_commands=available,
        resolution=dict(item.resolution_json or {}),
        required=item.required,
        status=item.status,
        revision=item.revision,
        assignee_user_id=item.assignee_user_id,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
    )


def _tenant_attention(db: Session, tenant_id: str, attention_id: str) -> SopWorkItem:
    """按主键和 tenant 双重检查 Attention，避免跨租户可枚举性。"""

    item = db.get(SopWorkItem, attention_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="ATTENTION_NOT_FOUND")
    return item


def _can_view(db: Session, item: SopWorkItem, user_id: str) -> bool:
    """判断用户是否是发起人、owner、assignee 或冻结候选人。"""

    if user_id in {item.initiator_user_id, item.owner_user_id, item.assignee_user_id}:
        return True
    return db.exec(
        select(SopWorkItemCandidate.id).where(
            SopWorkItemCandidate.tenant_id == item.tenant_id,
            SopWorkItemCandidate.work_item_id == item.id,
            SopWorkItemCandidate.user_id == user_id,
        )
    ).first() is not None


def _consume_synchronous_attention_signal(
    db: Session,
    control: ExecutionControlService,
    instance: SopInstance,
    item: SopWorkItem,
    *,
    worker_id: str,
) -> None:
    """正式 SOP 同事务恢复时消费 signal，防止异步 worker 再推进一次。"""

    signal = db.exec(
        select(ExecutionSignal).where(
            ExecutionSignal.tenant_id == item.tenant_id,
            ExecutionSignal.execution_id == instance.id,
            ExecutionSignal.signal_type == "attention_decided",
            ExecutionSignal.causation_id == f"{item.id}:{item.revision}",
        )
    ).first()
    if signal is not None and signal.status == "pending":
        control.claim_signal(signal, worker_id=worker_id)
        control.consume_signal(instance, signal, worker_id=worker_id)


def _attention_error(
    error: ExecutionControlError | SopExecutionConflictError | WorkItemError | RevisionConflictError,
) -> HTTPException:
    """把 typed Attention 领域拒绝映射为稳定 HTTP 状态。"""

    code = getattr(error, "code", "ATTENTION_CONFLICT")
    conflict_codes = {
        "ATTENTION_COMMAND_ID_REUSED",
        "WORK_ITEM_NOT_ACTIVE",
        "WORK_ITEM_ACTOR_ALREADY_DECIDED",
        "WORK_ITEM_COMMAND_ID_REUSED",
    }
    if (
        isinstance(error, RevisionConflictError)
        or "CONFLICT" in code
        or "FENCED" in code
        or code in conflict_codes
    ):
        status = 409
    elif "CANDIDATE" in code or "FORBIDDEN" in code or "PERMISSION" in code:
        status = 403
    else:
        status = 400
    return HTTPException(status_code=status, detail=code)
