"""
@Time       : 2026/07/27
@Author     : zhanglp8181
@File       : agents.py
@CallChain  : 企业端/对话端 → 数字员工 API → 权限校验、资源绑定与持久化
@Description: 提供数字员工创建、使用、复制、治理发布和能力资源管理接口。
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import sleep
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.agents.schema import (
    AgentGalleryPublicationRequest,
    AgentGalleryFacetsRead,
    AgentGalleryFacetRead,
    AgentGalleryPageRead,
    AgentGalleryScope,
    AgentListScope,
    AgentManagementPageRead,
    AgentManagementView,
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileRead,
    AgentProfileUpdateRequest,
    AgentResponsibilityUpdateRequest,
    AgentResourceBindingInput,
    AgentResourceImportRequest,
    AgentResourceBindingRead,
    AgentResourcesUpdateRequest,
    AgentScopeRead,
    AgentSkillRollbackRequest,
    AgentWorkRecordEventRead,
    AgentWorkRecordRead,
    AgentWorkRecordReplyStatsRead,
)
from app.agents.identity import (
    agent_category,
    agent_is_published,
    agent_owner_user_id,
    agent_visibility_scope,
)
from app.agents.branching import (
    agent_private_metadata,
    branch_versions,
    copy_overall_scope_to_agent,
    ensure_agent_skill_branch,
    ensure_knowledge_base_version,
    get_overall_agent,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    promote_branch_to_overall,
    promote_knowledge_branch_to_overall,
    rollback_branch,
    sync_branch_from_overall,
    visible_skill_rows,
)
from app.db import get_session
from app.db.models import (
    AgentModelBinding,
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    AgentUsage,
    ChatSession,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    OrganizationUnit,
    ScheduledTask,
    Skill,
    Tool,
    utc_now,
    User,
)
from app.organization.governance import ensure_governance_permission, has_governance_permission
from app.organization.reference_data import (
    ReferenceDataError,
    require_active_agent_category,
)
from app.security.auth import get_current_user
from app.security.permissions import agent_owned_by_user as _agent_owned_by_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant

IMPORT_LOCK_RETRY_ATTEMPTS = 2
IMPORT_LOCK_RETRY_DELAY_SECONDS = 0.5
GALLERY_GOVERNANCE_METADATA_KEYS = (
    "published_to_gallery",
    "gallery_published_at",
    "gallery_published_by",
)

enterprise_router = APIRouter(prefix="/api/enterprise/agents", tags=["enterprise:agents"])
chat_router = APIRouter(prefix="/api/chat/agents", tags=["chat:agents"])
scope_router = APIRouter(prefix="/api/enterprise/agent-scope", tags=["enterprise:agent-scope"])


@scope_router.get("", response_model=AgentScopeRead)
def get_agent_scope(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentScopeRead:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    return AgentScopeRead(tenant_id=tenant_id, agents=list_agents(tenant_id, db, current_user))


@enterprise_router.get("", response_model=list[AgentProfileRead])
def list_agents(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    scope: AgentListScope | None = Query(None),
) -> list[AgentProfileRead]:
    """按受控关系视图返回数字员工，兼容测试直接调用时 FastAPI 的 Query 默认对象。"""

    ensure_tenant(db, tenant_id)
    user = current_user
    _ensure_request_tenant(tenant_id, user)
    if scope not in {"manageable", "owned", "used", "gallery", "expert"}:
        scope = None
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, user)
    can_govern_agents = False
    statement = select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)
    if scope == "owned":
        statement = statement.where(AgentProfile.owner_user_id == user.id)
    elif scope == "used":
        statement = statement.where(AgentProfile.id.in_(used_agent_ids))
    elif scope == "gallery":
        statement = statement.where(
            AgentProfile.published_to_gallery == True,  # noqa: E712
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        )
    elif scope == "expert":
        statement = statement.where(
            AgentProfile.agent_category_code == "professional",
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        )
    elif scope == "manageable":
        can_govern_agents = _is_admin_user(user) or has_governance_permission(
            db,
            tenant_id=tenant_id,
            user_id=user.id,
            permission_code="agent.manage",
        )
        if not can_govern_agents:
            statement = statement.where(AgentProfile.owner_user_id == user.id)
    rows = db.exec(
        statement.order_by(AgentProfile.is_overall.desc(), AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_product(row)]
    if scope is None and not _is_admin_user(user):
        # Non-admin users still need the overall agent as a read-only open-gallery
        # source for copy/use flows. Mutations remain guarded by manage/update
        # endpoints, so this only exposes the source scope.
        rows = [
            row
            for row in rows
            if row.is_overall
            or _agent_visible_to_user(row, user)
            or row.id in used_agent_ids
        ]
    elif scope in {"used", "expert"}:
        rows = [
            row
            for row in rows
            if _agent_visible_to_user(row, user) or row.id in used_agent_ids
        ]
    bindings = _bindings_by_agent(db, tenant_id)
    responsible_org_names = _responsible_org_names(db, rows)
    return [
        agent_read(
            row,
            bindings.get(row.id, []),
            row.id in used_agent_ids,
            viewer=user,
            governance_summary=scope == "manageable" and can_govern_agents,
            responsible_org_unit_name=responsible_org_names.get(
                row.responsible_org_unit_id or ""
            ),
        )
        for row in rows
    ]


@enterprise_router.get("/gallery-page", response_model=AgentGalleryPageRead)
def page_agent_gallery(
    tenant_id: str = Query(...),
    scope: AgentGalleryScope = Query(...),
    q: str | None = Query(None),
    expert_source: str | None = Query(None),
    expert_department: str | None = Query(None),
    expert_direction: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=48),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentGalleryPageRead:
    """按使用、拥有、发布或专家关系分页，并只投影当前页的完整卡片资源。"""

    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, current_user)
    scope_counts = {
        item_scope: _agent_gallery_scope_count(
            db,
            tenant_id,
            current_user,
            item_scope,
        )
        for item_scope in ("used", "owned", "gallery", "expert")
    }
    conditions = _agent_gallery_scope_conditions(
        tenant_id,
        current_user,
        scope,
    )


    if scope == "expert":
        if expert_source:
            conditions.append(
                AgentProfile.metadata_json["expert_source_code"].as_string()
                == expert_source
            )
        if expert_department:
            conditions.append(
                AgentProfile.metadata_json["expert_category"].as_string()
                == expert_department
            )
        if expert_direction:
            conditions.append(
                AgentProfile.metadata_json["expert_subcategory"].as_string()
                == expert_direction
            )

    ordered = (
        select(AgentProfile)
        .where(*conditions)
        .order_by(AgentProfile.updated_at.desc(), AgentProfile.id.desc())
    )
    keyword = (q or "").strip().lower()
    if keyword:
        matching_rows = [
            row for row in db.exec(ordered).all() if _agent_matches_gallery_search(row, keyword)
        ]
        total = len(matching_rows)
        start = (page - 1) * page_size
        rows = matching_rows[start : start + page_size]
    else:
        total = int(
            db.exec(select(func.count()).select_from(AgentProfile).where(*conditions)).one()
        )
        rows = db.exec(ordered.offset((page - 1) * page_size).limit(page_size)).all()

    page_agent_ids = {row.id for row in rows}
    bindings = _bindings_by_agent(db, tenant_id, page_agent_ids)
    responsible_org_names = _responsible_org_names(db, rows)
    return AgentGalleryPageRead(
        items=[
            agent_read(
                row,
                bindings.get(row.id, []),
                row.id in used_agent_ids,
                viewer=current_user,
                responsible_org_unit_name=responsible_org_names.get(
                    row.responsible_org_unit_id or ""
                ),
            )
            for row in rows
        ],
        total=total,
        scope_counts=scope_counts,
        facets=(
            _agent_gallery_facets(
                db,
                tenant_id,
                current_user,
                expert_source or "",
                expert_department or "",
            )
            if scope == "expert"
            else AgentGalleryFacetsRead()
        ),
        page=page,
        page_size=page_size,
    )


@enterprise_router.get("/management-page", response_model=AgentManagementPageRead)
def page_managed_agents(
    tenant_id: str = Query(...),
    view: AgentManagementView = Query("all"),
    q: str | None = Query(None),
    expert_source: str | None = Query(None),
    expert_department: str | None = Query(None),
    expert_direction: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=48),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentManagementPageRead:
    """按所有权或治理权限过滤管理端员工，再执行状态、专家筛选和分页。"""

    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    can_govern = _is_admin_user(current_user) or has_governance_permission(
        db, tenant_id=tenant_id, user_id=current_user.id, permission_code="agent.manage"
    )
    view_counts = {
        item_view: _agent_management_count(db, tenant_id, current_user, item_view, can_govern)
        for item_view in ("all", "online", "offline", "pending", "expert", "governance")
    }
    conditions = _agent_management_conditions(tenant_id, current_user, view, can_govern)
    if view == "expert":
        for key, value in (
            ("expert_source_code", expert_source),
            ("expert_category", expert_department),
            ("expert_subcategory", expert_direction),
        ):
            if value:
                conditions.append(AgentProfile.metadata_json[key].as_string() == value)
    ordered = select(AgentProfile).where(*conditions).order_by(
        AgentProfile.updated_at.desc(), AgentProfile.id.desc()
    )
    keyword = (q or "").strip().lower()
    if keyword:
        matched = [
            row for row in db.exec(ordered).all() if _agent_matches_gallery_search(row, keyword)
        ]
        total = len(matched)
        rows = matched[(page - 1) * page_size : page * page_size]
    else:
        total = int(
            db.exec(select(func.count()).select_from(AgentProfile).where(*conditions)).one()
        )
        rows = db.exec(ordered.offset((page - 1) * page_size).limit(page_size)).all()
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, current_user)
    bindings = _bindings_by_agent(db, tenant_id, {row.id for row in rows})
    responsible_org_names = _responsible_org_names(db, rows)
    return AgentManagementPageRead(
        items=[
            agent_read(
                row,
                bindings.get(row.id, []),
                row.id in used_agent_ids,
                viewer=current_user,
                governance_summary=view == "governance",
                responsible_org_unit_name=responsible_org_names.get(
                    row.responsible_org_unit_id or ""
                ),
            )
            for row in rows
        ],
        total=total,
        view_counts=view_counts,
        facets=(
            _agent_management_facets(
                db, tenant_id, current_user, expert_source or "", expert_department or ""
            )
            if view == "expert"
            else AgentGalleryFacetsRead()
        ),
        page=page,
        page_size=page_size,
    )


@enterprise_router.post("", response_model=AgentProfileRead)
def create_agent(
    request: AgentProfileCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """创建当前成员拥有的数字员工，并阻止请求夹带广场治理状态。"""
    ensure_tenant(db, request.tenant_id)
    user = current_user
    _ensure_request_tenant(request.tenant_id, user)
    if request.is_overall and not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can create overall agent")
    category_code = _validated_agent_category(
        db,
        request.tenant_id,
        request.agent_category_code,
    )
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name cannot be empty")
    existing = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == request.tenant_id, AgentProfile.name == name
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Agent name already exists")
    row = AgentProfile(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        persona_prompt=request.persona_prompt,
        is_overall=request.is_overall,
        status="active",
        owner_user_id=None if request.is_overall else user.id,
        agent_category_code=category_code,
        visibility_scope=request.visibility_scope,
        metadata_json=_metadata_with_creator(request.metadata or {}, user),
    )
    copy_summary: dict[str, object] | None = None
    db.add(row)
    db.flush()
    if not row.is_overall:
        copy_from_agent_id = request.copy_from_agent_id
        if request.source_mode == "blank":
            pass
        elif copy_from_agent_id:
            source_agent = _get_agent(db, request.tenant_id, copy_from_agent_id)
            _ensure_can_copy_from_agent(source_agent, user)
            row.source_agent_id = source_agent.id
            row.source_agent_version = str(source_agent.profile_revision)
            if not row.persona_prompt:
                row.persona_prompt = source_agent.persona_prompt
            copy_summary = _copy_agent_scope_from_source(
                db,
                request.tenant_id,
                source_agent,
                row,
                current_user,
            )
        else:
            overall = get_overall_agent(db, request.tenant_id)
            if overall and not row.persona_prompt:
                row.persona_prompt = overall.persona_prompt
            copy_overall_scope_to_agent(db, request.tenant_id, row)
            if overall:
                _copy_agent_models_from_source(db, request.tenant_id, overall, row)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="agent.manage" if request.is_overall else None,
        action="agent.copy" if row.source_agent_id else "agent.create",
        action_kind="create",
        outcome="success",
        resource_type="agent_profile",
        resource_id=row.id,
        after={
            "name": row.name,
            "owner_user_id": row.owner_user_id,
            "source_agent_id": row.source_agent_id,
            "source_agent_version": row.source_agent_version,
            "agent_category_code": row.agent_category_code,
            "visibility_scope": row.visibility_scope,
        },
        detail={"copy_summary": copy_summary or {}},
    )
    db.commit()
    db.refresh(row)
    return agent_read(
        row,
        _bindings_by_agent(db, request.tenant_id).get(row.id, []),
        viewer=current_user,
        copy_summary=copy_summary,
        responsible_org_unit_name=_responsible_org_names(db, [row]).get(
            row.responsible_org_unit_id or ""
        ),
    )


@enterprise_router.get("/{agent_id}", response_model=AgentProfileRead)
def get_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(row, current_user)
    return agent_read(
        row,
        _bindings_by_agent(db, tenant_id).get(row.id, []),
        row.id in _used_agent_ids_for_user(db, tenant_id, current_user),
        viewer=current_user,
        responsible_org_unit_name=_responsible_org_names(db, [row]).get(
            row.responsible_org_unit_id or ""
        ),
    )


@enterprise_router.get("/{agent_id}/work-record", response_model=AgentWorkRecordRead)
def get_agent_work_record(
    agent_id: str,
    tenant_id: str = Query(...),
    timezone: str = Query("Asia/Shanghai"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentWorkRecordRead:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(agent, current_user)
    try:
        local_timezone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc

    now = utc_now()
    reply_rows = db.exec(
        select(Message)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(
            Message.tenant_id == tenant_id,
            Message.role == "assistant",
            ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == current_user.id,
        )
        .order_by(Message.created_at.asc())
    ).all()
    by_day: dict[str, int] = {}
    events = [
        AgentWorkRecordEventRead(
            id=f"{message.id}:reply",
            kind="chat",
            phase="reply",
            timestamp=_iso_utc(message.created_at),
            label="对话回复",
        )
        for message in reply_rows
    ]
    for message in reply_rows:
        day = _as_utc(message.created_at).astimezone(local_timezone).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    events.extend(_agent_resource_timeline_events(db, tenant_id, agent_id))
    events.extend(_agent_scheduled_task_timeline_events(db, tenant_id, agent_id, current_user))
    events.sort(key=lambda item: (item.timestamp, item.id))
    today = _as_utc(now).astimezone(local_timezone).date().isoformat()
    return AgentWorkRecordRead(
        agent_id=agent_id,
        timezone=timezone,
        generated_at=_iso_utc(now),
        reply_stats=AgentWorkRecordReplyStatsRead(
            total=len(reply_rows),
            today=by_day.get(today, 0),
            by_day=dict(sorted(by_day.items())),
        ),
        events=events,
    )


@enterprise_router.put("/{agent_id}", response_model=AgentProfileRead)
def update_agent(
    agent_id: str,
    request: AgentProfileUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """更新可管理数字员工的普通资料，同时保留服务端治理与责任字段。"""
    row = _get_agent(db, request.tenant_id, agent_id)
    user = current_user
    _ensure_can_manage_agent(row, user)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Agent name cannot be empty")
        conflict = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == request.tenant_id,
                AgentProfile.name == name,
                AgentProfile.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Agent name already exists")
        row.name = name
    if request.description is not None:
        row.description = request.description
    prompt_changed = (
        request.persona_prompt is not None and request.persona_prompt != row.persona_prompt
    )
    if request.persona_prompt is not None:
        row.persona_prompt = request.persona_prompt
    if request.status is not None:
        row.status = request.status
    if request.metadata is not None:
        row.metadata_json = _metadata_preserving_creator(
            row.metadata_json or {}, request.metadata, user
        )
    if prompt_changed:
        _bump_agent_revision(row)
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(
        row,
        _bindings_by_agent(db, request.tenant_id).get(row.id, []),
        viewer=current_user,
        responsible_org_unit_name=_responsible_org_names(db, [row]).get(
            row.responsible_org_unit_id or ""
        ),
    )


@enterprise_router.put("/{agent_id}/responsibility", response_model=AgentProfileRead)
def set_agent_responsibility(
    agent_id: str,
    request: AgentResponsibilityUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """设置治理责任组织；该事实不扩展数字员工的可见、执行或知识权限。"""

    row = _get_agent(db, request.tenant_id, agent_id)
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="agent.manage",
    )
    if row.is_overall:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AGENT_EXECUTION_SUBJECT_REQUIRED",
                "message": "开放广场资源池不是数字员工，不能设置责任组织。",
            },
        )
    responsible_org_unit_id = (
        request.responsible_org_unit_id.strip()
        if request.responsible_org_unit_id
        else None
    )
    organization = None
    if responsible_org_unit_id:
        organization = db.get(OrganizationUnit, responsible_org_unit_id)
        if (
            organization is None
            or organization.tenant_id != request.tenant_id
            or organization.status != "active"
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_AGENT_RESPONSIBLE_ORGANIZATION",
                    "message": "责任组织不存在、已停用或不属于当前企业。",
                },
            )
    before_org_unit_id = row.responsible_org_unit_id
    if before_org_unit_id != responsible_org_unit_id:
        row.responsible_org_unit_id = responsible_org_unit_id
        row.updated_at = utc_now()
        db.add(row)
        append_user_management_audit(
            db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="agent.manage",
            action="agent.responsibility.update",
            action_kind="update",
            outcome="success",
            resource_type="agent_profile",
            resource_id=row.id,
            target_org_unit_id=responsible_org_unit_id or before_org_unit_id,
            before={"responsible_org_unit_id": before_org_unit_id},
            after={"responsible_org_unit_id": responsible_org_unit_id},
            detail={
                "authorization_effect": "none",
                "service_scope_effect": "none",
            },
        )
        db.commit()
        db.refresh(row)
    return agent_read(
        row,
        _bindings_by_agent(db, request.tenant_id).get(row.id, []),
        viewer=current_user,
        responsible_org_unit_name=organization.name if organization else None,
    )


@enterprise_router.put("/{agent_id}/gallery-publication", response_model=AgentProfileRead)
def set_agent_gallery_publication(
    agent_id: str,
    request: AgentGalleryPublicationRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """由租户管理员执行数字员工广场发布或下架，并记录真实治理操作者。"""
    row = _get_agent(db, request.tenant_id, agent_id)
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="agent.manage",
    )
    if row.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent cannot be published to gallery")
    before = {
        "published_to_gallery": agent_is_published(row),
        "visibility_scope": row.visibility_scope,
        "gallery_published_by": row.gallery_published_by,
    }
    if request.published:
        _validate_gallery_publication(db, row)
    metadata = dict(row.metadata_json or {})
    metadata["published_to_gallery"] = request.published
    row.published_to_gallery = request.published
    row.visibility_scope = "tenant" if request.published else "private"
    if request.published:
        published_at = utc_now()
        metadata["gallery_published_at"] = published_at.isoformat()
        metadata["gallery_published_by"] = current_user.username
        row.gallery_published_at = published_at
        row.gallery_published_by = current_user.id
    else:
        metadata.pop("gallery_published_at", None)
        metadata.pop("gallery_published_by", None)
        row.gallery_published_at = None
        row.gallery_published_by = None
    row.metadata_json = metadata
    row.updated_at = utc_now()
    db.add(row)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="agent.manage",
        action="agent.publish" if request.published else "agent.unpublish",
        action_kind="update",
        outcome="success",
        resource_type="agent_profile",
        resource_id=row.id,
        before=before,
        after={
            "published_to_gallery": row.published_to_gallery,
            "visibility_scope": row.visibility_scope,
            "gallery_published_by": row.gallery_published_by,
        },
    )
    db.commit()
    db.refresh(row)
    return agent_read(
        row,
        _bindings_by_agent(db, request.tenant_id).get(row.id, []),
        viewer=current_user,
        responsible_org_unit_name=_responsible_org_names(db, [row]).get(
            row.responsible_org_unit_id or ""
        ),
    )


@enterprise_router.delete("/{agent_id}")
def delete_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    if row.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent cannot be deleted")
    bindings = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.agent_id == row.id)
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@enterprise_router.get("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def get_agent_resources(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceBindingRead]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    rows = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == tenant_id, AgentResourceBinding.agent_id == agent_id
        )
        .order_by(AgentResourceBinding.resource_type, AgentResourceBinding.created_at)
    ).all()
    return [binding_read(row) for row in rows]


@enterprise_router.put("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def update_agent_resources(
    agent_id: str,
    request: AgentResourcesUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceBindingRead]:
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent uses the global resource pool")
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == request.tenant_id,
            AgentResourceBinding.agent_id == agent_id,
        )
    ).all()
    by_key = {(row.resource_type, row.resource_id): row for row in existing}
    desired_keys: set[tuple[str, str]] = set()
    changed = False
    for item in request.resources:
        _ensure_resource_exists(db, request.tenant_id, item)
        key = (item.resource_type, item.resource_id)
        desired_keys.add(key)
        row = by_key.get(key)
        if row:
            if row.status != item.status or dict(row.metadata_json or {}) != item.metadata:
                changed = True
            row.status = item.status
            row.metadata_json = item.metadata
            row.updated_at = utc_now()
        else:
            changed = True
            row = AgentResourceBinding(
                tenant_id=request.tenant_id,
                agent_id=agent_id,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                status=item.status,
                metadata_json=item.metadata,
            )
        db.add(row)
    for key, row in by_key.items():
        if key not in desired_keys:
            changed = True
            db.delete(row)
    if changed:
        _bump_agent_revision(agent)
        db.add(agent)
    db.commit()
    return get_agent_resources(agent_id, request.tenant_id, db, current_user)


@enterprise_router.post("/{agent_id}/resources/import")
def import_agent_resources(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    for attempt in range(IMPORT_LOCK_RETRY_ATTEMPTS):
        try:
            return _import_agent_resources_once(agent_id, request, db, current_user)
        except OperationalError as exc:
            db.rollback()
            if not _is_database_locked_error(exc) or attempt >= IMPORT_LOCK_RETRY_ATTEMPTS - 1:
                raise
            sleep(IMPORT_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise HTTPException(status_code=503, detail="Resource import is temporarily busy")


def _import_agent_resources_once(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session,
    current_user: User | object,
) -> dict[str, object]:
    target_agent = _get_agent(db, request.tenant_id, agent_id)
    source_agent = _get_agent(db, request.tenant_id, request.source_agent_id)
    user = current_user
    _ensure_can_import_to_agent(target_agent, user)
    _ensure_can_copy_from_agent(source_agent, user)
    if source_agent.id == target_agent.id:
        raise HTTPException(status_code=400, detail="Source and target agent cannot be the same")
    resource_ids = _dedupe_ids(request.resource_ids)
    if not resource_ids:
        raise HTTPException(status_code=400, detail="No resources selected")
    imported: list[dict[str, object]] = []
    missing: list[dict[str, str]] = []
    for identifier in resource_ids:
        resolved = _resolve_resource(db, request.tenant_id, request.resource_type, identifier)
        if not resolved:
            missing.append({"resource_id": identifier, "reason": "resource_not_found"})
            continue
        source_binding = _source_resource_binding(
            db, request.tenant_id, source_agent, request.resource_type, resolved.id
        )
        if not source_agent.is_overall and not source_binding:
            missing.append({"resource_id": identifier, "reason": "not_visible_in_source_agent"})
            continue
        block_reason = _blocked_learning_reason(
            db,
            request.tenant_id,
            source_agent,
            request.resource_type,
            resolved,
            source_binding,
        )
        if block_reason:
            missing.append({"resource_id": identifier, "reason": block_reason})
            continue
        if target_agent.is_overall:
            _import_resource_to_overall(
                db, request.tenant_id, source_agent, request.resource_type, resolved
            )
        else:
            _upsert_imported_resource_binding(
                db,
                request.tenant_id,
                source_agent,
                target_agent,
                request.resource_type,
                resolved,
                source_binding,
            )
        imported.append(
            {
                "resource_type": request.resource_type,
                "resource_id": resolved.id,
                "display_id": _resource_display_id(request.resource_type, resolved),
                "name": getattr(resolved, "name", getattr(resolved, "slug", resolved.id)),
            }
        )
    db.commit()
    return {
        "status": "imported",
        "target_agent_id": target_agent.id,
        "source_agent_id": source_agent.id,
        "imported": imported,
        "missing": missing,
    }


@enterprise_router.get("/{agent_id}/skills")
def get_agent_skills(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        _skill_branch_read(skill)
        for skill in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    ]


@enterprise_router.post("/{agent_id}/skills/{skill_id}/sync-from-overall")
def sync_agent_skill_from_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    skill = _get_global_skill(db, tenant_id, skill_id)
    if skill.status != "published":
        raise HTTPException(
            status_code=400, detail="Disabled SOP cannot be learned from the open gallery"
        )
    branch = sync_branch_from_overall(db, tenant_id, agent_id, skill)
    db.commit()
    return {"status": "synced", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/promote-to-overall")
def promote_agent_skill_to_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _ensure_admin_user(tenant_id, current_user)
    agent = _get_agent(db, tenant_id, agent_id)
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Overall agent does not have a branch to promote"
        )
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill_id,
        )
    ).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    skill = promote_branch_to_overall(db, tenant_id, branch)
    db.commit()
    return {"status": "promoted", "skill_id": skill_id, "version": skill.version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/rollback")
def rollback_agent_skill(
    agent_id: str,
    skill_id: str,
    request: AgentSkillRollbackRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Use the global skill rollback endpoint for overall agent"
        )
    branch = rollback_branch(db, request.tenant_id, agent_id, skill_id, request.version)
    db.commit()
    return {"status": "rolled_back", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.get("/{agent_id}/skills/{skill_id}/versions")
def list_agent_skill_versions(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "agent_id": row.agent_id,
            "skill_id": row.skill_id,
            "version": row.version,
            "base_version": row.base_version,
            "sync_state": row.sync_state,
            "status": row.status,
            "content": row.content_json,
            "change_summary": row.change_summary,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        for row in branch_versions(db, tenant_id, agent_id, skill_id)
    ]


@enterprise_router.put("/{agent_id}/models")
def update_agent_models(
    agent_id: str,
    request: AgentModelsUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """更新模型角色绑定，并仅在有效能力配置变化时递增资料版本。"""

    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    changed = False
    for item in request.bindings:
        existing = db.exec(
            select(AgentModelBinding).where(
                AgentModelBinding.tenant_id == request.tenant_id,
                AgentModelBinding.agent_id == agent_id,
                AgentModelBinding.role == item.role,
            )
        ).first()
        if existing:
            if existing.model_config_id == item.model_config_id:
                continue
            existing.model_config_id = item.model_config_id
            existing.updated_at = utc_now()
            db.add(existing)
            changed = True
            continue
        db.add(
            AgentModelBinding(
                tenant_id=request.tenant_id,
                agent_id=agent_id,
                role=item.role,
                model_config_id=item.model_config_id,
            )
        )
        changed = True
    if changed:
        _bump_agent_revision(agent)
        db.add(agent)
    db.commit()
    return {"status": "updated", "agent_id": agent_id}


@chat_router.get("", response_model=list[AgentProfileRead])
def list_chat_agents(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AgentProfileRead]:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        )
        .order_by(AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_product(row)]
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, current_user)
    rows = [
        row for row in rows if _chat_agent_selectable_to_user(row, current_user, used_agent_ids)
    ]
    bindings = _bindings_by_agent(db, tenant_id)
    responsible_org_names = _responsible_org_names(db, rows)
    return [
        agent_read(
            row,
            bindings.get(row.id, []),
            row.id in used_agent_ids,
            viewer=current_user,
            responsible_org_unit_name=responsible_org_names.get(
                row.responsible_org_unit_id or ""
            ),
        )
        for row in rows
    ]


@chat_router.post("/{agent_id}/use", response_model=AgentProfileRead)
def use_chat_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentProfileRead:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    row = _get_agent(db, tenant_id, agent_id)
    if (
        row.is_overall
        or row.status != "active"
        or not _chat_agent_visible_to_user(row, current_user)
    ):
        raise HTTPException(status_code=403, detail="Cannot access this agent")
    _mark_agent_used(db, tenant_id, current_user, row.id)
    bindings = _bindings_by_agent(db, tenant_id)
    return agent_read(
        row,
        bindings.get(row.id, []),
        True,
        viewer=current_user,
        responsible_org_unit_name=_responsible_org_names(db, [row]).get(
            row.responsible_org_unit_id or ""
        ),
    )


@chat_router.delete("/{agent_id}/use")
def remove_chat_agent_usage(
    agent_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    """只移除当前成员的常用关系，不删除数字员工、会话或消息。"""

    _ensure_request_tenant(tenant_id, current_user)
    row = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == current_user.id,
            AgentUsage.agent_id == agent_id,
        )
    ).first()
    if row is not None:
        db.delete(row)
        db.commit()
    return {"status": "removed", "agent_id": agent_id, "removed": row is not None}


def _agent_resource_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
) -> list[AgentWorkRecordEventRead]:
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.status == "active",
        )
    ).all()
    ids_by_type = {
        resource_type: {
            binding.resource_id
            for binding in bindings
            if binding.resource_type == resource_type
        }
        for resource_type in ("skill", "general_skill", "knowledge_base", "tool")
    }
    skills = {
        row.id: row
        for row in db.exec(
            select(Skill).where(
                Skill.tenant_id == tenant_id,
                Skill.id.in_(ids_by_type["skill"]),
            )
        ).all()
    } if ids_by_type["skill"] else {}
    general_skills = {
        row.id: row
        for row in db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == tenant_id,
                GeneralSkill.id.in_(ids_by_type["general_skill"]),
            )
        ).all()
    } if ids_by_type["general_skill"] else {}
    knowledge_bases = {
        row.id: row
        for row in db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id.in_(ids_by_type["knowledge_base"]),
            )
        ).all()
    } if ids_by_type["knowledge_base"] else {}
    tools = {
        row.id: row
        for row in db.exec(
            select(Tool).where(
                Tool.tenant_id == tenant_id,
                Tool.id.in_(ids_by_type["tool"]),
            )
        ).all()
    } if ids_by_type["tool"] else {}
    skill_branches = {
        row.skill_id: row
        for row in db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent_id,
            )
        ).all()
    }
    knowledge_branches = {
        row.knowledge_base_id: row
        for row in db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent_id,
            )
        ).all()
    }

    events: list[AgentWorkRecordEventRead] = []
    for binding in bindings:
        kind: str
        label: str
        if binding.resource_type == "skill":
            resource = skills.get(binding.resource_id)
            branch = skill_branches.get(resource.skill_id) if resource else None
            if not resource or resource.status != "published" or (branch and branch.status != "active"):
                continue
            kind, label = "sop", resource.name
        elif binding.resource_type == "general_skill":
            resource = general_skills.get(binding.resource_id)
            if not resource or resource.status != "published":
                continue
            kind, label = "skill", resource.name
        elif binding.resource_type == "knowledge_base":
            resource = knowledge_bases.get(binding.resource_id)
            branch = knowledge_branches.get(binding.resource_id)
            if not resource or resource.status != "active" or (branch and branch.status != "active"):
                continue
            kind, label = "knowledge", resource.name
        elif binding.resource_type == "tool":
            resource = tools.get(binding.resource_id)
            if not resource or not resource.enabled:
                continue
            kind, label = "tool", resource.display_name or resource.name
        else:
            continue
        events.append(
            AgentWorkRecordEventRead(
                id=f"{binding.id}:assigned",
                kind=kind,  # type: ignore[arg-type]
                phase="assigned",
                timestamp=_iso_utc(binding.created_at),
                label=label,
            )
        )
    return events


def _agent_scheduled_task_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
    current_user: User,
) -> list[AgentWorkRecordEventRead]:
    conditions = [
        ScheduledTask.tenant_id == tenant_id,
        ScheduledTask.agent_id == agent_id,
        ScheduledTask.status != "archived",
    ]
    if not _is_admin_user(current_user):
        conditions.append(ScheduledTask.created_by_user_id == current_user.id)
    tasks = db.exec(select(ScheduledTask).where(*conditions)).all()
    events: list[AgentWorkRecordEventRead] = []
    for task in tasks:
        for phase, timestamp in (("last_run", task.last_run_at), ("next_run", task.next_run_at)):
            if not timestamp:
                continue
            events.append(
                AgentWorkRecordEventRead(
                    id=f"{task.id}:{phase}",
                    kind="task",
                    phase=phase,  # type: ignore[arg-type]
                    timestamp=_iso_utc(timestamp),
                    label=task.title,
                )
            )
    return events


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def agent_read(
    row: AgentProfile,
    bindings: list[AgentResourceBinding],
    used_by_current_user: bool | None = None,
    *,
    viewer: User | None = None,
    copy_summary: dict[str, object] | None = None,
    governance_summary: bool = False,
    responsible_org_unit_name: str | None = None,
) -> AgentProfileRead:
    """按调用者关系裁剪数字员工资料，并返回非授权性的责任组织摘要。"""

    metadata = dict(row.metadata_json or {})
    owner_user_id = agent_owner_user_id(row)
    published_to_gallery = agent_is_published(row)
    owned = bool(viewer and owner_user_id == viewer.id)
    restricted_summary = bool(
        viewer
        and not owned
        and not row.is_overall
        and (not published_to_gallery or governance_summary)
    )
    governance_view = bool(
        restricted_summary
        and viewer
        and (_is_admin_user(viewer) or governance_summary)
    )
    public_user_view = bool(
        viewer and not owned and not row.is_overall and published_to_gallery
    )
    if restricted_summary:
        metadata = _governance_agent_metadata(metadata)
        bindings = []
    elif public_user_view:
        metadata = _governance_agent_metadata(metadata)
    if owner_user_id:
        metadata["owner_user_id"] = owner_user_id
    metadata["published_to_gallery"] = published_to_gallery
    if used_by_current_user is not None:
        metadata["used_by_current_user"] = used_by_current_user
        metadata["chat_used_by_current_user"] = used_by_current_user
    return AgentProfileRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        persona_prompt=None if restricted_summary else row.persona_prompt,
        is_overall=row.is_overall,
        status=row.status,
        owner_user_id=owner_user_id,
        responsible_org_unit_id=row.responsible_org_unit_id,
        responsible_org_unit_name=responsible_org_unit_name,
        source_agent_id=row.source_agent_id,
        source_agent_version=row.source_agent_version,
        profile_revision=row.profile_revision,
        published_to_gallery=published_to_gallery,
        gallery_published_at=(
            row.gallery_published_at.isoformat() if row.gallery_published_at else None
        ),
        gallery_published_by=row.gallery_published_by,
        agent_category_code=agent_category(row),
        visibility_scope=agent_visibility_scope(row),  # type: ignore[arg-type]
        owned_by_current_user=owned,
        used_by_current_user=bool(used_by_current_user),
        manageable_by_current_user=owned
        or bool(viewer and row.is_overall and _is_admin_user(viewer)),
        view_level="governance" if governance_view else "manager" if owned else "user",
        copy_summary=copy_summary,
        metadata=metadata,
        resources=[
            binding_read(binding, include_metadata=not public_user_view)
            for binding in bindings
        ],
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _responsible_org_names(
    db: Session,
    rows: list[AgentProfile],
) -> dict[str, str]:
    """批量解析当前租户内责任组织名称，避免数字员工列表产生逐行查询。"""

    organization_ids = {
        row.responsible_org_unit_id
        for row in rows
        if row.responsible_org_unit_id
    }
    if not organization_ids:
        return {}
    tenant_ids = {row.tenant_id for row in rows}
    organizations = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.id.in_(organization_ids),
            OrganizationUnit.tenant_id.in_(tenant_ids),
        )
    ).all()
    return {
        organization.id: organization.name
        for organization in organizations
        if organization.status == "active"
    }


def _governance_agent_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """仅保留发布治理所需的非敏感 metadata，避免管理员读取私人配置。"""

    allowed_keys = {
        "owner_user_id",
        "owner_username",
        "owner_display_name",
        "created_by_user_id",
        "created_by_username",
        "created_by_display_name",
        "published_to_gallery",
        "gallery_published_at",
        "gallery_published_by",
        "employee_type",
        "expert_category",
        "expert_subcategory",
        "expert_source_code",
        "expert_source_label",
        "expert_tags",
        "expert_name_original",
        "expert_upstream_url",
        "expert_capability_manifest",
        "role_name",
        "position",
        "department",
        "team",
        "system_prompt_summary",
        "work_styles",
        "expertise_tags",
        "work_modes",
        "avatar_url",
        "avatar_preset",
        "avatar_kind",
        "avatar_image",
        "expert_avatar_category",
        "expert_avatar_key",
    }
    return {key: value for key, value in metadata.items() if key in allowed_keys}


def _is_database_locked_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _ensure_request_tenant(tenant_id: str, user: User) -> None:
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _validated_agent_category(db: Session, tenant_id: str, item_code: str) -> str:
    """验证数字员工分类为租户活动码项，并转换成稳定的管理 API 错误。"""

    normalized_code = item_code.strip()
    try:
        return require_active_agent_category(db, tenant_id, normalized_code).item_code
    except ReferenceDataError as error:
        message = str(error)
        if message.startswith("UNKNOWN_CODE_ITEM:"):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown agent category: {normalized_code}",
            ) from error
        if message.startswith("INACTIVE_CODE_ITEM:"):
            raise HTTPException(
                status_code=400,
                detail=f"Inactive agent category: {normalized_code}",
            ) from error
        raise HTTPException(
            status_code=409,
            detail="Agent category catalog is inactive",
        ) from error


def _agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    if _agent_hidden_from_product(row):
        return False
    if _is_admin_user(user):
        return True
    if row.is_overall:
        return True
    return _agent_owned_by_user(row, user) or _agent_published_to_gallery(row)


def _agent_gallery_scope_conditions(
    tenant_id: str,
    user: User,
    scope: AgentGalleryScope,
) -> list[ColumnElement[bool]]:
    """构造四类广场关系视图共用的正式字段过滤条件。"""

    hidden = AgentProfile.metadata_json["hidden_from_product"].as_boolean()
    conditions = [
        AgentProfile.tenant_id == tenant_id,
        AgentProfile.status == "active",
        AgentProfile.is_overall == False,  # noqa: E712
        or_(hidden.is_(None), hidden == False),  # noqa: E712
    ]
    used_agent_ids = select(AgentUsage.agent_id).where(
        AgentUsage.tenant_id == tenant_id,
        AgentUsage.user_id == user.id,
    )
    if scope == "owned":
        conditions.append(AgentProfile.owner_user_id == user.id)
    elif scope == "used":
        conditions.append(AgentProfile.id.in_(used_agent_ids))
    elif scope == "gallery":
        conditions.extend(
            [
                AgentProfile.published_to_gallery == True,  # noqa: E712
                AgentProfile.agent_category_code != "professional",
            ]
        )
    else:
        conditions.append(AgentProfile.agent_category_code == "professional")
        if not _is_admin_user(user):
            conditions.append(
                or_(
                    AgentProfile.owner_user_id == user.id,
                    AgentProfile.published_to_gallery == True,  # noqa: E712
                    AgentProfile.id.in_(used_agent_ids),
                )
            )
    return conditions


def _agent_gallery_scope_count(
    db: Session,
    tenant_id: str,
    user: User,
    scope: AgentGalleryScope,
) -> int:
    """统计当前用户某一广场关系视图中的活动数字员工数量。"""

    return int(
        db.exec(
            select(func.count())
            .select_from(AgentProfile)
            .where(*_agent_gallery_scope_conditions(tenant_id, user, scope))
        ).one()
    )


def _agent_management_conditions(
    tenant_id: str,
    user: User,
    view: AgentManagementView,
    can_govern: bool,
) -> list[ColumnElement[bool]]:
    """构造管理端拥有员工或发布治理视图的数据库过滤条件。"""

    hidden = AgentProfile.metadata_json["hidden_from_product"].as_boolean()
    conditions: list[ColumnElement[bool]] = [
        AgentProfile.tenant_id == tenant_id,
        AgentProfile.is_overall == False,  # noqa: E712
        or_(hidden.is_(None), hidden == False),  # noqa: E712
    ]
    if view == "governance":
        if not can_govern:
            conditions.append(AgentProfile.id == "__forbidden_governance_view__")
        else:
            conditions.append(
                or_(
                    AgentProfile.owner_user_id.is_(None),
                    AgentProfile.owner_user_id != user.id,
                )
            )
        return conditions
    conditions.append(AgentProfile.owner_user_id == user.id)
    if view == "online":
        conditions.append(AgentProfile.status == "active")
    elif view == "offline":
        conditions.append(AgentProfile.status != "active")
    elif view == "pending":
        conditions.append(
            or_(
                AgentProfile.status == "pending",
                AgentProfile.metadata_json["review_status"].as_string() == "pending",
                AgentProfile.metadata_json["approval_status"].as_string() == "pending",
                AgentProfile.metadata_json["audit_status"].as_string() == "pending",
            )
        )
    elif view == "expert":
        conditions.append(AgentProfile.agent_category_code == "professional")
    return conditions


def _agent_management_count(
    db: Session,
    tenant_id: str,
    user: User,
    view: AgentManagementView,
    can_govern: bool,
) -> int:
    """统计管理端某个员工视图的完整数量，不使用当前页长度。"""

    return int(
        db.exec(
            select(func.count())
            .select_from(AgentProfile)
            .where(*_agent_management_conditions(tenant_id, user, view, can_govern))
        ).one()
    )


def _agent_management_facets(
    db: Session,
    tenant_id: str,
    user: User,
    source: str,
    department: str,
) -> AgentGalleryFacetsRead:
    """按当前所有者的专家全集生成管理页级联筛选项。"""

    metadata_rows = list(
        db.exec(
            select(AgentProfile.metadata_json).where(
                *_agent_management_conditions(tenant_id, user, "expert", False)
            )
        ).all()
    )
    source_rows = [row for row in metadata_rows if isinstance(row, dict)]
    department_rows = [
        row
        for row in source_rows
        if not source or str(row.get("expert_source_code") or "").strip() == source
    ]
    direction_rows = [
        row
        for row in department_rows
        if not department or str(row.get("expert_category") or "").strip() == department
    ]
    return AgentGalleryFacetsRead(
        sources=_gallery_facet_options(source_rows, "expert_source_code", "expert_source_label"),
        departments=_gallery_facet_options(department_rows, "expert_category"),
        directions=_gallery_facet_options(direction_rows, "expert_subcategory"),
    )


def _agent_matches_gallery_search(row: AgentProfile, keyword: str) -> bool:
    """在服务端保持原卡片搜索覆盖的姓名、岗位、标签和专家来源字段。"""

    metadata = row.metadata_json or {}
    values: list[object] = [
        row.name,
        row.description,
        row.original_name,
        row.original_description,
        metadata.get("owner_username"),
        metadata.get("owner_display_name"),
        metadata.get("role_name"),
        metadata.get("position"),
        metadata.get("department"),
        metadata.get("team"),
        metadata.get("expert_name_original"),
        metadata.get("expert_source_code"),
        metadata.get("expert_source_label"),
        metadata.get("expert_category"),
        metadata.get("expert_subcategory"),
        metadata.get("work_styles"),
        metadata.get("expertise_tags"),
        metadata.get("expert_tags"),
    ]
    return any(keyword in _gallery_search_value(value) for value in values)


def _gallery_search_value(value: object) -> str:
    """把标量或标签数组转换为不区分大小写的搜索文本。"""

    if isinstance(value, list):
        return " ".join(str(item) for item in value).lower()
    return str(value or "").lower()


def _agent_gallery_facets(
    db: Session,
    tenant_id: str,
    user: User,
    source: str,
    department: str,
) -> AgentGalleryFacetsRead:
    """从可见专家的轻量 metadata 计算与现有级联交互一致的筛选计数。"""

    metadata_rows = list(
        db.exec(
            select(AgentProfile.metadata_json).where(
                *_agent_gallery_scope_conditions(tenant_id, user, "expert")
            )
        ).all()
    )
    source_rows = [row for row in metadata_rows if isinstance(row, dict)]
    department_rows = [
        row
        for row in source_rows
        if not source or str(row.get("expert_source_code") or "").strip() == source
    ]
    direction_rows = [
        row
        for row in department_rows
        if not department or str(row.get("expert_category") or "").strip() == department
    ]
    return AgentGalleryFacetsRead(
        sources=_gallery_facet_options(source_rows, "expert_source_code", "expert_source_label"),
        departments=_gallery_facet_options(department_rows, "expert_category"),
        directions=_gallery_facet_options(direction_rows, "expert_subcategory"),
    )


def _gallery_facet_options(
    rows: list[dict],
    value_key: str,
    label_key: str | None = None,
) -> list[AgentGalleryFacetRead]:
    """聚合并按数量倒序生成专家筛选项，来源缺省标签保持前端既有名称。"""

    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for row in rows:
        value = str(row.get(value_key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
        label = str(row.get(label_key) or "").strip() if label_key else value
        labels.setdefault(value, label or ("Agency Agents" if value == "agency-agents" else value))
    return [
        AgentGalleryFacetRead(value=value, label=labels[value], count=count)
        for value, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], labels[item[0]]),
        )
    ]


def _agent_hidden_from_product(row: AgentProfile) -> bool:
    metadata = row.metadata_json or {}
    return metadata.get("hidden_from_product") is True


def _agent_published_to_gallery(row: AgentProfile) -> bool:
    """使用集中双读契约判断广场发布状态。"""

    return agent_is_published(row)


def _used_agent_ids_for_user(db: Session, tenant_id: str, user: User) -> set[str]:
    """只从 AgentUsage 读取当前使用关系，历史会话由 0024 一次性幂等回填。"""

    usage_rows = db.exec(
        select(AgentUsage.agent_id).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id != None,  # noqa: E711
        )
    ).all()
    return {str(agent_id) for agent_id in usage_rows if agent_id}


def _mark_agent_used(db: Session, tenant_id: str, user: User, agent_id: str) -> AgentUsage:
    row = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id == agent_id,
        )
    ).first()
    if row:
        row.updated_at = utc_now()
    else:
        row = AgentUsage(tenant_id=tenant_id, user_id=user.id, agent_id=agent_id)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.exec(
            select(AgentUsage).where(
                AgentUsage.tenant_id == tenant_id,
                AgentUsage.user_id == user.id,
                AgentUsage.agent_id == agent_id,
            )
        ).first()
        if not row:
            raise
    db.refresh(row)
    return row


def _chat_agent_selectable_to_user(row: AgentProfile, user: User, used_agent_ids: set[str]) -> bool:
    if row.is_overall:
        return False
    if _agent_owned_by_user(row, user):
        return True
    if _agent_published_to_gallery(row):
        return row.id in used_agent_ids
    return False


def _ensure_can_access_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if not _agent_visible_to_user(row, user):
        raise HTTPException(status_code=403, detail="Cannot access this agent")


def _ensure_can_copy_from_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if row.is_overall or _agent_owned_by_user(row, user) or _agent_published_to_gallery(row):
        return
    raise HTTPException(status_code=403, detail="Cannot copy resources from this agent")


def _ensure_can_manage_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if row.is_overall:
        if _is_admin_user(user):
            return
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if _agent_owned_by_user(row, user):
        return
    raise HTTPException(status_code=403, detail="Only the owner can manage this staff")


def _ensure_can_import_to_agent(row: AgentProfile, user: User) -> None:
    if row.is_overall:
        _ensure_admin_user(row.tenant_id, user)
        return
    _ensure_can_manage_agent(row, user)


def _ensure_admin_user(tenant_id: str, user: User) -> None:
    _ensure_request_tenant(tenant_id, user)
    if not _is_admin_user(user):
        raise HTTPException(
            status_code=403, detail="Only administrator can update the open gallery"
        )


def _bump_agent_revision(row: AgentProfile) -> None:
    """在能力契约发生有效变化时单调递增数字员工资料版本。"""

    row.profile_revision = max(int(row.profile_revision or 1), 1) + 1
    row.updated_at = utc_now()


def _validate_gallery_publication(db: Session, row: AgentProfile) -> None:
    """发布前一次性校验责任人、分类与活动资源，失败时不写入任何发布事实。"""

    if row.status != "active":
        raise HTTPException(status_code=409, detail="Only active staff can be published")
    owner_id = agent_owner_user_id(row)
    owner = db.get(User, owner_id) if owner_id else None
    if (
        owner is None
        or owner.tenant_id != row.tenant_id
        or owner.membership_status != "active"
    ):
        raise HTTPException(status_code=409, detail="An active owner is required before publication")
    _validated_agent_category(db, row.tenant_id, agent_category(row))
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == row.tenant_id,
            AgentResourceBinding.agent_id == row.id,
            AgentResourceBinding.status == "active",
        )
    ).all()
    for binding in bindings:
        resource = _resolve_resource(
            db,
            row.tenant_id,
            binding.resource_type,
            binding.resource_id,
        )
        if resource is None or resource.tenant_id != row.tenant_id:
            raise HTTPException(
                status_code=409,
                detail=f"Active resource is unavailable: {binding.resource_type}/{binding.resource_id}",
            )


def _metadata_with_creator(metadata: dict[str, object], user: User) -> dict[str, object]:
    """规范新员工责任字段，并移除只能由治理命令写入的广场状态。"""
    normalized = dict(metadata or {})
    for key in GALLERY_GOVERNANCE_METADATA_KEYS:
        normalized.pop(key, None)
    display_name = user.display_name or user.username
    normalized["owner_user_id"] = user.id
    normalized["owner_username"] = user.username
    normalized["owner_display_name"] = display_name
    normalized["created_by_user_id"] = user.id
    normalized["created_by_username"] = user.username
    normalized["created_by"] = user.username
    normalized["created_by_display_name"] = display_name
    normalized["creator_name"] = user.username
    return normalized


def _metadata_preserving_creator(
    existing_metadata: dict[str, object],
    next_metadata: dict[str, object],
    user: User,
) -> dict[str, object]:
    """保留不可由通用资料更新覆盖的责任字段与广场治理状态。"""
    normalized = dict(next_metadata or {})
    for key in (
        "owner_user_id",
        "owner_username",
        "owner_display_name",
        "created_by_user_id",
        "created_by_username",
        "created_by",
        "created_by_display_name",
        "creator_name",
        *GALLERY_GOVERNANCE_METADATA_KEYS,
    ):
        existing_value = existing_metadata.get(key)
        if existing_value is not None:
            normalized[key] = existing_value
        else:
            normalized.pop(key, None)
    return normalized


def _chat_agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    """聊天使用边界不继承治理查看权，只允许所有者或已发布的租户员工。"""

    return _agent_owned_by_user(row, user) or _agent_published_to_gallery(row)


def binding_read(
    row: AgentResourceBinding,
    *,
    include_metadata: bool = True,
) -> AgentResourceBindingRead:
    """读取资源绑定；面向非所有者的公开摘要不返回可能含凭据的 metadata。"""

    return AgentResourceBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        status=row.status,
        metadata=dict(row.metadata_json or {}) if include_metadata else {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _copy_agent_scope_from_source(
    db: Session,
    tenant_id: str,
    source: AgentProfile,
    target: AgentProfile,
    current_user: User,
) -> dict[str, object]:
    """按当前操作者真实可复用范围复制能力，并返回可解释的采用与跳过摘要。"""

    copied: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    if source.is_overall:
        copy_overall_scope_to_agent(db, tenant_id, target)
        copied.append({"kind": "overall_scope", "id": source.id})
    else:
        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == source.id,
            )
        ).all()
        for binding in bindings:
            resolved = _resolve_resource(
                db,
                tenant_id,
                binding.resource_type,
                binding.resource_id,
            )
            source_owned = _agent_owned_by_user(source, current_user)
            reason = "resource_not_found" if resolved is None else None
            if resolved is not None and not source_owned:
                reason = (
                    None
                    if _open_gallery_resource_enabled(
                        db,
                        tenant_id,
                        binding.resource_type,
                        resolved,
                    )
                    else "not_reusable_from_gallery"
                )
            if resolved is not None and reason is None:
                reason = _blocked_learning_reason(
                    db,
                    tenant_id,
                    source,
                    binding.resource_type,
                    resolved,
                    binding,
                )
            if reason:
                skipped.append(
                    {
                        "kind": binding.resource_type,
                        "id": binding.resource_id,
                        "reason": reason,
                    }
                )
                continue
            _copy_resource_binding(
                db,
                tenant_id,
                source.id,
                target.id,
                binding,
                copy_source_branch=source_owned,
            )
            copied.append({"kind": binding.resource_type, "id": binding.resource_id})
    if source.is_overall or _agent_owned_by_user(source, current_user):
        _copy_agent_models_from_source(db, tenant_id, source, target)
        copied.append({"kind": "model_bindings", "id": source.id})
    else:
        skipped.append(
            {
                "kind": "model_bindings",
                "id": source.id,
                "reason": "private_model_configuration",
            }
        )
    return {"copied": copied, "skipped": skipped}


def _copy_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent_id: str,
    target_agent_id: str,
    binding: AgentResourceBinding,
    *,
    copy_source_branch: bool = True,
) -> None:
    """复制已允许复用的绑定；非所有者复制时不继承源员工的私人分支内容。"""

    if binding.status != "active":
        return
    copied_binding = AgentResourceBinding(
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        resource_type=binding.resource_type,
        resource_id=binding.resource_id,
        status=binding.status,
        metadata_json={},
    )
    db.add(copied_binding)
    if binding.resource_type == "skill":
        skill = db.get(Skill, binding.resource_id)
        if skill and skill.tenant_id == tenant_id:
            if copy_source_branch:
                _copy_skill_branch(db, tenant_id, source_agent_id, target_agent_id, skill)
            else:
                ensure_agent_skill_branch(db, tenant_id, target_agent_id, skill)
    elif binding.resource_type == "knowledge_base":
        kb = db.get(KnowledgeBase, binding.resource_id)
        if kb and kb.tenant_id == tenant_id and copy_source_branch:
            _copy_knowledge_branch(db, tenant_id, source_agent_id, target_agent_id, kb)


def _dedupe_ids(resource_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_id in resource_ids:
        resource_id = str(raw_id or "").strip()
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        deduped.append(resource_id)
    return deduped


def _source_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resource_id: str,
) -> AgentResourceBinding | None:
    if source_agent.is_overall:
        return None
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == source_agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()


AgentResource = Skill | GeneralSkill | KnowledgeBase | Tool


def _blocked_learning_reason(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
    source_binding: AgentResourceBinding | None,
) -> str | None:
    if source_agent.is_overall:
        return (
            None
            if _open_gallery_resource_enabled(db, tenant_id, resource_type, resolved)
            else "disabled_in_open_gallery"
        )
    if not source_binding or source_binding.status != "active":
        return "inactive_in_source_agent"
    if resource_type == "skill" and isinstance(resolved, Skill):
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent.id,
                AgentSkillBranch.skill_id == resolved.skill_id,
            )
        ).first()
        if branch and branch.status != "active":
            return "inactive_in_source_agent"
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent.id,
                AgentKnowledgeBranch.knowledge_base_id == resolved.id,
            )
        ).first()
        if branch and branch.status != "active":
            return "inactive_in_source_agent"
    return None


def _open_gallery_resource_enabled(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resolved: AgentResource,
) -> bool:
    if not is_open_gallery_resource(db, tenant_id, resource_type, resolved):
        return False
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.status == "published"
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.status == "published"
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        return resolved.status == "active"
    if resource_type == "tool" and isinstance(resolved, Tool):
        return resolved.enabled
    return False


def _import_resource_to_overall(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
) -> None:
    if source_agent.is_overall:
        return
    if resource_type == "skill" and isinstance(resolved, Skill):
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent.id,
                AgentSkillBranch.skill_id == resolved.skill_id,
            )
        ).first()
        if branch:
            promote_branch_to_overall(db, tenant_id, branch)
        return
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        promote_knowledge_branch_to_overall(db, tenant_id, source_agent.id, resolved.id)


def _upsert_imported_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    target_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
    source_binding: AgentResourceBinding | None,
) -> None:
    status = source_binding.status if source_binding else "active"
    metadata = agent_private_metadata(
        target_agent.id,
        dict(source_binding.metadata_json or {}) if source_binding else {},
    )
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == target_agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resolved.id,
        )
    ).first()
    if existing:
        existing.status = status
        existing.metadata_json = metadata
        existing.updated_at = utc_now()
        db.add(existing)
    else:
        db.add(
            AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=target_agent.id,
                resource_type=resource_type,
                resource_id=resolved.id,
                status=status,
                metadata_json=metadata,
            )
        )
    if resource_type == "skill" and isinstance(resolved, Skill):
        _copy_or_update_skill_branch(db, tenant_id, source_agent.id, target_agent.id, resolved)
    elif resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        _copy_or_update_knowledge_branch(db, tenant_id, source_agent.id, target_agent.id, resolved)


def _copy_or_update_skill_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill
) -> None:
    with db.no_autoflush:
        source_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent_id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
    if not source_branch:
        branch = sync_branch_from_overall(db, tenant_id, target_agent_id, skill)
        _ensure_copied_skill_branch_version(db, branch, "导入自整体智能体")
        return
    with db.no_autoflush:
        target_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == target_agent_id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
    if not target_branch:
        target_branch = AgentSkillBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            skill_id=source_branch.skill_id,
            source_skill_id=source_branch.source_skill_id,
        )
    target_branch.base_version = source_branch.base_version
    target_branch.head_version = source_branch.head_version
    target_branch.content_json = dict(source_branch.content_json or {})
    target_branch.status = source_branch.status
    target_branch.sync_state = source_branch.sync_state
    target_branch.metadata_json = dict(source_branch.metadata_json or {})
    target_branch.updated_at = utc_now()
    db.add(target_branch)
    db.flush()
    _ensure_copied_skill_branch_version(db, target_branch, f"导入自 {source_agent_id}")


def _ensure_copied_skill_branch_version(
    db: Session, branch: AgentSkillBranch, change_summary: str
) -> None:
    existing = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == branch.head_version,
        )
    ).first()
    if existing:
        existing.content_json = dict(branch.content_json or {})
        existing.status = branch.status
        existing.sync_state = branch.sync_state
        existing.updated_at = utc_now()
        db.add(existing)
        return
    db.add(
        AgentSkillBranchVersion(
            tenant_id=branch.tenant_id,
            agent_id=branch.agent_id,
            skill_id=branch.skill_id,
            source_skill_id=branch.source_skill_id,
            version=branch.head_version,
            base_version=branch.base_version,
            content_json=dict(branch.content_json or {}),
            status=branch.status,
            sync_state=branch.sync_state,
            change_summary=change_summary,
        )
    )


def _copy_or_update_knowledge_branch(
    db: Session,
    tenant_id: str,
    source_agent_id: str,
    target_agent_id: str,
    kb: KnowledgeBase,
) -> None:
    with db.no_autoflush:
        source_branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb.id,
            )
        ).first()
        target_branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == target_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb.id,
            )
        ).first()
    if source_branch:
        base_version = source_branch.base_version
        head_version = source_branch.head_version
        status = source_branch.status
        sync_state = source_branch.sync_state
        metadata = dict(source_branch.metadata_json or {})
    else:
        version = ensure_knowledge_base_version(db, kb).version
        base_version = version
        head_version = version
        status = "active"
        sync_state = "synced"
        metadata = {}
    if not target_branch:
        target_branch = AgentKnowledgeBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            knowledge_base_id=kb.id,
        )
    target_branch.base_version = base_version
    target_branch.head_version = head_version
    target_branch.status = status
    target_branch.sync_state = sync_state
    target_branch.metadata_json = metadata
    target_branch.updated_at = utc_now()
    db.add(target_branch)


def _resource_display_id(resource_type: str, resolved: AgentResource) -> str:
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.skill_id
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.slug
    if resource_type == "tool" and isinstance(resolved, Tool):
        return resolved.name
    return resolved.id


def _copy_agent_models_from_source(
    db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile
) -> None:
    bindings = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == source.id,
        )
    ).all()
    for binding in bindings:
        db.add(
            AgentModelBinding(
                tenant_id=tenant_id,
                agent_id=target.id,
                role=binding.role,
                model_config_id=binding.model_config_id,
            )
        )


def _copy_skill_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill
) -> None:
    source_branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == source_agent_id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    if not source_branch:
        ensure_agent_skill_branch(db, tenant_id, target_agent_id, skill)
        return
    target_branch = AgentSkillBranch(
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        skill_id=source_branch.skill_id,
        source_skill_id=source_branch.source_skill_id,
        base_version=source_branch.base_version,
        head_version=source_branch.head_version,
        content_json=dict(source_branch.content_json or {}),
        status=source_branch.status,
        sync_state=source_branch.sync_state,
        metadata_json=dict(source_branch.metadata_json or {}),
    )
    db.add(target_branch)
    db.flush()
    db.add(
        AgentSkillBranchVersion(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            skill_id=target_branch.skill_id,
            source_skill_id=target_branch.source_skill_id,
            version=target_branch.head_version,
            base_version=target_branch.base_version,
            content_json=dict(target_branch.content_json or {}),
            status=target_branch.status,
            sync_state=target_branch.sync_state,
            change_summary=f"复制自 {source_agent_id}",
        )
    )


def _copy_knowledge_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, kb: KnowledgeBase
) -> None:
    source_branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == source_agent_id,
            AgentKnowledgeBranch.knowledge_base_id == kb.id,
        )
    ).first()
    if not source_branch:
        return
    db.add(
        AgentKnowledgeBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            knowledge_base_id=source_branch.knowledge_base_id,
            base_version=source_branch.base_version,
            head_version=source_branch.head_version,
            status=source_branch.status,
            sync_state=source_branch.sync_state,
            metadata_json=dict(source_branch.metadata_json or {}),
        )
    )


def _resolve_resource(
    db: Session, tenant_id: str, resource_type: str, identifier: str
) -> AgentResource | None:
    if resource_type == "skill":
        return (
            db.get(Skill, identifier)
            or db.exec(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == identifier)
            ).first()
        )
    if resource_type == "general_skill":
        return (
            db.get(GeneralSkill, identifier)
            or db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == identifier
                )
            ).first()
        )
    if resource_type == "knowledge_base":
        return (
            db.get(KnowledgeBase, identifier)
            or db.exec(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == identifier
                )
            ).first()
        )
    if resource_type == "tool":
        return (
            db.get(Tool, identifier)
            or db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == identifier)
            ).first()
        )
    return None


def _get_agent(db: Session, tenant_id: str, agent_id: str) -> AgentProfile:
    ensure_tenant(db, tenant_id)
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row


def _bindings_by_agent(
    db: Session,
    tenant_id: str,
    agent_ids: set[str] | None = None,
) -> dict[str, list[AgentResourceBinding]]:
    """批量读取租户资源绑定；指定员工集合时避免列表页扫描无关绑定。"""

    if agent_ids is not None and not agent_ids:
        return {}
    statement = select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == tenant_id)
    if agent_ids is not None:
        statement = statement.where(AgentResourceBinding.agent_id.in_(agent_ids))
    rows = db.exec(statement.order_by(AgentResourceBinding.created_at.asc())).all()
    agent_ids = {row.agent_id for row in rows}
    agents_by_id = {
        row.id: row
        for row in db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.id.in_(agent_ids) if agent_ids else AgentProfile.id == "__none__",
            )
        ).all()
    }
    grouped: dict[str, list[AgentResourceBinding]] = {}
    for row in rows:
        if not _resource_binding_visible_in_agent_summary(
            db, tenant_id, agents_by_id.get(row.agent_id), row
        ):
            continue
        grouped.setdefault(row.agent_id, []).append(row)
    return grouped


def _resource_binding_visible_in_agent_summary(
    db: Session,
    tenant_id: str,
    agent: AgentProfile | None,
    binding: AgentResourceBinding,
) -> bool:
    if not agent or binding.status == "deleted":
        return False

    model_by_type = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }
    model = model_by_type.get(binding.resource_type)
    if model is None:
        return False
    resource = db.get(model, binding.resource_id)
    if not resource or resource.tenant_id != tenant_id:
        return False
    if isinstance(resource, KnowledgeBase) and _is_empty_default_knowledge_base(
        db, tenant_id, resource
    ):
        return False

    if agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, binding.resource_type, resource):
            return False
    elif not is_bound_resource_visible_for_agent(
        db, tenant_id, binding.resource_type, resource, binding
    ):
        return False

    if isinstance(resource, Skill) and not agent.is_overall:
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == resource.skill_id,
            )
        ).first()
        if branch and branch.status == "deleted":
            return False
        skill_status = branch.status if branch else resource.status
        if binding.status == "active" and skill_status not in {"active", "published"}:
            return False

    if isinstance(resource, KnowledgeBase) and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == resource.id,
                AgentKnowledgeBranch.status != "deleted",
            )
        ).first()
        if not branch:
            return False
        if binding.status == "active" and branch.status != "active":
            return False

    if binding.status != "active":
        return True
    if isinstance(resource, Skill):
        return agent.is_overall is False or resource.status == "published"
    if isinstance(resource, GeneralSkill):
        return resource.status == "published"
    if isinstance(resource, KnowledgeBase):
        return resource.status == "active"
    if isinstance(resource, Tool):
        return resource.enabled
    return False


def _is_empty_default_knowledge_base(db: Session, tenant_id: str, kb: KnowledgeBase) -> bool:
    metadata = kb.metadata_json or {}
    has_runtime_rows = any(
        db.exec(
            select(model.id).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == kb.id,
            )
        ).first()
        for model in (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk)
    )
    if has_runtime_rows:
        return False
    if metadata.get("created_from_document_upload") and not metadata.get("source_document_id"):
        return True
    return kb.name == "默认知识库"


def _ensure_resource_exists(db: Session, tenant_id: str, item: AgentResourceBindingInput) -> None:
    model = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }[item.resource_type]
    row = db.get(model, item.resource_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(
            status_code=404, detail=f"Resource not found: {item.resource_type}:{item.resource_id}"
        )


def _get_global_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _skill_branch_read(skill: Skill) -> dict[str, object]:
    metadata = getattr(skill, "agent_branch_meta", {}) or {}
    content = skill.content_json or {}
    if not metadata and isinstance(content.get("metadata"), dict):
        metadata = content.get("metadata", {}).get("agent_branch", {}) or {}
    return {
        "id": skill.id,
        "tenant_id": skill.tenant_id,
        "skill_id": skill.skill_id,
        "version": skill.version,
        "name": skill.name,
        "business_domain": skill.business_domain,
        "description": skill.description,
        "content": skill.content_json,
        "status": skill.status,
        "agent_id": metadata.get("agent_id"),
        "branch_status": metadata.get("status"),
        "branch_sync_state": metadata.get("sync_state"),
        "branch_base_version": metadata.get("base_version"),
        "branch_head_version": metadata.get("head_version"),
        "metadata": dict(metadata.get("metadata") or {}),
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }
