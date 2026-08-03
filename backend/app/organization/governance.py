"""
@Time       : 2026/07/28 19:20
@Author     : zhanglp8181
@File       : governance.py
@CallChain  : 管理 API/企业上下文 → 治理授权解析 → 角色任职/岗位任职/唯一组织子树服务
@Description: 解析可解释的治理权限和组织范围，并保持平台管理员兼容身份与业务权限严格分离。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlmodel import Session, select

from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Position,
    PositionAssignment,
    PositionRoleBinding,
    User,
    utc_now,
)
from app.organization.permissions import (
    ensure_builtin_permission_catalog,
    ensure_builtin_role_categories,
    role_permission_codes,
    sync_role_permissions,
)
from app.organization.query import resolve_organization_subtree_ids
from app.organization.units import OrganizationUnitError
from app.security.auth import ensure_current_user_tenant


GOVERNANCE_PERMISSION_CODES = frozenset(
    {
        "tenant.settings.manage",
        "member.read",
        "member.manage",
        "organization.read",
        "organization.manage",
        "position.read",
        "position.manage",
        "reference_data.read",
        "reference_data.manage",
        "authorization.read",
        "authorization.manage",
        "agent.read",
        "agent.manage",
        "knowledge.read",
        "knowledge.manage",
        "audit.read",
    }
)

BUILTIN_GOVERNANCE_ROLES: dict[str, tuple[str, frozenset[str]]] = {
    "governance_tenant_owner": ("租户所有者", GOVERNANCE_PERMISSION_CODES),
    "governance_tenant_admin": ("租户管理员", GOVERNANCE_PERMISSION_CODES),
    "governance_org_admin": (
        "组织管理员",
        frozenset(
            {
                "member.read",
                "member.manage",
                "organization.read",
                "organization.manage",
                "position.read",
                "position.manage",
                "authorization.read",
            }
        ),
    ),
    "governance_agent_admin": (
        "数字员工管理员",
        frozenset({"agent.read", "agent.manage", "authorization.read"}),
    ),
    "governance_knowledge_admin": (
        "知识管理员",
        frozenset({"knowledge.read", "knowledge.manage", "authorization.read"}),
    ),
    "governance_auditor": (
        "审计员",
        frozenset({"audit.read", "authorization.read"}),
    ),
}


@dataclass(frozen=True, slots=True)
class OrganizationScope:
    """表示租户级或已由唯一子树服务展开的组织授权范围。"""

    scope_type: str
    scope_id: str
    include_descendants: bool
    organization_unit_ids: frozenset[str] | None


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """描述一次有效权限的来源、组织范围和有效期，供判断与解释共同消费。"""

    permission_code: str
    role_id: str | None
    role_code: str
    role_name: str
    source_kind: str
    source_id: str | None
    scope: OrganizationScope
    effective_from: datetime | None
    effective_until: datetime | None
    granted_by_user_id: str | None


def ensure_builtin_governance_catalog(db: Session, tenant_id: str) -> None:
    """幂等创建治理权限与内置治理角色，不给任何成员隐式写入业务任职。"""

    ensure_builtin_role_categories(db, tenant_id)
    ensure_builtin_permission_catalog(db, tenant_id)
    existing = db.exec(
        select(BusinessRole).where(BusinessRole.tenant_id == tenant_id)
    ).all()
    by_code = {role.role_code: role for role in existing}
    now = utc_now()
    for role_code, (name, permission_codes) in BUILTIN_GOVERNANCE_ROLES.items():
        role = by_code.get(role_code)
        if role is None:
            role = BusinessRole(
                tenant_id=tenant_id,
                role_code=role_code,
                name=name,
                role_kind="governance",
                category="governance",
                metadata_json={"source": "builtin_governance_catalog"},
                created_at=now,
                updated_at=now,
            )
            db.add(role)
            db.flush()
        elif role.role_kind != "governance":
            raise ValueError(f"GOVERNANCE_ROLE_CODE_CONFLICT:{role_code}")
        else:
            role.name = name
            role.category = "governance"
            role.status = "active"
            role.updated_at = now
            db.add(role)
        sync_role_permissions(db, role=role, permission_codes=permission_codes)


def validate_role_assignment_scope(
    db: Session,
    *,
    tenant_id: str,
    scope_type: str,
    scope_id: str,
    include_descendants: bool,
) -> OrganizationScope:
    """校验并规范化租户或组织授权范围，拒绝自由字符串和跨租户组织。"""

    if scope_type == "tenant":
        if scope_id != "*" or not include_descendants:
            raise ValueError("INVALID_TENANT_ROLE_SCOPE")
        return OrganizationScope(
            scope_type="tenant",
            scope_id="*",
            include_descendants=True,
            organization_unit_ids=None,
        )
    if scope_type != "org_unit" or not scope_id or scope_id == "*":
        raise ValueError("INVALID_ROLE_SCOPE_TYPE")
    try:
        organization_ids = resolve_organization_subtree_ids(
            db,
            tenant_id=tenant_id,
            root_org_unit_id=scope_id,
            include_descendants=include_descendants,
        )
    except OrganizationUnitError as error:
        raise ValueError("INVALID_ORGANIZATION_ROLE_SCOPE") from error
    return OrganizationScope(
        scope_type="org_unit",
        scope_id=scope_id,
        include_descendants=include_descendants,
        organization_unit_ids=frozenset(organization_ids),
    )


def resolve_organization_scope(
    db: Session,
    *,
    tenant_id: str,
    scope_type: str,
    scope_id: str,
    include_descendants: bool,
) -> OrganizationScope:
    """通过 M2.5-B 唯一子树服务解释已保存的结构化组织范围。"""

    return validate_role_assignment_scope(
        db,
        tenant_id=tenant_id,
        scope_type=scope_type,
        scope_id=scope_id,
        include_descendants=include_descendants,
    )


def resolve_permission_grants(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    at: datetime | None = None,
) -> list[PermissionGrant]:
    """合并平台管理员兼容授权、直接治理角色和岗位治理角色并返回可解释结果。"""

    effective_at = at or utc_now()
    user = db.exec(
        select(User).where(User.id == user_id, User.tenant_id == tenant_id)
    ).first()
    if user is None or user.membership_status != "active":
        return []
    grants: list[PermissionGrant] = []
    if user.role == "admin":
        tenant_scope = OrganizationScope(
            scope_type="tenant",
            scope_id="*",
            include_descendants=True,
            organization_unit_ids=None,
        )
        grants.extend(
            PermissionGrant(
                permission_code=permission_code,
                role_id=None,
                role_code="governance_tenant_owner",
                role_name="租户所有者（兼容管理员）",
                source_kind="platform_admin_compat",
                source_id=user.id,
                scope=tenant_scope,
                effective_from=user.joined_at,
                effective_until=user.left_at,
                granted_by_user_id=None,
            )
            for permission_code in sorted(GOVERNANCE_PERMISSION_CODES)
        )
    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == user_id,
            EmployeeProfile.status == "active",
        )
    ).first()
    if profile is None:
        return _deduplicate_grants(grants)
    roles = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == tenant_id,
            BusinessRole.role_kind == "governance",
            BusinessRole.status == "active",
        )
    ).all()
    roles_by_id = {role.id: role for role in roles}
    direct_assignments = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == tenant_id,
            EmployeeRoleAssignment.employee_profile_id == profile.id,
            EmployeeRoleAssignment.status == "active",
        )
    ).all()
    for assignment in direct_assignments:
        role = roles_by_id.get(assignment.business_role_id)
        if role is None or not _is_effective(
            assignment.effective_from,
            assignment.effective_until,
            effective_at,
        ):
            continue
        scope = _safe_resolve_scope(
            db,
            tenant_id=tenant_id,
            scope_type=assignment.scope_type,
            scope_id=assignment.scope_id,
            include_descendants=assignment.include_descendants,
        )
        if scope is None:
            continue
        grants.extend(
            _role_grants(
                db,
                role=role,
                source_kind="direct_role",
                source_id=assignment.id,
                scope=scope,
                effective_from=assignment.effective_from,
                effective_until=assignment.effective_until,
                granted_by_user_id=assignment.granted_by_user_id,
            )
        )
    grants.extend(
        _position_permission_grants(
            db,
            tenant_id=tenant_id,
            employee_profile_id=profile.id,
            roles_by_id=roles_by_id,
            effective_at=effective_at,
        )
    )
    return _deduplicate_grants(grants)


def governance_permission_codes(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> list[str]:
    """返回当前成员有效治理权限编码，供认证上下文和导航使用。"""

    return sorted(
        {
            grant.permission_code
            for grant in resolve_permission_grants(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        }
    )


def authorized_organization_ids(
    grants: list[PermissionGrant],
    *,
    permission_code: str,
) -> frozenset[str] | None:
    """合并指定权限的组织范围；返回 None 表示拥有租户全范围。"""

    matching = [grant for grant in grants if grant.permission_code == permission_code]
    if any(grant.scope.organization_unit_ids is None for grant in matching):
        return None
    organization_ids: set[str] = set()
    for grant in matching:
        organization_ids.update(grant.scope.organization_unit_ids or ())
    return frozenset(organization_ids)


def has_governance_permission(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    permission_code: str,
    target_org_unit_id: str | None = None,
) -> bool:
    """使用结构化 grant 判断治理权限，并在有目标组织时应用范围交集。"""

    if permission_code not in GOVERNANCE_PERMISSION_CODES:
        return False
    grants = [
        grant
        for grant in resolve_permission_grants(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if grant.permission_code == permission_code
    ]
    if target_org_unit_id is None:
        return bool(grants)
    return any(
        grant.scope.organization_unit_ids is None
        or target_org_unit_id in grant.scope.organization_unit_ids
        for grant in grants
    )


def ensure_governance_permission(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    permission_code: str,
    target_org_unit_id: str | None = None,
) -> User:
    """校验租户边界和治理权限，作为旧硬编码管理员判断的统一替代入口。"""

    ensure_current_user_tenant(tenant_id, current_user)
    if not has_governance_permission(
        db,
        tenant_id=tenant_id,
        user_id=current_user.id,
        permission_code=permission_code,
        target_org_unit_id=target_org_unit_id,
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "GOVERNANCE_PERMISSION_DENIED",
                "permission": permission_code,
                "target_org_unit_id": target_org_unit_id,
            },
        )
    return current_user


def _position_permission_grants(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    roles_by_id: dict[str, BusinessRole],
    effective_at: datetime,
) -> list[PermissionGrant]:
    """解析有效岗位任职带入的治理角色，并以岗位所属组织生成结构化范围。"""

    assignments = db.exec(
        select(PositionAssignment).where(
            PositionAssignment.tenant_id == tenant_id,
            PositionAssignment.employee_profile_id == employee_profile_id,
            PositionAssignment.status == "active",
        )
    ).all()
    assignments = [
        row
        for row in assignments
        if _is_effective(row.effective_from, row.effective_until, effective_at)
    ]
    if not assignments:
        return []
    positions = db.exec(
        select(Position).where(
            Position.tenant_id == tenant_id,
            Position.id.in_({row.position_id for row in assignments}),
            Position.status == "active",
        )
    ).all()
    positions_by_id = {position.id: position for position in positions}
    bindings = db.exec(
        select(PositionRoleBinding).where(
            PositionRoleBinding.tenant_id == tenant_id,
            PositionRoleBinding.position_id.in_(set(positions_by_id)),
            PositionRoleBinding.status == "active",
        )
    ).all()
    assignment_by_position = {row.position_id: row for row in assignments}
    grants: list[PermissionGrant] = []
    for binding in bindings:
        role = roles_by_id.get(binding.business_role_id)
        position = positions_by_id.get(binding.position_id)
        assignment = assignment_by_position.get(binding.position_id)
        if role is None or position is None or assignment is None:
            continue
        if not _is_effective(
            binding.effective_from,
            binding.effective_until,
            effective_at,
        ):
            continue
        if binding.scope_mode == "tenant":
            scope_type, scope_id, include_descendants = "tenant", "*", True
        elif binding.scope_mode in {"position_org", "position_org_subtree"}:
            scope_type = "org_unit"
            scope_id = position.org_unit_id
            include_descendants = binding.scope_mode == "position_org_subtree"
        else:
            continue
        scope = _safe_resolve_scope(
            db,
            tenant_id=tenant_id,
            scope_type=scope_type,
            scope_id=scope_id,
            include_descendants=include_descendants,
        )
        if scope is None:
            continue
        grants.extend(
            _role_grants(
                db,
                role=role,
                source_kind="position_role",
                source_id=binding.id,
                scope=scope,
                effective_from=assignment.effective_from,
                effective_until=assignment.effective_until,
                granted_by_user_id=None,
            )
        )
    return grants


def _role_grants(
    db: Session,
    *,
    role: BusinessRole,
    source_kind: str,
    source_id: str,
    scope: OrganizationScope,
    effective_from: datetime | None,
    effective_until: datetime | None,
    granted_by_user_id: str | None,
) -> list[PermissionGrant]:
    """把治理角色的规范权限关系展开为可解释 grant。"""

    return [
        PermissionGrant(
            permission_code=permission_code,
            role_id=role.id,
            role_code=role.role_code,
            role_name=role.name,
            source_kind=source_kind,
            source_id=source_id,
            scope=scope,
            effective_from=effective_from,
            effective_until=effective_until,
            granted_by_user_id=granted_by_user_id,
        )
        for permission_code in role_permission_codes(db, role)
        if permission_code in GOVERNANCE_PERMISSION_CODES
    ]


def _safe_resolve_scope(
    db: Session,
    *,
    tenant_id: str,
    scope_type: str,
    scope_id: str,
    include_descendants: bool,
) -> OrganizationScope | None:
    """忽略历史脏范围，使非法授权默认不生效而不是放宽到租户级。"""

    try:
        return resolve_organization_scope(
            db,
            tenant_id=tenant_id,
            scope_type=scope_type,
            scope_id=scope_id,
            include_descendants=include_descendants,
        )
    except ValueError:
        return None


def _is_effective(
    effective_from: datetime | None,
    effective_until: datetime | None,
    effective_at: datetime,
) -> bool:
    """判断授权或岗位任期是否覆盖指定时点。"""

    if effective_from is not None and effective_from > effective_at:
        return False
    return effective_until is None or effective_until > effective_at


def _deduplicate_grants(grants: list[PermissionGrant]) -> list[PermissionGrant]:
    """按权限、来源和范围稳定去重，同时保留不同授权路径用于解释。"""

    unique: dict[tuple[object, ...], PermissionGrant] = {}
    for grant in grants:
        key = (
            grant.permission_code,
            grant.role_code,
            grant.source_kind,
            grant.source_id,
            grant.scope.scope_type,
            grant.scope.scope_id,
            grant.scope.include_descendants,
        )
        unique[key] = grant
    return sorted(
        unique.values(),
        key=lambda grant: (
            grant.permission_code,
            grant.role_code,
            grant.source_kind,
            grant.scope.scope_type,
            grant.scope.scope_id,
        ),
    )
