"""
@Time       : 2026/07/28 12:05
@Author     : zhanglp8181
@File       : organization_units.py
@CallChain  : 组织架构页面 → FastAPI → 组织树服务 → OrganizationUnit
@Description: 提供租户组织树查询、创建、编辑、移动和软停用 API。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.db import get_session
from app.db.models import (
    CodeItem,
    OrganizationLeaderAssignment,
    OrganizationUnit,
    User,
    utc_now,
)
from app.organization.governance import (
    authorized_organization_ids,
    ensure_governance_permission,
    resolve_permission_grants,
)
from app.organization.query import count_current_organization_members
from app.organization.reference_data import ensure_organization_unit_type_catalog
from app.organization.units import (
    OrganizationUnitError,
    create_organization_unit,
    deactivate_organization_unit,
    ensure_organization_foundation,
    get_tenant_organization_unit,
    update_organization_unit,
)
from app.security.auth import get_current_user


router = APIRouter(prefix="/api/organization", tags=["organization-units"])
ORGANIZATION_CODE_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"


class OrganizationUnitCreate(BaseModel):
    """创建当前租户非根组织的请求。"""

    tenant_id: str
    parent_id: str
    code: str = Field(pattern=ORGANIZATION_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=191)
    unit_type_code: str = Field(min_length=1, max_length=128)
    sort_order: int = 0


class OrganizationUnitUpdate(BaseModel):
    """更新组织显示资料或父节点，不允许改变稳定编码和根节点。"""

    tenant_id: str
    name: str | None = Field(default=None, min_length=1, max_length=191)
    unit_type_code: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: int | None = None
    parent_id: str | None = None


class OrganizationUnitRead(BaseModel):
    """返回前端构造组织树所需的稳定关系和状态。"""

    id: str
    tenant_id: str
    parent_id: str | None
    code: str
    name: str
    unit_type_code: str
    tree_path: str
    depth: int
    sort_order: int
    is_root: bool
    status: Literal["active", "inactive"]


class OrganizationUnitNodeRead(OrganizationUnitRead):
    """返回按层组织树节点及其是否还有直接下级。"""

    has_children: bool


class OrganizationPathItemRead(BaseModel):
    """返回搜索命中组织的单个祖先路径节点。"""

    id: str
    name: str


class OrganizationSearchResultRead(OrganizationUnitNodeRead):
    """返回组织搜索命中及前端恢复展开状态所需的祖先路径。"""

    path: list[OrganizationPathItemRead]


class OrganizationSummaryRead(BaseModel):
    """返回当前组织的直属、子树和责任事实摘要。"""

    org_unit_id: str
    direct_member_count: int
    subtree_member_count: int
    direct_child_count: int
    current_leader_count: int


class OrganizationUnitTypeRead(BaseModel):
    """返回组织类型码项，分类本身不产生层级或权限。"""

    code: str
    name: str
    status: Literal["active", "inactive"]
    is_builtin: bool
    sort_order: int


@router.get("/units", response_model=list[OrganizationUnitRead])
def list_organization_units(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[OrganizationUnitRead]:
    """列出当前认证租户组织，首次读取时幂等补齐根节点。"""

    allowed_ids = _authorized_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.read",
    )
    ensure_organization_foundation(db, tenant_id)
    db.commit()
    statement = select(OrganizationUnit).where(OrganizationUnit.tenant_id == tenant_id)
    if allowed_ids is not None:
        statement = statement.where(OrganizationUnit.id.in_(allowed_ids))
    rows = db.exec(
        statement
        .order_by(
            OrganizationUnit.depth,
            OrganizationUnit.sort_order,
            OrganizationUnit.name,
        )
    ).all()
    return [_unit_read(row) for row in rows]


@router.get("/unit-children", response_model=list[OrganizationUnitNodeRead])
def list_organization_unit_children(
    tenant_id: str = Query(...),
    parent_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[OrganizationUnitNodeRead]:
    """按父节点读取根或直接下级，并批量计算 has_children。"""

    allowed_ids = _authorized_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.read",
    )
    root = ensure_organization_foundation(db, tenant_id)
    db.commit()
    if parent_id is None:
        if allowed_ids is None:
            rows = [root]
        else:
            allowed_rows = db.exec(
                select(OrganizationUnit)
                .where(
                    OrganizationUnit.tenant_id == tenant_id,
                    OrganizationUnit.status == "active",
                    OrganizationUnit.id.in_(allowed_ids),
                )
                .order_by(
                    OrganizationUnit.depth,
                    OrganizationUnit.sort_order,
                    OrganizationUnit.name,
                    OrganizationUnit.id,
                )
            ).all()
            rows = [
                row
                for row in allowed_rows
                if row.parent_id is None or row.parent_id not in allowed_ids
            ]
    else:
        if allowed_ids is not None and parent_id not in allowed_ids:
            raise _organization_scope_denied("organization.read", parent_id)
        try:
            get_tenant_organization_unit(db, tenant_id, parent_id)
        except OrganizationUnitError as error:
            raise _organization_http_error(error) from error
        child_statement = select(OrganizationUnit).where(
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.parent_id == parent_id,
            OrganizationUnit.status == "active",
        )
        if allowed_ids is not None:
            child_statement = child_statement.where(OrganizationUnit.id.in_(allowed_ids))
        rows = db.exec(
            child_statement
            .order_by(
                OrganizationUnit.sort_order,
                OrganizationUnit.name,
                OrganizationUnit.id,
            )
        ).all()
    parent_ids = [row.id for row in rows]
    parents_with_children = set()
    if parent_ids:
        parents_with_children = set(
            db.exec(
                select(OrganizationUnit.parent_id).where(
                    OrganizationUnit.tenant_id == tenant_id,
                    OrganizationUnit.parent_id.in_(parent_ids),
                    OrganizationUnit.status == "active",
                    *(
                        (OrganizationUnit.id.in_(allowed_ids),)
                        if allowed_ids is not None
                        else ()
                    ),
                )
                .group_by(OrganizationUnit.parent_id)
            ).all()
        )
    return [
        OrganizationUnitNodeRead(
            **_unit_read(row).model_dump(),
            has_children=row.id in parents_with_children,
        )
        for row in rows
    ]


@router.get("/unit-search", response_model=list[OrganizationSearchResultRead])
def search_organization_units(
    tenant_id: str = Query(...),
    keyword: str = Query(..., min_length=1, max_length=80),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[OrganizationSearchResultRead]:
    """按名称或稳定编码搜索组织，并返回可逐层恢复的祖先路径。"""

    allowed_ids = _authorized_org_ids(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.read",
    )
    normalized_keyword = keyword.strip()
    if not normalized_keyword:
        raise HTTPException(status_code=400, detail="ORGANIZATION_KEYWORD_REQUIRED")
    pattern = f"%{normalized_keyword}%"
    statement = select(OrganizationUnit).where(
        OrganizationUnit.tenant_id == tenant_id,
        OrganizationUnit.status == "active",
        or_(
            OrganizationUnit.name.ilike(pattern),
            OrganizationUnit.code.ilike(pattern),
        ),
    )
    if allowed_ids is not None:
        statement = statement.where(OrganizationUnit.id.in_(allowed_ids))
    rows = db.exec(
        statement
        .order_by(
            OrganizationUnit.depth,
            OrganizationUnit.sort_order,
            OrganizationUnit.name,
            OrganizationUnit.id,
        )
        .limit(limit)
    ).all()
    path_ids = {
        path_id
        for row in rows
        for path_id in row.tree_path.split("/")
        if path_id and (allowed_ids is None or path_id in allowed_ids)
    }
    path_rows = (
        db.exec(
            select(OrganizationUnit).where(
                OrganizationUnit.tenant_id == tenant_id,
                OrganizationUnit.status == "active",
                OrganizationUnit.id.in_(path_ids),
            )
        ).all()
        if path_ids
        else []
    )
    units_by_id = {row.id: row for row in path_rows}
    parents_with_children = set(
        db.exec(
            select(OrganizationUnit.parent_id)
            .where(
                OrganizationUnit.tenant_id == tenant_id,
                OrganizationUnit.parent_id.in_([row.id for row in rows]),
                OrganizationUnit.status == "active",
                *(
                    (OrganizationUnit.id.in_(allowed_ids),)
                    if allowed_ids is not None
                    else ()
                ),
            )
            .group_by(OrganizationUnit.parent_id)
        ).all()
    ) if rows else set()
    return [
        OrganizationSearchResultRead(
            **_unit_read(row).model_dump(),
            has_children=row.id in parents_with_children,
            path=[
                OrganizationPathItemRead(id=path_id, name=units_by_id[path_id].name)
                for path_id in row.tree_path.split("/")
                if path_id in units_by_id
            ],
        )
        for row in rows
    ]


@router.get("/unit-summary", response_model=OrganizationSummaryRead)
def get_organization_unit_summary(
    tenant_id: str = Query(...),
    org_unit_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrganizationSummaryRead:
    """返回服务端计算的直属/子树人数、直接下级和当前负责人数量。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.read",
        target_org_unit_id=org_unit_id,
    )
    try:
        counts = count_current_organization_members(
            db,
            tenant_id=tenant_id,
            org_unit_id=org_unit_id,
        )
    except OrganizationUnitError as error:
        raise _organization_http_error(error) from error
    now = utc_now()
    direct_child_count = db.exec(
        select(func.count(OrganizationUnit.id)).where(
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.parent_id == org_unit_id,
            OrganizationUnit.status == "active",
        )
    ).one()
    current_leader_count = db.exec(
        select(func.count(OrganizationLeaderAssignment.id)).where(
            OrganizationLeaderAssignment.tenant_id == tenant_id,
            OrganizationLeaderAssignment.org_unit_id == org_unit_id,
            OrganizationLeaderAssignment.status == "active",
            OrganizationLeaderAssignment.effective_from <= now,
            or_(
                OrganizationLeaderAssignment.effective_until.is_(None),
                OrganizationLeaderAssignment.effective_until > now,
            ),
        )
    ).one()
    return OrganizationSummaryRead(
        org_unit_id=org_unit_id,
        direct_member_count=counts.direct,
        subtree_member_count=counts.subtree,
        direct_child_count=int(direct_child_count),
        current_leader_count=int(current_leader_count),
    )


@router.get("/unit-types", response_model=list[OrganizationUnitTypeRead])
def list_organization_unit_types(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[OrganizationUnitTypeRead]:
    """列出当前租户组织类型，普通成员可读但不能治理。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="organization.read",
    )
    code_set = ensure_organization_unit_type_catalog(db, tenant_id)
    db.commit()
    items = db.exec(
        select(CodeItem)
        .where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
        )
        .order_by(CodeItem.sort_order, CodeItem.item_code)
    ).all()
    return [
        OrganizationUnitTypeRead(
            code=item.item_code,
            name=item.name,
            status=item.status,
            is_builtin=item.is_builtin,
            sort_order=item.sort_order,
        )
        for item in items
    ]


@router.post("/units", response_model=OrganizationUnitRead)
def create_organization_unit_endpoint(
    request: OrganizationUnitCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrganizationUnitRead:
    """由租户管理员在活动父节点下创建组织。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="organization.manage",
        target_org_unit_id=request.parent_id,
    )
    try:
        unit = create_organization_unit(
            db,
            tenant_id=request.tenant_id,
            parent_id=request.parent_id,
            code=request.code,
            name=request.name,
            unit_type_code=request.unit_type_code,
            sort_order=request.sort_order,
        )
    except OrganizationUnitError as error:
        raise _organization_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="organization.manage",
        action="organization.create",
        action_kind="create",
        outcome="success",
        resource_type="organization_unit",
        resource_id=unit.id,
        target_org_unit_id=unit.id,
        after=_organization_audit_snapshot(unit),
    )
    db.commit()
    db.refresh(unit)
    return _unit_read(unit)


@router.put("/units/{unit_id}", response_model=OrganizationUnitRead)
def update_organization_unit_endpoint(
    unit_id: str,
    request: OrganizationUnitUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrganizationUnitRead:
    """由租户管理员修改非根组织资料或移动整棵子树。"""

    try:
        unit = get_tenant_organization_unit(db, request.tenant_id, unit_id)
        ensure_governance_permission(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            permission_code="organization.manage",
            target_org_unit_id=unit.id,
        )
        if request.parent_id is not None:
            ensure_governance_permission(
                db,
                tenant_id=request.tenant_id,
                current_user=current_user,
                permission_code="organization.manage",
                target_org_unit_id=request.parent_id,
            )
        before = _organization_audit_snapshot(unit)
        update_organization_unit(
            db,
            unit,
            name=request.name if "name" in request.model_fields_set else None,
            unit_type_code=(
                request.unit_type_code
                if "unit_type_code" in request.model_fields_set
                else None
            ),
            sort_order=(
                request.sort_order
                if "sort_order" in request.model_fields_set
                else None
            ),
            new_parent_id=(
                request.parent_id
                if "parent_id" in request.model_fields_set
                else None
            ),
        )
    except OrganizationUnitError as error:
        raise _organization_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="organization.manage",
        action="organization.update",
        action_kind="update",
        outcome="success",
        resource_type="organization_unit",
        resource_id=unit.id,
        target_org_unit_id=unit.id,
        before=before,
        after=_organization_audit_snapshot(unit),
    )
    db.commit()
    db.refresh(unit)
    return _unit_read(unit)


@router.delete("/units/{unit_id}", response_model=OrganizationUnitRead)
def deactivate_organization_unit_endpoint(
    unit_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> OrganizationUnitRead:
    """软停用无活动引用的非根组织。"""

    try:
        unit = get_tenant_organization_unit(db, tenant_id, unit_id)
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="organization.manage",
            target_org_unit_id=unit.id,
        )
        before = _organization_audit_snapshot(unit)
        audit_scope_org_id = unit.parent_id
        deactivate_organization_unit(db, unit)
    except OrganizationUnitError as error:
        raise _organization_http_error(error) from error
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="organization.manage",
        action="organization.deactivate",
        action_kind="delete",
        outcome="success",
        resource_type="organization_unit",
        resource_id=unit.id,
        target_org_unit_id=audit_scope_org_id,
        before=before,
        after=_organization_audit_snapshot(unit),
        detail={"deactivated_org_unit_id": unit.id},
    )
    db.commit()
    db.refresh(unit)
    return _unit_read(unit)


def _organization_audit_snapshot(unit: OrganizationUnit) -> dict[str, object]:
    """返回组织治理字段快照，不包含成员资料或其他个人信息。"""

    return {
        "parent_id": unit.parent_id,
        "code": unit.code,
        "name": unit.name,
        "unit_type_code": unit.unit_type_code,
        "tree_path": unit.tree_path,
        "depth": unit.depth,
        "sort_order": unit.sort_order,
        "status": unit.status,
    }


def _authorized_org_ids(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    permission_code: str,
) -> frozenset[str] | None:
    """校验组织读取权限并返回服务端可见组织集合，None 表示租户全范围。"""

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


def _organization_scope_denied(
    permission_code: str,
    org_unit_id: str,
) -> HTTPException:
    """返回不泄漏范围外组织详情的稳定治理拒绝。"""

    return HTTPException(
        status_code=403,
        detail={
            "code": "GOVERNANCE_PERMISSION_DENIED",
            "permission": permission_code,
            "target_org_unit_id": org_unit_id,
        },
    )


def _unit_read(unit: OrganizationUnit) -> OrganizationUnitRead:
    """把数据库组织实体转换为稳定 API 契约。"""

    return OrganizationUnitRead(
        id=unit.id,
        tenant_id=unit.tenant_id,
        parent_id=unit.parent_id,
        code=unit.code,
        name=unit.name,
        unit_type_code=unit.unit_type_code,
        tree_path=unit.tree_path,
        depth=unit.depth,
        sort_order=unit.sort_order,
        is_root=unit.is_root,
        status=unit.status,
    )


def _organization_http_error(error: OrganizationUnitError) -> HTTPException:
    """把组织领域错误映射为稳定 HTTP 状态，不暴露数据库异常。"""

    detail = str(error)
    if detail in {"ORGANIZATION_NOT_FOUND", "PARENT_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=detail)
    if detail in {
        "ORGANIZATION_CODE_EXISTS",
        "ACTIVE_CHILDREN_EXIST",
        "MULTIPLE_ROOT_UNITS",
    }:
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=detail)
