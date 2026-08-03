"""
@Time       : 2026/07/28 15:10
@Author     : zhanglp8181
@File       : leaders.py
@CallChain  : 负责人管理 API/成员生命周期 → 负责人命令 → OrganizationLeaderAssignment
@Description: 维护组织负责人当前态和不可覆盖的有效期历史，不产生角色、权限或流程候选。
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.db.models import (
    EmployeeProfile,
    MemberOrgAssignment,
    OrganizationLeaderAssignment,
    OrganizationUnit,
    Position,
    PositionAssignment,
    utc_now,
)
from app.organization.reference_data import (
    ReferenceDataError,
    require_active_organization_leader_type,
)


class OrganizationLeaderError(ValueError):
    """表示负责人命令违反租户、有效任职、唯一主要负责人或时间区间约束。"""


def create_organization_leader(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
    employee_profile_id: str,
    leader_type_code: str,
    actor_user_id: str,
    position_assignment_id: str | None = None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> OrganizationLeaderAssignment:
    """创建立即生效的负责人关系，重复相同命令返回原活动记录。"""

    started_at = effective_from or utc_now()
    now = utc_now()
    if started_at > now:
        raise OrganizationLeaderError("FUTURE_LEADER_ASSIGNMENT_NOT_SUPPORTED")
    if effective_until is not None and effective_until <= started_at:
        raise OrganizationLeaderError("INVALID_LEADER_INTERVAL")
    normalized_type = leader_type_code.strip()
    if normalized_type == "acting" and effective_until is None:
        raise OrganizationLeaderError("ACTING_LEADER_END_REQUIRED")

    unit = db.exec(
        select(OrganizationUnit)
        .where(
            OrganizationUnit.id == org_unit_id,
            OrganizationUnit.tenant_id == tenant_id,
        )
        .with_for_update()
    ).first()
    if unit is None:
        raise OrganizationLeaderError("ORGANIZATION_NOT_FOUND")
    if unit.status != "active":
        raise OrganizationLeaderError("ORGANIZATION_INACTIVE")
    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.id == employee_profile_id,
            EmployeeProfile.tenant_id == tenant_id,
        )
    ).first()
    if profile is None:
        raise OrganizationLeaderError("EMPLOYEE_PROFILE_NOT_FOUND")
    if profile.status != "active":
        raise OrganizationLeaderError("EMPLOYEE_PROFILE_INACTIVE")
    org_assignment = db.exec(
        select(MemberOrgAssignment).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.employee_profile_id == employee_profile_id,
            MemberOrgAssignment.org_unit_id == org_unit_id,
            MemberOrgAssignment.status == "active",
        )
    ).first()
    if org_assignment is None:
        raise OrganizationLeaderError("ACTIVE_ORG_ASSIGNMENT_REQUIRED")
    try:
        require_active_organization_leader_type(db, tenant_id, normalized_type)
    except ReferenceDataError as error:
        raise OrganizationLeaderError(str(error)) from error

    position_assignment = None
    if position_assignment_id:
        position_assignment = _validated_position_assignment(
            db,
            tenant_id=tenant_id,
            org_unit_id=org_unit_id,
            employee_profile_id=employee_profile_id,
            position_assignment_id=position_assignment_id,
        )

    active_rows = db.exec(
        select(OrganizationLeaderAssignment).where(
            OrganizationLeaderAssignment.tenant_id == tenant_id,
            OrganizationLeaderAssignment.org_unit_id == org_unit_id,
            OrganizationLeaderAssignment.status == "active",
        )
    ).all()
    current_rows: list[OrganizationLeaderAssignment] = []
    for active_row in active_rows:
        if active_row.effective_until is not None and active_row.effective_until <= now:
            active_row.status = "inactive"
            active_row.updated_at = now
            db.add(active_row)
        else:
            current_rows.append(active_row)
    same = next(
        (
            row
            for row in current_rows
            if row.employee_profile_id == employee_profile_id
            and row.leader_type_code == normalized_type
            and row.position_assignment_id
            == (position_assignment.id if position_assignment else None)
        ),
        None,
    )
    if same is not None:
        return same
    if normalized_type == "primary" and any(
        row.leader_type_code == "primary" for row in current_rows
    ):
        raise OrganizationLeaderError("PRIMARY_LEADER_EXISTS")

    row = OrganizationLeaderAssignment(
        tenant_id=tenant_id,
        org_unit_id=org_unit_id,
        employee_profile_id=employee_profile_id,
        position_assignment_id=position_assignment.id if position_assignment else None,
        leader_type_code=normalized_type,
        effective_from=started_at,
        effective_until=effective_until,
        source_kind="manual",
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def end_organization_leader(
    db: Session,
    *,
    tenant_id: str,
    assignment_id: str,
    effective_until: datetime | None = None,
) -> OrganizationLeaderAssignment:
    """结束负责人关系并保留历史，重复结束保持原结束时间。"""

    row = db.exec(
        select(OrganizationLeaderAssignment).where(
            OrganizationLeaderAssignment.id == assignment_id,
            OrganizationLeaderAssignment.tenant_id == tenant_id,
        )
    ).first()
    if row is None:
        raise OrganizationLeaderError("LEADER_ASSIGNMENT_NOT_FOUND")
    if row.status != "active":
        return row
    _close_leader(row, effective_until or utc_now())
    db.add(row)
    db.flush()
    return row


def close_leaders_for_org_assignment(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    org_unit_id: str,
    effective_at: datetime,
) -> None:
    """组织归属失效时关闭同成员同组织的活动负责人关系。"""

    rows = db.exec(
        select(OrganizationLeaderAssignment).where(
            OrganizationLeaderAssignment.tenant_id == tenant_id,
            OrganizationLeaderAssignment.employee_profile_id == employee_profile_id,
            OrganizationLeaderAssignment.org_unit_id == org_unit_id,
            OrganizationLeaderAssignment.status == "active",
        )
    ).all()
    for row in rows:
        _close_leader(row, effective_at)
        db.add(row)
    db.flush()


def close_leaders_for_position_assignment(
    db: Session,
    *,
    tenant_id: str,
    position_assignment_id: str,
    effective_at: datetime,
) -> None:
    """显式关联的岗位任职失效时关闭对应负责人关系。"""

    rows = db.exec(
        select(OrganizationLeaderAssignment).where(
            OrganizationLeaderAssignment.tenant_id == tenant_id,
            OrganizationLeaderAssignment.position_assignment_id
            == position_assignment_id,
            OrganizationLeaderAssignment.status == "active",
        )
    ).all()
    for row in rows:
        _close_leader(row, effective_at)
        db.add(row)
    db.flush()


def close_leaders_for_member(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    effective_at: datetime,
) -> None:
    """成员停用或离职时关闭其全部活动负责人关系。"""

    rows = db.exec(
        select(OrganizationLeaderAssignment).where(
            OrganizationLeaderAssignment.tenant_id == tenant_id,
            OrganizationLeaderAssignment.employee_profile_id == employee_profile_id,
            OrganizationLeaderAssignment.status == "active",
        )
    ).all()
    for row in rows:
        _close_leader(row, effective_at)
        db.add(row)
    db.flush()


def _validated_position_assignment(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
    employee_profile_id: str,
    position_assignment_id: str,
) -> PositionAssignment:
    """校验可选岗位任职活动、同成员且岗位属于负责人组织。"""

    assignment = db.exec(
        select(PositionAssignment).where(
            PositionAssignment.id == position_assignment_id,
            PositionAssignment.tenant_id == tenant_id,
        )
    ).first()
    if assignment is None:
        raise OrganizationLeaderError("POSITION_ASSIGNMENT_NOT_FOUND")
    if (
        assignment.status != "active"
        or assignment.employee_profile_id != employee_profile_id
    ):
        raise OrganizationLeaderError("POSITION_ASSIGNMENT_MISMATCH")
    position = db.exec(
        select(Position).where(
            Position.id == assignment.position_id,
            Position.tenant_id == tenant_id,
        )
    ).first()
    if position is None or position.org_unit_id != org_unit_id:
        raise OrganizationLeaderError("POSITION_ASSIGNMENT_ORG_MISMATCH")
    return assignment


def _close_leader(row: OrganizationLeaderAssignment, effective_at: datetime) -> None:
    """关闭活动负责人区间并拒绝结束时间早于开始时间。"""

    if effective_at < row.effective_from:
        raise OrganizationLeaderError("INVALID_LEADER_INTERVAL")
    if row.effective_until is None or effective_at < row.effective_until:
        row.effective_until = effective_at
    row.status = "inactive"
    row.updated_at = utc_now()
