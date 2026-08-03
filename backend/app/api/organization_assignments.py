"""
@Time       : 2026/07/28 16:05
@Author     : zhanglp8181
@File       : organization_assignments.py
@CallChain  : 组织岗位管理页面 → FastAPI → 组织归属/岗位任职领域命令
@Description: 提供岗位目录、成员组织归属和岗位任职历史的租户隔离 API。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.db import get_session
from app.db.models import (
    CodeItem,
    EmployeeProfile,
    MemberOrgAssignment,
    OrganizationMigrationIssue,
    Position,
    PositionAssignment,
    PositionRoleBinding,
    BusinessRole,
    User,
)
from app.organization.assignments import (
    OrganizationAssignmentError,
    assign_member_to_organization,
    assign_member_to_position,
    create_position,
    deactivate_position,
    end_member_org_assignment,
    end_position_assignment,
    ensure_assignment_foundation,
    get_tenant_position,
    update_position,
)
from app.organization.governance import (
    authorized_organization_ids,
    ensure_governance_permission,
    resolve_permission_grants,
)
from app.organization.reference_data import ensure_position_type_catalog
from app.organization.query import current_assignment_predicates
from app.organization.roles import (
    bind_position_business_role,
    deactivate_position_role_binding,
)
from app.security.auth import get_current_user


router = APIRouter(prefix="/api/organization", tags=["organization-assignments"])
POSITION_CODE_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"


class PositionCreate(BaseModel):
    """创建当前租户岗位的请求。"""

    tenant_id: str
    org_unit_id: str
    code: str = Field(pattern=POSITION_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=191)
    position_type_code: str = Field(min_length=1, max_length=128)
    reports_to_position_id: str | None = None
    headcount_limit: int | None = Field(default=None, ge=1)
    responsibility: str | None = Field(default=None, max_length=4096)


class PositionUpdate(BaseModel):
    """更新岗位可变资料的请求，稳定编码不可修改。"""

    tenant_id: str
    org_unit_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=191)
    position_type_code: str | None = Field(default=None, min_length=1, max_length=128)
    reports_to_position_id: str | None = None
    clear_reports_to: bool = False
    headcount_limit: int | None = Field(default=None, ge=1)
    responsibility: str | None = Field(default=None, max_length=4096)


class PositionRead(BaseModel):
    """返回岗位目录和汇报关系。"""

    id: str
    tenant_id: str
    org_unit_id: str
    code: str
    name: str
    position_type_code: str
    grade_code: str | None
    reports_to_position_id: str | None
    headcount_limit: int | None
    responsibility: str | None
    status: Literal["active", "inactive"]


class PositionTypeRead(BaseModel):
    """返回租户岗位类型码项。"""

    code: str
    name: str
    status: Literal["active", "inactive"]
    sort_order: int


class OrgAssignmentCreate(BaseModel):
    """执行入职、调岗或兼任组织命令的请求。"""

    tenant_id: str
    employee_profile_id: str
    org_unit_id: str
    assignment_type: Literal["primary", "concurrent", "temporary", "project"] = "primary"
    effective_from: datetime | None = None


class PositionAssignmentCreate(BaseModel):
    """执行主岗位、兼任、代理或临时岗位任职命令的请求。"""

    tenant_id: str
    employee_profile_id: str
    position_id: str
    assignment_type: Literal["primary", "concurrent", "acting", "temporary"] = "primary"
    effective_from: datetime | None = None


class AssignmentEnd(BaseModel):
    """结束组织归属或岗位任职区间的请求。"""

    tenant_id: str
    effective_until: datetime | None = None


class OrgAssignmentRead(BaseModel):
    """返回一段不可覆盖的成员组织归属历史。"""

    id: str
    tenant_id: str
    employee_profile_id: str
    org_unit_id: str
    assignment_type: str
    is_primary: bool
    effective_from: datetime
    effective_until: datetime | None
    status: Literal["active", "inactive"]


class OrgAssignmentMemberRead(OrgAssignmentRead):
    """返回组织成员分页所需的真人名称和账号摘要。"""

    user_id: str
    username: str
    display_name: str | None
    employee_id: str
    employee_name: str


class OrgAssignmentMemberPageRead(BaseModel):
    """返回单个组织当前直属成员的稳定分页。"""

    items: list[OrgAssignmentMemberRead]
    total: int
    page: int
    page_size: int


class PositionAssignmentRead(BaseModel):
    """返回一段不可覆盖的岗位任职历史。"""

    id: str
    tenant_id: str
    employee_profile_id: str
    position_id: str
    assignment_type: str
    is_primary: bool
    effective_from: datetime
    effective_until: datetime | None
    status: Literal["active", "inactive"]


class MigrationIssueRead(BaseModel):
    """返回旧部门字段无法自动判断的治理事项。"""

    id: str
    employee_profile_id: str
    source_field: str
    source_value: str | None
    issue_code: str
    resolution_status: str


class PositionRoleBindingCreate(BaseModel):
    """创建岗位默认业务角色绑定的请求。"""

    tenant_id: str
    position_id: str
    business_role_id: str
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class PositionRoleBindingRead(BaseModel):
    """返回岗位默认角色及其来源和状态。"""

    id: str
    tenant_id: str
    position_id: str
    business_role_id: str
    business_role_code: str
    business_role_name: str
    scope_mode: Literal["position_org"]
    granted_by_user_id: str | None
    status: Literal["active", "inactive"]
    effective_from: datetime | None
    effective_until: datetime | None


@router.get("/positions", response_model=list[PositionRead])
def list_positions(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    org_unit_id: str | None = None,
) -> list[PositionRead]:
    """列出当前认证租户岗位，可按当前组织缩小读取范围。"""

    allowed_ids = _authorized_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="position.read",
    )
    statement = select(Position).where(Position.tenant_id == tenant_id)
    if org_unit_id:
        _require_org_in_scope(allowed_ids, "position.read", org_unit_id)
        statement = statement.where(Position.org_unit_id == org_unit_id)
    elif allowed_ids is not None:
        statement = statement.where(Position.org_unit_id.in_(allowed_ids))
    rows = db.exec(
        statement.order_by(Position.org_unit_id, Position.code, Position.id)
    ).all()
    return [_position_read(row) for row in rows]


@router.get("/position-types", response_model=list[PositionTypeRead])
def list_position_types(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[PositionTypeRead]:
    """列出当前租户岗位类型，首次访问时幂等补齐内置项。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="position.read",
    )
    code_set = ensure_position_type_catalog(db, tenant_id)
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
        PositionTypeRead(
            code=row.item_code,
            name=row.name,
            status=row.status,
            sort_order=row.sort_order,
        )
        for row in rows
    ]


@router.post("/positions", response_model=PositionRead)
def create_position_endpoint(
    request: PositionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionRead:
    """由租户管理员创建岗位。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="position.manage",
        target_org_unit_id=request.org_unit_id,
    )
    try:
        ensure_assignment_foundation(db, request.tenant_id)
        row = create_position(db, **request.model_dump())
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="position.manage",
        action="position.create",
        action_kind="create",
        outcome="success",
        resource_type="position",
        resource_id=row.id,
        target_org_unit_id=row.org_unit_id,
        after=_position_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _position_read(row)


@router.put("/positions/{position_id}", response_model=PositionRead)
def update_position_endpoint(
    position_id: str,
    request: PositionUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionRead:
    """由租户管理员更新岗位资料或汇报关系。"""

    try:
        row = get_tenant_position(db, request.tenant_id, position_id)
        before = _position_audit_snapshot(row)
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="position.manage",
            target_org_unit_id=row.org_unit_id,
        )
        if request.org_unit_id is not None:
            ensure_governance_permission(
                db,
                tenant_id=request.tenant_id,
                current_user=current_user,
                permission_code="position.manage",
                target_org_unit_id=request.org_unit_id,
            )
        fields = request.model_fields_set
        update_position(
            db,
            row,
            org_unit_id=request.org_unit_id if "org_unit_id" in fields else None,
            name=request.name if "name" in fields else None,
            position_type_code=(
                request.position_type_code if "position_type_code" in fields else None
            ),
            reports_to_position_id=(
                request.reports_to_position_id
                if "reports_to_position_id" in fields
                else None
            ),
            clear_reports_to=request.clear_reports_to,
            headcount_limit=(
                request.headcount_limit if "headcount_limit" in fields else None
            ),
            responsibility=(
                request.responsibility if "responsibility" in fields else None
            ),
        )
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="position.manage",
        action="position.update",
        action_kind="update",
        outcome="success",
        resource_type="position",
        resource_id=row.id,
        target_org_unit_id=row.org_unit_id,
        before=before,
        after=_position_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _position_read(row)


@router.delete("/positions/{position_id}", response_model=PositionRead)
def deactivate_position_endpoint(
    position_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionRead:
    """软停用没有活动任职和活动下级的岗位。"""

    try:
        row = get_tenant_position(db, tenant_id, position_id)
        before = _position_audit_snapshot(row)
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="position.manage",
            target_org_unit_id=row.org_unit_id,
        )
        deactivate_position(db, row)
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="position.manage",
        action="position.deactivate",
        action_kind="delete",
        outcome="success",
        resource_type="position",
        resource_id=row.id,
        target_org_unit_id=row.org_unit_id,
        before=before,
        after=_position_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _position_read(row)


@router.get("/member-org-assignments", response_model=list[OrgAssignmentRead])
def list_member_org_assignments(
    tenant_id: str = Query(...),
    employee_profile_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    org_unit_id: str | None = None,
) -> list[OrgAssignmentRead]:
    """按组织或员工读取归属历史，普通成员只能读取本人。"""

    self_read = _is_self_profile(db, tenant_id, employee_profile_id, current_user)
    allowed_ids = None
    if not self_read:
        allowed_ids = _authorized_org_ids(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="member.read",
        )
    statement = select(MemberOrgAssignment).where(
        MemberOrgAssignment.tenant_id == tenant_id
    )
    if employee_profile_id:
        statement = statement.where(
            MemberOrgAssignment.employee_profile_id == employee_profile_id
        )
    if org_unit_id:
        if not self_read:
            _require_org_in_scope(allowed_ids, "member.read", org_unit_id)
        statement = statement.where(MemberOrgAssignment.org_unit_id == org_unit_id)
    elif not self_read and allowed_ids is not None:
        statement = statement.where(MemberOrgAssignment.org_unit_id.in_(allowed_ids))
    rows = db.exec(
        statement.order_by(
            MemberOrgAssignment.employee_profile_id,
            MemberOrgAssignment.effective_from,
            MemberOrgAssignment.id,
        )
    ).all()
    return [_org_assignment_read(row) for row in rows]


@router.get(
    "/member-org-assignments/page",
    response_model=OrgAssignmentMemberPageRead,
)
def list_current_organization_members_page(
    tenant_id: str = Query(...),
    org_unit_id: str = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgAssignmentMemberPageRead:
    """分页返回有权查看组织的当前直属成员，不下载全部任期或整棵人员树。"""

    allowed_ids = _authorized_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="member.read",
    )
    _require_org_in_scope(allowed_ids, "member.read", org_unit_id)
    predicates = current_assignment_predicates()
    base_filters = (
        MemberOrgAssignment.tenant_id == tenant_id,
        MemberOrgAssignment.org_unit_id == org_unit_id,
        *predicates,
    )
    total = int(
        db.exec(
            select(func.count())
            .select_from(MemberOrgAssignment)
            .where(*base_filters)
        ).one()
    )
    rows = db.exec(
        select(MemberOrgAssignment, EmployeeProfile, User)
        .join(
            EmployeeProfile,
            EmployeeProfile.id == MemberOrgAssignment.employee_profile_id,
        )
        .join(User, User.id == EmployeeProfile.user_id)
        .where(
            *base_filters,
            EmployeeProfile.tenant_id == tenant_id,
            User.tenant_id == tenant_id,
        )
        .order_by(
            EmployeeProfile.employee_name,
            EmployeeProfile.employee_id,
            MemberOrgAssignment.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return OrgAssignmentMemberPageRead(
        items=[
            OrgAssignmentMemberRead(
                **_org_assignment_read(assignment).model_dump(),
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                employee_id=profile.employee_id,
                employee_name=profile.employee_name,
            )
            for assignment, profile, user in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/member-org-assignments", response_model=OrgAssignmentRead)
def assign_member_to_organization_endpoint(
    request: OrgAssignmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgAssignmentRead:
    """由租户管理员执行组织入职、调岗或兼任命令。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="member.manage",
        target_org_unit_id=request.org_unit_id,
    )
    _reject_self_profile_change(
        db, request.tenant_id, request.employee_profile_id, current_user
    )
    try:
        row = assign_member_to_organization(db, **request.model_dump())
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="member.manage",
        action="member.organization.assign",
        action_kind="create",
        outcome="success",
        resource_type="member_org_assignment",
        resource_id=row.id,
        target_org_unit_id=row.org_unit_id,
        after=_member_org_assignment_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _org_assignment_read(row)


@router.post(
    "/member-org-assignments/{assignment_id}/end",
    response_model=OrgAssignmentRead,
)
def end_member_org_assignment_endpoint(
    assignment_id: str,
    request: AssignmentEnd,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrgAssignmentRead:
    """由租户管理员结束组织归属区间。"""

    try:
        assignment = db.get(MemberOrgAssignment, assignment_id)
        if assignment is None or assignment.tenant_id != request.tenant_id:
            raise OrganizationAssignmentError("MEMBER_ORG_ASSIGNMENT_NOT_FOUND")
        before = _member_org_assignment_audit_snapshot(assignment)
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="member.manage",
            target_org_unit_id=assignment.org_unit_id,
        )
        row = end_member_org_assignment(
            db,
            tenant_id=request.tenant_id,
            assignment_id=assignment_id,
            effective_until=request.effective_until,
        )
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="member.manage",
        action="member.organization.end",
        action_kind="delete",
        outcome="success",
        resource_type="member_org_assignment",
        resource_id=row.id,
        target_org_unit_id=row.org_unit_id,
        before=before,
        after=_member_org_assignment_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _org_assignment_read(row)


@router.get("/position-assignments", response_model=list[PositionAssignmentRead])
def list_position_assignments(
    tenant_id: str = Query(...),
    employee_profile_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    position_id: str | None = None,
    org_unit_id: str | None = None,
) -> list[PositionAssignmentRead]:
    """按成员、岗位或组织读取任职历史，普通成员只能读取本人。"""

    self_read = _is_self_profile(db, tenant_id, employee_profile_id, current_user)
    allowed_ids = None
    if not self_read:
        allowed_ids = _authorized_org_ids(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="position.read",
        )
    statement = select(PositionAssignment).where(
        PositionAssignment.tenant_id == tenant_id
    )
    if employee_profile_id:
        statement = statement.where(
            PositionAssignment.employee_profile_id == employee_profile_id
        )
    if position_id:
        position = get_tenant_position(db, tenant_id, position_id)
        if not self_read:
            _require_org_in_scope(allowed_ids, "position.read", position.org_unit_id)
        statement = statement.where(PositionAssignment.position_id == position_id)
    if org_unit_id:
        if not self_read:
            _require_org_in_scope(allowed_ids, "position.read", org_unit_id)
        statement = statement.join(
            Position,
            (Position.id == PositionAssignment.position_id)
            & (Position.tenant_id == PositionAssignment.tenant_id),
        ).where(Position.org_unit_id == org_unit_id)
    elif not self_read and position_id is None and allowed_ids is not None:
        statement = statement.join(
            Position,
            (Position.id == PositionAssignment.position_id)
            & (Position.tenant_id == PositionAssignment.tenant_id),
        ).where(Position.org_unit_id.in_(allowed_ids))
    rows = db.exec(
        statement.order_by(
            PositionAssignment.employee_profile_id,
            PositionAssignment.effective_from,
            PositionAssignment.id,
        )
    ).all()
    return [_position_assignment_read(row) for row in rows]


@router.post("/position-assignments", response_model=PositionAssignmentRead)
def assign_member_to_position_endpoint(
    request: PositionAssignmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionAssignmentRead:
    """由租户管理员执行岗位任职命令。"""

    position = get_tenant_position(db, request.tenant_id, request.position_id)
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="position.manage",
        target_org_unit_id=position.org_unit_id,
    )
    _reject_self_profile_change(
        db, request.tenant_id, request.employee_profile_id, current_user
    )
    try:
        row = assign_member_to_position(db, **request.model_dump())
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="position.manage",
        action="member.position.assign",
        action_kind="create",
        outcome="success",
        resource_type="position_assignment",
        resource_id=row.id,
        target_org_unit_id=position.org_unit_id,
        after=_position_assignment_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _position_assignment_read(row)


@router.post(
    "/position-assignments/{assignment_id}/end",
    response_model=PositionAssignmentRead,
)
def end_position_assignment_endpoint(
    assignment_id: str,
    request: AssignmentEnd,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionAssignmentRead:
    """由租户管理员结束岗位任职区间。"""

    try:
        assignment = db.get(PositionAssignment, assignment_id)
        if assignment is None or assignment.tenant_id != request.tenant_id:
            raise OrganizationAssignmentError("POSITION_ASSIGNMENT_NOT_FOUND")
        before = _position_assignment_audit_snapshot(assignment)
        position = get_tenant_position(db, request.tenant_id, assignment.position_id)
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="position.manage",
            target_org_unit_id=position.org_unit_id,
        )
        row = end_position_assignment(
            db,
            tenant_id=request.tenant_id,
            assignment_id=assignment_id,
            effective_until=request.effective_until,
        )
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="position.manage",
        action="member.position.end",
        action_kind="delete",
        outcome="success",
        resource_type="position_assignment",
        resource_id=row.id,
        target_org_unit_id=position.org_unit_id,
        before=before,
        after=_position_assignment_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _position_assignment_read(row)


@router.get("/migration-issues", response_model=list[MigrationIssueRead])
def list_organization_migration_issues(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[MigrationIssueRead]:
    """列出旧部门映射产生的待治理问题，仅租户管理员可读。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.manage",
    )
    rows = db.exec(
        select(OrganizationMigrationIssue).where(
            OrganizationMigrationIssue.tenant_id == tenant_id
        )
    ).all()
    return [
        MigrationIssueRead(
            id=row.id,
            employee_profile_id=row.employee_profile_id,
            source_field=row.source_field,
            source_value=row.source_value,
            issue_code=row.issue_code,
            resolution_status=row.resolution_status,
        )
        for row in rows
    ]


@router.get("/position-role-bindings", response_model=list[PositionRoleBindingRead])
def list_position_role_bindings(
    tenant_id: str = Query(...),
    position_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[PositionRoleBindingRead]:
    """列出岗位默认角色，普通成员可查看角色来源但不能治理。"""

    allowed_ids = _authorized_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="position.read",
    )
    statement = select(PositionRoleBinding).where(
        PositionRoleBinding.tenant_id == tenant_id
    )
    if position_id:
        position = get_tenant_position(db, tenant_id, position_id)
        _require_org_in_scope(allowed_ids, "position.read", position.org_unit_id)
        statement = statement.where(PositionRoleBinding.position_id == position_id)
    elif allowed_ids is not None:
        position_ids = db.exec(
            select(Position.id).where(
                Position.tenant_id == tenant_id,
                Position.org_unit_id.in_(allowed_ids),
            )
        ).all()
        statement = statement.where(PositionRoleBinding.position_id.in_(position_ids))
    rows = db.exec(statement).all()
    roles = db.exec(
        select(BusinessRole).where(BusinessRole.tenant_id == tenant_id)
    ).all()
    roles_by_id = {role.id: role for role in roles}
    return [
        _position_role_binding_read(row, roles_by_id[row.business_role_id])
        for row in rows
        if row.business_role_id in roles_by_id
    ]


@router.post("/position-role-bindings", response_model=PositionRoleBindingRead)
def create_position_role_binding_endpoint(
    request: PositionRoleBindingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionRoleBindingRead:
    """由租户管理员给岗位配置默认业务角色。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="position.manage",
    )
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="authorization.manage",
    )
    try:
        position = get_tenant_position(db, request.tenant_id, request.position_id)
    except OrganizationAssignmentError as error:
        raise _assignment_http_error(error) from error
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="position.manage",
        target_org_unit_id=position.org_unit_id,
    )
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="authorization.manage",
        target_org_unit_id=position.org_unit_id,
    )
    existing = db.exec(
        select(PositionRoleBinding).where(
            PositionRoleBinding.tenant_id == request.tenant_id,
            PositionRoleBinding.position_id == request.position_id,
            PositionRoleBinding.business_role_id == request.business_role_id,
        )
    ).first()
    existing_was_active = existing is not None and existing.status == "active"
    before = _position_role_binding_audit_snapshot(existing) if existing else {}
    try:
        row = bind_position_business_role(
            db,
            **request.model_dump(),
            granted_by_user_id=current_user.id,
        )
    except ValueError as error:
        raise _value_http_error(error) from error
    role = db.get(BusinessRole, row.business_role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="BUSINESS_ROLE_NOT_FOUND")
    if not existing_was_active:
        append_user_management_audit(
            db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="authorization.manage",
            action=(
                "authorization.position_role.reactivate"
                if existing
                else "authorization.position_role.create"
            ),
            action_kind="update" if existing else "create",
            outcome="success",
            resource_type="position_role_binding",
            resource_id=row.id,
            target_org_unit_id=position.org_unit_id,
            before=before,
            after=_position_role_binding_audit_snapshot(row),
        )
    db.commit()
    db.refresh(row)
    return _position_role_binding_read(row, role)


@router.delete(
    "/position-role-bindings/{binding_id}",
    response_model=PositionRoleBindingRead,
)
def deactivate_position_role_binding_endpoint(
    binding_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PositionRoleBindingRead:
    """由租户管理员停用岗位默认角色绑定。"""

    try:
        existing = db.get(PositionRoleBinding, binding_id)
        if existing is None or existing.tenant_id != tenant_id:
            raise ValueError("POSITION_ROLE_BINDING_NOT_FOUND")
        position = get_tenant_position(db, tenant_id, existing.position_id)
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="position.manage",
            target_org_unit_id=position.org_unit_id,
        )
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="authorization.manage",
            target_org_unit_id=position.org_unit_id,
        )
        before = _position_role_binding_audit_snapshot(existing)
        row = deactivate_position_role_binding(
            db, tenant_id=tenant_id, binding_id=binding_id
        )
    except ValueError as error:
        raise _value_http_error(error) from error
    role = db.get(BusinessRole, row.business_role_id)
    if role is None or role.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="BUSINESS_ROLE_NOT_FOUND")
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="authorization.manage",
        action="authorization.position_role.deactivate",
        action_kind="delete",
        outcome="success",
        resource_type="position_role_binding",
        resource_id=row.id,
        target_org_unit_id=position.org_unit_id,
        before=before,
        after=_position_role_binding_audit_snapshot(row),
    )
    db.commit()
    db.refresh(row)
    return _position_role_binding_read(row, role)


def _is_self_profile(
    db: Session,
    tenant_id: str,
    employee_profile_id: str | None,
    current_user: User,
) -> bool:
    """判断请求是否为成员读取自己的明确档案；未给档案不视为本人查询。"""

    _require_current_tenant(tenant_id, current_user)
    if not employee_profile_id:
        return False
    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.id == employee_profile_id,
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == current_user.id,
        )
    ).first()
    return profile is not None


def _reject_self_profile_change(
    db: Session,
    tenant_id: str,
    employee_profile_id: str,
    current_user: User,
) -> None:
    """拒绝管理员通过岗位任职旁路给自己增加业务候选资格。"""

    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.id == employee_profile_id,
            EmployeeProfile.tenant_id == tenant_id,
        )
    ).first()
    if profile is not None and profile.user_id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="Cannot change your own organization or position assignment",
        )


def _require_current_tenant(tenant_id: str, current_user: User) -> None:
    """拒绝认证成员借请求参数读取其他租户资源。"""

    if current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access another tenant")


def _authorized_org_ids(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    permission_code: str,
) -> frozenset[str] | None:
    """返回指定治理权限可访问的组织集合，None 表示租户全范围。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code=permission_code,
    )
    return authorized_organization_ids(
        resolve_permission_grants(
            db,
            tenant_id=tenant_id,
            user_id=current_user.id,
        ),
        permission_code=permission_code,
    )


def _require_org_in_scope(
    allowed_ids: frozenset[str] | None,
    permission_code: str,
    org_unit_id: str,
) -> None:
    """拒绝范围管理员读取或修改授权子树外的岗位和任职。"""

    if allowed_ids is not None and org_unit_id not in allowed_ids:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "GOVERNANCE_PERMISSION_DENIED",
                "permission": permission_code,
                "target_org_unit_id": org_unit_id,
            },
        )


def _assignment_http_error(error: OrganizationAssignmentError) -> HTTPException:
    """把领域错误映射为稳定 HTTP 状态，不泄露其他租户资源是否存在。"""

    detail = str(error)
    if detail == "POSITION_CODE_EXISTS":
        return HTTPException(status_code=409, detail=detail)
    if detail.endswith("_NOT_FOUND"):
        return HTTPException(status_code=404, detail=detail)
    return HTTPException(status_code=400, detail=detail)


def _value_http_error(error: ValueError) -> HTTPException:
    """把岗位角色治理错误转换为稳定的 API 状态。"""

    detail = str(error)
    if detail.endswith("_NOT_FOUND"):
        return HTTPException(status_code=404, detail=detail)
    return HTTPException(status_code=400, detail=detail)


def _position_audit_snapshot(row: Position) -> dict[str, object]:
    """返回岗位目录和汇报关系快照，不保存职责正文。"""

    return {
        "org_unit_id": row.org_unit_id,
        "code": row.code,
        "name": row.name,
        "position_type_code": row.position_type_code,
        "reports_to_position_id": row.reports_to_position_id,
        "headcount_limit": row.headcount_limit,
        "status": row.status,
    }


def _member_org_assignment_audit_snapshot(
    row: MemberOrgAssignment,
) -> dict[str, object]:
    """返回成员组织归属的类型、范围和有效期，不保存姓名等个人资料。"""

    return {
        "employee_profile_id": row.employee_profile_id,
        "org_unit_id": row.org_unit_id,
        "assignment_type": row.assignment_type,
        "is_primary": row.is_primary,
        "status": row.status,
        "effective_from": row.effective_from.isoformat(),
        "effective_until": row.effective_until.isoformat() if row.effective_until else None,
    }


def _position_assignment_audit_snapshot(
    row: PositionAssignment,
) -> dict[str, object]:
    """返回岗位任职关系和有效期，不保存成员姓名或岗位职责。"""

    return {
        "employee_profile_id": row.employee_profile_id,
        "position_id": row.position_id,
        "is_primary": row.is_primary,
        "status": row.status,
        "effective_from": row.effective_from.isoformat(),
        "effective_until": row.effective_until.isoformat() if row.effective_until else None,
    }


def _position_read(row: Position) -> PositionRead:
    """把岗位模型转换为公开响应。"""

    return PositionRead.model_validate(row, from_attributes=True)


def _org_assignment_read(row: MemberOrgAssignment) -> OrgAssignmentRead:
    """把组织归属模型转换为公开响应。"""

    return OrgAssignmentRead.model_validate(row, from_attributes=True)


def _position_assignment_read(row: PositionAssignment) -> PositionAssignmentRead:
    """把岗位任职模型转换为公开响应。"""

    return PositionAssignmentRead.model_validate(row, from_attributes=True)


def _position_role_binding_read(
    row: PositionRoleBinding,
    role: BusinessRole,
) -> PositionRoleBindingRead:
    """把岗位角色绑定和角色摘要组合为公开响应。"""

    return PositionRoleBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        position_id=row.position_id,
        business_role_id=row.business_role_id,
        business_role_code=role.role_code,
        business_role_name=role.name,
        scope_mode="position_org",
        granted_by_user_id=row.granted_by_user_id,
        status=row.status,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
    )


def _position_role_binding_audit_snapshot(
    row: PositionRoleBinding,
) -> dict[str, object]:
    """冻结岗位角色绑定的治理字段，供创建、重启和停用审计复用。"""

    return {
        "id": row.id,
        "position_id": row.position_id,
        "business_role_id": row.business_role_id,
        "scope_mode": row.scope_mode,
        "granted_by_user_id": row.granted_by_user_id,
        "status": row.status,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "effective_until": row.effective_until.isoformat() if row.effective_until else None,
    }
