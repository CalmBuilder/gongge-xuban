"""
@Time       : 2026/07/22 16:50
@Author     : zhanglp8181
@File       : auth.py
@CallChain  : 登录/账号管理 API → User/EmployeeProfile → SOP 身份上下文
@Description: 管理租户账号认证、基础角色以及账号与业务员工档案的绑定。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.db import get_session
from app.db.models import (
    AgentRoleBinding,
    BusinessRole,
    CodeItem,
    EmployeeProfile,
    EmployeeRoleAssignment,
    MemberOrgAssignment,
    OrganizationUnit,
    Position,
    PositionAssignment,
    Tenant,
    User,
    utc_now,
)
from app.experts.builtin import ensure_builtin_experts_for_tenant
from app.organization.assignments import (
    OrganizationAssignmentError,
    assign_member_to_organization,
    end_active_member_assignments,
    member_has_assignment_history,
)
from app.organization.governance import (
    authorized_organization_ids,
    ensure_governance_permission,
    governance_permission_codes,
    resolve_permission_grants,
)
from app.organization.reference_data import (
    ReferenceDataError,
    create_business_code_item,
    ensure_member_category_catalog,
    require_active_member_category,
    update_business_code_item,
)
from app.organization.query import (
    current_assignment_predicates,
    resolve_organization_subtree_ids,
)
from app.organization.roles import (
    active_business_role_codes,
    active_business_role_sources,
    replace_employee_business_roles,
)
from app.organization.units import OrganizationUnitError, ensure_organization_foundation
from app.security.auth import (
    create_access_token,
    ensure_active_member,
    get_current_user,
    hash_password,
    verify_password,
)
from app.security.permissions import MEMBER_ROLE, is_admin_user
from app.security.tenant import ensure_tenant


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    tenant_id: str
    username: str
    password: str


class UserCreateRequest(BaseModel):
    """创建租户成员，并可在同一事务中建立首个主组织归属。"""

    tenant_id: str
    username: str
    password: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"] = MEMBER_ROLE
    membership_status: Literal["active", "suspended", "left"] = "active"
    member_category_code: str = "employee"
    employee_id: Optional[str] = None
    employee_name: Optional[str] = None
    department_id: Optional[str] = None
    initial_org_unit_id: Optional[str] = None
    business_role_codes: list[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    tenant_id: str
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["admin", "member"]] = None
    membership_status: Optional[Literal["active", "suspended", "left"]] = None
    member_category_code: Optional[str] = None
    employee_id: Optional[str] = None
    employee_name: Optional[str] = None
    department_id: Optional[str] = None
    business_role_codes: Optional[list[str]] = None


class UserRead(BaseModel):
    id: str
    tenant_id: str
    username: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"]
    membership_status: Literal["active", "suspended", "left"]
    member_category_code: str
    joined_at: Optional[str] = None
    left_at: Optional[str] = None
    employee_profile_id: Optional[str] = None
    employee_id: Optional[str] = None
    employee_name: Optional[str] = None
    department_id: Optional[str] = None
    employee_status: Optional[str] = None
    business_role_codes: list[str] = Field(default_factory=list)
    business_role_sources: dict[str, list[str]] = Field(default_factory=dict)
    governance_permission_codes: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MemberPageItem(UserRead):
    """返回成员分页行及当前主组织、主岗位和任期历史摘要。"""

    primary_org_unit_id: str | None = None
    primary_org_name: str | None = None
    primary_position_id: str | None = None
    primary_position_name: str | None = None
    assignment_history_count: int = 0


class MemberPageRead(BaseModel):
    """返回服务端稳定分页成员和总数。"""

    items: list[MemberPageItem]
    total: int
    page: int
    page_size: int


class LoginResponse(BaseModel):
    token: str
    user: UserRead


class TenantContextRead(BaseModel):
    """返回认证会话唯一可信的租户事实。"""

    id: str
    name: str


class EnterpriseContextRead(BaseModel):
    """聚合当前租户和成员，供前端恢复运行时企业上下文。"""

    tenant: TenantContextRead
    member: UserRead
    is_administrator: bool


class TenantDisplayUpdateRequest(BaseModel):
    """只允许修改企业显示名称，不暴露稳定租户编码变更能力。"""

    name: str


class BusinessRoleRead(BaseModel):
    """向账号管理页面返回可分配的公司业务角色摘要。"""

    role_code: str
    name: str
    category: str
    permissions: list[str]


class MemberCategoryRead(BaseModel):
    """返回成员类别的稳定编码、显示名称与可选状态。"""

    code: str
    name: str
    description: Optional[str] = None
    status: Literal["active", "inactive"]
    is_builtin: bool
    sort_order: int
    revision: int


class MemberCategoryCreateRequest(BaseModel):
    """创建租户自定义成员类别，不允许客户端创建新的码表。"""

    tenant_id: str
    code: str
    name: str
    description: Optional[str] = None
    sort_order: int = 100


class MemberCategoryUpdateRequest(BaseModel):
    """更新成员类别显示信息或状态，编码本身不在更新契约中。"""

    tenant_id: str
    name: str
    description: Optional[str] = None
    status: Literal["active", "inactive"]
    sort_order: int
    revision: int


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_session)) -> LoginResponse:
    """校验租户账号密码，并返回包含员工档案摘要的登录会话。"""

    ensure_tenant(db, request.tenant_id)
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    user = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    ensure_active_member(user)
    ensure_builtin_experts_for_tenant(db, tenant_id=request.tenant_id)
    db.commit()

    return LoginResponse(
        token=create_access_token(user),
        user=_user_read_with_roles(db, user, _employee_profile(db, user.tenant_id, user.id)),
    )


@router.get("/me", response_model=UserRead)
def me(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    """返回当前可信账号及其员工身份绑定。"""

    return _user_read_with_roles(db, user, _employee_profile(db, user.tenant_id, user.id))


@router.get("/context", response_model=EnterpriseContextRead)
def enterprise_context(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EnterpriseContextRead:
    """仅依据认证成员恢复企业上下文，不接受客户端传入 tenant。"""

    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid tenant context")
    return EnterpriseContextRead(
        tenant=TenantContextRead(id=tenant.id, name=tenant.name),
        member=_user_read_with_roles(
            db,
            user,
            _employee_profile(db, user.tenant_id, user.id),
        ),
        is_administrator=is_admin_user(user),
    )


@router.put("/context/tenant", response_model=TenantContextRead)
def update_tenant_display(
    request: TenantDisplayUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> TenantContextRead:
    """由当前租户管理员修改企业显示名称，稳定 tenant id 不可变。"""

    ensure_governance_permission(
        db,
        tenant_id=user.tenant_id,
        current_user=user,
        permission_code="tenant.settings.manage",
    )
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tenant name is required")
    tenant.name = name[:191]
    tenant.updated_at = utc_now()
    db.add(tenant)
    root = ensure_organization_foundation(db, tenant.id)
    root.name = tenant.name
    root.updated_at = tenant.updated_at
    db.add(root)
    db.commit()
    db.refresh(tenant)
    return TenantContextRead(id=tenant.id, name=tenant.name)


@router.post("/users", response_model=UserRead)
def create_user(
    request: UserCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    """在调用人有权管理的组织内创建账号、员工档案和首个主归属。"""

    root = ensure_organization_foundation(db, request.tenant_id)
    target_org_unit_id = request.initial_org_unit_id or root.id
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="member.manage",
        target_org_unit_id=target_org_unit_id,
    )
    if request.role == "admin":
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="authorization.manage",
            target_org_unit_id=root.id,
        )
    elif request.business_role_codes:
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="authorization.manage",
            target_org_unit_id=target_org_unit_id,
        )
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    if request.initial_org_unit_id and not (request.employee_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Initial organization assignment requires an employee ID",
        )
    existing = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists")
    member_category_code = _validated_member_category(
        db,
        request.tenant_id,
        request.member_category_code,
    )
    now = utc_now()
    user = User(
        tenant_id=request.tenant_id,
        username=username,
        display_name=(request.display_name or username).strip()[:80],
        role=request.role,
        membership_status=request.membership_status,
        member_category_code=member_category_code,
        joined_at=now,
        left_at=now if request.membership_status == "left" else None,
        password_hash=hash_password(request.password),
    )
    db.add(user)
    db.flush()
    profile = _upsert_employee_profile(
        db,
        user,
        employee_id=request.employee_id,
        employee_name=request.employee_name,
        department_id=request.department_id,
    )
    if profile is not None:
        _sync_employee_lifecycle(
            db, profile, request.membership_status, now=now
        )
        if request.initial_org_unit_id:
            try:
                assign_member_to_organization(
                    db,
                    tenant_id=request.tenant_id,
                    employee_profile_id=profile.id,
                    org_unit_id=request.initial_org_unit_id,
                    assignment_type="primary",
                    effective_from=now,
                )
            except OrganizationAssignmentError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
    if request.business_role_codes:
        if profile is None:
            raise HTTPException(status_code=400, detail="Business roles require an employee profile")
        _replace_business_roles(
            db,
            profile,
            request.business_role_codes,
            actor_user_id=current_user.id,
        )
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="member.manage",
        action="member.create",
        action_kind="create",
        outcome="success",
        resource_type="user",
        resource_id=user.id,
        target_org_unit_id=target_org_unit_id,
        after=_member_audit_snapshot(user, profile),
    )
    db.commit()
    db.refresh(user)
    if profile is not None:
        db.refresh(profile)
    return _user_read_with_roles(db, user, profile)


@router.get("/users", response_model=list[UserRead])
def list_users(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[UserRead]:
    """列出当前租户账号及员工身份摘要，避免前端额外逐行查询。"""

    allowed_ids = _governance_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="member.read",
    )
    conditions = [User.tenant_id == tenant_id]
    if allowed_ids is not None:
        scoped_profile_ids = select(MemberOrgAssignment.employee_profile_id).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.org_unit_id.in_(allowed_ids),
            *current_assignment_predicates(),
        )
        conditions.append(
            User.id.in_(
                select(EmployeeProfile.user_id).where(
                    EmployeeProfile.tenant_id == tenant_id,
                    EmployeeProfile.id.in_(scoped_profile_ids),
                )
            )
        )
    rows = db.exec(
        select(User).where(*conditions).order_by(User.created_at.desc())
    ).all()
    profiles = db.exec(
        select(EmployeeProfile).where(EmployeeProfile.tenant_id == tenant_id)
    ).all()
    profiles_by_user_id = {profile.user_id: profile for profile in profiles}
    return [
        _user_read_with_roles(db, row, profiles_by_user_id.get(row.id))
        for row in rows
    ]


@router.get("/users/page", response_model=MemberPageRead)
def page_users(
    tenant_id: str = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=80),
    membership_status: Literal["active", "suspended", "left"] | None = Query(
        default=None
    ),
    member_category_code: str | None = Query(default=None, max_length=128),
    org_unit_id: str | None = Query(default=None),
    include_descendants: bool = Query(default=False),
    assignment_type: Literal["primary", "concurrent", "temporary", "project"] | None = Query(
        default=None
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> MemberPageRead:
    """按成员与当前组织归属条件服务端分页，禁止普通成员枚举租户目录。"""

    allowed_ids = _governance_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="member.read",
    )
    if org_unit_id is not None:
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="member.read",
            target_org_unit_id=org_unit_id,
        )
    conditions = [User.tenant_id == tenant_id]
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        pattern = f"%{normalized_keyword}%"
        conditions.append(
            or_(
                User.username.ilike(pattern),
                User.display_name.ilike(pattern),
                EmployeeProfile.employee_id.ilike(pattern),
                EmployeeProfile.employee_name.ilike(pattern),
            )
        )
    if membership_status is not None:
        conditions.append(User.membership_status == membership_status)
    if member_category_code:
        conditions.append(User.member_category_code == member_category_code)

    assignment_filter_requested = (
        org_unit_id is not None
        or assignment_type is not None
        or allowed_ids is not None
    )
    if assignment_filter_requested:
        try:
            requested_organization_ids = (
                resolve_organization_subtree_ids(
                    db,
                    tenant_id=tenant_id,
                    root_org_unit_id=org_unit_id,
                    include_descendants=include_descendants,
                )
                if org_unit_id is not None
                else None
            )
            organization_ids = requested_organization_ids
            if allowed_ids is not None:
                if requested_organization_ids is None:
                    organization_ids = sorted(allowed_ids)
                else:
                    organization_ids = sorted(
                        set(requested_organization_ids).intersection(allowed_ids)
                    )
        except OrganizationUnitError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        assignment_query = select(MemberOrgAssignment.employee_profile_id).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            *current_assignment_predicates(),
        )
        if organization_ids is not None:
            assignment_query = assignment_query.where(
                MemberOrgAssignment.org_unit_id.in_(organization_ids)
            )
        if assignment_type is not None:
            assignment_query = assignment_query.where(
                MemberOrgAssignment.assignment_type == assignment_type
            )
        conditions.append(EmployeeProfile.id.in_(assignment_query.distinct()))

    base = (
        select(User, EmployeeProfile)
        .join(
            EmployeeProfile,
            (EmployeeProfile.tenant_id == User.tenant_id)
            & (EmployeeProfile.user_id == User.id),
            isouter=True,
        )
        .where(*conditions)
    )
    total = db.exec(
        select(func.count(User.id))
        .select_from(User)
        .join(
            EmployeeProfile,
            (EmployeeProfile.tenant_id == User.tenant_id)
            & (EmployeeProfile.user_id == User.id),
            isouter=True,
        )
        .where(*conditions)
    ).one()
    rows = db.exec(
        base.order_by(User.created_at.desc(), User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    profiles = [profile for _, profile in rows if profile is not None]
    summaries = _member_page_summaries(
        db,
        tenant_id,
        profiles,
        allowed_org_unit_ids=allowed_ids,
    )
    items = []
    for user, profile in rows:
        base_item = _user_read_with_roles(db, user, profile)
        summary = summaries.get(profile.id, {}) if profile is not None else {}
        items.append(MemberPageItem(**base_item.model_dump(), **summary))
    return MemberPageRead(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
    )


def _member_page_summaries(
    db: Session,
    tenant_id: str,
    profiles: list[EmployeeProfile],
    *,
    allowed_org_unit_ids: frozenset[str] | None = None,
) -> dict[str, dict[str, object]]:
    """批量汇总当前页成员的主组织、主岗位和历史条数，避免逐成员领域请求。"""

    profile_ids = [profile.id for profile in profiles]
    if not profile_ids:
        return {}
    org_rows = db.exec(
        select(MemberOrgAssignment).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.employee_profile_id.in_(profile_ids),
            *(
                (MemberOrgAssignment.org_unit_id.in_(allowed_org_unit_ids),)
                if allowed_org_unit_ids is not None
                else ()
            ),
        )
    ).all()
    position_rows = db.exec(
        select(PositionAssignment).where(
            PositionAssignment.tenant_id == tenant_id,
            PositionAssignment.employee_profile_id.in_(profile_ids),
        )
    ).all()
    if allowed_org_unit_ids is not None and position_rows:
        allowed_position_ids = set(
            db.exec(
                select(Position.id).where(
                    Position.tenant_id == tenant_id,
                    Position.org_unit_id.in_(allowed_org_unit_ids),
                )
            ).all()
        )
        position_rows = [
            row for row in position_rows if row.position_id in allowed_position_ids
        ]
    organization_ids = {row.org_unit_id for row in org_rows}
    position_ids = {row.position_id for row in position_rows}
    organizations = (
        db.exec(
            select(OrganizationUnit).where(
                OrganizationUnit.tenant_id == tenant_id,
                OrganizationUnit.id.in_(organization_ids),
            )
        ).all()
        if organization_ids
        else []
    )
    positions = (
        db.exec(
            select(Position).where(
                Position.tenant_id == tenant_id,
                Position.id.in_(position_ids),
            )
        ).all()
        if position_ids
        else []
    )
    organizations_by_id = {row.id: row for row in organizations}
    positions_by_id = {row.id: row for row in positions}
    now = utc_now()
    result: dict[str, dict[str, object]] = {}
    for profile_id in profile_ids:
        member_org_rows = [
            row for row in org_rows if row.employee_profile_id == profile_id
        ]
        member_position_rows = [
            row for row in position_rows if row.employee_profile_id == profile_id
        ]
        primary_org = next(
            (
                row
                for row in member_org_rows
                if row.is_primary
                and row.status == "active"
                and row.effective_from <= now
                and (row.effective_until is None or row.effective_until > now)
            ),
            None,
        )
        primary_position = next(
            (
                row
                for row in member_position_rows
                if row.is_primary
                and row.status == "active"
                and row.effective_from <= now
                and (row.effective_until is None or row.effective_until > now)
            ),
            None,
        )
        organization = (
            organizations_by_id.get(primary_org.org_unit_id)
            if primary_org is not None
            else None
        )
        position = (
            positions_by_id.get(primary_position.position_id)
            if primary_position is not None
            else None
        )
        result[profile_id] = {
            "primary_org_unit_id": organization.id if organization else None,
            "primary_org_name": organization.name if organization else None,
            "primary_position_id": position.id if position else None,
            "primary_position_name": position.name if position else None,
            "assignment_history_count": len(member_org_rows)
            + len(member_position_rows),
        }
    return result


@router.get("/business-roles", response_model=list[BusinessRoleRead])
def list_business_roles(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[BusinessRoleRead]:
    """列出当前租户可分配的有效业务角色，不混入 admin/member。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="authorization.read",
    )
    roles = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == tenant_id,
            BusinessRole.role_kind == "business",
            BusinessRole.status == "active",
        ).order_by(BusinessRole.role_code)
    ).all()
    return [
        BusinessRoleRead(
            role_code=role.role_code,
            name=role.name,
            category=role.category,
            permissions=list(role.permissions_json),
        )
        for role in roles
    ]


@router.get("/member-categories", response_model=list[MemberCategoryRead])
def list_member_categories(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[MemberCategoryRead]:
    """列出成员管理可用及历史引用所需的成员类别。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="reference_data.read",
    )
    code_set = ensure_member_category_catalog(db, tenant_id)
    items = db.exec(
        select(CodeItem)
        .where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
        )
        .order_by(CodeItem.sort_order, CodeItem.item_code)
    ).all()
    return [
        _member_category_read(item)
        for item in items
    ]


@router.post("/member-categories", response_model=MemberCategoryRead)
def create_member_category(
    request: MemberCategoryCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> MemberCategoryRead:
    """由租户管理员新增编码稳定的自定义成员类别。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="reference_data.manage",
    )
    try:
        item = create_business_code_item(
            db,
            tenant_id=request.tenant_id,
            set_code="member_category",
            item_code=request.code,
            name=request.name,
            description=request.description,
            sort_order=request.sort_order,
            actor_user_id=current_user.id,
        )
    except ReferenceDataError as error:
        raise _member_category_http_error(error) from error
    db.commit()
    db.refresh(item)
    return _member_category_read(item)


@router.put("/member-categories/{item_code}", response_model=MemberCategoryRead)
def update_member_category(
    item_code: str,
    request: MemberCategoryUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> MemberCategoryRead:
    """以 revision 更新显示信息和状态，路径编码创建后永不修改。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="reference_data.manage",
    )
    try:
        item = update_business_code_item(
            db,
            tenant_id=request.tenant_id,
            set_code="member_category",
            item_code=item_code,
            name=request.name,
            description=request.description,
            status=request.status,
            sort_order=request.sort_order,
            revision=request.revision,
            actor_user_id=current_user.id,
        )
    except ReferenceDataError as error:
        raise _member_category_http_error(error) from error
    db.commit()
    db.refresh(item)
    return _member_category_read(item)


def _member_category_http_error(error: ReferenceDataError) -> HTTPException:
    """把统一码表错误映射为旧成员类别 API 的兼容状态与文案。"""

    mappings = {
        "CODE_SET_CUSTOM_ITEMS_DISABLED": (409, "Member category does not allow custom items"),
        "INVALID_CODE_ITEM_CODE": (400, "Invalid member category code"),
        "CODE_ITEM_NAME_REQUIRED": (400, "Member category name is required"),
        "CODE_ITEM_EXISTS": (409, "Member category code already exists"),
        "CODE_ITEM_NOT_FOUND": (404, "Member category not found"),
        "CODE_ITEM_REVISION_CONFLICT": (409, "Member category revision conflict"),
    }
    status_code, detail = mappings.get(str(error), (400, str(error)))
    return HTTPException(status_code=status_code, detail=detail)


@router.put("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: str,
    request: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    """更新账号基础信息和员工档案绑定，空工号表示解除绑定。"""

    user = db.get(User, user_id)
    if not user or user.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    target_org_unit_id = _member_target_org_unit_id(db, request.tenant_id, user.id)
    profile = _employee_profile(db, request.tenant_id, user.id)
    before = _member_audit_snapshot(user, profile)
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="member.manage",
        target_org_unit_id=target_org_unit_id,
    )
    if request.role is not None or request.business_role_codes is not None:
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="authorization.manage",
            target_org_unit_id=target_org_unit_id,
        )
    if request.display_name is not None:
        display_name = request.display_name.strip()[:80]
        user.display_name = display_name or user.username
    if request.password is not None:
        password = request.password.strip()
        if password:
            user.password_hash = hash_password(password)
    if request.role is not None and request.role != user.role:
        if user.id == current_user.id:
            raise HTTPException(status_code=400, detail="Cannot change your own account role")
        user.role = request.role
    employee_fields = {"employee_id", "employee_name", "department_id"}
    member_governance_fields = {"membership_status", "member_category_code"}
    identity_authorization_fields = (
        employee_fields | member_governance_fields | {"business_role_codes"}
    )
    if user.id == current_user.id and identity_authorization_fields.intersection(
        request.model_fields_set
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot change your own employee identity or business roles",
        )
    if request.member_category_code is not None:
        user.member_category_code = _validated_member_category(
            db,
            request.tenant_id,
            request.member_category_code,
        )
    if employee_fields.intersection(request.model_fields_set):
        normalized_employee_id = (request.employee_id or "").strip()
        if "employee_id" in request.model_fields_set and not normalized_employee_id:
            if profile is not None:
                if member_has_assignment_history(
                    db, profile.tenant_id, profile.id
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot remove employee profile with assignment history",
                    )
                _delete_profile_role_assignments(db, profile)
                db.delete(profile)
                profile = None
        else:
            profile = _upsert_employee_profile(
                db,
                user,
                employee_id=(normalized_employee_id or (profile.employee_id if profile else None)),
                employee_name=(
                    request.employee_name
                    if "employee_name" in request.model_fields_set
                    else profile.employee_name if profile else None
                ),
                department_id=(
                    request.department_id
                    if "department_id" in request.model_fields_set
                    else profile.department_id if profile else None
                ),
            )
    if "business_role_codes" in request.model_fields_set:
        if profile is None and request.business_role_codes:
            raise HTTPException(status_code=400, detail="Business roles require an employee profile")
        if profile is not None:
            _replace_business_roles(
                db,
                profile,
                request.business_role_codes or [],
                actor_user_id=current_user.id,
            )
    if request.membership_status is not None:
        now = utc_now()
        user.membership_status = request.membership_status
        user.left_at = now if request.membership_status == "left" else None
        if profile is not None:
            _sync_employee_lifecycle(
                db, profile, request.membership_status, now=now
            )
    user.updated_at = utc_now()
    db.add(user)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="member.manage",
        action="member.update",
        action_kind="update",
        outcome="success",
        resource_type="user",
        resource_id=user.id,
        target_org_unit_id=target_org_unit_id,
        before=before,
        after=_member_audit_snapshot(user, profile),
    )
    db.commit()
    db.refresh(user)
    if profile is not None:
        db.refresh(profile)
    return _user_read_with_roles(db, user, profile)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, bool]:
    """兼容旧删除入口，将普通成员标记离职并保留全部历史 actor 关系。"""

    user = db.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="member.manage",
        target_org_unit_id=_member_target_org_unit_id(db, tenant_id, user.id),
    )
    if user.id == current_user.id or is_admin_user(user):
        raise HTTPException(status_code=400, detail="Administrator account cannot be deleted")
    profile = _employee_profile(db, tenant_id, user.id)
    target_org_unit_id = _member_target_org_unit_id(db, tenant_id, user.id)
    before = _member_audit_snapshot(user, profile)
    now = utc_now()
    user.membership_status = "left"
    user.left_at = now
    user.updated_at = now
    db.add(user)
    if profile is not None:
        _sync_employee_lifecycle(db, profile, "left", now=now)
        db.add(profile)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="member.manage",
        action="member.leave",
        action_kind="delete",
        outcome="success",
        resource_type="user",
        resource_id=user.id,
        target_org_unit_id=target_org_unit_id,
        before=before,
        after=_member_audit_snapshot(user, profile),
    )
    db.commit()
    return {"ok": True}


def _member_audit_snapshot(
    user: User,
    profile: EmployeeProfile | None,
) -> dict[str, object]:
    """返回成员生命周期与身份类型快照，不保存密码、姓名或完整档案。"""

    return {
        "user_id": user.id,
        "role": user.role,
        "membership_status": user.membership_status,
        "member_category_code": user.member_category_code,
        "employee_profile_id": profile.id if profile else None,
        "employee_status": profile.status if profile else None,
    }


def _user_read(
    user: User,
    profile: EmployeeProfile | None = None,
    *,
    role_codes: list[str] | None = None,
    role_sources: dict[str, list[str]] | None = None,
    governance_codes: list[str] | None = None,
) -> UserRead:
    """把账号及可选员工档案转换为稳定 API 返回模型。"""

    return UserRead(
        id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        membership_status=user.membership_status,
        member_category_code=user.member_category_code,
        joined_at=user.joined_at.isoformat() if user.joined_at else None,
        left_at=user.left_at.isoformat() if user.left_at else None,
        employee_profile_id=profile.id if profile else None,
        employee_id=profile.employee_id if profile else None,
        employee_name=profile.employee_name if profile else None,
        department_id=profile.department_id if profile else None,
        employee_status=profile.status if profile else None,
        business_role_codes=role_codes or [],
        business_role_sources=role_sources or {},
        governance_permission_codes=governance_codes or [],
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
    )


def _member_category_read(item: CodeItem) -> MemberCategoryRead:
    """把成员类别转换为不暴露内部码表主键的 API 契约。"""

    return MemberCategoryRead(
        code=item.item_code,
        name=item.name,
        description=item.description,
        status=item.status,
        is_builtin=item.is_builtin,
        sort_order=item.sort_order,
        revision=item.revision,
    )


def _user_read_with_roles(
    db: Session, user: User, profile: EmployeeProfile | None
) -> UserRead:
    """为单个账号解析当前有效业务角色并构造 API 返回。"""

    role_codes = (
        active_business_role_codes(
            db,
            tenant_id=user.tenant_id,
            employee_profile_id=profile.id,
        )
        if profile is not None
        else []
    )
    role_sources = (
        active_business_role_sources(
            db,
            tenant_id=user.tenant_id,
            employee_profile_id=profile.id,
        )
        if profile is not None
        else {}
    )
    return _user_read(
        user,
        profile,
        role_codes=role_codes,
        role_sources=role_sources,
        governance_codes=governance_permission_codes(
            db,
            tenant_id=user.tenant_id,
            user_id=user.id,
        ),
    )


def _governance_org_ids(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    permission_code: str,
) -> frozenset[str] | None:
    """校验治理权限并返回服务端有效组织集合，None 表示租户全范围。"""

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


def _member_target_org_unit_id(
    db: Session,
    tenant_id: str,
    user_id: str,
) -> str:
    """返回成员当前主组织作为治理目标；未归属成员落到租户根范围。"""

    profile = _employee_profile(db, tenant_id, user_id)
    if profile is not None:
        rows = db.exec(
            select(MemberOrgAssignment)
            .where(
                MemberOrgAssignment.tenant_id == tenant_id,
                MemberOrgAssignment.employee_profile_id == profile.id,
                *current_assignment_predicates(),
            )
            .order_by(
                MemberOrgAssignment.is_primary.desc(),
                MemberOrgAssignment.effective_from.desc(),
                MemberOrgAssignment.id,
            )
        ).all()
        if rows:
            return rows[0].org_unit_id
    return ensure_organization_foundation(db, tenant_id).id


def _employee_profile(
    db: Session, tenant_id: str, user_id: str
) -> EmployeeProfile | None:
    """按租户和账号读取唯一员工档案。"""

    return db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == user_id,
        )
    ).first()


def _upsert_employee_profile(
    db: Session,
    user: User,
    *,
    employee_id: str | None,
    employee_name: str | None,
    department_id: str | None,
) -> EmployeeProfile | None:
    """创建或更新账号员工档案，并在写入前执行租户内工号唯一校验。"""

    normalized_employee_id = (employee_id or "").strip()
    if not normalized_employee_id:
        return _employee_profile(db, user.tenant_id, user.id)
    if len(normalized_employee_id) > 128:
        raise HTTPException(status_code=400, detail="Employee ID is too long")
    conflicting = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == user.tenant_id,
            EmployeeProfile.employee_id == normalized_employee_id,
            EmployeeProfile.user_id != user.id,
        )
    ).first()
    if conflicting is not None:
        raise HTTPException(status_code=409, detail="Employee ID is already bound")
    profile = _employee_profile(db, user.tenant_id, user.id)
    if profile is None:
        profile = EmployeeProfile(
            tenant_id=user.tenant_id,
            user_id=user.id,
            employee_id=normalized_employee_id,
        )
    profile.employee_id = normalized_employee_id
    profile.employee_name = (employee_name or user.display_name or user.username).strip()[:191]
    profile.department_id = (department_id or "").strip()[:128] or None
    profile.status = "active"
    profile.leave_date = None
    profile.updated_at = utc_now()
    db.add(profile)
    db.flush()
    return profile


def _validated_member_category(db: Session, tenant_id: str, item_code: str) -> str:
    """把码表校验错误转换为稳定的成员管理 API 响应。"""

    normalized_code = item_code.strip()
    try:
        return require_active_member_category(db, tenant_id, normalized_code).item_code
    except ValueError as error:
        message = str(error)
        if message.startswith("UNKNOWN_MEMBER_CATEGORY:"):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown member category: {normalized_code}",
            ) from error
        if message.startswith("INACTIVE_MEMBER_CATEGORY:"):
            raise HTTPException(
                status_code=400,
                detail=f"Inactive member category: {normalized_code}",
            ) from error
        raise HTTPException(status_code=409, detail="Member category catalog is inactive") from error


def _sync_employee_lifecycle(
    db: Session,
    profile: EmployeeProfile,
    membership_status: str,
    *,
    now: datetime,
) -> None:
    """把成员状态同步为员工业务档案状态，同时保留离职时间和历史身份。"""

    profile.status = membership_status
    profile.leave_date = now if membership_status == "left" else None
    profile.updated_at = now
    if membership_status != "active":
        end_active_member_assignments(
            db,
            tenant_id=profile.tenant_id,
            employee_profile_id=profile.id,
            effective_until=now,
        )


def _replace_business_roles(
    db: Session,
    profile: EmployeeProfile,
    role_codes: list[str],
    *,
    actor_user_id: str,
) -> None:
    """把业务角色替换错误转换为稳定的账号管理 API 响应。"""

    try:
        replace_employee_business_roles(
            db,
            profile=profile,
            role_codes=role_codes,
            source=f"account_management:{actor_user_id}",
        )
    except ValueError as error:
        if str(error).startswith("UNKNOWN_BUSINESS_ROLES:"):
            unknown = str(error).partition(":")[2]
            raise HTTPException(
                status_code=400, detail=f"Unknown business roles: {unknown}"
            ) from error
        raise


def _delete_profile_role_assignments(db: Session, profile: EmployeeProfile) -> None:
    """删除员工档案前清理任职，并解除数字员工的人类监督者引用。"""

    assignments = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == profile.tenant_id,
            EmployeeRoleAssignment.employee_profile_id == profile.id,
        )
    ).all()
    for assignment in assignments:
        db.delete(assignment)
    supervised_bindings = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == profile.tenant_id,
            AgentRoleBinding.supervisor_employee_profile_id == profile.id,
        )
    ).all()
    for binding in supervised_bindings:
        binding.supervisor_employee_profile_id = None
        binding.updated_at = utc_now()
        db.add(binding)
