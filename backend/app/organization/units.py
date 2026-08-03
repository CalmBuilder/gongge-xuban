"""
@Time       : 2026/07/28 11:25
@Author     : zhanglp8181
@File       : units.py
@CallChain  : 租户初始化/组织 API → 组织树服务 → OrganizationUnit/CodeSet
@Description: 维护租户单根组织树、稳定路径、移动环保护和停用引用约束。
"""

from __future__ import annotations

import hashlib

from sqlmodel import Session, select

from app.db.models import OrganizationUnit, Tenant, utc_now
from app.organization.reference_data import (
    ensure_organization_unit_type_catalog,
    require_active_organization_unit_type,
)


MAX_ORGANIZATION_DEPTH = 12
ROOT_ORGANIZATION_CODE = "ROOT"


class OrganizationUnitError(ValueError):
    """表示组织树命令违反稳定标识、层级或活动引用约束。"""


def ensure_organization_foundation(db: Session, tenant_id: str) -> OrganizationUnit:
    """幂等初始化租户组织类型码表和唯一根组织，不创建业务部门。"""

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise OrganizationUnitError("TENANT_NOT_FOUND")
    ensure_organization_unit_type_catalog(db, tenant_id)
    roots = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.is_root.is_(True),
        )
    ).all()
    if len(roots) > 1:
        raise OrganizationUnitError("MULTIPLE_ROOT_UNITS")
    if roots:
        return roots[0]

    root_id = _stable_root_id(tenant_id)
    root = OrganizationUnit(
        id=root_id,
        tenant_id=tenant_id,
        parent_id=None,
        code=ROOT_ORGANIZATION_CODE,
        name=tenant.name,
        unit_type_code="company",
        tree_path=root_id,
        depth=0,
        sort_order=0,
        is_root=True,
        root_tenant_id=tenant_id,
        status="active",
    )
    db.add(root)
    db.flush()
    return root


def create_organization_unit(
    db: Session,
    *,
    tenant_id: str,
    parent_id: str,
    code: str,
    name: str,
    unit_type_code: str,
    sort_order: int = 0,
) -> OrganizationUnit:
    """在活动父节点下创建稳定编码的组织，并计算路径和深度。"""

    normalized_code = code.strip()
    normalized_name = name.strip()
    if not normalized_code or normalized_code.upper() == ROOT_ORGANIZATION_CODE:
        raise OrganizationUnitError("INVALID_ORGANIZATION_CODE")
    if not normalized_name:
        raise OrganizationUnitError("ORGANIZATION_NAME_REQUIRED")
    parent = _active_tenant_unit(db, tenant_id, parent_id, error_code="PARENT_NOT_FOUND")
    if parent.depth >= MAX_ORGANIZATION_DEPTH:
        raise OrganizationUnitError("MAX_ORGANIZATION_DEPTH_EXCEEDED")
    duplicate = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.code == normalized_code,
        )
    ).first()
    if duplicate is not None:
        raise OrganizationUnitError("ORGANIZATION_CODE_EXISTS")
    try:
        require_active_organization_unit_type(db, tenant_id, unit_type_code)
    except ValueError as error:
        raise OrganizationUnitError(str(error)) from error

    unit = OrganizationUnit(
        tenant_id=tenant_id,
        parent_id=parent.id,
        code=normalized_code,
        name=normalized_name,
        unit_type_code=unit_type_code.strip(),
        tree_path="",
        depth=parent.depth + 1,
        sort_order=sort_order,
    )
    db.add(unit)
    db.flush()
    unit.tree_path = f"{parent.tree_path}/{unit.id}"
    unit.updated_at = utc_now()
    db.add(unit)
    db.flush()
    return unit


def get_tenant_organization_unit(
    db: Session,
    tenant_id: str,
    unit_id: str,
) -> OrganizationUnit:
    """读取当前租户组织，拒绝用其他租户的稳定 ID 访问资源。"""

    unit = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.id == unit_id,
            OrganizationUnit.tenant_id == tenant_id,
        )
    ).first()
    if unit is None:
        raise OrganizationUnitError("ORGANIZATION_NOT_FOUND")
    return unit


def update_organization_unit(
    db: Session,
    unit: OrganizationUnit,
    *,
    name: str | None = None,
    unit_type_code: str | None = None,
    sort_order: int | None = None,
    new_parent_id: str | None = None,
) -> OrganizationUnit:
    """更新非根组织的可变资料，并按需调用统一移动命令。"""

    if unit.is_root:
        raise OrganizationUnitError("ROOT_UNIT_IMMUTABLE")
    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise OrganizationUnitError("ORGANIZATION_NAME_REQUIRED")
        unit.name = normalized_name
    if unit_type_code is not None:
        try:
            require_active_organization_unit_type(
                db,
                unit.tenant_id,
                unit_type_code,
            )
        except ValueError as error:
            raise OrganizationUnitError(str(error)) from error
        unit.unit_type_code = unit_type_code.strip()
    if sort_order is not None:
        unit.sort_order = sort_order
    if new_parent_id is not None:
        move_organization_unit(db, unit, new_parent_id)
    unit.updated_at = utc_now()
    db.add(unit)
    db.flush()
    return unit


def move_organization_unit(
    db: Session,
    unit: OrganizationUnit,
    new_parent_id: str,
) -> OrganizationUnit:
    """移动非根组织并在同一事务内重写整棵子树的稳定路径。"""

    if unit.is_root:
        raise OrganizationUnitError("ROOT_UNIT_IMMUTABLE")
    parent = _active_tenant_unit(
        db,
        unit.tenant_id,
        new_parent_id,
        error_code="PARENT_NOT_FOUND",
    )
    if parent.id == unit.id or parent.tree_path.startswith(f"{unit.tree_path}/"):
        raise OrganizationUnitError("ORGANIZATION_CYCLE")
    if unit.parent_id == parent.id:
        return unit

    subtree = db.exec(
        select(OrganizationUnit).where(OrganizationUnit.tenant_id == unit.tenant_id)
    ).all()
    subtree = [
        row
        for row in subtree
        if row.tree_path == unit.tree_path
        or row.tree_path.startswith(f"{unit.tree_path}/")
    ]
    depth_delta = parent.depth + 1 - unit.depth
    if any(row.depth + depth_delta > MAX_ORGANIZATION_DEPTH for row in subtree):
        raise OrganizationUnitError("MAX_ORGANIZATION_DEPTH_EXCEEDED")

    old_path = unit.tree_path
    new_path = f"{parent.tree_path}/{unit.id}"
    now = utc_now()
    for row in subtree:
        suffix = row.tree_path[len(old_path) :]
        row.tree_path = f"{new_path}{suffix}"
        row.depth += depth_delta
        row.updated_at = now
        if row.id == unit.id:
            row.parent_id = parent.id
        db.add(row)
    db.flush()
    return unit


def deactivate_organization_unit(
    db: Session,
    unit: OrganizationUnit,
) -> OrganizationUnit:
    """停用没有活动下级引用的非根组织，保留历史路径和标识。"""

    if unit.is_root:
        raise OrganizationUnitError("ROOT_UNIT_IMMUTABLE")
    active_child = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.tenant_id == unit.tenant_id,
            OrganizationUnit.parent_id == unit.id,
            OrganizationUnit.status == "active",
        )
    ).first()
    if active_child is not None:
        raise OrganizationUnitError("ACTIVE_CHILDREN_EXIST")
    from app.organization.assignments import organization_has_active_assignments

    if organization_has_active_assignments(db, unit.tenant_id, unit.id):
        raise OrganizationUnitError("ACTIVE_ORGANIZATION_REFERENCES_EXIST")
    unit.status = "inactive"
    unit.updated_at = utc_now()
    db.add(unit)
    db.flush()
    return unit


def _active_tenant_unit(
    db: Session,
    tenant_id: str,
    unit_id: str,
    *,
    error_code: str,
) -> OrganizationUnit:
    """读取当前租户活动组织，避免跨租户 ID 被当作父节点使用。"""

    unit = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.id == unit_id,
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.status == "active",
        )
    ).first()
    if unit is None:
        raise OrganizationUnitError(error_code)
    return unit


def _stable_root_id(tenant_id: str) -> str:
    """基于租户 ID 生成可重复计算且跨数据库一致的根组织 ID。"""

    digest = hashlib.sha256(f"{tenant_id}:organization-root".encode()).hexdigest()[:16]
    return f"orgroot_{digest}"
