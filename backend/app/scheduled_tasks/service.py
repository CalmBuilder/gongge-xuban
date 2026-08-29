"""
@Time       : 2026/08/11 01:05
@Author     : zhanglp8181
@File       : service.py
@CallChain  : ScheduledTask API/worker → AgentLoop/DynamicTaskAgent → ScheduledTaskRun 对账
@Description: 管理调度定义、到期租约、稳定运行来源及动态 Execution 的挂起恢复和终态回写。
"""

from __future__ import annotations

import calendar
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from threading import BoundedSemaphore
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.branching import model_for_agent
from app.agents.session_snapshot import anchor_chat_session
from app.core import AgentLoop
from app.db import engine
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    Message,
    ScheduledTask,
    ScheduledTaskRun,
    SopInstance,
    User,
    new_id,
    utc_now,
)
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation
from app.scheduled_tasks.schema import (
    ScheduledTaskCreateRequest,
    ScheduledTaskDraftRead,
    ScheduledTaskRead,
    ScheduledTaskRunRead,
    ScheduledTaskUpdateRequest,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse
from app.sop_runtime.execution_control import canonical_checksum
from app.security.permissions import agent_owned_by_user as _agent_owned_by_user
from app.security.permissions import can_use_agent_in_chat
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TASK_TIME = "09:00"
LEASE_SECONDS = 15 * 60
WORKER_SLEEP_SECONDS = 5
MISFIRE_GRACE_SECONDS = 60
SCHEDULE_TYPES = {"once", "daily", "weekly", "monthly"}
SCHEDULE_DISPATCH_WORKERS = 4
SCHEDULE_DISPATCH_CAPACITY = 16
_scheduled_dispatch_executor = ThreadPoolExecutor(
    max_workers=SCHEDULE_DISPATCH_WORKERS,
    thread_name_prefix="gongge-xuban-schedule-run",
)
_scheduled_dispatch_slots = BoundedSemaphore(SCHEDULE_DISPATCH_CAPACITY)


class _ScheduledRunSuperseded(RuntimeError):
    """表示删除或暂停已先把调度运行推进到终态，迟到 worker 应安静退出。"""


class _LLMScheduledTaskDraft(BaseModel):
    should_create: bool = False
    title: str = ""
    prompt: str = ""
    description: str | None = None
    schedule_type: str = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str | None = None
    rrule: str | None = None
    confidence: float = 0.0
    reason: str | None = None


SCHEDULE_DRAFT_PROMPT = """
你是共格·序伴数字员工的自动任务配置解析器。
用户已经在对话框中选择了“创建定时任务”模式。请把用户输入整理成一个可编辑的自动任务草案。
如果用户没有写清时间计划，默认每天 09:00 执行；如果用户没有写清任务目标，用原始输入作为执行内容。

返回一个 JSON object，字段如下：
- should_create: boolean
- title: 12 到 32 个中文字符，概括自动任务名称
- prompt: 每次到点后交给数字员工的新会话任务描述，不要包含“帮我设个定时任务”等配置话术
- description: 可选，解释为什么这样拆解
- schedule_type: one of "once", "daily", "weekly", "monthly"
- schedule:
  - once: {"run_at": "YYYY-MM-DDTHH:mm:ss±HH:MM"}
  - daily: {"time": "HH:mm"}
  - weekly: {"time": "HH:mm", "weekdays": [0-6]}，0=周一，6=周日
  - monthly: {"time": "HH:mm", "day_of_month": 1-31}
- timezone: IANA 时区，默认使用 default_timezone
- rrule: 可选 RRULE 字符串
- confidence: 0 到 1
- reason: 简短说明

时间不完整时可以合理补齐：只说“每天”默认 09:00；只说“每周一”默认 09:00。
调度类型判断规则：
- 用户只给出一个具体时间点，例如“下午2点10分”“14:10”“今晚8点”，且没有明确“每天/每日/每周/每月/定期/重复”等周期要求时，生成 once。
- once.run_at 使用 now 所在日期和用户给出的时间；如果该时间已经过去，则顺延到下一天。
- 只有用户明确说“每天/每日/每晚/每早/每周/每月/工作日/定期/重复”等周期要求时，才生成 daily/weekly/monthly。
不要输出 Markdown，不要输出解释文本，只输出 JSON。
"""


def scheduled_task_read(row: ScheduledTask) -> ScheduledTaskRead:
    return ScheduledTaskRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        created_by_user_id=row.created_by_user_id,
        title=row.title,
        prompt=row.prompt,
        description=row.description,
        schedule_type=row.schedule_type,
        schedule=row.schedule_json or {},
        timezone=row.timezone,
        rrule=row.rrule,
        status=row.status,
        concurrency_policy=row.concurrency_policy,
        misfire_policy=row.misfire_policy,
        max_runs=row.max_runs,
        end_at=_dt(row.end_at),
        next_run_at=_dt(row.next_run_at),
        last_run_at=_dt(row.last_run_at),
        last_status=row.last_status,
        run_count=row.run_count,
        source_session_id=row.source_session_id,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def scheduled_task_run_read(row: ScheduledTaskRun, task: ScheduledTask | None = None) -> ScheduledTaskRunRead:
    return ScheduledTaskRunRead(
        id=row.id,
        tenant_id=row.tenant_id,
        scheduled_task_id=row.scheduled_task_id,
        task_title=task.title if task else None,
        task_status=task.status if task else None,
        agent_id=row.agent_id,
        user_id=row.user_id,
        session_id=row.session_id,
        execution_id=row.execution_id,
        source_kind=row.source_kind,
        source_ref=row.source_ref,
        source_checksum=row.source_checksum,
        scheduled_for=row.scheduled_for.isoformat(),
        status=row.status,
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        result_summary=row.result_summary,
        error=row.error,
        trace=row.trace_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def create_scheduled_task(
    db: Session,
    request: ScheduledTaskCreateRequest,
    current_user: User,
) -> ScheduledTask:
    ensure_tenant(db, request.tenant_id)
    _ensure_agent_access(db, request.tenant_id, request.agent_id, current_user)
    schedule = normalize_schedule(request.schedule_type, request.schedule, request.timezone)
    now = utc_now()
    end_at = parse_user_datetime(request.end_at, request.timezone) if request.end_at else None
    row = ScheduledTask(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        created_by_user_id=current_user.id,
        title=_nonempty(request.title, "自动任务名称不能为空", 80),
        prompt=_nonempty(request.prompt, "自动任务描述不能为空", 10000),
        description=(request.description or "").strip() or None,
        schedule_type=request.schedule_type,
        schedule_json=schedule,
        timezone=request.timezone or DEFAULT_TIMEZONE,
        rrule=(request.rrule or "").strip() or build_rrule(request.schedule_type, schedule),
        status=request.status,
        concurrency_policy=request.concurrency_policy,
        misfire_policy=request.misfire_policy,
        max_runs=request.max_runs,
        end_at=end_at,
        source_session_id=request.source_session_id,
        metadata_json=request.metadata or {},
        created_at=now,
        updated_at=now,
    )
    row.next_run_at = compute_next_run_at(row, after=now)
    if row.status != "active":
        row.next_run_at = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_scheduled_task(
    db: Session,
    row: ScheduledTask,
    request: ScheduledTaskUpdateRequest,
    current_user: User,
) -> ScheduledTask:
    """在 Agent→Task 锁序下更新任务，并把暂停/归档后的未启动运行收敛为 skipped。"""

    _ensure_task_access(row, current_user)
    if request.agent_id is not None and request.agent_id != row.agent_id:
        _lock_agent_rows_for_mutation(db, request.tenant_id, (row.agent_id, request.agent_id))
    _ensure_task_agent_mutable(db, row)
    row = _lock_scheduled_task_for_mutation(db, row)
    _ensure_task_access(row, current_user)
    if request.agent_id is not None and request.agent_id != row.agent_id:
        _ensure_agent_access(db, request.tenant_id, request.agent_id, current_user)
        row.agent_id = request.agent_id
    if request.title is not None:
        row.title = _nonempty(request.title, "自动任务名称不能为空", 80)
    if request.prompt is not None:
        row.prompt = _nonempty(request.prompt, "自动任务描述不能为空", 10000)
    if request.description is not None:
        row.description = request.description.strip() or None
    if request.timezone is not None:
        row.timezone = request.timezone or DEFAULT_TIMEZONE
    if request.schedule_type is not None:
        row.schedule_type = request.schedule_type
    if request.schedule is not None or request.schedule_type is not None or request.timezone is not None:
        row.schedule_json = normalize_schedule(row.schedule_type, request.schedule or row.schedule_json, row.timezone)
        row.rrule = request.rrule if request.rrule is not None else build_rrule(row.schedule_type, row.schedule_json)
    elif request.rrule is not None:
        row.rrule = request.rrule.strip() or None
    if request.status is not None:
        row.status = request.status
    if request.concurrency_policy is not None:
        row.concurrency_policy = request.concurrency_policy
    if request.misfire_policy is not None:
        row.misfire_policy = request.misfire_policy
    if request.max_runs is not None:
        row.max_runs = request.max_runs
    if request.end_at is not None:
        row.end_at = parse_user_datetime(request.end_at, row.timezone) if request.end_at else None
    if request.metadata is not None:
        row.metadata_json = request.metadata
    row.updated_at = utc_now()
    row.next_run_at = compute_next_run_at(row, after=utc_now()) if row.status == "active" else None
    if row.status != "active":
        _skip_unstarted_scheduled_runs(db, row, reason=f"SCHEDULE_TASK_{row.status.upper()}")
        row.lease_owner = None
        row.lease_until = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def archive_scheduled_task(
    db: Session,
    row: ScheduledTask,
    current_user: User,
) -> ScheduledTask:
    """在锁定 Agent 后归档任务，停止未来唤醒并收敛没有 Execution 的活动运行。"""

    _ensure_task_access(row, current_user)
    _ensure_task_agent_mutable(db, row)
    row = _lock_scheduled_task_for_mutation(db, row)
    _ensure_task_access(row, current_user)
    _skip_unstarted_scheduled_runs(db, row, reason="SCHEDULE_TASK_ARCHIVED")
    row.status = "archived"
    row.next_run_at = None
    row.lease_owner = None
    row.lease_until = None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def detect_scheduled_task_draft(
    db: Session,
    tenant_id: str,
    agent_id: str,
    user_id: str,
    message: str,
    source_session_id: str | None = None,
    timezone: str | None = None,
) -> ScheduledTaskDraftRead | None:
    ensure_tenant(db, tenant_id)
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id or agent.is_overall or agent.status != "active":
        return None
    user_timezone = _safe_timezone(timezone)
    llm_draft = _detect_with_llm(db, tenant_id, agent_id, message, user_timezone)
    if llm_draft is None or not llm_draft.should_create:
        return None
    draft = llm_draft
    try:
        schedule_type = _normalize_schedule_type(draft.schedule_type)
        draft_timezone = _safe_timezone(draft.timezone, user_timezone)
        schedule = normalize_schedule(schedule_type, draft.schedule, draft_timezone)
    except HTTPException:
        return None
    title = (draft.title or _compact_title(message)).strip()[:80]
    prompt = (draft.prompt or _execution_goal_from_message(message)).strip()
    if not prompt:
        return None
    return ScheduledTaskDraftRead(
        should_create=True,
        tenant_id=tenant_id,
        agent_id=agent_id,
        title=title,
        prompt=prompt,
        description=draft.description,
        schedule_type=schedule_type,
        schedule=schedule,
        timezone=draft_timezone,
        rrule=draft.rrule or build_rrule(schedule_type, schedule),
        confidence=draft.confidence,
        reason=draft.reason,
        source_session_id=source_session_id,
    )


def due_scheduled_tasks(db: Session, now: datetime | None = None, limit: int = 10) -> list[ScheduledTask]:
    """领取到期且归属活动 Agent 的任务，避免墓碑 Agent 继续产生调度运行。"""

    now = now or utc_now()
    candidate_ids = db.exec(
        select(ScheduledTask.id)
        .join(AgentProfile, AgentProfile.id == ScheduledTask.agent_id)
        .where(
            AgentProfile.tenant_id == ScheduledTask.tenant_id,
            AgentProfile.status == "active",
            ScheduledTask.status == "active",
            ScheduledTask.next_run_at <= now,  # type: ignore[operator]
            or_(ScheduledTask.lease_until == None, ScheduledTask.lease_until < now),  # noqa: E711
        )
        .order_by(ScheduledTask.next_run_at)
        .limit(limit)
    ).all()
    lease_owner = f"{socket.gethostname()}:{new_id('worker')}"
    claimed: list[ScheduledTask] = []
    for task_id in candidate_ids:
        candidate = db.get(ScheduledTask, task_id)
        if candidate is None:
            continue
        prepared = _lock_scheduled_task_for_preparation(db, candidate)
        if prepared is None:
            continue
        locked_task, _agent = prepared
        result = db.exec(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.status == "active",
                select(AgentProfile.id)
                .where(
                    AgentProfile.id == ScheduledTask.agent_id,
                    AgentProfile.tenant_id == ScheduledTask.tenant_id,
                    AgentProfile.status == "active",
                )
                .exists(),
                ScheduledTask.next_run_at <= now,  # type: ignore[operator]
                or_(ScheduledTask.lease_until == None, ScheduledTask.lease_until < now),  # noqa: E711
            )
            .values(
                lease_owner=lease_owner,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            continue
        locked_task.lease_owner = lease_owner
        locked_task.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        locked_task.updated_at = now
        claimed.append(locked_task)
    if claimed:
        db.commit()
        for row in claimed:
            db.refresh(row)
    return claimed


def execute_scheduled_task(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None = None,
    manual: bool = False,
) -> ScheduledTaskRun | None:
    """幂等准备并执行一次调度任务；Agent 归档时返回空并不创建新事实。"""

    scheduled_for = scheduled_for or task.next_run_at or utc_now()
    dispatch_owner = None if manual else task.lease_owner
    run = _prepare_scheduled_task_run(
        db,
        task,
        scheduled_for,
        manual,
        lease_owner=dispatch_owner,
    )
    if run is None:
        return None
    if run.status != "running" or not run.session_id:
        return run
    return _execute_prepared_scheduled_task(
        db,
        task,
        run,
        manual=manual,
        lease_owner=dispatch_owner,
    )


def start_scheduled_task_async(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None = None,
    manual: bool = False,
) -> ScheduledTaskRun | None:
    """在有界执行池中启动调度入口；容量耗尽时释放租约并保留到期事实重试。"""

    dispatch_owner = None if manual else task.lease_owner
    if not manual and not _task_lease_is_current(task, dispatch_owner):
        return None
    if not _scheduled_dispatch_slots.acquire(blocking=False):
        _release_scheduled_task_lease(db, task, dispatch_owner)
        db.commit()
        return None
    scheduled_for = scheduled_for or task.next_run_at or utc_now()
    try:
        run = _prepare_scheduled_task_run(
            db,
            task,
            scheduled_for,
            manual,
            lease_owner=dispatch_owner,
        )
    except Exception:
        _scheduled_dispatch_slots.release()
        raise
    if run is None:
        _scheduled_dispatch_slots.release()
        return None
    if run.status != "running" or not run.session_id:
        _scheduled_dispatch_slots.release()
        return run
    try:
        _scheduled_dispatch_executor.submit(
            _execute_prepared_scheduled_task_in_background,
            task.id,
            run.id,
            manual,
            dispatch_owner,
        )
    except RuntimeError:
        _scheduled_dispatch_slots.release()
        _release_scheduled_task_lease(db, task, dispatch_owner)
        db.commit()
        raise
    return run


def _prepare_scheduled_task_run(
    db: Session,
    task: ScheduledTask,
    scheduled_for: datetime,
    manual: bool,
    lease_owner: str | None = None,
) -> ScheduledTaskRun | None:
    """幂等准备调度运行，并为新会话固化 scheduled 来源与员工能力版本。"""

    prepared = _lock_scheduled_task_for_preparation(db, task)
    if prepared is None:
        db.rollback()
        return None
    task, _agent = prepared
    if lease_owner is not None and not _task_lease_is_current(task, lease_owner):
        db.rollback()
        return None
    existing = db.exec(
        select(ScheduledTaskRun).where(
            ScheduledTaskRun.scheduled_task_id == task.id,
            ScheduledTaskRun.scheduled_for == scheduled_for,
        )
    ).first()
    if existing:
        return existing
    now = utc_now()
    if (
        not manual
        and task.misfire_policy == "skip"
        and scheduled_for < now - timedelta(seconds=MISFIRE_GRACE_SECONDS)
    ):
        run = _create_run(db, task, scheduled_for, "skipped", manual=manual)
        run.error = "自动任务已超过 misfire 宽限时间，按 skip 策略跳过本次唤醒。"
        run.finished_at = now
        _finish_task_schedule(db, task, scheduled_for, "skipped", manual)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    if task.concurrency_policy == "forbid":
        running = db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.scheduled_task_id == task.id,
                ScheduledTaskRun.status.in_(("queued", "running", "waiting")),
            )
        ).first()
        if running:
            run = _create_run(db, task, scheduled_for, "skipped", manual=manual)
            run.error = "上一轮自动任务仍在执行，已按 forbid 策略跳过本次唤醒。"
            run.finished_at = utc_now()
            _finish_task_schedule(db, task, scheduled_for, "skipped", manual)
            db.add(run)
            db.commit()
            db.refresh(run)
            return run

    run = _create_run(db, task, scheduled_for, "running", manual=manual)
    try:
        # 在同一事务中先落 Run、会话和 Agent 能力锚点，避免删除赢得竞态后留下无会话的运行事实。
        db.flush()
        session = ChatSession(
            id=new_id("session"),
            tenant_id=task.tenant_id,
            user_id=task.created_by_user_id,
            agent_id=task.agent_id,
            title=f"自动任务：{task.title}",
            status="active",
        )
        anchor_chat_session(db, session, _agent, origin="scheduled")
        db.add(session)
        db.flush()
        run.session_id = session.id
        run.updated_at = utc_now()
        db.add(run)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.scheduled_task_id == task.id,
                ScheduledTaskRun.scheduled_for == scheduled_for,
            )
        ).first()
        if existing:
            return existing
        raise
    db.refresh(run)
    return run


def _lock_scheduled_task_for_preparation(
    db: Session,
    task: ScheduledTask,
) -> tuple[ScheduledTask, AgentProfile] | None:
    """在创建新 Run 前按 Agent→Task 加锁，阻断删除竞态制造半成品事实。"""

    agent = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == task.tenant_id,
            AgentProfile.id == task.agent_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    current_task = db.exec(
        select(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == task.tenant_id,
            ScheduledTask.id == task.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        agent is None
        or agent.status != "active"
        or agent.is_overall
        or _agent_deletion_state(agent) in {"deleting", "deletion_pending", "deleted"}
        or current_task is None
        or current_task.status != "active"
    ):
        return None
    return current_task, agent


def _lock_scheduled_task_for_mutation(db: Session, task: ScheduledTask) -> ScheduledTask:
    """在 Agent 已锁定后锁定最新 Task 行，避免更新请求覆盖并发的任务状态。"""

    current = db.exec(
        select(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == task.tenant_id,
            ScheduledTask.id == task.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if current is None:
        raise HTTPException(status_code=404, detail="自动任务不存在")
    return current


def _skip_unstarted_scheduled_runs(
    db: Session,
    task: ScheduledTask,
    *,
    reason: str,
) -> None:
    """任务停止接收新唤醒时，终结尚未绑定 Execution 的活动 Run，防止无人消费。"""

    now = utc_now()
    rows = db.exec(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.tenant_id == task.tenant_id,
            ScheduledTaskRun.scheduled_task_id == task.id,
            ScheduledTaskRun.execution_id.is_(None),
            ScheduledTaskRun.status.in_(("queued", "running", "waiting")),
        )
        .with_for_update()
    ).all()
    for row in rows:
        row.status = "skipped"
        row.error = reason[:1000]
        row.finished_at = now
        row.updated_at = now
        db.add(row)


def _task_lease_is_current(task: ScheduledTask, lease_owner: str | None) -> bool:
    """判断异步派发 token 是否仍是活动任务当前持有者。"""

    return bool(
        lease_owner
        and task.lease_owner == lease_owner
        and task.lease_until is not None
        and task.lease_until > utc_now()
    )


def _release_scheduled_task_lease(
    db: Session,
    task: ScheduledTask,
    lease_owner: str | None,
) -> bool:
    """仅以 owner CAS 释放任务 lease，迟到 worker 不能清掉新 owner 的租约。"""

    if not lease_owner:
        return False
    now = utc_now()
    result = db.exec(
        update(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == task.tenant_id,
            ScheduledTask.id == task.id,
            ScheduledTask.lease_owner == lease_owner,
        )
        .values(lease_owner=None, lease_until=None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) == 1:
        task.lease_owner = None
        task.lease_until = None
        task.updated_at = now
        return True
    return False


def _execute_prepared_scheduled_task_in_background(
    task_id: str,
    run_id: str,
    manual: bool,
    lease_owner: str | None,
) -> None:
    """使用独立会话推进已持久化运行，并确保任何退出路径都会归还有界容量。"""

    try:
        with Session(engine) as db:
            task = db.get(ScheduledTask, task_id)
            run = db.get(ScheduledTaskRun, run_id)
            if not task or not run:
                return
            _execute_prepared_scheduled_task(
                db,
                task,
                run,
                manual=manual,
                lease_owner=lease_owner,
            )
    finally:
        _scheduled_dispatch_slots.release()


def _execute_prepared_scheduled_task(
    db: Session,
    task: ScheduledTask,
    run: ScheduledTaskRun,
    *,
    manual: bool,
    lease_owner: str | None = None,
) -> ScheduledTaskRun:
    """执行已准备的调度运行，并在每个外部阶段用 Agent/Run/Task 锁保护终态。"""

    if not _lock_scheduled_run_for_execution(db, task, run, lease_owner=lease_owner):
        if lease_owner is not None:
            _release_scheduled_task_lease(db, task, lease_owner)
            db.commit()
        return run
    try:
        if not run.session_id:
            raise RuntimeError("自动任务缺少独立会话")
        _ensure_scheduled_execution_access(db, task)
        request = ChatTurnRequest(
            tenant_id=task.tenant_id,
            session_id=run.session_id,
            agent_id=task.agent_id,
            user_id=task.created_by_user_id,
            client_turn_id=f"scheduled-run:{run.id}",
            message=automatic_task_message(task),
            channel="scheduled_task",
            interaction_mode="scheduled_task",
            client_timezone=task.timezone,
        )
        result: ChatTurnResponse | None = None
        for seq, item in enumerate(AgentLoop(db).handle_turn_stream(request), start=1):
            _record_scheduled_task_stream_event(
                db,
                run,
                run.session_id,
                seq,
                item,
                lease_owner=lease_owner,
            )
            if item.get("event") in {"complete", "done"} and isinstance(item.get("data"), dict):
                result = ChatTurnResponse.model_validate(item["data"])
        if result is None:
            raise RuntimeError("自动任务执行未返回完整结果")
        dynamic_execution = _scheduled_dynamic_execution(db, run)
        if dynamic_execution is not None:
            run.execution_id = dynamic_execution.id
        run.result_summary = result.reply[:500]
        run.trace_json = {
            "router_decision": result.router_decision.model_dump(mode="json")
            if result.router_decision
            else None,
            "session_state": result.session_state.model_dump(mode="json"),
            "execution_id": dynamic_execution.id if dynamic_execution is not None else None,
        }
        if not _lock_scheduled_run_for_execution(db, task, run, lease_owner=lease_owner):
            return run
        if dynamic_execution is not None and dynamic_execution.status in {
            "created",
            "running",
            "waiting",
        }:
            run.status = "waiting"
            _finish_task_schedule(db, task, run.scheduled_for, "waiting", manual)
        else:
            run.status = "succeeded"
            run.finished_at = utc_now()
            _finish_task_schedule(db, task, run.scheduled_for, "succeeded", manual)
    except _ScheduledRunSuperseded:
        db.rollback()
        return run
    except Exception as exc:
        if not _lock_scheduled_run_for_execution(db, task, run, lease_owner=lease_owner):
            db.rollback()
            return run
        if run.session_id:
            try:
                _record_scheduled_task_stream_event(
                    db,
                    run,
                    run.session_id,
                    0,
                    {"event": "error", "data": {"message": str(exc), "sessionId": run.session_id}},
                    lease_owner=lease_owner,
                )
            except _ScheduledRunSuperseded:
                db.rollback()
                return run
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = utc_now()
        _finish_task_schedule(db, task, run.scheduled_for, "failed", manual)
    finally:
        if lease_owner is not None:
            _release_scheduled_task_lease(db, task, lease_owner)
        run.updated_at = utc_now()
        if run.status in {"queued", "running", "waiting", "succeeded", "failed", "skipped"}:
            db.add(run)
        db.commit()
        db.refresh(run)
    return run


def _lock_scheduled_run_for_execution(
    db: Session,
    task: ScheduledTask,
    run: ScheduledTaskRun,
    *,
    lease_owner: str | None = None,
) -> bool:
    """按 Agent→Run→Task 顺序加锁并刷新状态，阻止迟到 worker 覆盖删除终态。"""

    agent = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == task.tenant_id,
            AgentProfile.id == task.agent_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    current_run = db.exec(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.tenant_id == run.tenant_id,
            ScheduledTaskRun.id == run.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    current_task = db.exec(
        select(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == task.tenant_id,
            ScheduledTask.id == task.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        agent is None
        or agent.status != "active"
        or _agent_deletion_state(agent) in {"deleting", "deletion_pending", "deleted"}
        or current_run is None
        or current_run.status not in {"queued", "running", "waiting"}
        or current_task is None
        or current_task.status not in {"active", "paused", "completed", "archived"}
    ):
        return False
    if (
        lease_owner is not None
        and current_task.status == "active"
        and not _task_lease_is_current(current_task, lease_owner)
    ):
        return False
    if current_run is not run:
        run.status = current_run.status
        run.updated_at = current_run.updated_at
    if current_task is not task:
        task.status = current_task.status
        task.lease_owner = current_task.lease_owner
        task.lease_until = current_task.lease_until
    return True


def _agent_deletion_state(agent: AgentProfile) -> str | None:
    """读取 Agent 删除墓碑状态，兼容历史 metadata 中缺失或异常的值。"""

    deletion = (agent.metadata_json or {}).get("agent_deletion")
    return deletion.get("state") if isinstance(deletion, dict) else None


def reconcile_scheduled_dynamic_runs(db: Session, *, limit: int = 100) -> int:
    """把已挂起调度关联的动态 Execution 终态幂等回写到原 ScheduledTaskRun。"""

    runs = db.exec(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.execution_id != None,  # noqa: E711
            ScheduledTaskRun.status.in_(("running", "waiting")),
        )
        .order_by(ScheduledTaskRun.scheduled_for, ScheduledTaskRun.id)
        .limit(limit)
    ).all()
    settled = 0
    for run in runs:
        task = db.get(ScheduledTask, run.scheduled_task_id)
        if task is None or task.tenant_id != run.tenant_id:
            continue
        if not _lock_scheduled_run_for_execution(db, task, run):
            continue
        execution = db.exec(
            select(SopInstance)
            .where(
                SopInstance.tenant_id == run.tenant_id,
                SopInstance.id == run.execution_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if execution is None:
            continue
        if execution.status in {"created", "running", "waiting"}:
            if run.status != "waiting":
                run.status = "waiting"
                run.updated_at = utc_now()
                db.add(run)
            continue
        run.status = "succeeded" if execution.status == "succeeded" else "failed"
        run.finished_at = execution.completed_at or utc_now()
        if run.status == "succeeded":
            message = db.exec(
                select(Message)
                .where(
                    Message.tenant_id == run.tenant_id,
                    Message.session_id == execution.session_id,
                    Message.role == "assistant",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
            ).first()
            if message is not None:
                run.result_summary = message.content[:500]
        if run.status == "failed":
            reason = execution.terminal_reason_json or {}
            run.error = str(reason.get("code") or execution.status)[:1000]
        run.trace_json = {
            **dict(run.trace_json or {}),
            "execution_id": execution.id,
            "execution_status": execution.status,
        }
        run.updated_at = utc_now()
        db.add(run)
        _refresh_latest_task_status(db, run)
        settled += 1
    if runs:
        db.commit()
    return settled


def _record_scheduled_task_stream_event(
    db: Session,
    run: ScheduledTaskRun,
    session_id: str,
    seq: int,
    item: dict[str, Any],
    *,
    lease_owner: str | None = None,
) -> None:
    task = db.get(ScheduledTask, run.scheduled_task_id)
    if task is None or not _lock_scheduled_run_for_execution(
        db,
        task,
        run,
        lease_owner=lease_owner,
    ):
        raise _ScheduledRunSuperseded("调度运行已被删除或暂停")
    event = str(item.get("event") or "")
    data = item.get("data")
    if not isinstance(data, dict):
        data = {"value": data}
    payload = dict(data)
    payload.setdefault("sessionId", session_id)
    db.add(
        AgentEvent(
            tenant_id=run.tenant_id,
            session_id=session_id,
            event_type="scheduled_task_stream_event",
            payload_json={
                "run_id": run.id,
                "seq": seq,
                "event": event,
                "data": payload,
            },
            created_at=utc_now(),
        )
    )
    run.updated_at = utc_now()
    db.add(run)
    db.commit()


def automatic_task_message(task: ScheduledTask) -> str:
    return task.prompt.strip() or task.title


def compute_next_run_at(task: ScheduledTask, after: datetime | None = None) -> datetime | None:
    if task.schedule_type == "once":
        run_at = parse_user_datetime(str((task.schedule_json or {}).get("run_at") or ""), task.timezone)
        return run_at if run_at and run_at > (after or utc_now()) else None
    after_local = _to_local(after or utc_now(), task.timezone)
    schedule = task.schedule_json or {}
    if task.schedule_type == "daily":
        candidate = datetime.combine(after_local.date(), _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME)))
        candidate = candidate.replace(tzinfo=_tz(task.timezone))
        if candidate <= after_local:
            candidate += timedelta(days=1)
        return _to_utc_naive(candidate)
    if task.schedule_type == "weekly":
        weekdays = _normalize_weekdays(schedule.get("weekdays") or [after_local.weekday()])
        target_time = _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME))
        best: datetime | None = None
        for offset in range(0, 8):
            day = after_local.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            candidate = datetime.combine(day, target_time).replace(tzinfo=_tz(task.timezone))
            if candidate <= after_local:
                continue
            if not best or candidate < best:
                best = candidate
        return _to_utc_naive(best) if best else None
    if task.schedule_type == "monthly":
        target_time = _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME))
        day_of_month = _normalize_day_of_month(schedule.get("day_of_month") or 1)
        year = after_local.year
        month = after_local.month
        for _ in range(14):
            day = min(day_of_month, calendar.monthrange(year, month)[1])
            candidate = datetime(year, month, day, target_time.hour, target_time.minute, tzinfo=_tz(task.timezone))
            if candidate > after_local:
                return _to_utc_naive(candidate)
            month += 1
            if month > 12:
                year += 1
                month = 1
    return None


def normalize_schedule(schedule_type: str, schedule: dict[str, Any], timezone: str) -> dict[str, Any]:
    schedule_type = _normalize_schedule_type(schedule_type)
    _tz(timezone)
    raw = schedule or {}
    if schedule_type == "once":
        run_at = raw.get("run_at") or raw.get("datetime") or raw.get("start_at")
        parsed = parse_user_datetime(str(run_at or ""), timezone)
        if not parsed:
            raise HTTPException(status_code=400, detail="一次性自动任务需要填写执行时间")
        return {"run_at": _to_local(parsed, timezone).isoformat()}
    if schedule_type == "daily":
        return {"time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME)))}
    if schedule_type == "weekly":
        return {
            "time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME))),
            "weekdays": _normalize_weekdays(raw.get("weekdays") or [0]),
        }
    if schedule_type == "monthly":
        return {
            "time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME))),
            "day_of_month": _normalize_day_of_month(raw.get("day_of_month") or 1),
        }
    raise HTTPException(status_code=400, detail="不支持的自动任务调度类型")


def build_rrule(schedule_type: str, schedule: dict[str, Any]) -> str | None:
    time_text = str(schedule.get("time") or DEFAULT_TASK_TIME)
    hour, minute = time_text.split(":", 1)
    if schedule_type == "once":
        return None
    if schedule_type == "daily":
        return f"FREQ=DAILY;BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
    if schedule_type == "weekly":
        byday = ",".join(["MO", "TU", "WE", "TH", "FR", "SA", "SU"][int(day)] for day in schedule.get("weekdays", [0]))
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
    if schedule_type == "monthly":
        return (
            f"FREQ=MONTHLY;BYMONTHDAY={int(schedule.get('day_of_month') or 1)};"
            f"BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
        )
    return None


def parse_user_datetime(value: str, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz(timezone))
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _create_run(
    db: Session,
    task: ScheduledTask,
    scheduled_for: datetime,
    status: str,
    *,
    manual: bool,
) -> ScheduledTaskRun:
    """用任务定义和到期时间生成稳定来源身份，网络或租约重放不会改变语义。"""

    source_kind = "manual" if manual else "schedule"
    source_ref = f"scheduled-task:{task.id}:{source_kind}:{scheduled_for.isoformat()}"
    source_snapshot = {
        "scheduled_task_id": task.id,
        "tenant_id": task.tenant_id,
        "agent_id": task.agent_id,
        "initiator_user_id": task.created_by_user_id,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "scheduled_for": scheduled_for.isoformat(),
        "prompt": task.prompt,
        "timezone": task.timezone,
        "schedule_type": task.schedule_type,
        "schedule": dict(task.schedule_json or {}),
        "rrule": task.rrule,
        "concurrency_policy": task.concurrency_policy,
        "misfire_policy": task.misfire_policy,
    }
    run = ScheduledTaskRun(
        tenant_id=task.tenant_id,
        scheduled_task_id=task.id,
        agent_id=task.agent_id,
        user_id=task.created_by_user_id,
        source_kind=source_kind,
        source_ref=source_ref,
        source_snapshot_json=source_snapshot,
        source_checksum=canonical_checksum(source_snapshot),
        scheduled_for=scheduled_for,
        status=status,
        started_at=utc_now() if status == "running" else None,
    )
    db.add(run)
    return run


def _scheduled_dynamic_execution(db: Session, run: ScheduledTaskRun) -> SopInstance | None:
    """按不可变 run identity 查找本轮唯一动态 Execution，不依赖聊天标题或尾消息。"""

    return db.exec(
        select(SopInstance).where(
            SopInstance.tenant_id == run.tenant_id,
            SopInstance.kind == "dynamic_task",
            SopInstance.source_kind == "schedule",
            SopInstance.source_ref == run.id,
        )
    ).first()


def _ensure_scheduled_execution_access(
    db: Session,
    task: ScheduledTask,
) -> tuple[User, AgentProfile]:
    """每次到期重新校验发起成员与 Agent 使用关系，历史配置不能永久授权。"""

    user = db.get(User, task.created_by_user_id)
    agent = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == task.tenant_id,
            AgentProfile.id == task.agent_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        user is None
        or user.tenant_id != task.tenant_id
        or user.membership_status != "active"
    ):
        raise RuntimeError("SCHEDULE_INITIATOR_INACTIVE")
    if agent is None or not can_use_agent_in_chat(db, agent, user):
        raise RuntimeError("SCHEDULE_AGENT_ACCESS_DENIED")
    return user, agent


def _refresh_latest_task_status(db: Session, run: ScheduledTaskRun) -> None:
    """仅允许最新一轮完成事实更新任务摘要，迟到旧 run 不覆盖新状态。"""

    newer = db.exec(
        select(ScheduledTaskRun.id).where(
            ScheduledTaskRun.scheduled_task_id == run.scheduled_task_id,
            ScheduledTaskRun.scheduled_for > run.scheduled_for,
        )
    ).first()
    if newer is not None:
        return
    task = db.get(ScheduledTask, run.scheduled_task_id)
    if task is None or task.tenant_id != run.tenant_id:
        return
    task.last_status = run.status
    task.last_run_at = run.finished_at
    task.updated_at = utc_now()
    db.add(task)


def _finish_task_schedule(
    db: Session,
    task: ScheduledTask,
    scheduled_for: datetime,
    status: str,
    manual: bool,
) -> None:
    """记录本轮结果；任务已暂停/归档时绝不重新设置下一次唤醒时间。"""

    now = utc_now()
    task.last_run_at = now
    task.last_status = status
    task.run_count += 1
    if task.status != "active":
        task.next_run_at = None
    elif not manual:
        next_run = compute_next_run_at(task, after=scheduled_for + timedelta(seconds=1))
        if task.max_runs is not None and task.run_count >= task.max_runs:
            task.status = "completed"
            task.next_run_at = None
        elif task.end_at and next_run and next_run > task.end_at:
            task.status = "completed"
            task.next_run_at = None
        else:
            task.next_run_at = next_run
            if task.schedule_type == "once" and next_run is None:
                task.status = "completed"
    db.add(task)


def _detect_with_llm(
    db: Session,
    tenant_id: str,
    agent_id: str,
    message: str,
    timezone: str,
) -> _LLMScheduledTaskDraft | None:
    model_config = model_for_agent(db, tenant_id, agent_id, "router") or model_for_agent(db, tenant_id, agent_id)
    if not model_config:
        return None
    try:
        with llm_operation("scheduled_task.detect"):
            raw = LLMClient(model_config).generate_json(
                SCHEDULE_DRAFT_PROMPT,
                {
                    "now": _to_local(utc_now(), timezone).isoformat(),
                    "default_timezone": timezone,
                    "user_message": message,
                },
            )
        return _LLMScheduledTaskDraft.model_validate(raw)
    except (LLMError, ValidationError):
        return None


def _execution_goal_from_message(message: str) -> str:
    return message.strip()


def _compact_title(message: str) -> str:
    text = _execution_goal_from_message(message)
    text = re.sub(r"\s+", " ", text).strip(" ，,。")
    return (text[:28] or "自动任务").strip()


def _normalize_schedule_type(value: str) -> str:
    if value not in SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail="不支持的自动任务调度类型")
    return value


def _normalize_weekdays(value: Any) -> list[int]:
    if not isinstance(value, list):
        value = [value]
    days = sorted({int(item) for item in value if str(item).strip() != ""})
    if not days or any(day < 0 or day > 6 for day in days):
        raise HTTPException(status_code=400, detail="每周自动任务需要 0-6 的星期设置")
    return days


def _normalize_day_of_month(value: Any) -> int:
    day = int(value)
    if day < 1 or day > 31:
        raise HTTPException(status_code=400, detail="每月执行日需要在 1 到 31 之间")
    return day


def _parse_time(value: str) -> time:
    text = value.strip()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", text)
    if not match:
        raise HTTPException(status_code=400, detail="时间格式需要为 HH:mm")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HTTPException(status_code=400, detail="时间格式需要为 HH:mm")
    return time(hour, minute)


def _format_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _tz(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=400, detail="无效时区") from exc


def _safe_timezone(value: str | None, fallback: str = DEFAULT_TIMEZONE) -> str:
    candidate = (value or "").strip() or fallback
    try:
        _tz(candidate)
        return candidate
    except HTTPException:
        _tz(fallback)
        return fallback


def _to_local(value: datetime, timezone: str) -> datetime:
    source = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return source.astimezone(_tz(timezone))


def _to_utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _nonempty(value: str, message: str, max_length: int) -> str:
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=message)
    return text[:max_length]


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ensure_agent_access(db: Session, tenant_id: str, agent_id: str, current_user: User) -> AgentProfile:
    agent = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id == agent_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if not agent or agent.is_overall or agent.status != "active":
        raise HTTPException(status_code=404, detail="员工不可用")
    if _is_admin_user(current_user):
        return agent
    metadata = agent.metadata_json or {}
    owns_agent = _agent_owned_by_user(agent, current_user)
    in_gallery = metadata.get("published_to_gallery") is True
    if not (owns_agent or in_gallery):
        raise HTTPException(status_code=403, detail="无权为该员工设置自动任务")
    return agent


def _ensure_task_access(row: ScheduledTask, current_user: User) -> None:
    if _is_admin_user(current_user):
        return
    if row.created_by_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该自动任务")


def _ensure_task_agent_mutable(db: Session, row: ScheduledTask) -> AgentProfile:
    """按 Agent→Task 锁序阻断墓碑员工关联任务的迟到修改。"""

    agent = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == row.tenant_id,
            AgentProfile.id == row.agent_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    deletion = (agent.metadata_json or {}).get("agent_deletion") if agent else None
    deletion_state = deletion.get("state") if isinstance(deletion, dict) else None
    if (
        agent is None
        or agent.status != "active"
        or deletion_state in {"deleting", "deletion_pending", "deleted"}
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENT_TOMBSTONE_IMMUTABLE",
                "message": "数字员工已进入不可逆删除流程，关联自动任务不能继续修改。",
                "state": deletion_state or "unavailable",
            },
        )
    return agent


def _lock_agent_rows_for_mutation(
    db: Session,
    tenant_id: str,
    agent_ids: tuple[str, ...],
) -> dict[str, AgentProfile]:
    """按稳定 Agent 主键顺序预锁定调度迁移涉及的员工，避免双向换绑死锁。"""

    unique_ids = tuple(sorted(set(agent_ids)))
    if not unique_ids:
        return {}
    rows = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id.in_(unique_ids),
        )
        .order_by(AgentProfile.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(unique_ids):
        raise HTTPException(status_code=404, detail="员工不可用")
    return by_id
