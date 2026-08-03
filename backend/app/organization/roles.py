"""
@Time       : 2026/07/22 18:45
@Author     : zhanglp8181
@File       : roles.py
@CallChain  : 账号管理/SOP Runtime/Seed → EmployeeProfile/BusinessRole/Assignment
@Description: 管理独立于平台 admin/member 的公司业务角色、有效任职和能力解析。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlmodel import Session, select

from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Position,
    PositionAssignment,
    PositionRoleBinding,
    utc_now,
)
from app.organization.query import resolve_organization_subtree_ids


def bind_position_business_role(
    db: Session,
    *,
    tenant_id: str,
    position_id: str,
    business_role_id: str,
    granted_by_user_id: str | None = None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> PositionRoleBinding:
    """幂等绑定活动岗位与角色，并保存授予人和可审计有效区间。"""

    now = utc_now()
    starts_at = effective_from or now
    if effective_until is not None and effective_until <= starts_at:
        raise ValueError("INVALID_POSITION_ROLE_EFFECTIVE_RANGE")

    position = db.exec(
        select(Position).where(
            Position.id == position_id,
            Position.tenant_id == tenant_id,
            Position.status == "active",
        )
    ).first()
    if position is None:
        raise ValueError("POSITION_NOT_FOUND")
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.id == business_role_id,
            BusinessRole.tenant_id == tenant_id,
            BusinessRole.status == "active",
        )
    ).first()
    if role is None:
        raise ValueError("BUSINESS_ROLE_NOT_FOUND")
    existing = db.exec(
        select(PositionRoleBinding).where(
            PositionRoleBinding.tenant_id == tenant_id,
            PositionRoleBinding.position_id == position_id,
            PositionRoleBinding.business_role_id == business_role_id,
        )
    ).first()
    if existing is not None:
        if existing.status == "active":
            return existing
        existing.status = "active"
        existing.scope_mode = "position_org"
        existing.granted_by_user_id = granted_by_user_id
        existing.effective_from = starts_at
        existing.effective_until = effective_until
        existing.updated_at = now
        db.add(existing)
        db.flush()
        return existing
    binding = PositionRoleBinding(
        tenant_id=tenant_id,
        position_id=position_id,
        business_role_id=business_role_id,
        scope_mode="position_org",
        granted_by_user_id=granted_by_user_id,
        effective_from=starts_at,
        effective_until=effective_until,
    )
    db.add(binding)
    db.flush()
    return binding


def deactivate_position_role_binding(
    db: Session,
    *,
    tenant_id: str,
    binding_id: str,
) -> PositionRoleBinding:
    """停用岗位默认角色绑定，不删除其治理历史。"""

    binding = db.exec(
        select(PositionRoleBinding).where(
            PositionRoleBinding.id == binding_id,
            PositionRoleBinding.tenant_id == tenant_id,
        )
    ).first()
    if binding is None:
        raise ValueError("POSITION_ROLE_BINDING_NOT_FOUND")
    binding.status = "inactive"
    binding.updated_at = utc_now()
    db.add(binding)
    db.flush()
    return binding


def active_business_roles(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    at: datetime | None = None,
    organization_unit_ids: set[str] | None = None,
) -> list[BusinessRole]:
    """按统一时点和可选组织上下文解析员工业务角色，不读取平台管理员角色。"""

    effective_at = at or utc_now()
    roles = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == tenant_id,
            BusinessRole.role_kind == "business",
            BusinessRole.status == "active",
        )
    ).all()
    sources = role_source_codes(
        db,
        tenant_id=tenant_id,
        employee_profile_id=employee_profile_id,
        role_ids={role.id for role in roles},
        at=effective_at,
        organization_unit_ids=organization_unit_ids,
    )
    return sorted(
        (role for role in roles if role.id in sources),
        key=lambda role: role.role_code,
    )


def active_business_role_codes(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
) -> list[str]:
    """返回员工当前有效的稳定业务角色编码。"""

    return [
        role.role_code
        for role in active_business_roles(
            db,
            tenant_id=tenant_id,
            employee_profile_id=employee_profile_id,
        )
    ]


def active_business_role_sources(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
) -> dict[str, list[str]]:
    """按角色编码返回当前有效来源，供 API 区分直接授权与岗位带入。"""

    roles = active_business_roles(
        db,
        tenant_id=tenant_id,
        employee_profile_id=employee_profile_id,
    )
    sources_by_role_id = role_source_codes(
        db,
        tenant_id=tenant_id,
        employee_profile_id=employee_profile_id,
        role_ids={role.id for role in roles},
    )
    return {role.role_code: sorted(sources_by_role_id.get(role.id, set())) for role in roles}


def replace_employee_business_roles(
    db: Session,
    *,
    profile: EmployeeProfile,
    role_codes: Iterable[str],
    source: str,
) -> list[str]:
    """以租户级作用域替换员工任职，并拒绝不存在或停用的业务角色。"""

    normalized_codes = sorted({code.strip() for code in role_codes if code.strip()})
    roles = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == profile.tenant_id,
            BusinessRole.role_kind == "business",
            BusinessRole.status == "active",
        )
    ).all()
    roles_by_code = {role.role_code: role for role in roles}
    unknown_codes = [code for code in normalized_codes if code not in roles_by_code]
    if unknown_codes:
        raise ValueError(f"UNKNOWN_BUSINESS_ROLES:{','.join(unknown_codes)}")

    existing = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == profile.tenant_id,
            EmployeeRoleAssignment.employee_profile_id == profile.id,
            EmployeeRoleAssignment.scope_type == "tenant",
            EmployeeRoleAssignment.scope_id == "*",
        )
    ).all()
    existing_by_role_id = {assignment.business_role_id: assignment for assignment in existing}
    desired_role_ids = {roles_by_code[code].id for code in normalized_codes}
    now = utc_now()
    for assignment in existing:
        assignment.status = (
            "active" if assignment.business_role_id in desired_role_ids else "inactive"
        )
        assignment.updated_at = now
        db.add(assignment)
    for code in normalized_codes:
        role = roles_by_code[code]
        if role.id in existing_by_role_id:
            continue
        db.add(
            EmployeeRoleAssignment(
                tenant_id=profile.tenant_id,
                employee_profile_id=profile.id,
                business_role_id=role.id,
                scope_type="tenant",
                scope_id="*",
                include_descendants=True,
                status="active",
                effective_from=now,
                metadata_json={"source": source},
            )
        )
    db.flush()
    return normalized_codes


def _assignment_is_effective(assignment: EmployeeRoleAssignment, effective_at: datetime) -> bool:
    """判断任职的起止时间是否覆盖当前授权时点。"""

    if assignment.effective_from and assignment.effective_from > effective_at:
        return False
    return not assignment.effective_until or assignment.effective_until > effective_at


def _position_assignment_is_effective(
    assignment: PositionAssignment, effective_at: datetime
) -> bool:
    """判断岗位任职区间是否覆盖当前角色解析时点。"""

    if assignment.effective_from > effective_at:
        return False
    return not assignment.effective_until or assignment.effective_until > effective_at


def role_source_codes(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    role_ids: set[str],
    at: datetime | None = None,
    organization_unit_ids: set[str] | None = None,
) -> dict[str, set[str]]:
    """返回指定角色的有效来源，可把岗位来源限制在已解析组织范围。"""

    if not role_ids:
        return {}
    effective_at = at or utc_now()
    result: dict[str, set[str]] = {}
    direct_rows = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == tenant_id,
            EmployeeRoleAssignment.employee_profile_id == employee_profile_id,
            EmployeeRoleAssignment.business_role_id.in_(role_ids),
            EmployeeRoleAssignment.status == "active",
        )
    ).all()
    for row in direct_rows:
        if not _assignment_is_effective(row, effective_at):
            continue
        if row.scope_type == "tenant" and row.scope_id == "*":
            result.setdefault(row.business_role_id, set()).add("business_role")
            continue
        if row.scope_type != "org_unit" or not row.scope_id or row.scope_id == "*":
            continue
        try:
            assignment_org_ids = set(
                resolve_organization_subtree_ids(
                    db,
                    tenant_id=tenant_id,
                    root_org_unit_id=row.scope_id,
                    include_descendants=row.include_descendants,
                )
            )
        except ValueError:
            continue
        if organization_unit_ids is None or assignment_org_ids & organization_unit_ids:
            result.setdefault(row.business_role_id, set()).add("business_role")

    position_rows = db.exec(
        select(PositionAssignment).where(
            PositionAssignment.tenant_id == tenant_id,
            PositionAssignment.employee_profile_id == employee_profile_id,
            PositionAssignment.status == "active",
        )
    ).all()
    position_ids = {
        row.position_id
        for row in position_rows
        if _position_assignment_is_effective(row, effective_at)
    }
    if position_ids:
        positions = db.exec(
            select(Position).where(
                Position.tenant_id == tenant_id,
                Position.id.in_(position_ids),
                Position.status == "active",
            )
        ).all()
        position_ids = {
            position.id
            for position in positions
            if organization_unit_ids is None or position.org_unit_id in organization_unit_ids
        }
    if position_ids:
        bindings = db.exec(
            select(PositionRoleBinding).where(
                PositionRoleBinding.tenant_id == tenant_id,
                PositionRoleBinding.position_id.in_(position_ids),
                PositionRoleBinding.business_role_id.in_(role_ids),
                PositionRoleBinding.scope_mode == "position_org",
                PositionRoleBinding.status == "active",
            )
        ).all()
        for row in bindings:
            if _binding_is_effective(row, effective_at):
                result.setdefault(row.business_role_id, set()).add("position_role")
    return result


def _binding_is_effective(
    binding: PositionRoleBinding,
    effective_at: datetime,
) -> bool:
    """判断岗位默认角色绑定的有效区间是否覆盖指定解析时点。"""

    if binding.effective_from is not None and binding.effective_from > effective_at:
        return False
    return binding.effective_until is None or binding.effective_until > effective_at
