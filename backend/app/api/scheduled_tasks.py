"""
@Time       : 2026/08/01 21:30
@Author     : zhanglp8181
@File       : scheduled_tasks.py
@CallChain  : 管理端/聊天端 → FastAPI → ScheduledTask service/数据库
@Description: 提供租户隔离的定时任务管理、运行记录分页和聊天草稿确认接口。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import Message, ScheduledTask, ScheduledTaskRun, User, utc_now
from app.scheduled_tasks.schema import (
    ScheduledTaskCreateRequest,
    ScheduledTaskDraftRead,
    ScheduledTaskDraftRequest,
    ScheduledTaskOverviewRead,
    ScheduledTaskPageRead,
    ScheduledTaskRead,
    ScheduledTaskRunPageRead,
    ScheduledTaskRunRead,
    ScheduledTaskUpdateRequest,
)
from app.scheduled_tasks.service import (
    archive_scheduled_task,
    create_scheduled_task,
    detect_scheduled_task_draft,
    scheduled_task_read,
    scheduled_task_run_read,
    start_scheduled_task_async,
    update_scheduled_task,
)
from app.security.auth import get_current_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant


enterprise_router = APIRouter(prefix="/api/enterprise/scheduled-tasks", tags=["enterprise:scheduled-tasks"])
chat_router = APIRouter(prefix="/api/chat/scheduled-tasks", tags=["chat:scheduled-tasks"])
chat_draft_router = APIRouter(prefix="/api/chat/scheduled-task-drafts", tags=["chat:scheduled-task-drafts"])

RunStatusFilter = Literal["all", "pending", "completed", "failed"]
TaskStatusFilter = Literal["all", "pending", "completed", "paused"]
RUN_STATUS_FILTERS: dict[RunStatusFilter, tuple[str, ...]] = {
    "all": (),
    "pending": ("queued", "running", "waiting"),
    "completed": ("succeeded",),
    "failed": ("failed", "skipped"),
}
TASK_STATUS_FILTERS: dict[TaskStatusFilter, tuple[str, ...]] = {
    "all": (),
    "pending": ("active",),
    "completed": ("completed",),
    "paused": ("paused",),
}


@enterprise_router.get("", response_model=list[ScheduledTaskRead])
def list_enterprise_scheduled_tasks(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    status: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ScheduledTaskRead]:
    _ensure_request_tenant(tenant_id, current_user)
    rows = _list_tasks(db, tenant_id, current_user, agent_id, status)
    return [scheduled_task_read(row) for row in rows]


@enterprise_router.get("/page", response_model=ScheduledTaskPageRead)
def page_enterprise_scheduled_tasks(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    status_filter: TaskStatusFilter = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskPageRead:
    """在租户、访问者、员工和非归档条件过滤后返回稳定任务页及状态统计。"""

    _ensure_request_tenant(tenant_id, current_user)
    conditions = _task_conditions(db, tenant_id, current_user, agent_id, None)
    conditions.append(ScheduledTask.status != "archived")
    status_rows = db.exec(
        select(ScheduledTask.status, func.count())
        .where(*conditions)
        .group_by(ScheduledTask.status)
    ).all()
    status_counts = {status: int(count) for status, count in status_rows}

    filtered_conditions = list(conditions)
    statuses = TASK_STATUS_FILTERS[status_filter]
    if statuses:
        filtered_conditions.append(ScheduledTask.status.in_(statuses))
    total = int(
        db.exec(select(func.count()).select_from(ScheduledTask).where(*filtered_conditions)).one()
    )
    rows = db.exec(
        select(ScheduledTask)
        .where(*filtered_conditions)
        .order_by(ScheduledTask.updated_at.desc(), ScheduledTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ScheduledTaskPageRead(
        items=[scheduled_task_read(row) for row in rows],
        total=total,
        status_counts=status_counts,
        page=page,
        page_size=page_size,
    )


@enterprise_router.get("/overview", response_model=ScheduledTaskOverviewRead)
def overview_enterprise_scheduled_tasks(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskOverviewRead:
    """为员工档案返回当前访问范围的启用任务总数及最近两条摘要。"""

    _ensure_request_tenant(tenant_id, current_user)
    conditions = _task_conditions(db, tenant_id, current_user, agent_id, "active")
    active_count = int(
        db.exec(select(func.count()).select_from(ScheduledTask).where(*conditions)).one()
    )
    rows = db.exec(
        select(ScheduledTask)
        .where(*conditions)
        .order_by(ScheduledTask.updated_at.desc(), ScheduledTask.id.desc())
        .limit(2)
    ).all()
    return ScheduledTaskOverviewRead(
        active_count=active_count,
        active_items=[scheduled_task_read(row) for row in rows],
    )


@enterprise_router.post("", response_model=ScheduledTaskRead)
def create_enterprise_scheduled_task(
    request: ScheduledTaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    row = create_scheduled_task(db, request, current_user)
    return scheduled_task_read(row)


@enterprise_router.get("/runs", response_model=list[ScheduledTaskRunRead])
def list_enterprise_scheduled_task_runs_for_agent(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ScheduledTaskRunRead]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    conditions = [ScheduledTaskRun.tenant_id == tenant_id]
    if agent_id:
        conditions.append(ScheduledTaskRun.agent_id == agent_id)
    if status:
        conditions.append(ScheduledTaskRun.status == status)
    if not _is_admin_user(current_user):
        conditions.append(ScheduledTaskRun.user_id == current_user.id)
    rows = db.exec(
        select(ScheduledTaskRun, ScheduledTask)
        .join(
            ScheduledTask,
            ScheduledTaskRun.scheduled_task_id == ScheduledTask.id,
            isouter=True,
        )
        .where(*conditions)
        .order_by(ScheduledTaskRun.created_at.desc())
        .limit(limit)
    ).all()
    return [scheduled_task_run_read(run, task) for run, task in rows]


@enterprise_router.get("/runs/page", response_model=ScheduledTaskRunPageRead)
def page_enterprise_scheduled_task_runs(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    task_id: str | None = Query(None),
    status_filter: RunStatusFilter = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskRunPageRead:
    """在租户、访问者、员工和任务范围过滤后统计并稳定分页运行记录。"""

    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    scope_conditions = [ScheduledTaskRun.tenant_id == tenant_id]
    if agent_id:
        scope_conditions.append(ScheduledTaskRun.agent_id == agent_id)
    if task_id:
        task = _get_task(db, tenant_id, task_id, current_user)
        scope_conditions.append(ScheduledTaskRun.scheduled_task_id == task.id)
    if not _is_admin_user(current_user):
        scope_conditions.append(ScheduledTaskRun.user_id == current_user.id)

    run_total = int(
        db.exec(select(func.count()).select_from(ScheduledTaskRun).where(*scope_conditions)).one()
    )
    conditions = list(scope_conditions)
    statuses = RUN_STATUS_FILTERS[status_filter]
    if statuses:
        conditions.append(ScheduledTaskRun.status.in_(statuses))
    total = int(
        db.exec(select(func.count()).select_from(ScheduledTaskRun).where(*conditions)).one()
    )
    rows = db.exec(
        select(ScheduledTaskRun, ScheduledTask)
        .join(
            ScheduledTask,
            ScheduledTaskRun.scheduled_task_id == ScheduledTask.id,
            isouter=True,
        )
        .where(*conditions)
        .order_by(ScheduledTaskRun.scheduled_for.desc(), ScheduledTaskRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ScheduledTaskRunPageRead(
        items=[scheduled_task_run_read(run, task) for run, task in rows],
        total=total,
        run_total=run_total,
        page=page,
        page_size=page_size,
    )


@enterprise_router.get("/{task_id}", response_model=ScheduledTaskRead)
def get_enterprise_scheduled_task(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskRead:
    row = _get_task(db, tenant_id, task_id, current_user)
    return scheduled_task_read(row)


@enterprise_router.put("/{task_id}", response_model=ScheduledTaskRead)
def update_enterprise_scheduled_task(
    task_id: str,
    request: ScheduledTaskUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    row = _get_task(db, request.tenant_id, task_id, current_user)
    row = update_scheduled_task(db, row, request, current_user)
    return scheduled_task_read(row)


@enterprise_router.delete("/{task_id}")
def archive_enterprise_scheduled_task(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, bool]:
    """归档任务并在服务端锁序下停止未来唤醒、收敛未绑定 Execution 的运行。"""

    row = _get_task(db, tenant_id, task_id, current_user)
    archive_scheduled_task(db, row, current_user)
    return {"ok": True}


@enterprise_router.get("/{task_id}/runs", response_model=list[ScheduledTaskRunRead])
def list_enterprise_scheduled_task_runs(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ScheduledTaskRunRead]:
    row = _get_task(db, tenant_id, task_id, current_user)
    runs = db.exec(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.tenant_id == tenant_id, ScheduledTaskRun.scheduled_task_id == row.id)
        .order_by(ScheduledTaskRun.scheduled_for.desc())
    ).all()
    return [scheduled_task_run_read(item, row) for item in runs]


@enterprise_router.post("/{task_id}/run-now", response_model=ScheduledTaskRunRead)
def run_enterprise_scheduled_task_now(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskRunRead:
    row = _get_task(db, tenant_id, task_id, current_user)
    if row.status == "archived":
        raise HTTPException(status_code=400, detail="已删除的自动任务不能运行")
    run = start_scheduled_task_async(db, row, scheduled_for=utc_now(), manual=True)
    if run is None:
        raise HTTPException(status_code=503, detail="自动任务执行队列繁忙，请稍后重试")
    return scheduled_task_run_read(run, row)


@chat_router.get("", response_model=list[ScheduledTaskRead])
def list_chat_scheduled_tasks(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ScheduledTaskRead]:
    _ensure_request_tenant(tenant_id, current_user)
    rows = _list_tasks(db, tenant_id, current_user, agent_id, None)
    return [scheduled_task_read(row) for row in rows]


@chat_router.post("", response_model=ScheduledTaskRead)
def create_chat_scheduled_task(
    request: ScheduledTaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    row = create_scheduled_task(db, request, current_user)
    read = scheduled_task_read(row)
    _mark_chat_draft_created(db, row, read)
    return read


@chat_draft_router.post("", response_model=ScheduledTaskDraftRead)
def create_chat_scheduled_task_draft(
    request: ScheduledTaskDraftRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ScheduledTaskDraftRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    draft = detect_scheduled_task_draft(
        db,
        request.tenant_id,
        request.agent_id,
        current_user.id,
        request.message,
        request.session_id,
        request.timezone,
    )
    if not draft:
        return ScheduledTaskDraftRead(
            should_create=False,
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            source_session_id=request.session_id,
        )
    return draft


def _mark_chat_draft_created(db: Session, row: ScheduledTask, read: ScheduledTaskRead) -> None:
    if not row.source_session_id:
        return
    messages = db.exec(
        select(Message)
        .where(
            Message.tenant_id == row.tenant_id,
            Message.session_id == row.source_session_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at.desc())
        .limit(20)
    ).all()
    for message in messages:
        metadata = dict(message.metadata_json or {})
        if not isinstance(metadata.get("scheduled_task_draft"), dict):
            continue
        metadata["scheduled_task_created"] = read.model_dump(mode="json")
        message.metadata_json = metadata
        db.add(message)
        db.commit()
        return


def _list_tasks(
    db: Session,
    tenant_id: str,
    current_user: User,
    agent_id: str | None,
    status: str | None,
) -> list[ScheduledTask]:
    """按兼容数组接口的原有条件列出可访问任务。"""

    conditions = _task_conditions(db, tenant_id, current_user, agent_id, status)
    return db.exec(select(ScheduledTask).where(*conditions).order_by(ScheduledTask.updated_at.desc())).all()


def _task_conditions(
    db: Session,
    tenant_id: str,
    current_user: User,
    agent_id: str | None,
    status: str | None,
) -> list[ColumnElement[bool]]:
    """构造供兼容列表、分页和概览共享的租户与用户访问条件。"""

    ensure_tenant(db, tenant_id)
    conditions = [ScheduledTask.tenant_id == tenant_id]
    if agent_id:
        conditions.append(ScheduledTask.agent_id == agent_id)
    if status:
        conditions.append(ScheduledTask.status == status)
    if not _is_admin_user(current_user):
        conditions.append(ScheduledTask.created_by_user_id == current_user.id)
    return conditions


def _get_task(db: Session, tenant_id: str, task_id: str, current_user: User) -> ScheduledTask:
    _ensure_request_tenant(tenant_id, current_user)
    row = db.get(ScheduledTask, task_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="自动任务不存在")
    if not _is_admin_user(current_user) and row.created_by_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该自动任务")
    return row


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    if current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access another tenant")
