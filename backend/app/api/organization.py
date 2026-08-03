"""
@Time       : 2026/07/22 09:18
@Author     : zhanglp8181
@File       : organization.py
@CallChain  : 组织角色管理页面 → FastAPI → BusinessRole/EmployeeRoleAssignment/AgentRoleBinding
@Description: 管理公司业务角色、员工多角色任职和数字员工角色绑定，保持与平台角色严格分离。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.db import get_session
from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    BusinessRoleCategory,
    BusinessRolePermission,
    EmployeeProfile,
    EmployeeRoleAssignment,
    MemberOrgAssignment,
    PermissionDefinition,
    Skill,
    User,
    utc_now,
)
from app.organization.permissions import (
    PermissionCatalogError,
    active_role_category_codes,
    ensure_builtin_permission_catalog,
    ensure_builtin_role_categories,
    role_permission_codes,
    sync_role_permissions,
)
from app.organization.governance import (
    authorized_organization_ids,
    PermissionGrant,
    ensure_builtin_governance_catalog,
    ensure_governance_permission,
    resolve_permission_grants,
    validate_role_assignment_scope,
)
from app.organization.query import current_assignment_predicates
from app.organization.units import ensure_organization_foundation
from app.security.auth import ensure_current_user_tenant, get_current_user


router = APIRouter(prefix="/api/organization", tags=["organization"])

ROLE_CODE_PATTERN = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,7}$"
CATALOG_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
PERMISSION_CODE_PATTERN = r"^[a-z][a-z0-9_.]*\.[a-z][a-z0-9_]*(?::[a-z0-9_*.-]+)?$"


class BusinessRoleCreate(BaseModel):
    """创建租户内稳定且不可变编码的业务或治理角色。"""

    tenant_id: str
    role_code: str = Field(pattern=ROLE_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=191)
    role_kind: Literal["business", "governance"] = "business"
    category: str = Field(default="cross_functional", min_length=1, max_length=64)
    permissions: list[str] = Field(default_factory=list)


class BusinessRoleUpdate(BaseModel):
    """更新业务角色可变属性，不允许改变已经被流程引用的角色编码。"""

    tenant_id: str
    name: str | None = Field(default=None, min_length=1, max_length=191)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    permissions: list[str] | None = None
    status: Literal["active", "inactive"] | None = None


class BusinessRoleDetail(BaseModel):
    """返回业务或治理角色及其员工和数字员工引用计数。"""

    id: str
    role_code: str
    name: str
    role_kind: Literal["business", "governance"]
    category: str
    permissions: list[str]
    status: str
    employee_count: int
    agent_count: int
    created_at: str
    updated_at: str


class BusinessRolePageRead(BaseModel):
    """返回角色目录当前页以及跨页统计。"""

    items: list[BusinessRoleDetail]
    total: int
    active_count: int
    assignment_count: int
    page: int
    page_size: int


class BusinessRoleOptionRead(BaseModel):
    """为授权和绑定选择器返回活动角色轻量选项。"""

    id: str
    role_code: str
    name: str
    role_kind: Literal["business", "governance"]


class EmployeeRoleAssignmentCreate(BaseModel):
    """为员工增加一条带结构化组织作用域和有效期的角色任职。"""

    tenant_id: str
    employee_profile_id: str
    role_code: str = Field(pattern=ROLE_CODE_PATTERN)
    scope_type: Literal["tenant", "org_unit"] = "tenant"
    scope_id: str = Field(default="*", min_length=1, max_length=128)
    include_descendants: bool = True
    grant_reason: str = Field(min_length=4, max_length=500)
    effective_from: datetime | None = None
    effective_until: datetime


class EmployeeRoleAssignmentUpdate(BaseModel):
    """更新任职有效期或状态，角色和作用域变化必须新建任职。"""

    tenant_id: str
    status: Literal["active", "inactive"] | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class EmployeeRoleAssignmentRead(BaseModel):
    """返回员工任职及账号、员工和角色摘要。"""

    id: str
    employee_profile_id: str
    user_id: str
    employee_id: str
    employee_name: str | None
    role_code: str
    role_name: str
    role_kind: Literal["business", "governance"]
    scope_type: str
    scope_id: str
    include_descendants: bool
    granted_by_user_id: str | None
    grant_reason: str | None
    status: str
    effective_from: str | None
    effective_until: str | None
    created_at: str
    updated_at: str


class AgentRoleBindingCreate(BaseModel):
    """把数字员工绑定到公司业务角色，并声明工作模式和人类监督者。"""

    tenant_id: str
    agent_id: str
    role_code: str = Field(pattern=ROLE_CODE_PATTERN)
    assignment_mode: Literal["assist", "execute"] = "assist"
    supervisor_employee_profile_id: str | None = None
    scope_type: Literal["tenant", "org_unit"] = "tenant"
    scope_id: str = Field(default="*", min_length=1, max_length=128)
    include_descendants: bool = True
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class AgentRoleBindingUpdate(BaseModel):
    """更新数字员工角色绑定的工作模式、监督者或状态。"""

    tenant_id: str
    assignment_mode: Literal["assist", "execute"] | None = None
    supervisor_employee_profile_id: str | None = None
    status: Literal["active", "inactive"] | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class AgentRoleBindingRead(BaseModel):
    """返回数字员工角色绑定及角色、监督员工摘要。"""

    id: str
    agent_id: str
    agent_name: str
    role_code: str
    role_name: str
    assignment_mode: str
    supervisor_employee_profile_id: str | None
    supervisor_employee_id: str | None
    supervisor_employee_name: str | None
    scope_type: str
    scope_id: str
    include_descendants: bool
    granted_by_user_id: str | None
    status: str
    effective_from: str | None
    effective_until: str | None
    created_at: str
    updated_at: str


class PermissionDefinitionRead(BaseModel):
    """返回角色选择器需要的稳定权限编码、名称、业务域和资源动作。"""

    id: str
    permission_code: str
    name: str
    category: str
    resource: str
    action: str
    scope: str | None
    description: str | None
    status: str


class PermissionDefinitionCreate(BaseModel):
    """创建语义不可变的原子权限契约。"""

    tenant_id: str
    permission_code: str = Field(pattern=PERMISSION_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=191)
    category: str = Field(pattern=CATALOG_CODE_PATTERN)
    resource: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    scope: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


class PermissionDefinitionUpdate(BaseModel):
    """只修改权限显示信息或状态，资源动作语义变化必须创建新编码。"""

    tenant_id: str
    name: str | None = Field(default=None, min_length=1, max_length=191)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "inactive"] | None = None


class RoleCategoryRead(BaseModel):
    """返回受控角色分类及其编码前缀提示。"""

    id: str
    code: str
    name: str
    description: str
    role_code_prefix: str
    status: str


class RoleCategoryCreate(BaseModel):
    """创建编码稳定的租户业务角色分类。"""

    tenant_id: str
    code: str = Field(pattern=CATALOG_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=191)
    description: str | None = Field(default=None, max_length=2000)
    role_code_prefix: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")


class RoleCategoryUpdate(BaseModel):
    """更新分类显示信息和状态，分类编码保持不可变。"""

    tenant_id: str
    name: str | None = Field(default=None, min_length=1, max_length=191)
    description: str | None = Field(default=None, max_length=2000)
    role_code_prefix: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    status: Literal["active", "inactive"] | None = None


class PermissionGrantRead(BaseModel):
    """返回服务端实际用于授权判断的单条可解释治理 grant。"""

    permission_code: str
    role_code: str
    role_name: str
    source_kind: str
    source_id: str | None
    scope_type: str
    scope_id: str
    include_descendants: bool
    effective_from: str | None
    effective_until: str | None
    granted_by_user_id: str | None


def _ensure_authorization_read(
    db: Session,
    tenant_id: str,
    current_user: User,
) -> None:
    """统一校验角色、权限目录和有效授权解释读取权限。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="authorization.read",
    )


def _ensure_authorization_manage(
    db: Session,
    tenant_id: str,
    current_user: User,
    *,
    target_org_unit_id: str | None = None,
) -> None:
    """统一校验角色、权限目录和员工角色授权写权限。"""

    target_id = target_org_unit_id or ensure_organization_foundation(db, tenant_id).id
    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="authorization.manage",
        target_org_unit_id=target_id,
    )


@router.get("/effective-permissions", response_model=list[PermissionGrantRead])
def list_effective_permissions(
    tenant_id: str = Query(...),
    user_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[PermissionGrantRead]:
    """返回当前用户或授权管理员指定成员的服务端有效治理权限解释。"""

    target_user_id = user_id or current_user.id
    if target_user_id != current_user.id:
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="authorization.read",
            target_org_unit_id=_user_target_org_unit_id(db, tenant_id, target_user_id),
        )
    else:
        ensure_current_user_tenant(tenant_id, current_user)
    grants = resolve_permission_grants(
        db,
        tenant_id=tenant_id,
        user_id=target_user_id,
    )
    return [_permission_grant_read(grant) for grant in grants]


@router.get("/role-categories", response_model=list[RoleCategoryRead])
def list_role_categories(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RoleCategoryRead]:
    """列出租户受控业务域分类，供角色和权限表单查询回填。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    ensure_builtin_role_categories(db, tenant_id)
    db.commit()
    rows = db.exec(
        select(BusinessRoleCategory)
        .where(BusinessRoleCategory.tenant_id == tenant_id)
        .order_by(BusinessRoleCategory.category_code)
    ).all()
    return [
        RoleCategoryRead(
            id=item.id,
            code=item.category_code,
            name=item.name,
            description=item.description or "",
            role_code_prefix=item.role_code_prefix,
            status=item.status,
        )
        for item in rows
    ]


@router.post("/role-categories", response_model=RoleCategoryRead)
def create_role_category(
    request: RoleCategoryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RoleCategoryRead:
    """创建新业务域分类，并拒绝租户内重复稳定编码。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    ensure_builtin_role_categories(db, request.tenant_id)
    existing = db.exec(
        select(BusinessRoleCategory).where(
            BusinessRoleCategory.tenant_id == request.tenant_id,
            BusinessRoleCategory.category_code == request.code,
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Role category code already exists")
    row = BusinessRoleCategory(
        tenant_id=request.tenant_id,
        category_code=request.code,
        name=request.name.strip(),
        description=(request.description or "").strip() or None,
        role_code_prefix=request.role_code_prefix,
        metadata_json={"created_by_user_id": current_user.id},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _role_category_read(row)


@router.put("/role-categories/{category_id}", response_model=RoleCategoryRead)
def update_role_category(
    category_id: str,
    request: RoleCategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RoleCategoryRead:
    """更新分类可变字段；有活动引用时拒绝停用。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    row = _tenant_role_category(db, request.tenant_id, category_id)
    if request.status == "inactive":
        _assert_role_category_not_referenced(db, row)
    if request.name is not None:
        row.name = request.name.strip()
    if "description" in request.model_fields_set:
        row.description = (request.description or "").strip() or None
    if request.role_code_prefix is not None:
        row.role_code_prefix = request.role_code_prefix
    if request.status is not None:
        row.status = request.status
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _role_category_read(row)


@router.delete("/role-categories/{category_id}", response_model=RoleCategoryRead)
def deactivate_role_category(
    category_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RoleCategoryRead:
    """以带引用检查的软停用代替物理删除分类。"""

    return update_role_category(
        category_id,
        RoleCategoryUpdate(tenant_id=tenant_id, status="inactive"),
        current_user,
        db,
    )


@router.get("/permission-definitions", response_model=list[PermissionDefinitionRead])
def list_permission_definitions(
    tenant_id: str = Query(...),
    q: str | None = Query(default=None, max_length=128),
    category: str | None = Query(default=None, max_length=64),
    status: Literal["active", "inactive", "all"] = Query("active"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[PermissionDefinitionRead]:
    """按编码、名称、描述或业务域查询权限目录，供角色多选控件检索和回填。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    ensure_builtin_governance_catalog(db, tenant_id)
    db.commit()
    rows = db.exec(
        select(PermissionDefinition)
        .where(PermissionDefinition.tenant_id == tenant_id)
        .order_by(PermissionDefinition.category, PermissionDefinition.permission_code)
    ).all()
    normalized_query = (q or "").strip().casefold()
    filtered = [
        row
        for row in rows
        if (status == "all" or row.status == status)
        and (not category or row.category == category)
        and (
            not normalized_query
            or normalized_query
            in " ".join(
                (
                    row.permission_code,
                    row.name,
                    row.description or "",
                )
            ).casefold()
        )
    ]
    return [
        _permission_definition_read(row)
        for row in filtered
    ]


@router.post("/permission-definitions", response_model=PermissionDefinitionRead)
def create_permission_definition(
    request: PermissionDefinitionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PermissionDefinitionRead:
    """创建原子权限并校验编码与资源、动作、作用域的分解完全一致。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    _validate_role_category(db, request.tenant_id, request.category)
    expected_code = f"{request.resource}.{request.action}"
    if request.scope:
        expected_code = f"{expected_code}:{request.scope}"
    if request.permission_code != expected_code:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PERMISSION_CODE_MISMATCH",
                "message": "权限编码必须与资源、动作和作用域一致。",
                "expected_permission_code": expected_code,
            },
        )
    ensure_builtin_permission_catalog(db, request.tenant_id)
    existing = db.exec(
        select(PermissionDefinition).where(
            PermissionDefinition.tenant_id == request.tenant_id,
            PermissionDefinition.permission_code == request.permission_code,
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Permission code already exists")
    row = PermissionDefinition(
        tenant_id=request.tenant_id,
        permission_code=request.permission_code,
        name=request.name.strip(),
        category=request.category,
        resource=request.resource,
        action=request.action,
        scope=request.scope,
        description=(request.description or "").strip() or None,
        metadata_json={"created_by_user_id": current_user.id},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _permission_definition_read(row)


@router.put("/permission-definitions/{permission_id}", response_model=PermissionDefinitionRead)
def update_permission_definition(
    permission_id: str,
    request: PermissionDefinitionUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PermissionDefinitionRead:
    """更新权限显示信息；停用前检查角色和已发布 SOP 的活动引用。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    row = _tenant_permission_definition(db, request.tenant_id, permission_id)
    if request.status == "inactive":
        _assert_permission_not_referenced(db, row)
    if request.name is not None:
        row.name = request.name.strip()
    if "description" in request.model_fields_set:
        row.description = (request.description or "").strip() or None
    if request.status is not None:
        row.status = request.status
    row.updated_at = utc_now()
    row.metadata_json = {
        **(row.metadata_json or {}),
        "last_updated_by_user_id": current_user.id,
    }
    db.add(row)
    db.commit()
    db.refresh(row)
    return _permission_definition_read(row)


@router.delete("/permission-definitions/{permission_id}", response_model=PermissionDefinitionRead)
def deactivate_permission_definition(
    permission_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PermissionDefinitionRead:
    """以带引用检查的软停用代替物理删除权限定义。"""

    return update_permission_definition(
        permission_id,
        PermissionDefinitionUpdate(tenant_id=tenant_id, status="inactive"),
        current_user,
        db,
    )


@router.get("/business-roles", response_model=list[BusinessRoleDetail])
def list_business_roles(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[BusinessRoleDetail]:
    """列出当前租户全部业务角色及引用计数，包含已停用角色。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    ensure_builtin_governance_catalog(db, tenant_id)
    db.commit()
    roles = db.exec(
        select(BusinessRole)
        .where(BusinessRole.tenant_id == tenant_id)
        .order_by(BusinessRole.role_code)
    ).all()
    assignments = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == tenant_id,
            EmployeeRoleAssignment.status == "active",
        )
    ).all()
    agent_bindings = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == tenant_id,
            AgentRoleBinding.status == "active",
        )
    ).all()
    employee_counts: dict[str, int] = {}
    for assignment in assignments:
        employee_counts[assignment.business_role_id] = (
            employee_counts.get(assignment.business_role_id, 0) + 1
        )
    agent_counts: dict[str, int] = {}
    for binding in agent_bindings:
        agent_counts[binding.business_role_id] = agent_counts.get(binding.business_role_id, 0) + 1
    return [
        _business_role_detail(
            db,
            role,
            employee_count=employee_counts.get(role.id, 0),
            agent_count=agent_counts.get(role.id, 0),
        )
        for role in roles
    ]


@router.get("/business-roles/page", response_model=BusinessRolePageRead)
def page_business_roles(
    tenant_id: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BusinessRolePageRead:
    """在租户与授权校验后稳定分页角色，并只聚合当前页引用计数。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    ensure_builtin_governance_catalog(db, tenant_id)
    db.commit()
    base_conditions = [BusinessRole.tenant_id == tenant_id]
    total = int(
        db.exec(select(func.count()).select_from(BusinessRole).where(*base_conditions)).one()
    )
    active_count = int(
        db.exec(
            select(func.count()).select_from(BusinessRole).where(
                *base_conditions, BusinessRole.status == "active"
            )
        ).one()
    )
    assignment_count = int(
        db.exec(
            select(func.count()).select_from(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.tenant_id == tenant_id,
                EmployeeRoleAssignment.status == "active",
            )
        ).one()
    )
    roles = db.exec(
        select(BusinessRole)
        .where(*base_conditions)
        .order_by(BusinessRole.role_code, BusinessRole.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    role_ids = [role.id for role in roles]
    employee_counts = {
        role_id: int(count)
        for role_id, count in db.exec(
            select(EmployeeRoleAssignment.business_role_id, func.count())
            .where(
                EmployeeRoleAssignment.tenant_id == tenant_id,
                EmployeeRoleAssignment.status == "active",
                EmployeeRoleAssignment.business_role_id.in_(role_ids),
            )
            .group_by(EmployeeRoleAssignment.business_role_id)
        ).all()
    } if role_ids else {}
    agent_counts = {
        role_id: int(count)
        for role_id, count in db.exec(
            select(AgentRoleBinding.business_role_id, func.count())
            .where(
                AgentRoleBinding.tenant_id == tenant_id,
                AgentRoleBinding.status == "active",
                AgentRoleBinding.business_role_id.in_(role_ids),
            )
            .group_by(AgentRoleBinding.business_role_id)
        ).all()
    } if role_ids else {}
    return BusinessRolePageRead(
        items=[
            _business_role_detail(
                db,
                role,
                employee_count=employee_counts.get(role.id, 0),
                agent_count=agent_counts.get(role.id, 0),
            )
            for role in roles
        ],
        total=total,
        active_count=active_count,
        assignment_count=assignment_count,
        page=page,
        page_size=page_size,
    )


@router.get("/business-role-options", response_model=list[BusinessRoleOptionRead])
def list_business_role_options(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[BusinessRoleOptionRead]:
    """返回活动角色轻量选项，避免选择器依赖完整角色目录。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    roles = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == tenant_id,
            BusinessRole.status == "active",
        ).order_by(BusinessRole.role_code, BusinessRole.id)
    ).all()
    return [
        BusinessRoleOptionRead(
            id=role.id,
            role_code=role.role_code,
            name=role.name,
            role_kind=role.role_kind,
        )
        for role in roles
    ]


@router.post("/business-roles", response_model=BusinessRoleDetail)
def create_business_role(
    request: BusinessRoleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BusinessRoleDetail:
    """创建公司业务角色，并拒绝租户内重复编码。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    ensure_builtin_governance_catalog(db, request.tenant_id)
    _validate_role_category(db, request.tenant_id, request.category)
    _validate_role_kind_category(request.role_kind, request.category)
    role_code = request.role_code.strip()
    existing = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == request.tenant_id,
            BusinessRole.role_code == role_code,
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Business role code already exists")
    now = utc_now()
    role = BusinessRole(
        tenant_id=request.tenant_id,
        role_code=role_code,
        name=request.name.strip(),
        role_kind=request.role_kind,
        category=request.category.strip(),
        permissions_json=[],
        metadata_json={"created_by_user_id": current_user.id},
        created_at=now,
        updated_at=now,
    )
    db.add(role)
    db.flush()
    try:
        sync_role_permissions(db, role=role, permission_codes=request.permissions)
    except PermissionCatalogError as error:
        db.rollback()
        raise _permission_catalog_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="authorization.manage",
        action="authorization.role.create",
        action_kind="create",
        outcome="success",
        resource_type="business_role",
        resource_id=role.id,
        after=_role_audit_snapshot(db, role),
    )
    db.commit()
    db.refresh(role)
    return _business_role_detail(db, role)


@router.put("/business-roles/{role_id}", response_model=BusinessRoleDetail)
def update_business_role(
    role_id: str,
    request: BusinessRoleUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BusinessRoleDetail:
    """更新角色名称、分类、权限或状态，保留稳定角色编码。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    role = _tenant_role(db, request.tenant_id, role_id)
    before = _role_audit_snapshot(db, role)
    if request.name is not None:
        role.name = request.name.strip()
    if request.category is not None:
        _validate_role_category(db, request.tenant_id, request.category)
        _validate_role_kind_category(role.role_kind, request.category)
        role.category = request.category.strip()
    if request.permissions is not None:
        ensure_builtin_permission_catalog(db, request.tenant_id)
        try:
            sync_role_permissions(db, role=role, permission_codes=request.permissions)
        except PermissionCatalogError as error:
            db.rollback()
            raise _permission_catalog_http_error(error) from error
    if request.status is not None:
        role.status = request.status
    role.metadata_json = {
        **(role.metadata_json or {}),
        "last_updated_by_user_id": current_user.id,
    }
    role.updated_at = utc_now()
    db.add(role)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="authorization.manage",
        action="authorization.role.update",
        action_kind="update",
        outcome="success",
        resource_type="business_role",
        resource_id=role.id,
        before=before,
        after=_role_audit_snapshot(db, role),
    )
    db.commit()
    db.refresh(role)
    employee_count, agent_count = _role_reference_counts(db, role)
    return _business_role_detail(
        db,
        role,
        employee_count=employee_count,
        agent_count=agent_count,
    )


@router.delete("/business-roles/{role_id}", response_model=BusinessRoleDetail)
def deactivate_business_role(
    role_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BusinessRoleDetail:
    """以停用代替物理删除，保留流程定义、任职和历史审计引用。"""

    _ensure_authorization_manage(db, tenant_id, current_user)
    role = _tenant_role(db, tenant_id, role_id)
    before = _role_audit_snapshot(db, role)
    role.status = "inactive"
    role.metadata_json = {
        **(role.metadata_json or {}),
        "deactivated_by_user_id": current_user.id,
    }
    role.updated_at = utc_now()
    db.add(role)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="authorization.manage",
        action="authorization.role.deactivate",
        action_kind="delete",
        outcome="success",
        resource_type="business_role",
        resource_id=role.id,
        before=before,
        after=_role_audit_snapshot(db, role),
    )
    db.commit()
    db.refresh(role)
    employee_count, agent_count = _role_reference_counts(db, role)
    return _business_role_detail(
        db,
        role,
        employee_count=employee_count,
        agent_count=agent_count,
    )


@router.get("/employee-role-assignments", response_model=list[EmployeeRoleAssignmentRead])
def list_employee_role_assignments(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[EmployeeRoleAssignmentRead]:
    """列出当前租户员工的全部有效和历史业务任职。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    assignments = db.exec(
        select(EmployeeRoleAssignment)
        .where(EmployeeRoleAssignment.tenant_id == tenant_id)
        .order_by(EmployeeRoleAssignment.created_at.desc())
    ).all()
    allowed_ids = authorized_organization_ids(
        resolve_permission_grants(
            db,
            tenant_id=tenant_id,
            user_id=current_user.id,
        ),
        permission_code="authorization.read",
    )
    if allowed_ids is not None:
        assignments = [
            assignment
            for assignment in assignments
            if assignment.scope_type == "org_unit"
            and assignment.scope_id in allowed_ids
        ]
    profiles = db.exec(select(EmployeeProfile).where(EmployeeProfile.tenant_id == tenant_id)).all()
    roles = db.exec(select(BusinessRole).where(BusinessRole.tenant_id == tenant_id)).all()
    profiles_by_id = {profile.id: profile for profile in profiles}
    roles_by_id = {role.id: role for role in roles}
    return [
        _assignment_read(assignment, profiles_by_id, roles_by_id)
        for assignment in assignments
        if assignment.employee_profile_id in profiles_by_id
        and assignment.business_role_id in roles_by_id
    ]


@router.post("/employee-role-assignments", response_model=EmployeeRoleAssignmentRead)
def create_employee_role_assignment(
    request: EmployeeRoleAssignmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EmployeeRoleAssignmentRead:
    """创建或重新启用员工多角色任职，并禁止管理员给自己授权。"""

    ensure_current_user_tenant(request.tenant_id, current_user)
    profile = _tenant_profile(db, request.tenant_id, request.employee_profile_id)
    if profile.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own business roles")
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == request.tenant_id,
            BusinessRole.role_code == request.role_code,
            BusinessRole.status == "active",
        )
    ).first()
    if role is None:
        raise HTTPException(status_code=404, detail="Active role not found")
    try:
        scope = validate_role_assignment_scope(
            db,
            tenant_id=request.tenant_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            include_descendants=request.include_descendants,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": str(error), "message": "角色授权范围无效。"},
        ) from error
    _ensure_authorization_manage(
        db,
        request.tenant_id,
        current_user,
        target_org_unit_id=(
            scope.scope_id
            if scope.scope_type == "org_unit"
            else ensure_organization_foundation(db, request.tenant_id).id
        ),
    )
    now = utc_now()
    effective_from, effective_until = _validated_effective_range(
        request.effective_from or now,
        request.effective_until,
    )
    existing = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == request.tenant_id,
            EmployeeRoleAssignment.employee_profile_id == profile.id,
            EmployeeRoleAssignment.business_role_id == role.id,
            EmployeeRoleAssignment.scope_type == request.scope_type,
            EmployeeRoleAssignment.scope_id == request.scope_id,
        )
    ).first()
    if (
        existing is not None
        and existing.status == "active"
        and (existing.effective_from is None or existing.effective_from <= now)
        and (existing.effective_until is None or existing.effective_until > now)
    ):
        return _assignment_read(
            assignment=existing,
            profiles_by_id={profile.id: profile},
            roles_by_id={role.id: role},
        )
    assignment = existing or EmployeeRoleAssignment(
        tenant_id=request.tenant_id,
        employee_profile_id=profile.id,
        business_role_id=role.id,
        scope_type=request.scope_type,
        scope_id=request.scope_id,
        include_descendants=scope.include_descendants,
        granted_by_user_id=current_user.id,
    )
    before = _employee_role_assignment_audit_snapshot(assignment) if existing else {}
    assignment.status = "active"
    assignment.effective_from = effective_from
    assignment.effective_until = effective_until
    assignment.include_descendants = scope.include_descendants
    assignment.granted_by_user_id = current_user.id
    assignment.metadata_json = {
        **(assignment.metadata_json or {}),
        "source": f"organization_management:{current_user.id}",
        "grant_reason": request.grant_reason.strip(),
    }
    assignment.updated_at = utc_now()
    db.add(assignment)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="authorization.manage",
        action=(
            "authorization.member_role.reactivate"
            if existing
            else "authorization.member_role.create"
        ),
        action_kind="update" if existing else "create",
        outcome="success",
        resource_type="employee_role_assignment",
        resource_id=assignment.id,
        target_org_unit_id=_assignment_target_org_unit_id(db, assignment),
        before=before,
        after=_employee_role_assignment_audit_snapshot(assignment),
    )
    db.commit()
    db.refresh(assignment)
    return _assignment_read(assignment, {profile.id: profile}, {role.id: role})


@router.put(
    "/employee-role-assignments/{assignment_id}",
    response_model=EmployeeRoleAssignmentRead,
)
def update_employee_role_assignment(
    assignment_id: str,
    request: EmployeeRoleAssignmentUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EmployeeRoleAssignmentRead:
    """更新既有任职的状态和有效期，并再次执行自我提权防护。"""

    ensure_current_user_tenant(request.tenant_id, current_user)
    assignment = _tenant_assignment(db, request.tenant_id, assignment_id)
    before = _employee_role_assignment_audit_snapshot(assignment)
    _ensure_authorization_manage(
        db,
        request.tenant_id,
        current_user,
        target_org_unit_id=_assignment_target_org_unit_id(db, assignment),
    )
    profile = _tenant_profile(db, request.tenant_id, assignment.employee_profile_id)
    if profile.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own business roles")
    role = _tenant_role(db, request.tenant_id, assignment.business_role_id)
    effective_from, effective_until = _validated_effective_range(
        request.effective_from,
        request.effective_until,
    )
    if "status" in request.model_fields_set and request.status is not None:
        assignment.status = request.status
    if "effective_from" in request.model_fields_set:
        assignment.effective_from = effective_from
    if "effective_until" in request.model_fields_set:
        assignment.effective_until = effective_until
    assignment.metadata_json = {
        **(assignment.metadata_json or {}),
        "last_updated_by_user_id": current_user.id,
    }
    assignment.updated_at = utc_now()
    db.add(assignment)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="authorization.manage",
        action="authorization.member_role.update",
        action_kind="update",
        outcome="success",
        resource_type="employee_role_assignment",
        resource_id=assignment.id,
        target_org_unit_id=_assignment_target_org_unit_id(db, assignment),
        before=before,
        after=_employee_role_assignment_audit_snapshot(assignment),
    )
    db.commit()
    db.refresh(assignment)
    return _assignment_read(assignment, {profile.id: profile}, {role.id: role})


@router.delete(
    "/employee-role-assignments/{assignment_id}",
    response_model=EmployeeRoleAssignmentRead,
)
def deactivate_employee_role_assignment(
    assignment_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EmployeeRoleAssignmentRead:
    """停用员工任职而不删除历史，并禁止管理员撤销自己的任职。"""

    ensure_current_user_tenant(tenant_id, current_user)
    assignment = _tenant_assignment(db, tenant_id, assignment_id)
    before = _employee_role_assignment_audit_snapshot(assignment)
    _ensure_authorization_manage(
        db,
        tenant_id,
        current_user,
        target_org_unit_id=_assignment_target_org_unit_id(db, assignment),
    )
    profile = _tenant_profile(db, tenant_id, assignment.employee_profile_id)
    if profile.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own business roles")
    role = _tenant_role(db, tenant_id, assignment.business_role_id)
    assignment.status = "inactive"
    assignment.metadata_json = {
        **(assignment.metadata_json or {}),
        "deactivated_by_user_id": current_user.id,
    }
    assignment.updated_at = utc_now()
    db.add(assignment)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="authorization.manage",
        action="authorization.member_role.deactivate",
        action_kind="delete",
        outcome="success",
        resource_type="employee_role_assignment",
        resource_id=assignment.id,
        target_org_unit_id=_assignment_target_org_unit_id(db, assignment),
        before=before,
        after=_employee_role_assignment_audit_snapshot(assignment),
    )
    db.commit()
    db.refresh(assignment)
    return _assignment_read(assignment, {profile.id: profile}, {role.id: role})


@router.get("/agent-role-bindings", response_model=list[AgentRoleBindingRead])
def list_agent_role_bindings(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AgentRoleBindingRead]:
    """列出数字员工全部当前和历史业务角色绑定。"""

    _ensure_authorization_read(db, tenant_id, current_user)
    bindings = db.exec(
        select(AgentRoleBinding)
        .where(AgentRoleBinding.tenant_id == tenant_id)
        .order_by(AgentRoleBinding.created_at.desc())
    ).all()
    agents = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
    roles = db.exec(select(BusinessRole).where(BusinessRole.tenant_id == tenant_id)).all()
    profiles = db.exec(select(EmployeeProfile).where(EmployeeProfile.tenant_id == tenant_id)).all()
    return [
        _agent_binding_read(
            binding,
            {agent.id: agent for agent in agents},
            {role.id: role for role in roles},
            {profile.id: profile for profile in profiles},
        )
        for binding in bindings
        if binding.agent_id in {agent.id for agent in agents}
        and binding.business_role_id in {role.id for role in roles}
    ]


@router.post("/agent-role-bindings", response_model=AgentRoleBindingRead)
def create_agent_role_binding(
    request: AgentRoleBindingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentRoleBindingRead:
    """创建或重新启用数字员工的多角色绑定，不授予其人工审批候选资格。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    agent = _tenant_agent(db, request.tenant_id, request.agent_id)
    if agent.is_overall:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AGENT_EXECUTION_SUBJECT_REQUIRED",
                "message": "开放广场资源池不是数字员工，不能绑定业务角色。",
            },
        )
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == request.tenant_id,
            BusinessRole.role_code == request.role_code,
            BusinessRole.role_kind == "business",
            BusinessRole.status == "active",
        )
    ).first()
    if role is None:
        raise HTTPException(status_code=404, detail="Active business role not found")
    try:
        validate_role_assignment_scope(
            db,
            tenant_id=request.tenant_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            include_descendants=request.include_descendants,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": str(error), "message": "数字员工角色范围无效。"},
        ) from error
    supervisor = (
        _tenant_profile(db, request.tenant_id, request.supervisor_employee_profile_id)
        if request.supervisor_employee_profile_id
        else None
    )
    existing = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == request.tenant_id,
            AgentRoleBinding.agent_id == agent.id,
            AgentRoleBinding.business_role_id == role.id,
            AgentRoleBinding.scope_type == request.scope_type,
            AgentRoleBinding.scope_id == request.scope_id,
        )
    ).first()
    effective_from, effective_until = _validated_effective_range(
        request.effective_from,
        request.effective_until,
    )
    binding = existing or AgentRoleBinding(
        tenant_id=request.tenant_id,
        agent_id=agent.id,
        business_role_id=role.id,
        scope_type=request.scope_type,
        scope_id=request.scope_id,
    )
    before = _agent_role_binding_audit_snapshot(binding) if existing else {}
    binding.assignment_mode = request.assignment_mode
    binding.supervisor_employee_profile_id = supervisor.id if supervisor else None
    binding.include_descendants = request.include_descendants
    binding.granted_by_user_id = current_user.id
    binding.status = "active"
    binding.effective_from = effective_from or utc_now()
    binding.effective_until = effective_until
    binding.metadata_json = {
        **(binding.metadata_json or {}),
        "source": f"organization_management:{current_user.id}",
    }
    binding.updated_at = utc_now()
    db.add(binding)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="authorization.manage",
        action=(
            "authorization.agent_role.reactivate"
            if existing
            else "authorization.agent_role.create"
        ),
        action_kind="update" if existing else "create",
        outcome="success",
        resource_type="agent_role_binding",
        resource_id=binding.id,
        target_org_unit_id=request.scope_id if request.scope_type == "org_unit" else None,
        before=before,
        after=_agent_role_binding_audit_snapshot(binding),
    )
    db.commit()
    db.refresh(binding)
    return _agent_binding_read(
        binding,
        {agent.id: agent},
        {role.id: role},
        {supervisor.id: supervisor} if supervisor else {},
    )


@router.put("/agent-role-bindings/{binding_id}", response_model=AgentRoleBindingRead)
def update_agent_role_binding(
    binding_id: str,
    request: AgentRoleBindingUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentRoleBindingRead:
    """更新数字员工角色绑定，同时复核所有关联实体的租户边界。"""

    _ensure_authorization_manage(db, request.tenant_id, current_user)
    binding = _tenant_agent_binding(db, request.tenant_id, binding_id)
    before = _agent_role_binding_audit_snapshot(binding)
    agent = _tenant_agent(db, request.tenant_id, binding.agent_id)
    role = _tenant_role(db, request.tenant_id, binding.business_role_id)
    supervisor = None
    if "supervisor_employee_profile_id" in request.model_fields_set:
        supervisor = (
            _tenant_profile(db, request.tenant_id, request.supervisor_employee_profile_id)
            if request.supervisor_employee_profile_id
            else None
        )
        binding.supervisor_employee_profile_id = supervisor.id if supervisor else None
    elif binding.supervisor_employee_profile_id:
        supervisor = _tenant_profile(db, request.tenant_id, binding.supervisor_employee_profile_id)
    if request.assignment_mode is not None:
        binding.assignment_mode = request.assignment_mode
    if request.status is not None:
        binding.status = request.status
    effective_from, effective_until = _validated_effective_range(
        request.effective_from,
        request.effective_until,
    )
    if "effective_from" in request.model_fields_set:
        binding.effective_from = effective_from
    if "effective_until" in request.model_fields_set:
        binding.effective_until = effective_until
    binding.metadata_json = {
        **(binding.metadata_json or {}),
        "last_updated_by_user_id": current_user.id,
    }
    binding.updated_at = utc_now()
    db.add(binding)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="authorization.manage",
        action="authorization.agent_role.update",
        action_kind="update",
        outcome="success",
        resource_type="agent_role_binding",
        resource_id=binding.id,
        target_org_unit_id=binding.scope_id if binding.scope_type == "org_unit" else None,
        before=before,
        after=_agent_role_binding_audit_snapshot(binding),
    )
    db.commit()
    db.refresh(binding)
    return _agent_binding_read(
        binding,
        {agent.id: agent},
        {role.id: role},
        {supervisor.id: supervisor} if supervisor else {},
    )


@router.delete("/agent-role-bindings/{binding_id}", response_model=AgentRoleBindingRead)
def deactivate_agent_role_binding(
    binding_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentRoleBindingRead:
    """停用数字员工角色绑定，并保留历史和监督关系快照。"""

    _ensure_authorization_manage(db, tenant_id, current_user)
    binding = _tenant_agent_binding(db, tenant_id, binding_id)
    before = _agent_role_binding_audit_snapshot(binding)
    binding.status = "inactive"
    binding.metadata_json = {
        **(binding.metadata_json or {}),
        "deactivated_by_user_id": current_user.id,
    }
    binding.updated_at = utc_now()
    db.add(binding)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="authorization.manage",
        action="authorization.agent_role.deactivate",
        action_kind="delete",
        outcome="success",
        resource_type="agent_role_binding",
        resource_id=binding.id,
        target_org_unit_id=binding.scope_id if binding.scope_type == "org_unit" else None,
        before=before,
        after=_agent_role_binding_audit_snapshot(binding),
    )
    db.commit()
    db.refresh(binding)
    agent = _tenant_agent(db, tenant_id, binding.agent_id)
    role = _tenant_role(db, tenant_id, binding.business_role_id)
    supervisor = (
        _tenant_profile(db, tenant_id, binding.supervisor_employee_profile_id)
        if binding.supervisor_employee_profile_id
        else None
    )
    return _agent_binding_read(
        binding,
        {agent.id: agent},
        {role.id: role},
        {supervisor.id: supervisor} if supervisor else {},
    )


def _business_role_detail(
    db: Session,
    role: BusinessRole,
    *,
    employee_count: int = 0,
    agent_count: int = 0,
) -> BusinessRoleDetail:
    """把业务或治理角色实体转换为管理端稳定响应。"""

    return BusinessRoleDetail(
        id=role.id,
        role_code=role.role_code,
        name=role.name,
        role_kind=role.role_kind,
        category=role.category,
        permissions=role_permission_codes(db, role),
        status=role.status,
        employee_count=employee_count,
        agent_count=agent_count,
        created_at=role.created_at.isoformat(),
        updated_at=role.updated_at.isoformat(),
    )


def _assignment_read(
    assignment: EmployeeRoleAssignment,
    profiles_by_id: dict[str, EmployeeProfile],
    roles_by_id: dict[str, BusinessRole],
) -> EmployeeRoleAssignmentRead:
    """把任职实体与员工、角色摘要合并为管理端响应。"""

    profile = profiles_by_id[assignment.employee_profile_id]
    role = roles_by_id[assignment.business_role_id]
    return EmployeeRoleAssignmentRead(
        id=assignment.id,
        employee_profile_id=profile.id,
        user_id=profile.user_id,
        employee_id=profile.employee_id,
        employee_name=profile.employee_name,
        role_code=role.role_code,
        role_name=role.name,
        role_kind=role.role_kind,
        scope_type=assignment.scope_type,
        scope_id=assignment.scope_id,
        include_descendants=assignment.include_descendants,
        granted_by_user_id=assignment.granted_by_user_id,
        grant_reason=str((assignment.metadata_json or {}).get("grant_reason") or "") or None,
        status=assignment.status,
        effective_from=(
            assignment.effective_from.isoformat() if assignment.effective_from else None
        ),
        effective_until=(
            assignment.effective_until.isoformat() if assignment.effective_until else None
        ),
        created_at=assignment.created_at.isoformat(),
        updated_at=assignment.updated_at.isoformat(),
    )


def _agent_binding_read(
    binding: AgentRoleBinding,
    agents_by_id: dict[str, AgentProfile],
    roles_by_id: dict[str, BusinessRole],
    profiles_by_id: dict[str, EmployeeProfile],
) -> AgentRoleBindingRead:
    """把数字员工角色绑定与角色、监督员工摘要合并为管理端响应。"""

    agent = agents_by_id[binding.agent_id]
    role = roles_by_id[binding.business_role_id]
    supervisor = (
        profiles_by_id.get(binding.supervisor_employee_profile_id)
        if binding.supervisor_employee_profile_id
        else None
    )
    return AgentRoleBindingRead(
        id=binding.id,
        agent_id=agent.id,
        agent_name=agent.name,
        role_code=role.role_code,
        role_name=role.name,
        assignment_mode=binding.assignment_mode,
        supervisor_employee_profile_id=binding.supervisor_employee_profile_id,
        supervisor_employee_id=supervisor.employee_id if supervisor else None,
        supervisor_employee_name=supervisor.employee_name if supervisor else None,
        scope_type=binding.scope_type,
        scope_id=binding.scope_id,
        include_descendants=binding.include_descendants,
        granted_by_user_id=binding.granted_by_user_id,
        status=binding.status,
        effective_from=(
            binding.effective_from.isoformat() if binding.effective_from else None
        ),
        effective_until=(
            binding.effective_until.isoformat() if binding.effective_until else None
        ),
        created_at=binding.created_at.isoformat(),
        updated_at=binding.updated_at.isoformat(),
    )


def _agent_role_binding_audit_snapshot(binding: AgentRoleBinding) -> dict[str, object]:
    """冻结数字员工角色绑定的执行、监督、范围和有效期治理字段。"""

    return {
        "id": binding.id,
        "agent_id": binding.agent_id,
        "business_role_id": binding.business_role_id,
        "assignment_mode": binding.assignment_mode,
        "supervisor_employee_profile_id": binding.supervisor_employee_profile_id,
        "scope_type": binding.scope_type,
        "scope_id": binding.scope_id,
        "include_descendants": binding.include_descendants,
        "granted_by_user_id": binding.granted_by_user_id,
        "status": binding.status,
        "effective_from": (
            binding.effective_from.isoformat() if binding.effective_from else None
        ),
        "effective_until": (
            binding.effective_until.isoformat() if binding.effective_until else None
        ),
    }


def _validate_role_category(db: Session, tenant_id: str, category: str) -> None:
    """拒绝不在租户有效目录中的角色分类，避免自由文本形成第二套分类。"""

    if category.strip() not in active_role_category_codes(db, tenant_id):
        raise HTTPException(status_code=422, detail="Unknown business role category")


def _validate_role_kind_category(role_kind: str, category: str) -> None:
    """保证治理角色与治理权限域成对，防止业务和平台治理语义混装。"""

    if (role_kind == "governance") != (category == "governance"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ROLE_KIND_CATEGORY_MISMATCH",
                "message": "治理角色必须使用平台治理分类，业务角色不能使用平台治理分类。",
            },
        )


def _permission_grant_read(grant: PermissionGrant) -> PermissionGrantRead:
    """把内部结构化 grant 转换为不暴露组织成员或凭据的解释响应。"""

    return PermissionGrantRead(
        permission_code=grant.permission_code,
        role_code=grant.role_code,
        role_name=grant.role_name,
        source_kind=grant.source_kind,
        source_id=grant.source_id,
        scope_type=grant.scope.scope_type,
        scope_id=grant.scope.scope_id,
        include_descendants=grant.scope.include_descendants,
        effective_from=grant.effective_from.isoformat() if grant.effective_from else None,
        effective_until=grant.effective_until.isoformat() if grant.effective_until else None,
        granted_by_user_id=grant.granted_by_user_id,
    )


def _user_target_org_unit_id(
    db: Session,
    tenant_id: str,
    user_id: str,
) -> str:
    """返回目标成员当前主组织；未归属或不存在时使用租户根以避免范围放宽。"""

    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == user_id,
        )
    ).first()
    if profile is not None:
        assignments = db.exec(
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
        if assignments:
            return assignments[0].org_unit_id
    return ensure_organization_foundation(db, tenant_id).id


def _assignment_target_org_unit_id(
    db: Session,
    assignment: EmployeeRoleAssignment,
) -> str:
    """把角色任职范围转换为授权变更目标组织。"""

    if assignment.scope_type == "org_unit":
        return assignment.scope_id
    return ensure_organization_foundation(db, assignment.tenant_id).id


def _role_category_read(row: BusinessRoleCategory) -> RoleCategoryRead:
    """把分类实体转换为管理端稳定响应。"""

    return RoleCategoryRead(
        id=row.id,
        code=row.category_code,
        name=row.name,
        description=row.description or "",
        role_code_prefix=row.role_code_prefix,
        status=row.status,
    )


def _permission_definition_read(row: PermissionDefinition) -> PermissionDefinitionRead:
    """把权限目录实体转换为管理端稳定响应。"""

    return PermissionDefinitionRead(
        id=row.id,
        permission_code=row.permission_code,
        name=row.name,
        category=row.category,
        resource=row.resource,
        action=row.action,
        scope=row.scope,
        description=row.description,
        status=row.status,
    )


def _tenant_role_category(
    db: Session,
    tenant_id: str,
    category_id: str,
) -> BusinessRoleCategory:
    """读取当前租户分类，拒绝通过主键跨租户修改。"""

    row = db.get(BusinessRoleCategory, category_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Role category not found")
    return row


def _tenant_permission_definition(
    db: Session,
    tenant_id: str,
    permission_id: str,
) -> PermissionDefinition:
    """读取当前租户权限定义，拒绝通过主键跨租户修改。"""

    row = db.get(PermissionDefinition, permission_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Permission definition not found")
    return row


def _assert_role_category_not_referenced(db: Session, category: BusinessRoleCategory) -> None:
    """分类仍被有效角色或权限使用时拒绝停用。"""

    role_count = len(
        db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == category.tenant_id,
                BusinessRole.category == category.category_code,
                BusinessRole.status == "active",
            )
        ).all()
    )
    permission_count = len(
        db.exec(
            select(PermissionDefinition).where(
                PermissionDefinition.tenant_id == category.tenant_id,
                PermissionDefinition.category == category.category_code,
                PermissionDefinition.status == "active",
            )
        ).all()
    )
    if role_count or permission_count:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ROLE_CATEGORY_REFERENCED",
                "active_role_count": role_count,
                "active_permission_count": permission_count,
            },
        )


def _assert_permission_not_referenced(db: Session, permission: PermissionDefinition) -> None:
    """权限仍被有效角色或已发布 SOP 引用时拒绝停用。"""

    active_role_ids = {
        role.id
        for role in db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == permission.tenant_id,
                BusinessRole.status == "active",
            )
        ).all()
    }
    role_references = [
        mapping
        for mapping in db.exec(
            select(BusinessRolePermission).where(
                BusinessRolePermission.tenant_id == permission.tenant_id,
                BusinessRolePermission.permission_definition_id == permission.id,
            )
        ).all()
        if mapping.business_role_id in active_role_ids
    ]
    published_skills = db.exec(
        select(Skill).where(
            Skill.tenant_id == permission.tenant_id,
            Skill.status == "published",
        )
    ).all()
    published_skill_ids = [
        skill.skill_id
        for skill in published_skills
        if _json_contains_string(skill.content_json, permission.permission_code)
    ]
    if role_references or published_skill_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PERMISSION_DEFINITION_REFERENCED",
                "active_role_count": len(role_references),
                "published_skill_ids": sorted(published_skill_ids),
            },
        )


def _json_contains_string(value: object, target: str) -> bool:
    """递归检查 JSON 值是否精确包含稳定编码，不使用易误报的文本 LIKE。"""

    if isinstance(value, str):
        return value == target
    if isinstance(value, dict):
        return any(_json_contains_string(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains_string(item, target) for item in value)
    return False


def _permission_catalog_http_error(error: PermissionCatalogError) -> HTTPException:
    """把权限目录领域错误转换为包含稳定编码和未知权限列表的 HTTP 422。"""

    return HTTPException(
        status_code=422,
        detail={
            "code": error.code,
            "message": str(error),
            "permission_codes": error.permission_codes,
        },
    )


def _role_audit_snapshot(db: Session, role: BusinessRole) -> dict[str, object]:
    """返回角色定义及规范权限编码，不包含成员或数字员工详情。"""

    return {
        "role_code": role.role_code,
        "name": role.name,
        "role_kind": role.role_kind,
        "category": role.category,
        "permissions": role_permission_codes(db, role),
        "status": role.status,
    }


def _employee_role_assignment_audit_snapshot(
    assignment: EmployeeRoleAssignment,
) -> dict[str, object]:
    """返回员工角色任职的结构化范围和有效期，不保存员工姓名等个人资料。"""

    return {
        "employee_profile_id": assignment.employee_profile_id,
        "business_role_id": assignment.business_role_id,
        "scope_type": assignment.scope_type,
        "scope_id": assignment.scope_id,
        "include_descendants": assignment.include_descendants,
        "grant_reason": (assignment.metadata_json or {}).get("grant_reason"),
        "status": assignment.status,
        "effective_from": (
            assignment.effective_from.isoformat()
            if assignment.effective_from
            else None
        ),
        "effective_until": (
            assignment.effective_until.isoformat()
            if assignment.effective_until
            else None
        ),
    }


def _tenant_role(db: Session, tenant_id: str, role_id: str) -> BusinessRole:
    """读取当前租户角色，拒绝通过 ID 跨租户访问。"""

    role = db.get(BusinessRole, role_id)
    if role is None or role.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Business role not found")
    return role


def _tenant_profile(db: Session, tenant_id: str, profile_id: str) -> EmployeeProfile:
    """读取当前租户有效员工档案，拒绝跨租户任职。"""

    profile = db.get(EmployeeProfile, profile_id)
    if profile is None or profile.tenant_id != tenant_id or profile.status != "active":
        raise HTTPException(status_code=404, detail="Active employee profile not found")
    return profile


def _tenant_agent(db: Session, tenant_id: str, agent_id: str) -> AgentProfile:
    """读取当前租户数字员工，拒绝跨租户角色绑定。"""

    agent = db.get(AgentProfile, agent_id)
    if agent is None or agent.tenant_id != tenant_id or agent.status != "active":
        raise HTTPException(status_code=404, detail="Active agent not found")
    return agent


def _tenant_agent_binding(db: Session, tenant_id: str, binding_id: str) -> AgentRoleBinding:
    """读取当前租户数字员工角色绑定，拒绝跨租户修改。"""

    binding = db.get(AgentRoleBinding, binding_id)
    if binding is None or binding.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent role binding not found")
    return binding


def _tenant_assignment(
    db: Session,
    tenant_id: str,
    assignment_id: str,
) -> EmployeeRoleAssignment:
    """读取当前租户任职，拒绝通过主键跨租户修改。"""

    assignment = db.get(EmployeeRoleAssignment, assignment_id)
    if assignment is None or assignment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Employee role assignment not found")
    return assignment


def _validated_effective_range(
    effective_from: datetime | None,
    effective_until: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """校验任职有效期顺序，并转换为项目统一的无时区 UTC 存储值。"""

    normalized_from = _naive_utc(effective_from)
    normalized_until = _naive_utc(effective_until)
    if normalized_from and normalized_until and normalized_until <= normalized_from:
        raise HTTPException(
            status_code=400,
            detail="Effective until must be later than effective from",
        )
    return normalized_from, normalized_until


def _naive_utc(value: datetime | None) -> datetime | None:
    """将带时区时间转换为无时区 UTC，保留已有无时区输入。"""

    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _role_reference_counts(db: Session, role: BusinessRole) -> tuple[int, int]:
    """统计角色当前有效员工任职和数字员工绑定数量。"""

    employee_count = len(
        db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.tenant_id == role.tenant_id,
                EmployeeRoleAssignment.business_role_id == role.id,
                EmployeeRoleAssignment.status == "active",
            )
        ).all()
    )
    agent_count = len(
        db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.tenant_id == role.tenant_id,
                AgentRoleBinding.business_role_id == role.id,
                AgentRoleBinding.status == "active",
            )
        ).all()
    )
    return employee_count, agent_count
