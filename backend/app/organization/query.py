"""
@Time       : 2026/07/28 17:10
@Author     : zhanglp8181
@File       : query.py
@CallChain  : 组织/成员查询 API → 组织事实查询服务 → OrganizationUnit/MemberOrgAssignment
@Description: 统一解释组织子树、当前有效归属和大组织统计，供后续权限与知识范围复用。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import distinct, func, or_
from sqlmodel import Session, select

from app.db.models import MemberOrgAssignment, OrganizationUnit, utc_now
from app.organization.units import OrganizationUnitError, get_tenant_organization_unit


@dataclass(frozen=True)
class OrganizationMemberCounts:
    """承载指定组织直属和整棵子树的去重当前成员数。"""

    direct: int
    subtree: int


def resolve_organization_subtree_ids(
    db: Session,
    *,
    tenant_id: str,
    root_org_unit_id: str,
    include_descendants: bool,
) -> list[str]:
    """在租户边界内解析根组织或活动子树，禁止调用方自行复制路径算法。"""

    root = get_tenant_organization_unit(db, tenant_id, root_org_unit_id)
    if root.status != "active":
        raise OrganizationUnitError("ORGANIZATION_NOT_FOUND")
    if not include_descendants:
        return [root.id]
    rows = db.exec(
        select(OrganizationUnit.id)
        .where(
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.status == "active",
            or_(
                OrganizationUnit.tree_path == root.tree_path,
                OrganizationUnit.tree_path.startswith(f"{root.tree_path}/"),
            ),
        )
        .order_by(
            OrganizationUnit.depth,
            OrganizationUnit.sort_order,
            OrganizationUnit.name,
            OrganizationUnit.id,
        )
    ).all()
    return list(rows)


def current_assignment_predicates() -> tuple[object, ...]:
    """返回组织归属当前有效区间的统一 SQL 条件，避免统计与分页口径漂移。"""

    now = utc_now()
    return (
        MemberOrgAssignment.status == "active",
        MemberOrgAssignment.effective_from <= now,
        or_(
            MemberOrgAssignment.effective_until.is_(None),
            MemberOrgAssignment.effective_until > now,
        ),
    )


def count_current_organization_members(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
) -> OrganizationMemberCounts:
    """按有效组织归属去重统计直属人数和包含下级人数。"""

    subtree_ids = resolve_organization_subtree_ids(
        db,
        tenant_id=tenant_id,
        root_org_unit_id=org_unit_id,
        include_descendants=True,
    )
    active_conditions = current_assignment_predicates()
    direct = db.exec(
        select(func.count(distinct(MemberOrgAssignment.employee_profile_id))).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.org_unit_id == org_unit_id,
            *active_conditions,
        )
    ).one()
    subtree = db.exec(
        select(func.count(distinct(MemberOrgAssignment.employee_profile_id))).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.org_unit_id.in_(subtree_ids),
            *active_conditions,
        )
    ).one()
    return OrganizationMemberCounts(direct=int(direct), subtree=int(subtree))


def require_organization_subtree_ids(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str | None,
    include_descendants: bool,
) -> list[str] | None:
    """为可选组织筛选解析统一子树，未传组织时保持无范围过滤。"""

    if org_unit_id is None:
        return None
    try:
        return resolve_organization_subtree_ids(
            db,
            tenant_id=tenant_id,
            root_org_unit_id=org_unit_id,
            include_descendants=include_descendants,
        )
    except OrganizationUnitError:
        raise
