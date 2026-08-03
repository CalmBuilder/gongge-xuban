"""
@Time       : 2026/07/28 16:10
@Author     : zhanglp8181
@File       : test_organization_leaders.py
@CallChain  : pytest → 负责人领域/API → 组织归属/岗位任职/负责人历史
@Description: 验证负责人唯一性、任期、生命周期、租户权限及不产生角色候选。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.organization_leaders import (
    LeaderAssignmentCreate,
    create_leader_assignment,
    list_leader_assignments,
)
from app.db.models import (
    EmployeeProfile,
    OrganizationLeaderAssignment,
    Tenant,
    User,
)
from app.organization.assignments import (
    assign_member_to_organization,
    assign_member_to_position,
    create_position,
    end_member_org_assignment,
    end_position_assignment,
)
from app.organization.leaders import (
    OrganizationLeaderError,
    create_organization_leader,
)
from app.organization.units import (
    create_organization_unit,
    ensure_organization_foundation,
)


def test_primary_is_unique_and_deputies_coexist_idempotently() -> None:
    """验证主要负责人唯一，副负责人可并存且重复命令不产生重复记录。"""

    with _test_session() as db:
        admin, first, second, unit = _fixture(db)
        primary = create_organization_leader(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=first.id,
            leader_type_code="primary",
            actor_user_id=admin.id,
        )
        repeated = create_organization_leader(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=first.id,
            leader_type_code="primary",
            actor_user_id=admin.id,
        )
        deputy = create_organization_leader(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=second.id,
            leader_type_code="deputy",
            actor_user_id=admin.id,
        )
        with pytest.raises(OrganizationLeaderError, match="PRIMARY_LEADER_EXISTS"):
            create_organization_leader(
                db,
                tenant_id="tenant_a",
                org_unit_id=unit.id,
                employee_profile_id=second.id,
                leader_type_code="primary",
                actor_user_id=admin.id,
            )
        db.commit()

        assert repeated.id == primary.id
        assert deputy.id != primary.id
        assert len(db.exec(select(OrganizationLeaderAssignment)).all()) == 2


def test_leader_requires_active_org_and_matching_position_assignment() -> None:
    """验证负责人由同组织活动归属解释，可选岗位任职必须同成员同组织。"""

    with _test_session() as db:
        admin, first, second, unit = _fixture(db)
        position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            code="DIRECTOR",
            name="负责人岗位",
            position_type_code="management",
        )
        second_position = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=second.id,
            position_id=position.id,
        )
        with pytest.raises(
            OrganizationLeaderError, match="POSITION_ASSIGNMENT_MISMATCH"
        ):
            create_organization_leader(
                db,
                tenant_id="tenant_a",
                org_unit_id=unit.id,
                employee_profile_id=first.id,
                leader_type_code="primary",
                actor_user_id=admin.id,
                position_assignment_id=second_position.id,
            )
        with pytest.raises(
            OrganizationLeaderError, match="ACTING_LEADER_END_REQUIRED"
        ):
            create_organization_leader(
                db,
                tenant_id="tenant_a",
                org_unit_id=unit.id,
                employee_profile_id=first.id,
                leader_type_code="acting",
                actor_user_id=admin.id,
            )


def test_org_and_position_end_close_only_explainable_leaders() -> None:
    """验证组织归属和显式岗位任职结束关闭负责人且保留历史。"""

    with _test_session() as db:
        admin, first, _, unit = _fixture(db)
        position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            code="PROJECT_LEAD",
            name="项目负责人岗",
            position_type_code="project",
        )
        position_assignment = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=first.id,
            position_id=position.id,
        )
        linked = create_organization_leader(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=first.id,
            leader_type_code="project",
            actor_user_id=admin.id,
            position_assignment_id=position_assignment.id,
        )
        independent = create_organization_leader(
            db,
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=first.id,
            leader_type_code="deputy",
            actor_user_id=admin.id,
        )
        end_position_assignment(
            db,
            tenant_id="tenant_a",
            assignment_id=position_assignment.id,
        )
        db.commit()
        assert linked.status == "inactive"
        assert independent.status == "active"

        org_assignment = _active_org_assignment_id(db, first.id)
        end_member_org_assignment(
            db,
            tenant_id="tenant_a",
            assignment_id=org_assignment,
        )
        db.commit()
        assert independent.status == "inactive"
        assert independent.effective_until is not None


def test_leader_api_separates_current_read_from_history_and_writes() -> None:
    """验证普通成员可读当前负责人，但历史和写入仅管理员可用。"""

    with _test_session() as db:
        admin, first, second, unit = _fixture(db)
        member = db.get(User, "user_second")
        request = LeaderAssignmentCreate(
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=first.id,
            leader_type_code="primary",
        )
        with pytest.raises(HTTPException) as forbidden:
            create_leader_assignment(request, member, db)
        assert forbidden.value.status_code == 403

        created = create_leader_assignment(request, admin, db)
        current = list_leader_assignments(
            tenant_id="tenant_a",
            org_unit_id=unit.id,
            employee_profile_id=None,
            include_history=False,
            current_user=member,
            db=db,
        )
        assert [row.id for row in current] == [created.id]
        with pytest.raises(HTTPException) as history_forbidden:
            list_leader_assignments(
                tenant_id="tenant_a",
                org_unit_id=unit.id,
                employee_profile_id=None,
                include_history=True,
                current_user=member,
                db=db,
            )
        assert history_forbidden.value.status_code == 403
        assert second.status == "active"


def _fixture(db: Session):
    """创建两个活动成员、一个组织及其活动归属。"""

    db.add(Tenant(id="tenant_a", name="测试企业"))
    admin = User(
        id="admin_a",
        tenant_id="tenant_a",
        username="admin",
        role="admin",
        password_hash="test-only",
    )
    first_user = User(
        id="user_first",
        tenant_id="tenant_a",
        username="first",
        password_hash="test-only",
    )
    second_user = User(
        id="user_second",
        tenant_id="tenant_a",
        username="second",
        password_hash="test-only",
    )
    first = EmployeeProfile(
        id="profile_first",
        tenant_id="tenant_a",
        user_id=first_user.id,
        employee_id="E001",
    )
    second = EmployeeProfile(
        id="profile_second",
        tenant_id="tenant_a",
        user_id=second_user.id,
        employee_id="E002",
    )
    db.add_all([admin, first_user, second_user, first, second])
    db.commit()
    root = ensure_organization_foundation(db, "tenant_a")
    unit = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=root.id,
        code="TEST_UNIT",
        name="匿名测试组织",
        unit_type_code="department",
    )
    for profile in (first, second):
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=unit.id,
        )
    db.commit()
    return admin, first, second, unit


def _active_org_assignment_id(db: Session, employee_profile_id: str) -> str:
    """返回测试成员当前活动组织归属 ID。"""

    from app.db.models import MemberOrgAssignment

    return db.exec(
        select(MemberOrgAssignment.id).where(
            MemberOrgAssignment.employee_profile_id == employee_profile_id,
            MemberOrgAssignment.status == "active",
        )
    ).one()


def _test_session() -> Session:
    """创建共享内存 SQLite 会话覆盖双方言无关领域规则。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
