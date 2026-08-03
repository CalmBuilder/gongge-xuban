"""
@Time       : 2026/07/28 15:45
@Author     : zhanglp8181
@File       : organization_leaders.py
@CallChain  : 组织负责人页面 → FastAPI → 负责人领域服务 → OrganizationLeaderAssignment
@Description: 提供负责人当前态、历史、创建和结束的租户隔离 API。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import (
    CodeItem,
    EmployeeProfile,
    MemberOrgAssignment,
    OrganizationLeaderAssignment,
    User,
    utc_now,
)
from app.organization.governance import (
    authorized_organization_ids,
    ensure_governance_permission,
    resolve_permission_grants,
)
from app.organization.leaders import (
    OrganizationLeaderError,
    create_organization_leader,
    end_organization_leader,
)
from app.organization.reference_data import ensure_organization_leader_type_catalog
from app.security.auth import get_current_user


router = APIRouter(prefix="/api/organization", tags=["organization-leaders"])


class LeaderTypeRead(BaseModel):
    """返回负责人类型选项，分类本身不产生权限。"""

    code: str
    name: str
    status: Literal["active", "inactive"]
    sort_order: int


class LeaderAssignmentCreate(BaseModel):
    """创建立即生效负责人关系的请求。"""

    tenant_id: str
    org_unit_id: str
    employee_profile_id: str
    leader_type_code: str
    position_assignment_id: str | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class LeaderAssignmentEnd(BaseModel):
    """结束活动负责人关系的请求。"""

    tenant_id: str
    effective_until: datetime | None = None


class LeaderAssignmentRead(BaseModel):
    """返回负责人当前态与不可覆盖历史。"""

    id: str
    tenant_id: str
    org_unit_id: str
    employee_profile_id: str
    position_assignment_id: str | None
    leader_type_code: str
    effective_from: datetime
    effective_until: datetime | None
    status: Literal["active", "inactive"]
    source_kind: Literal["manual"]
    created_by_user_id: str | None


@router.get("/leader-types", response_model=list[LeaderTypeRead])
def list_leader_types(
    tenant_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[LeaderTypeRead]:
    """列出同租户负责人类型，普通成员可用于当前组织责任展示。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.read",
    )
    code_set = ensure_organization_leader_type_catalog(db, tenant_id)
    db.commit()
    rows = db.exec(
        select(CodeItem)
        .where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
        )
        .order_by(CodeItem.sort_order, CodeItem.item_code)
    ).all()
    return [
        LeaderTypeRead(
            code=row.item_code,
            name=row.name,
            status=row.status,
            sort_order=row.sort_order,
        )
        for row in rows
    ]


@router.get("/leader-assignments", response_model=list[LeaderAssignmentRead])
def list_leader_assignments(
    tenant_id: str,
    org_unit_id: str | None = None,
    employee_profile_id: str | None = None,
    include_history: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[LeaderAssignmentRead]:
    """按组织或成员读取负责人；普通成员只能读取当前活动关系。"""

    _require_current_tenant(tenant_id, current_user)
    if not org_unit_id and not employee_profile_id:
        raise HTTPException(status_code=400, detail="LEADER_FILTER_REQUIRED")
    self_read = _is_self_profile(db, tenant_id, employee_profile_id, current_user)
    current_org_read = (
        not include_history
        and org_unit_id is not None
        and _is_current_member_of_org(
            db,
            tenant_id=tenant_id,
            org_unit_id=org_unit_id,
            current_user=current_user,
        )
    )
    allowed_ids = None
    if (not self_read and not current_org_read) or include_history:
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="organization.read",
            target_org_unit_id=org_unit_id,
        )
        allowed_ids = authorized_organization_ids(
            resolve_permission_grants(
                db,
                tenant_id=tenant_id,
                user_id=current_user.id,
            ),
            permission_code="organization.read",
        )
    query = select(OrganizationLeaderAssignment).where(
        OrganizationLeaderAssignment.tenant_id == tenant_id
    )
    if org_unit_id:
        query = query.where(OrganizationLeaderAssignment.org_unit_id == org_unit_id)
    if employee_profile_id:
        query = query.where(
            OrganizationLeaderAssignment.employee_profile_id == employee_profile_id
        )
    if allowed_ids is not None:
        query = query.where(OrganizationLeaderAssignment.org_unit_id.in_(allowed_ids))
    if not include_history:
        now = utc_now()
        query = query.where(
            OrganizationLeaderAssignment.status == "active",
            OrganizationLeaderAssignment.effective_from <= now,
            or_(
                OrganizationLeaderAssignment.effective_until.is_(None),
                OrganizationLeaderAssignment.effective_until > now,
            ),
        )
    rows = db.exec(
        query.order_by(
            OrganizationLeaderAssignment.status,
            OrganizationLeaderAssignment.leader_type_code,
            OrganizationLeaderAssignment.effective_from.desc(),
        )
    ).all()
    return [_leader_read(row) for row in rows]


@router.post("/leader-assignments", response_model=LeaderAssignmentRead)
def create_leader_assignment(
    request: LeaderAssignmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> LeaderAssignmentRead:
    """由租户管理员创建负责人关系，不产生角色和权限。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="organization.manage",
        target_org_unit_id=request.org_unit_id,
    )
    try:
        row = create_organization_leader(
            db,
            tenant_id=request.tenant_id,
            org_unit_id=request.org_unit_id,
            employee_profile_id=request.employee_profile_id,
            leader_type_code=request.leader_type_code,
            actor_user_id=current_user.id,
            position_assignment_id=request.position_assignment_id,
            effective_from=request.effective_from,
            effective_until=request.effective_until,
        )
    except OrganizationLeaderError as error:
        raise _leader_http_error(error) from error
    db.commit()
    db.refresh(row)
    return _leader_read(row)


@router.post(
    "/leader-assignments/{assignment_id}/end",
    response_model=LeaderAssignmentRead,
)
def end_leader_assignment(
    assignment_id: str,
    request: LeaderAssignmentEnd,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> LeaderAssignmentRead:
    """由租户管理员结束负责人任期并保留历史。"""

    try:
        existing = db.get(OrganizationLeaderAssignment, assignment_id)
        if existing is None or existing.tenant_id != request.tenant_id:
            raise OrganizationLeaderError("LEADER_ASSIGNMENT_NOT_FOUND")
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="organization.manage",
            target_org_unit_id=existing.org_unit_id,
        )
        row = end_organization_leader(
            db,
            tenant_id=request.tenant_id,
            assignment_id=assignment_id,
            effective_until=request.effective_until,
        )
    except OrganizationLeaderError as error:
        raise _leader_http_error(error) from error
    db.commit()
    db.refresh(row)
    return _leader_read(row)


def _require_current_tenant(tenant_id: str, current_user: User) -> None:
    """拒绝通过查询参数读取其他租户负责人事实。"""

    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access another tenant")


def _is_self_profile(
    db: Session,
    tenant_id: str,
    employee_profile_id: str | None,
    current_user: User,
) -> bool:
    """仅允许成员按明确员工档案读取自己的当前负责人事实。"""

    if employee_profile_id is None:
        return False
    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.id == employee_profile_id,
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == current_user.id,
        )
    ).first()
    return profile is not None


def _is_current_member_of_org(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
    current_user: User,
) -> bool:
    """允许成员查看本人当前归属组织的现任负责人，不开放历史或组织治理目录。"""

    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == current_user.id,
            EmployeeProfile.status == "active",
        )
    ).first()
    if profile is None:
        return False
    now = utc_now()
    assignment = db.exec(
        select(MemberOrgAssignment).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.employee_profile_id == profile.id,
            MemberOrgAssignment.org_unit_id == org_unit_id,
            MemberOrgAssignment.status == "active",
            MemberOrgAssignment.effective_from <= now,
            or_(
                MemberOrgAssignment.effective_until.is_(None),
                MemberOrgAssignment.effective_until > now,
            ),
        )
    ).first()
    return assignment is not None


def _leader_read(row: OrganizationLeaderAssignment) -> LeaderAssignmentRead:
    """把负责人数据库实体转换为稳定响应。"""

    return LeaderAssignmentRead(
        id=row.id,
        tenant_id=row.tenant_id,
        org_unit_id=row.org_unit_id,
        employee_profile_id=row.employee_profile_id,
        position_assignment_id=row.position_assignment_id,
        leader_type_code=row.leader_type_code,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        status=row.status,
        source_kind="manual",
        created_by_user_id=row.created_by_user_id,
    )


def _leader_http_error(error: OrganizationLeaderError) -> HTTPException:
    """把负责人领域错误映射为稳定 HTTP 状态。"""

    detail = str(error)
    if detail in {
        "ORGANIZATION_NOT_FOUND",
        "EMPLOYEE_PROFILE_NOT_FOUND",
        "POSITION_ASSIGNMENT_NOT_FOUND",
        "LEADER_ASSIGNMENT_NOT_FOUND",
    }:
        return HTTPException(status_code=404, detail=detail)
    if detail == "PRIMARY_LEADER_EXISTS":
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=detail)
