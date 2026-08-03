"""
@Time       : 2026/07/28 15:20
@Author     : zhanglp8181
@File       : assignments.py
@CallChain  : 组织/岗位管理 API → 任职命令 → EmployeeProfile/OrganizationUnit/Position
@Description: 维护成员组织归属、岗位目录和不可覆盖的岗位任职历史。
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.db.models import (
    EmployeeProfile,
    MemberOrgAssignment,
    OrganizationUnit,
    Position,
    PositionAssignment,
    utc_now,
)
from app.organization.reference_data import (
    ensure_position_type_catalog,
    require_active_position_type,
)
from app.organization.leaders import (
    close_leaders_for_member,
    close_leaders_for_org_assignment,
    close_leaders_for_position_assignment,
)


ORG_ASSIGNMENT_TYPES = {"primary", "concurrent", "temporary", "project"}
POSITION_ASSIGNMENT_TYPES = {"primary", "concurrent", "acting", "temporary"}


class OrganizationAssignmentError(ValueError):
    """表示组织归属或岗位任职命令违反租户、活动状态或历史区间约束。"""


def ensure_assignment_foundation(db: Session, tenant_id: str) -> None:
    """幂等初始化岗位类型码表，不创建业务岗位。"""

    ensure_position_type_catalog(db, tenant_id)


def assign_member_to_organization(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    org_unit_id: str,
    assignment_type: str = "primary",
    effective_from: datetime | None = None,
) -> MemberOrgAssignment:
    """追加一段立即生效的组织归属，主归属调动时关闭原活动区间。"""

    effective_at = effective_from or utc_now()
    _require_not_future(effective_at)
    profile = _locked_active_profile(db, tenant_id, employee_profile_id)
    unit = _active_unit(db, tenant_id, org_unit_id)
    normalized_type = assignment_type.strip()
    if normalized_type not in ORG_ASSIGNMENT_TYPES:
        raise OrganizationAssignmentError("INVALID_ORG_ASSIGNMENT_TYPE")
    is_primary = normalized_type == "primary"
    current_rows = _active_org_assignments(db, tenant_id, profile.id)
    same = next(
        (
            row
            for row in current_rows
            if row.org_unit_id == unit.id
            and row.assignment_type == normalized_type
            and row.is_primary == is_primary
        ),
        None,
    )
    if same is not None:
        return same
    if is_primary:
        for row in current_rows:
            if row.is_primary:
                _close_org_assignment(row, effective_at)
                db.add(row)
                close_leaders_for_org_assignment(
                    db,
                    tenant_id=tenant_id,
                    employee_profile_id=profile.id,
                    org_unit_id=row.org_unit_id,
                    effective_at=effective_at,
                )

    assignment = MemberOrgAssignment(
        tenant_id=tenant_id,
        employee_profile_id=profile.id,
        org_unit_id=unit.id,
        assignment_type=normalized_type,
        is_primary=is_primary,
        effective_from=effective_at,
    )
    db.add(assignment)
    db.flush()
    _close_unexplained_position_assignments(
        db,
        tenant_id=tenant_id,
        employee_profile_id=profile.id,
        effective_at=effective_at,
    )
    return assignment


def end_member_org_assignment(
    db: Session,
    *,
    tenant_id: str,
    assignment_id: str,
    effective_until: datetime | None = None,
) -> MemberOrgAssignment:
    """结束一段活动组织归属并保留历史，不删除旧记录。"""

    assignment = _tenant_org_assignment(db, tenant_id, assignment_id)
    if assignment.status != "active":
        return assignment
    _close_org_assignment(assignment, effective_until or utc_now())
    close_leaders_for_org_assignment(
        db,
        tenant_id=tenant_id,
        employee_profile_id=assignment.employee_profile_id,
        org_unit_id=assignment.org_unit_id,
        effective_at=assignment.effective_until or utc_now(),
    )
    db.add(assignment)
    db.flush()
    _close_unexplained_position_assignments(
        db,
        tenant_id=tenant_id,
        employee_profile_id=assignment.employee_profile_id,
        effective_at=assignment.effective_until or utc_now(),
    )
    return assignment


def create_position(
    db: Session,
    *,
    tenant_id: str,
    org_unit_id: str,
    code: str,
    name: str,
    position_type_code: str,
    reports_to_position_id: str | None = None,
    headcount_limit: int | None = None,
    responsibility: str | None = None,
) -> Position:
    """在活动组织下创建稳定编码岗位，并校验上级岗位属于同一租户。"""

    unit = _active_unit(db, tenant_id, org_unit_id)
    normalized_code = code.strip()
    normalized_name = name.strip()
    if not normalized_code:
        raise OrganizationAssignmentError("POSITION_CODE_REQUIRED")
    if not normalized_name:
        raise OrganizationAssignmentError("POSITION_NAME_REQUIRED")
    if headcount_limit is not None and headcount_limit < 1:
        raise OrganizationAssignmentError("INVALID_HEADCOUNT_LIMIT")
    if db.exec(
        select(Position).where(
            Position.tenant_id == tenant_id,
            Position.code == normalized_code,
        )
    ).first():
        raise OrganizationAssignmentError("POSITION_CODE_EXISTS")
    try:
        require_active_position_type(db, tenant_id, position_type_code)
    except ValueError as error:
        raise OrganizationAssignmentError(str(error)) from error
    manager = (
        _active_position(db, tenant_id, reports_to_position_id)
        if reports_to_position_id
        else None
    )
    position = Position(
        tenant_id=tenant_id,
        org_unit_id=unit.id,
        code=normalized_code,
        name=normalized_name,
        position_type_code=position_type_code.strip(),
        reports_to_position_id=manager.id if manager else None,
        headcount_limit=headcount_limit,
        responsibility=(responsibility or "").strip() or None,
    )
    db.add(position)
    db.flush()
    return position


def update_position(
    db: Session,
    position: Position,
    *,
    org_unit_id: str | None = None,
    name: str | None = None,
    position_type_code: str | None = None,
    reports_to_position_id: str | None = None,
    clear_reports_to: bool = False,
    headcount_limit: int | None = None,
    responsibility: str | None = None,
) -> Position:
    """更新岗位可变资料，拒绝移动仍有活动任职的岗位或形成汇报环。"""

    if org_unit_id is not None and org_unit_id != position.org_unit_id:
        if _active_position_assignments(db, position.tenant_id, position_id=position.id):
            raise OrganizationAssignmentError("ACTIVE_POSITION_ASSIGNMENTS_EXIST")
        position.org_unit_id = _active_unit(
            db, position.tenant_id, org_unit_id
        ).id
    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise OrganizationAssignmentError("POSITION_NAME_REQUIRED")
        position.name = normalized_name
    if position_type_code is not None:
        try:
            require_active_position_type(db, position.tenant_id, position_type_code)
        except ValueError as error:
            raise OrganizationAssignmentError(str(error)) from error
        position.position_type_code = position_type_code.strip()
    if clear_reports_to:
        position.reports_to_position_id = None
    elif reports_to_position_id is not None:
        manager = _active_position(db, position.tenant_id, reports_to_position_id)
        _require_no_position_cycle(db, position, manager)
        position.reports_to_position_id = manager.id
    if headcount_limit is not None:
        if headcount_limit < 1:
            raise OrganizationAssignmentError("INVALID_HEADCOUNT_LIMIT")
        position.headcount_limit = headcount_limit
    if responsibility is not None:
        position.responsibility = responsibility.strip() or None
    position.updated_at = utc_now()
    db.add(position)
    db.flush()
    return position


def deactivate_position(db: Session, position: Position) -> Position:
    """停用没有活动任职或活动下级岗位的岗位。"""

    if _active_position_assignments(db, position.tenant_id, position_id=position.id):
        raise OrganizationAssignmentError("ACTIVE_POSITION_ASSIGNMENTS_EXIST")
    child = db.exec(
        select(Position).where(
            Position.tenant_id == position.tenant_id,
            Position.reports_to_position_id == position.id,
            Position.status == "active",
        )
    ).first()
    if child is not None:
        raise OrganizationAssignmentError("ACTIVE_CHILD_POSITIONS_EXIST")
    position.status = "inactive"
    position.updated_at = utc_now()
    db.add(position)
    db.flush()
    return position


def assign_member_to_position(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    position_id: str,
    assignment_type: str = "primary",
    effective_from: datetime | None = None,
) -> PositionAssignment:
    """追加岗位任职；任职组织必须存在该成员的当前有效组织归属。"""

    effective_at = effective_from or utc_now()
    _require_not_future(effective_at)
    profile = _locked_active_profile(db, tenant_id, employee_profile_id)
    position = _active_position(db, tenant_id, position_id)
    normalized_type = assignment_type.strip()
    if normalized_type not in POSITION_ASSIGNMENT_TYPES:
        raise OrganizationAssignmentError("INVALID_POSITION_ASSIGNMENT_TYPE")
    is_primary = normalized_type == "primary"
    org_rows = _active_org_assignments(db, tenant_id, profile.id)
    matching_org = [row for row in org_rows if row.org_unit_id == position.org_unit_id]
    if not matching_org:
        raise OrganizationAssignmentError("POSITION_ORG_ASSIGNMENT_REQUIRED")
    if is_primary and not any(row.is_primary for row in matching_org):
        raise OrganizationAssignmentError("PRIMARY_POSITION_ORG_MISMATCH")

    current_rows = _active_position_assignments(
        db, tenant_id, employee_profile_id=profile.id
    )
    same = next(
        (
            row
            for row in current_rows
            if row.position_id == position.id
            and row.assignment_type == normalized_type
            and row.is_primary == is_primary
        ),
        None,
    )
    if same is not None:
        return same
    if is_primary:
        for row in current_rows:
            if row.is_primary:
                _close_position_assignment(row, effective_at)
                db.add(row)

    assignment = PositionAssignment(
        tenant_id=tenant_id,
        employee_profile_id=profile.id,
        position_id=position.id,
        assignment_type=normalized_type,
        is_primary=is_primary,
        effective_from=effective_at,
    )
    db.add(assignment)
    db.flush()
    return assignment


def end_position_assignment(
    db: Session,
    *,
    tenant_id: str,
    assignment_id: str,
    effective_until: datetime | None = None,
) -> PositionAssignment:
    """结束一段活动岗位任职并保留历史。"""

    assignment = _tenant_position_assignment(db, tenant_id, assignment_id)
    if assignment.status != "active":
        return assignment
    _close_position_assignment(assignment, effective_until or utc_now())
    close_leaders_for_position_assignment(
        db,
        tenant_id=tenant_id,
        position_assignment_id=assignment.id,
        effective_at=assignment.effective_until or utc_now(),
    )
    db.add(assignment)
    db.flush()
    return assignment


def end_active_member_assignments(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    effective_until: datetime,
) -> None:
    """成员停用或离职时统一关闭活动组织归属与岗位任职，保留全部历史。"""

    for row in _active_org_assignments(db, tenant_id, employee_profile_id):
        _close_org_assignment(row, effective_until)
        db.add(row)
    for row in _active_position_assignments(
        db, tenant_id, employee_profile_id=employee_profile_id
    ):
        _close_position_assignment(row, effective_until)
        db.add(row)
    close_leaders_for_member(
        db,
        tenant_id=tenant_id,
        employee_profile_id=employee_profile_id,
        effective_at=effective_until,
    )
    db.flush()


def member_has_assignment_history(
    db: Session, tenant_id: str, employee_profile_id: str
) -> bool:
    """判断员工档案是否已产生必须保留的组织或岗位任职历史。"""

    org_row = db.exec(
        select(MemberOrgAssignment).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.employee_profile_id == employee_profile_id,
        )
    ).first()
    if org_row is not None:
        return True
    return (
        db.exec(
            select(PositionAssignment).where(
                PositionAssignment.tenant_id == tenant_id,
                PositionAssignment.employee_profile_id == employee_profile_id,
            )
        ).first()
        is not None
    )


def get_tenant_position(db: Session, tenant_id: str, position_id: str) -> Position:
    """按租户读取岗位，避免用其他租户的岗位 ID 访问或变更资源。"""

    position = db.exec(
        select(Position).where(
            Position.id == position_id,
            Position.tenant_id == tenant_id,
        )
    ).first()
    if position is None:
        raise OrganizationAssignmentError("POSITION_NOT_FOUND")
    return position


def organization_has_active_assignments(
    db: Session, tenant_id: str, org_unit_id: str
) -> bool:
    """判断组织是否仍被活动成员归属或活动岗位引用。"""

    member = db.exec(
        select(MemberOrgAssignment).where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.org_unit_id == org_unit_id,
            MemberOrgAssignment.status == "active",
        )
    ).first()
    if member is not None:
        return True
    return (
        db.exec(
            select(Position).where(
                Position.tenant_id == tenant_id,
                Position.org_unit_id == org_unit_id,
                Position.status == "active",
            )
        ).first()
        is not None
    )


def _locked_active_profile(
    db: Session, tenant_id: str, profile_id: str
) -> EmployeeProfile:
    """锁定当前租户员工档案，使主归属和主岗位切换在同一事务串行执行。"""

    profile = db.exec(
        select(EmployeeProfile)
        .where(
            EmployeeProfile.id == profile_id,
            EmployeeProfile.tenant_id == tenant_id,
        )
        .with_for_update()
    ).first()
    if profile is None:
        raise OrganizationAssignmentError("EMPLOYEE_PROFILE_NOT_FOUND")
    if profile.status != "active":
        raise OrganizationAssignmentError("EMPLOYEE_PROFILE_INACTIVE")
    return profile


def _active_unit(db: Session, tenant_id: str, unit_id: str) -> OrganizationUnit:
    """读取当前租户活动组织。"""

    unit = db.exec(
        select(OrganizationUnit).where(
            OrganizationUnit.id == unit_id,
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.status == "active",
        )
    ).first()
    if unit is None:
        raise OrganizationAssignmentError("ORGANIZATION_NOT_FOUND")
    return unit


def _active_position(db: Session, tenant_id: str, position_id: str) -> Position:
    """读取当前租户活动岗位。"""

    position = get_tenant_position(db, tenant_id, position_id)
    if position.status != "active":
        raise OrganizationAssignmentError("POSITION_INACTIVE")
    return position


def _active_org_assignments(
    db: Session, tenant_id: str, employee_profile_id: str
) -> list[MemberOrgAssignment]:
    """列出员工当前活动组织归属。"""

    return list(
        db.exec(
            select(MemberOrgAssignment).where(
                MemberOrgAssignment.tenant_id == tenant_id,
                MemberOrgAssignment.employee_profile_id == employee_profile_id,
                MemberOrgAssignment.status == "active",
            )
        ).all()
    )


def _active_position_assignments(
    db: Session,
    tenant_id: str,
    *,
    employee_profile_id: str | None = None,
    position_id: str | None = None,
) -> list[PositionAssignment]:
    """按员工或岗位列出当前活动岗位任职。"""

    statement = select(PositionAssignment).where(
        PositionAssignment.tenant_id == tenant_id,
        PositionAssignment.status == "active",
    )
    if employee_profile_id is not None:
        statement = statement.where(
            PositionAssignment.employee_profile_id == employee_profile_id
        )
    if position_id is not None:
        statement = statement.where(PositionAssignment.position_id == position_id)
    return list(db.exec(statement).all())


def _tenant_org_assignment(
    db: Session, tenant_id: str, assignment_id: str
) -> MemberOrgAssignment:
    """按租户读取组织归属历史。"""

    row = db.exec(
        select(MemberOrgAssignment).where(
            MemberOrgAssignment.id == assignment_id,
            MemberOrgAssignment.tenant_id == tenant_id,
        )
    ).first()
    if row is None:
        raise OrganizationAssignmentError("ORG_ASSIGNMENT_NOT_FOUND")
    return row


def _tenant_position_assignment(
    db: Session, tenant_id: str, assignment_id: str
) -> PositionAssignment:
    """按租户读取岗位任职历史。"""

    row = db.exec(
        select(PositionAssignment).where(
            PositionAssignment.id == assignment_id,
            PositionAssignment.tenant_id == tenant_id,
        )
    ).first()
    if row is None:
        raise OrganizationAssignmentError("POSITION_ASSIGNMENT_NOT_FOUND")
    return row


def _close_org_assignment(row: MemberOrgAssignment, effective_at: datetime) -> None:
    """关闭组织归属区间并校验结束时间不早于开始时间。"""

    if effective_at < row.effective_from:
        raise OrganizationAssignmentError("INVALID_ASSIGNMENT_INTERVAL")
    row.effective_until = effective_at
    row.status = "inactive"
    row.updated_at = utc_now()


def _close_position_assignment(row: PositionAssignment, effective_at: datetime) -> None:
    """关闭岗位任职区间并校验结束时间不早于开始时间。"""

    if effective_at < row.effective_from:
        raise OrganizationAssignmentError("INVALID_ASSIGNMENT_INTERVAL")
    row.effective_until = effective_at
    row.status = "inactive"
    row.updated_at = utc_now()


def _close_unexplained_position_assignments(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    effective_at: datetime,
) -> None:
    """关闭已无法由当前活动组织归属解释的岗位任职。"""

    active_org_ids = {
        row.org_unit_id
        for row in _active_org_assignments(db, tenant_id, employee_profile_id)
    }
    position_rows = _active_position_assignments(
        db, tenant_id, employee_profile_id=employee_profile_id
    )
    if not position_rows:
        return
    positions = {
        row.id: row
        for row in db.exec(
            select(Position).where(
                Position.tenant_id == tenant_id,
                Position.id.in_([assignment.position_id for assignment in position_rows]),
            )
        ).all()
    }
    for assignment in position_rows:
        position = positions.get(assignment.position_id)
        if position is None or position.org_unit_id not in active_org_ids:
            _close_position_assignment(assignment, effective_at)
            db.add(assignment)
            close_leaders_for_position_assignment(
                db,
                tenant_id=tenant_id,
                position_assignment_id=assignment.id,
                effective_at=effective_at,
            )
    db.flush()


def _require_not_future(effective_at: datetime) -> None:
    """MVP 命令只接受立即生效时间，避免未来记录提前参与当前态解析。"""

    if effective_at > utc_now():
        raise OrganizationAssignmentError("FUTURE_ASSIGNMENT_NOT_SUPPORTED")


def _require_no_position_cycle(
    db: Session, position: Position, manager: Position
) -> None:
    """沿岗位汇报链检查自引用和后代回指。"""

    if manager.id == position.id:
        raise OrganizationAssignmentError("POSITION_REPORTING_CYCLE")
    cursor = manager
    visited: set[str] = set()
    while cursor.reports_to_position_id:
        if cursor.id in visited:
            raise OrganizationAssignmentError("POSITION_REPORTING_CYCLE")
        visited.add(cursor.id)
        if cursor.reports_to_position_id == position.id:
            raise OrganizationAssignmentError("POSITION_REPORTING_CYCLE")
        cursor = _active_position(
            db, position.tenant_id, cursor.reports_to_position_id
        )
