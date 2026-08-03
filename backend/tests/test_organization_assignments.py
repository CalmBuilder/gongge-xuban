"""
@Time       : 2026/07/28 15:25
@Author     : zhanglp8181
@File       : test_organization_assignments.py
@CallChain  : pytest → 组织任职命令 → 组织归属/岗位/任职历史
@Description: 验证 M2-B 调岗追加历史、主任职唯一、组织一致性和活动引用约束。
"""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    CodeItem,
    CodeSet,
    BusinessRole,
    EmployeeProfile,
    MemberOrgAssignment,
    PositionAssignment,
    Tenant,
    User,
    utc_now,
)
from app.api.organization_assignments import (
    OrgAssignmentCreate,
    PositionAssignmentCreate,
    PositionCreate,
    assign_member_to_organization_endpoint,
    assign_member_to_position_endpoint,
    create_position_endpoint,
    list_current_organization_members_page,
    list_member_org_assignments,
)
from app.organization.assignments import (
    OrganizationAssignmentError,
    assign_member_to_organization,
    assign_member_to_position,
    create_position,
    deactivate_position,
    end_member_org_assignment,
    ensure_assignment_foundation,
    update_position,
)
from app.organization.roles import (
    active_business_role_codes,
    bind_position_business_role,
    deactivate_position_role_binding,
)
from app.organization.units import (
    OrganizationUnitError,
    create_organization_unit,
    deactivate_organization_unit,
    ensure_organization_foundation,
)


def test_primary_org_transfer_appends_history_and_keeps_one_current_primary() -> None:
    """验证调岗关闭旧区间并追加新归属，而不是覆盖旧组织事实。"""

    with _test_session() as db:
        profile, finance, research = _organization_fixture(db)
        started_at = utc_now() - timedelta(days=10)
        transferred_at = utc_now() - timedelta(days=2)

        first = assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
            effective_from=started_at,
        )
        second = assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=research.id,
            effective_from=transferred_at,
        )
        repeated = assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=research.id,
        )
        db.commit()

        rows = db.exec(
            select(MemberOrgAssignment).where(
                MemberOrgAssignment.employee_profile_id == profile.id
            )
        ).all()
        assert repeated.id == second.id
        assert len(rows) == 2
        assert first.status == "inactive"
        assert first.effective_until == transferred_at
        assert [
            row.org_unit_id
            for row in rows
            if row.status == "active" and row.is_primary
        ] == [research.id]


def test_concurrent_org_and_position_require_explainable_active_org() -> None:
    """验证兼任可并存，但岗位任职必须由同组织的活动归属解释。"""

    with _test_session() as db:
        profile, finance, research = _organization_fixture(db)
        finance_position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            code="FIN_ADMIN",
            name="财务专员",
            position_type_code="professional",
        )
        research_position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=research.id,
            code="RD_COORDINATOR",
            name="研发协调员",
            position_type_code="support",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        primary_position = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=finance_position.id,
        )

        with pytest.raises(
            OrganizationAssignmentError, match="POSITION_ORG_ASSIGNMENT_REQUIRED"
        ):
            assign_member_to_position(
                db,
                tenant_id="tenant_a",
                employee_profile_id=profile.id,
                position_id=research_position.id,
                assignment_type="concurrent",
            )

        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=research.id,
            assignment_type="concurrent",
        )
        concurrent_position = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=research_position.id,
            assignment_type="concurrent",
        )
        db.commit()

        active_positions = db.exec(
            select(PositionAssignment).where(
                PositionAssignment.employee_profile_id == profile.id,
                PositionAssignment.status == "active",
            )
        ).all()
        assert {row.id for row in active_positions} == {
            primary_position.id,
            concurrent_position.id,
        }


def test_position_transfer_closes_old_primary_and_rejects_reporting_cycle() -> None:
    """验证主岗位切换保留历史，并拒绝岗位汇报链形成循环。"""

    with _test_session() as db:
        profile, finance, _ = _organization_fixture(db)
        manager = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            code="FIN_MANAGER",
            name="财务经理",
            position_type_code="management",
        )
        specialist = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            code="FIN_SPECIALIST",
            name="财务专员",
            position_type_code="professional",
            reports_to_position_id=manager.id,
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        first = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=specialist.id,
        )
        second = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=manager.id,
        )

        with pytest.raises(
            OrganizationAssignmentError, match="POSITION_REPORTING_CYCLE"
        ):
            update_position(
                db,
                manager,
                reports_to_position_id=specialist.id,
            )
        db.commit()

        assert first.status == "inactive"
        assert first.effective_until is not None
        assert second.status == "active"
        assert second.is_primary is True


def test_org_transfer_closes_positions_that_no_longer_have_active_org_basis() -> None:
    """验证调离组织时同步结束无法再由活动组织归属解释的岗位任职。"""

    with _test_session() as db:
        profile, finance, research = _organization_fixture(db)
        position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            code="FIN_TRANSFER",
            name="待调岗岗位",
            position_type_code="professional",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        position_assignment = assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=position.id,
        )

        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=research.id,
        )
        db.commit()

        assert position_assignment.status == "inactive"
        assert position_assignment.effective_until is not None


def test_activity_references_block_position_and_org_deactivation() -> None:
    """验证活动归属、岗位和任职都会阻止破坏其引用的停用操作。"""

    with _test_session() as db:
        profile, finance, _ = _organization_fixture(db)
        assignment = assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            code="FIN_ADMIN",
            name="财务专员",
            position_type_code="professional",
        )
        assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=position.id,
        )

        with pytest.raises(
            OrganizationAssignmentError, match="ACTIVE_POSITION_ASSIGNMENTS_EXIST"
        ):
            deactivate_position(db, position)
        with pytest.raises(
            OrganizationUnitError, match="ACTIVE_ORGANIZATION_REFERENCES_EXIST"
        ):
            deactivate_organization_unit(db, finance)

        end_member_org_assignment(
            db,
            tenant_id="tenant_a",
            assignment_id=assignment.id,
        )
        db.commit()


def test_position_type_catalog_is_tenant_scoped_and_idempotent() -> None:
    """验证每个租户拥有独立且幂等的岗位类型码表。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="企业 A"))
        db.add(Tenant(id="tenant_b", name="企业 B"))
        db.commit()

        ensure_assignment_foundation(db, "tenant_a")
        ensure_assignment_foundation(db, "tenant_a")
        ensure_assignment_foundation(db, "tenant_b")
        db.commit()

        sets = db.exec(
            select(CodeSet).where(CodeSet.set_code == "position_type")
        ).all()
        items = db.exec(select(CodeItem)).all()
        assert len(sets) == 2
        assert all(
            len([item for item in items if item.code_set_id == code_set.id]) == 5
            for code_set in sets
        )


def test_assignment_api_enforces_admin_writes_tenant_and_member_self_read() -> None:
    """验证管理员执行任职命令，普通成员只能读本人且不能伪造写入。"""

    with _test_session() as db:
        profile, finance, _ = _organization_fixture(db)
        admin = User(
            id="admin_a",
            tenant_id="tenant_a",
            username="admin",
            role="admin",
            password_hash="test-only",
        )
        member = db.get(User, "user_a")
        db.add(admin)
        db.commit()

        org_request = OrgAssignmentCreate(
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        with pytest.raises(HTTPException) as forbidden:
            assign_member_to_organization_endpoint(org_request, member, db)
        assert forbidden.value.status_code == 403

        org_assignment = assign_member_to_organization_endpoint(
            org_request, admin, db
        )
        position = create_position_endpoint(
            PositionCreate(
                tenant_id="tenant_a",
                org_unit_id=finance.id,
                code="FIN_API",
                name="财务 API 岗位",
                position_type_code="professional",
            ),
            admin,
            db,
        )
        position_assignment = assign_member_to_position_endpoint(
            PositionAssignmentCreate(
                tenant_id="tenant_a",
                employee_profile_id=profile.id,
                position_id=position.id,
            ),
            admin,
            db,
        )

        own_rows = list_member_org_assignments(
            "tenant_a", profile.id, member, db
        )
        assert [row.id for row in own_rows] == [org_assignment.id]
        assert position_assignment.position_id == position.id
        with pytest.raises(HTTPException) as broad_read:
            list_member_org_assignments("tenant_a", None, member, db)
        assert broad_read.value.status_code == 403
        with pytest.raises(HTTPException) as cross_tenant:
            list_member_org_assignments("tenant_b", profile.id, member, db)
        assert cross_tenant.value.status_code == 403


def test_current_organization_member_page_returns_names_and_stable_pagination() -> None:
    """大组织成员接口按页返回真人名称和归属命令 ID，不暴露未解析档案 ID。"""

    with _test_session() as db:
        profile, finance, _ = _organization_fixture(db)
        admin = User(
            id="admin_page",
            tenant_id="tenant_a",
            username="admin_page",
            role="admin",
            password_hash="test-only",
        )
        second_user = User(
            id="user_page_2",
            tenant_id="tenant_a",
            username="member_two",
            display_name="成员二",
            password_hash="test-only",
        )
        second_profile = EmployeeProfile(
            id="profile_page_2",
            tenant_id="tenant_a",
            user_id=second_user.id,
            employee_id="E_PAGE_2",
            employee_name="成员二",
        )
        db.add(admin)
        db.add(second_user)
        db.add(second_profile)
        db.commit()
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=second_profile.id,
            org_unit_id=finance.id,
        )
        db.commit()

        first_page = list_current_organization_members_page(
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            page=1,
            page_size=1,
            current_user=admin,
            db=db,
        )
        second_page = list_current_organization_members_page(
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            page=2,
            page_size=1,
            current_user=admin,
            db=db,
        )

        assert first_page.total == 2
        assert first_page.page_size == 1
        assert len(first_page.items) == len(second_page.items) == 1
        assert {
            first_page.items[0].employee_name,
            second_page.items[0].employee_name,
        } == {"成员一", "成员二"}


def test_position_role_binding_enters_and_leaves_effective_role_resolution() -> None:
    """验证岗位默认角色同时受有效区间和软停用约束，并保留授予人事实。"""

    with _test_session() as db:
        profile, finance, _ = _organization_fixture(db)
        role = BusinessRole(
            id="role_finance",
            tenant_id="tenant_a",
            role_code="finance.approver",
            name="财务审批人",
        )
        db.add(role)
        position = create_position(
            db,
            tenant_id="tenant_a",
            org_unit_id=finance.id,
            code="FIN_APPROVER",
            name="财务审批岗",
            position_type_code="professional",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=finance.id,
        )
        assign_member_to_position(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            position_id=position.id,
        )
        binding = bind_position_business_role(
            db,
            tenant_id="tenant_a",
            position_id=position.id,
            business_role_id=role.id,
            granted_by_user_id="user_admin",
            effective_from=utc_now() + timedelta(days=1),
        )
        db.commit()

        assert binding.granted_by_user_id == "user_admin"
        assert active_business_role_codes(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
        ) == []

        binding.effective_from = utc_now() - timedelta(days=1)
        binding.effective_until = utc_now() + timedelta(days=1)
        db.add(binding)
        db.commit()
        assert active_business_role_codes(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
        ) == ["finance.approver"]

        effective_from = binding.effective_from
        effective_until = binding.effective_until
        duplicate = bind_position_business_role(
            db,
            tenant_id="tenant_a",
            position_id=position.id,
            business_role_id=role.id,
            granted_by_user_id="different_admin",
        )
        db.commit()
        assert duplicate.id == binding.id
        assert duplicate.granted_by_user_id == "user_admin"
        assert duplicate.effective_from == effective_from
        assert duplicate.effective_until == effective_until

        binding.effective_until = utc_now() - timedelta(seconds=1)
        db.add(binding)
        db.commit()
        assert active_business_role_codes(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
        ) == []

        binding.effective_until = None
        db.add(binding)
        db.commit()

        deactivate_position_role_binding(
            db,
            tenant_id="tenant_a",
            binding_id=binding.id,
        )
        db.commit()
        assert (
            active_business_role_codes(
                db,
                tenant_id="tenant_a",
                employee_profile_id=profile.id,
            )
            == []
        )


def _organization_fixture(db: Session):
    """创建租户、活动员工档案和两个平级部门。"""

    db.add(Tenant(id="tenant_a", name="企业 A"))
    user = User(
        id="user_a",
        tenant_id="tenant_a",
        username="member",
        password_hash="test-only",
    )
    db.add(user)
    profile = EmployeeProfile(
        id="profile_a",
        tenant_id="tenant_a",
        user_id=user.id,
        employee_id="E001",
        employee_name="成员一",
    )
    db.add(profile)
    db.commit()
    root = ensure_organization_foundation(db, "tenant_a")
    finance = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=root.id,
        code="FINANCE",
        name="财务部",
        unit_type_code="department",
    )
    research = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=root.id,
        code="RESEARCH",
        name="研发部",
        unit_type_code="department",
    )
    ensure_assignment_foundation(db, "tenant_a")
    db.commit()
    return profile, finance, research


def _test_session() -> Session:
    """创建包含全部模型表的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
