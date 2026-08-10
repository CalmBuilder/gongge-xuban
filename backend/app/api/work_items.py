"""
@Time       : 2026/07/22 10:36
@Author     : zhanglp8181
@File       : work_items.py
@CallChain  : 我的待办/审批详情 → FastAPI → SopWorkItemService/Coordinator
@Description: 提供候选工作项查询、认领、释放和结构化决定接口，并按服务端资格返回允许动作。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlmodel import Session, select
from sqlmodel.sql.expression import SelectOfScalar

from app.db import get_session
from app.db.models import (
    AgentEvent,
    ChatSession,
    ExecutionSignal,
    SopInstance,
    SopWorkItem,
    SopWorkItemCandidate,
    User,
)
from app.organization.permissions import user_permission_codes
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.execution_control import ExecutionControlError, ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionConflictError
from app.sop_runtime.state_machine import RevisionConflictError
from app.sop_runtime.work_items import (
    ACTIVE_WORK_ITEM_STATUSES,
    SopWorkItemService,
    WorkItemError,
)


router = APIRouter(prefix="/api/work-items", tags=["work-items"])


class WorkItemCommandRequest(BaseModel):
    """人工工作项认领或释放命令的幂等信封。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)


class WorkItemCompleteRequest(WorkItemCommandRequest):
    """提交工作项结构化结果和可选处理意见。"""

    outcome: str = Field(min_length=1, max_length=64)
    comment: str | None = Field(default=None, max_length=10000)


class WorkItemCandidateRead(BaseModel):
    """返回候选用户和创建工作项时冻结的来源角色。"""

    user_id: str
    employee_profile_id: str | None
    source_role_codes: list[str]
    source_types: list[str]


class WorkItemDecisionRead(BaseModel):
    """返回已经接受的结构化处理决定。"""

    actor_user_id: str
    outcome: str
    comment: str | None
    created_at: str


class WorkItemOutcomeOptionRead(BaseModel):
    """返回创建工作项时冻结的结构化办理结果展示契约。"""

    value: str
    label: str
    tone: str
    comment_required: bool


class WorkItemRead(BaseModel):
    """返回任务箱和人工办理详情所需的完整服务端投影。"""

    id: str
    instance_id: str
    session_id: str
    skill_id: str
    skill_version: str
    node_id: str
    status: str
    initiator_user_id: str | None
    assignee_user_id: str | None
    completion_mode: str
    claim_required: bool
    required_count: int | None
    allowed_outcomes: list[str]
    outcome_options: list[WorkItemOutcomeOptionRead]
    allowed_actions: list[str]
    outcome: str | None
    comment: str | None
    revision: int
    candidate_count: int
    decision_count: int
    candidates: list[WorkItemCandidateRead]
    decisions: list[WorkItemDecisionRead]
    expires_at: str | None
    created_at: str
    updated_at: str


class WorkItemPageRead(BaseModel):
    """返回工作项总数、页码、页大小和当前页投影。"""

    items: list[WorkItemRead]
    total: int
    page: int
    page_size: int


@router.get("", response_model=list[WorkItemRead])
def list_work_items(
    tenant_id: str = Query(...),
    view: Literal["pending", "claimed", "completed", "all"] = Query("pending"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[WorkItemRead]:
    """兼容旧调用，返回当前用户可见工作项的前 500 条。"""

    statement = _visible_work_item_query(tenant_id, view, current_user, db)
    items = db.exec(
        statement
        .order_by(SopWorkItem.created_at.desc(), SopWorkItem.id.desc())
        .limit(500)
    ).all()
    return [_work_item_read(db, item, current_user.id) for item in items]


@router.get("/page", response_model=WorkItemPageRead)
def page_work_items(
    tenant_id: str = Query(...),
    view: Literal["pending", "claimed", "completed", "all"] = Query("pending"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkItemPageRead:
    """先按当前用户业务关系过滤工作项，再执行稳定的服务端分页。"""

    statement = _visible_work_item_query(tenant_id, view, current_user, db)
    total = db.exec(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ).one()
    items = db.exec(
        statement
        .order_by(SopWorkItem.created_at.desc(), SopWorkItem.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return WorkItemPageRead(
        items=[_work_item_read(db, item, current_user.id) for item in items],
        total=int(total),
        page=page,
        page_size=page_size,
    )


def _visible_work_item_query(
    tenant_id: str,
    view: Literal["pending", "claimed", "completed", "all"],
    current_user: User,
    db: Session,
) -> SelectOfScalar[SopWorkItem]:
    """构造 tenant、用户关系和任务箱视图共同约束的工作项查询。"""

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
        SopWorkItem.attention_kind == "sop_human_task",
        or_(
            SopWorkItem.initiator_user_id == current_user.id,
            SopWorkItem.owner_user_id == current_user.id,
            SopWorkItem.assignee_user_id == current_user.id,
            candidate_exists,
        ),
    )
    if view == "completed":
        statement = statement.where(SopWorkItem.status == "completed")
    elif view == "claimed":
        statement = statement.where(
            SopWorkItem.status == "claimed",
            SopWorkItem.assignee_user_id == current_user.id,
        )
    elif view == "pending":
        statement = statement.where(SopWorkItem.status.in_(ACTIVE_WORK_ITEM_STATUSES))
    return statement


@router.get("/{work_item_id}", response_model=WorkItemRead)
def get_work_item(
    work_item_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkItemRead:
    """读取用户有权查看的工作项详情，不授予平台管理员业务旁路。"""

    ensure_current_user_tenant(tenant_id, current_user)
    item = _tenant_work_item(db, tenant_id, work_item_id)
    if not _can_view_work_item(db, item, current_user.id):
        raise HTTPException(status_code=403, detail="Cannot view this work item")
    return _work_item_read(db, item, current_user.id)


@router.post("/{work_item_id}/claim", response_model=WorkItemRead)
def claim_work_item(
    work_item_id: str,
    request: WorkItemCommandRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkItemRead:
    """由候选用户认领工作项并返回更新后的服务端允许动作。"""

    ensure_current_user_tenant(request.tenant_id, current_user)
    item = _tenant_work_item(db, request.tenant_id, work_item_id)
    try:
        SopWorkItemService(db).claim(
            item,
            actor_user_id=current_user.id,
            command_id=request.command_id,
            expected_revision=request.expected_revision,
        )
        _record_work_item_event(db, item, "sop_work_item_claimed", current_user.id)
        db.commit()
        db.refresh(item)
    except (WorkItemError, RevisionConflictError) as error:
        db.rollback()
        raise _work_item_http_error(error) from error
    return _work_item_read(db, item, current_user.id)


@router.post("/{work_item_id}/unclaim", response_model=WorkItemRead)
def unclaim_work_item(
    work_item_id: str,
    request: WorkItemCommandRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkItemRead:
    """由当前实际处理人释放工作项并恢复原候选池。"""

    ensure_current_user_tenant(request.tenant_id, current_user)
    item = _tenant_work_item(db, request.tenant_id, work_item_id)
    try:
        SopWorkItemService(db).unclaim(
            item,
            actor_user_id=current_user.id,
            command_id=request.command_id,
            expected_revision=request.expected_revision,
        )
        _record_work_item_event(db, item, "sop_work_item_unclaimed", current_user.id)
        db.commit()
        db.refresh(item)
    except (WorkItemError, RevisionConflictError) as error:
        db.rollback()
        raise _work_item_http_error(error) from error
    return _work_item_read(db, item, current_user.id)


@router.post("/{work_item_id}/complete", response_model=WorkItemRead)
def complete_work_item(
    work_item_id: str,
    request: WorkItemCompleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> WorkItemRead:
    """提交结构化决定，并在满足门槛时于同一事务恢复等待的 SOP。"""

    ensure_current_user_tenant(request.tenant_id, current_user)
    item = _tenant_work_item(db, request.tenant_id, work_item_id)
    was_completed = item.status == "completed"
    try:
        instance = db.get(SopInstance, item.instance_id)
        if instance is None or instance.tenant_id != item.tenant_id:
            raise WorkItemError("WORK_ITEM_INSTANCE_NOT_FOUND", "工作项所属 Execution 不存在。")
        coordinator = DeterministicSopCoordinator(db)
        control = ExecutionControlService(db, coordinator.store)
        replay = control.replayed_attention_resolution(
            item,
            actor_user_id=current_user.id,
            command_id=request.command_id,
            command=request.outcome,
        )
        if replay is not None:
            return _work_item_read(db, item, current_user.id)
        with coordinator.store.owned(
            instance,
            worker_id=f"attention-{item.id[-16:]}",
        ):
            _, completed = control.resolve_attention(
                instance,
                item,
                actor_user_id=current_user.id,
                command_id=request.command_id,
                command=request.outcome,
                comment=request.comment,
                expected_revision=request.expected_revision
                if request.expected_revision is not None
                else item.revision,
            )
            if completed:
                signal = db.exec(
                    select(ExecutionSignal).where(
                        ExecutionSignal.tenant_id == item.tenant_id,
                        ExecutionSignal.execution_id == instance.id,
                        ExecutionSignal.signal_type == "attention_decided",
                        ExecutionSignal.causation_id == f"{item.id}:{item.revision}",
                    )
                ).first()
                if signal is not None and signal.status == "pending":
                    control.claim_signal(signal, worker_id=f"attention-{item.id[-16:]}")
                    control.consume_signal(
                        instance,
                        signal,
                        worker_id=f"attention-{item.id[-16:]}",
                    )
                if not was_completed:
                    coordinator.resume_completed_work_item(item)
        _record_work_item_event(
            db,
            item,
            "sop_work_item_decided",
            current_user.id,
            payload={"outcome": request.outcome, "completed": completed},
        )
        db.commit()
        db.refresh(item)
    except (
        ExecutionControlError,
        SopExecutionConflictError,
        WorkItemError,
        RevisionConflictError,
    ) as error:
        db.rollback()
        raise _work_item_http_error(error) from error
    return _work_item_read(db, item, current_user.id)


def _work_item_read(db: Session, item: SopWorkItem, current_user_id: str) -> WorkItemRead:
    """组合实例、候选、决定和允许动作形成单一任务箱投影。"""

    instance = db.get(SopInstance, item.instance_id)
    if instance is None or instance.tenant_id != item.tenant_id:
        raise HTTPException(status_code=409, detail="Work item instance is missing")
    candidates = SopWorkItemService(db).candidates(item)
    decisions = SopWorkItemService(db).decisions(item)
    outcome_options = _outcome_options(item)
    return WorkItemRead(
        id=item.id,
        instance_id=item.instance_id,
        session_id=instance.session_id,
        skill_id=instance.skill_id,
        skill_version=instance.skill_version,
        node_id=item.node_id,
        status=item.status,
        initiator_user_id=item.initiator_user_id,
        assignee_user_id=item.assignee_user_id,
        completion_mode=item.completion_mode,
        claim_required=item.claim_required,
        required_count=item.required_count,
        allowed_outcomes=list(item.allowed_outcomes_json or []),
        outcome_options=outcome_options,
        allowed_actions=_allowed_actions(db, item, candidates, current_user_id),
        outcome=item.outcome,
        comment=item.comment,
        revision=item.revision,
        candidate_count=len(candidates),
        decision_count=len(decisions),
        candidates=[
            WorkItemCandidateRead(
                user_id=candidate.user_id,
                employee_profile_id=candidate.employee_profile_id,
                source_role_codes=list(candidate.source_role_codes_json or []),
                source_types=list(candidate.source_types_json or []),
            )
            for candidate in candidates
        ],
        decisions=[
            WorkItemDecisionRead(
                actor_user_id=decision.actor_user_id,
                outcome=decision.outcome,
                comment=decision.comment,
                created_at=decision.created_at.isoformat(),
            )
            for decision in decisions
        ],
        expires_at=item.expires_at.isoformat() if item.expires_at else None,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
    )


def _outcome_options(item: SopWorkItem) -> list[WorkItemOutcomeOptionRead]:
    """规范化新版结果快照，并为历史批准/拒绝工作项提供兼容标签。"""

    configured: list[WorkItemOutcomeOptionRead] = []
    for raw_option in item.outcome_options_json or []:
        if not isinstance(raw_option, dict):
            continue
        configured.append(
            WorkItemOutcomeOptionRead(
                value=str(raw_option.get("value") or ""),
                label=str(raw_option.get("label") or raw_option.get("value") or ""),
                tone=str(raw_option.get("tone") or "primary"),
                comment_required=bool(raw_option.get("comment_required", False)),
            )
        )
    if configured:
        return configured
    labels = {"approved": "同意", "rejected": "拒绝"}
    return [
        WorkItemOutcomeOptionRead(
            value=outcome,
            label=labels.get(outcome, outcome),
            tone="danger" if outcome == "rejected" else "success",
            comment_required=False,
        )
        for outcome in item.allowed_outcomes_json or []
    ]


def _allowed_actions(
    db: Session,
    item: SopWorkItem,
    candidates: list[SopWorkItemCandidate],
    current_user_id: str,
) -> list[str]:
    """根据候选、状态和当前有效权限计算前端可渲染动作。"""

    candidate_user_ids = {candidate.user_id for candidate in candidates}
    if (
        item.status not in ACTIVE_WORK_ITEM_STATUSES
        or current_user_id not in candidate_user_ids
        or not SopWorkItemService(db).is_current_candidate(item, current_user_id)
    ):
        return []
    if item.exclude_initiator and item.initiator_user_id == current_user_id:
        return []
    effective_permissions = set(
        user_permission_codes(
            db,
            tenant_id=item.tenant_id,
            user_id=current_user_id,
            organization_unit_ids=set(
                (item.participant_scope_snapshot_json or {}).get(
                    "organization_unit_ids"
                )
                or []
            )
            or None,
        )
    )

    def permitted(action_key: str) -> bool:
        """无显式权限要求时兼容旧工作项，否则检查当前员工权限并只收窄动作。"""

        required_permission = (item.action_permissions_json or {}).get(action_key)
        return not required_permission or required_permission in effective_permissions

    if item.status == "claimed":
        if item.assignee_user_id != current_user_id:
            return []
        actions = ["unclaim"] if permitted("unclaim") else []
        actions.extend(
            outcome
            for outcome in item.allowed_outcomes_json or []
            if permitted(f"outcome:{outcome}")
        )
        return actions
    actions: list[str] = []
    if item.claim_required and permitted("claim"):
        actions.append("claim")
    else:
        actions.extend(
            outcome
            for outcome in item.allowed_outcomes_json or []
            if not item.claim_required and permitted(f"outcome:{outcome}")
        )
    return actions


def _can_view_work_item(db: Session, item: SopWorkItem, current_user_id: str) -> bool:
    """判断用户是否是申请人、owner、assignee 或冻结候选人。"""

    if current_user_id in {
        item.initiator_user_id,
        item.owner_user_id,
        item.assignee_user_id,
    }:
        return True
    return (
        db.exec(
            select(SopWorkItemCandidate).where(
                SopWorkItemCandidate.tenant_id == item.tenant_id,
                SopWorkItemCandidate.work_item_id == item.id,
                SopWorkItemCandidate.user_id == current_user_id,
            )
        ).first()
        is not None
    )


def _tenant_work_item(db: Session, tenant_id: str, work_item_id: str) -> SopWorkItem:
    """读取当前租户工作项并拒绝通过主键跨租户访问。"""

    item = db.get(SopWorkItem, work_item_id)
    if (
        item is None
        or item.tenant_id != tenant_id
        or item.attention_kind != "sop_human_task"
    ):
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


def _record_work_item_event(
    db: Session,
    item: SopWorkItem,
    event_type: str,
    actor_user_id: str,
    *,
    payload: dict[str, object] | None = None,
) -> None:
    """把工作项动作写入所属会话事件流，保留实例和处理人关联。"""

    instance = db.get(SopInstance, item.instance_id)
    if instance is None:
        return
    session = db.get(ChatSession, instance.session_id)
    if session is None or session.tenant_id != item.tenant_id:
        return
    db.add(
        AgentEvent(
            tenant_id=item.tenant_id,
            session_id=session.id,
            event_type=event_type,
            payload_json={
                "instance_id": item.instance_id,
                "work_item_id": item.id,
                "node_id": item.node_id,
                "actor_user_id": actor_user_id,
                "revision": item.revision,
                **dict(payload or {}),
            },
        )
    )


def _work_item_http_error(
    error: WorkItemError | RevisionConflictError | ExecutionControlError | SopExecutionConflictError,
) -> HTTPException:
    """将领域错误映射为稳定 HTTP 状态，不向客户端泄露内部异常。"""

    if isinstance(error, RevisionConflictError):
        return HTTPException(status_code=409, detail=error.code)
    forbidden_codes = {
        "WORK_ITEM_NOT_CANDIDATE",
        "WORK_ITEM_NOT_ASSIGNEE",
        "WORK_ITEM_SELF_APPROVAL_FORBIDDEN",
        "WORK_ITEM_CLAIM_REQUIRED",
        "WORK_ITEM_CANDIDATE_NO_LONGER_ELIGIBLE",
    }
    conflict_codes = {
        "WORK_ITEM_ALREADY_CLAIMED",
        "WORK_ITEM_NOT_ACTIVE",
        "WORK_ITEM_ACTOR_ALREADY_DECIDED",
        "WORK_ITEM_COMMAND_ID_REUSED",
    }
    if error.code in forbidden_codes:
        status_code = 403
    elif error.code in conflict_codes:
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=error.code)
