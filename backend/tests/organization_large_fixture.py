"""
@Time       : 2026/07/28 17:55
@Author     : zhanglp8181
@File       : organization_large_fixture.py
@CallChain  : M2.5-B SQLite/MySQL/浏览器验收 → 匿名组织夹具 → 统一组织事实表
@Description: 可重复生成真实结构特征和 500/5000/8000 规模数据，不包含真实单位或人员信息。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session

from app.db.models import (
    EmployeeProfile,
    MemberOrgAssignment,
    OrganizationLeaderAssignment,
    OrganizationUnit,
    Position,
    PositionAssignment,
    Tenant,
    User,
    utc_now,
)


@dataclass(frozen=True)
class LargeOrganizationFixture:
    """记录规模夹具的稳定根节点和实际生成数量。"""

    tenant_id: str
    root_id: str
    organization_count: int
    member_count: int
    org_assignment_count: int
    position_count: int
    leader_count: int


def seed_large_organization_fixture(
    db: Session,
    *,
    tenant_id: str = "tenant_scale",
    organization_count: int = 500,
    member_count: int = 5000,
    org_assignment_count: int = 8000,
) -> LargeOrganizationFixture:
    """批量写入匿名单根、多机构、职能/项目兼任和负责人规模事实。"""

    if organization_count < 20 or member_count < organization_count:
        raise ValueError("Scale fixture requires at least 20 organizations and one member per unit")
    if org_assignment_count < member_count:
        raise ValueError("Organization assignments must cover every member primary assignment")

    now = utc_now()
    root_id = "org_scale_0000"
    db.add(Tenant(id=tenant_id, name="匿名协作企业", created_at=now, updated_at=now))
    paths = {0: root_id}
    units: list[OrganizationUnit] = []
    for index in range(organization_count):
        parent_index = None if index == 0 else (index - 1) // 10
        unit_id = f"org_scale_{index:04d}"
        if index == 0:
            name, unit_type = "匿名协作企业", "company"
        elif index == 1:
            name, unit_type = "北区研究中心", "division"
        elif index == 2:
            name, unit_type = "运营本部", "division"
        elif index == 3:
            name, unit_type = "管理层", "department"
        elif 4 <= index < 16:
            name, unit_type = f"职能部门 {index - 3:02d}", "department"
        elif 16 <= index < 26:
            name, unit_type = f"技术部门 {index - 15:02d}", "department"
        elif index < 60:
            name, unit_type = f"项目组合 {index - 25:02d}", "project"
        else:
            name, unit_type = f"匿名组织 {index:04d}", "team"
        path = (
            unit_id
            if parent_index is None
            else f"{paths[parent_index]}/{unit_id}"
        )
        paths[index] = path
        units.append(
            OrganizationUnit(
                id=unit_id,
                tenant_id=tenant_id,
                parent_id=(
                    None
                    if parent_index is None
                    else f"org_scale_{parent_index:04d}"
                ),
                code="ROOT" if index == 0 else f"UNIT_{index:04d}",
                name=name,
                unit_type_code=unit_type,
                tree_path=path,
                depth=path.count("/"),
                sort_order=index,
                is_root=index == 0,
                root_tenant_id=tenant_id if index == 0 else None,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
    db.add_all(units)

    users: list[User] = []
    profiles: list[EmployeeProfile] = []
    primary_org_by_profile: dict[str, str] = {}
    assignments: list[MemberOrgAssignment] = []
    for index in range(member_count):
        user_id = f"user_scale_{index:05d}"
        profile_id = f"profile_scale_{index:05d}"
        primary_index = 1 + index % (organization_count - 1)
        primary_org_id = f"org_scale_{primary_index:04d}"
        primary_org_by_profile[profile_id] = primary_org_id
        users.append(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"member_{index:05d}",
                display_name=f"成员 {index:05d}",
                role="member",
                membership_status="active",
                member_category_code="employee",
                password_hash="scale-fixture-only",
                created_at=now,
                updated_at=now,
            )
        )
        profiles.append(
            EmployeeProfile(
                id=profile_id,
                tenant_id=tenant_id,
                user_id=user_id,
                employee_id=f"E{index:05d}",
                employee_name=f"成员 {index:05d}",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        assignments.append(
            MemberOrgAssignment(
                id=f"memberorg_scale_{index:05d}",
                tenant_id=tenant_id,
                employee_profile_id=profile_id,
                org_unit_id=primary_org_id,
                assignment_type="primary",
                is_primary=True,
                effective_from=now,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
    for index in range(member_count, org_assignment_count):
        member_index = index % member_count
        project_index = 26 + index % max(1, min(34, organization_count - 26))
        assignments.append(
            MemberOrgAssignment(
                id=f"memberorg_scale_{index:05d}",
                tenant_id=tenant_id,
                employee_profile_id=f"profile_scale_{member_index:05d}",
                org_unit_id=f"org_scale_{project_index:04d}",
                assignment_type="project",
                is_primary=False,
                effective_from=now,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
    db.add_all(users)
    db.add_all(profiles)
    db.add_all(assignments)

    positions: list[Position] = []
    position_assignments: list[PositionAssignment] = []
    leaders: list[OrganizationLeaderAssignment] = []
    first_profile_by_org: dict[str, str] = {}
    for profile_id, org_id in primary_org_by_profile.items():
        first_profile_by_org.setdefault(org_id, profile_id)
    for index in range(1, organization_count):
        org_id = f"org_scale_{index:04d}"
        position_id = f"position_scale_{index:04d}"
        profile_id = first_profile_by_org[org_id]
        positions.append(
            Position(
                id=position_id,
                tenant_id=tenant_id,
                org_unit_id=org_id,
                code=f"POSITION_{index:04d}",
                name=f"责任岗位 {index:04d}",
                position_type_code="management" if index < 26 else "professional",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        position_assignment_id = f"posassign_scale_{index:04d}"
        position_assignments.append(
            PositionAssignment(
                id=position_assignment_id,
                tenant_id=tenant_id,
                employee_profile_id=profile_id,
                position_id=position_id,
                assignment_type="primary",
                is_primary=True,
                effective_from=now,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        leaders.append(
            OrganizationLeaderAssignment(
                id=f"orgleader_scale_{index:04d}",
                tenant_id=tenant_id,
                org_unit_id=org_id,
                employee_profile_id=profile_id,
                position_assignment_id=position_assignment_id,
                leader_type_code=(
                    "project" if 26 <= index < 60 else "primary"
                ),
                effective_from=now,
                status="active",
                source_kind="manual",
                created_at=now,
                updated_at=now,
            )
        )
    db.add_all(positions)
    db.add_all(position_assignments)
    db.add_all(leaders)
    db.commit()
    return LargeOrganizationFixture(
        tenant_id=tenant_id,
        root_id=root_id,
        organization_count=len(units),
        member_count=len(users),
        org_assignment_count=len(assignments),
        position_count=len(positions),
        leader_count=len(leaders),
    )
