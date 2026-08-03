"""
@Time       : 2026/07/28 19:45
@Author     : zhanglp8181
@File       : test_governance_permissions.py
@CallChain  : pytest → M3-A 治理解析 → 角色任职/岗位任职/唯一组织子树服务
@Description: 以 OpenFGA 风格正反矩阵验证租户、组织、子树和来源可解释治理授权。
"""

from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.organization import (
    EmployeeRoleAssignmentCreate,
    create_employee_role_assignment,
)
from app.api.auth import UserCreateRequest, create_user
from app.api.organization_assignments import (
    PositionRoleBindingCreate,
    create_position_role_binding_endpoint,
)
from app.api.organization_units import (
    OrganizationUnitCreate,
    create_organization_unit_endpoint,
    get_organization_unit_summary,
    list_organization_unit_children,
)
from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    MemberOrgAssignment,
    Position,
    PositionAssignment,
    PositionRoleBinding,
    Tenant,
    User,
    utc_now,
)
from app.organization.governance import (
    ensure_builtin_governance_catalog,
    governance_permission_codes,
    has_governance_permission,
    resolve_permission_grants,
    validate_role_assignment_scope,
)
from app.organization.permissions import user_permission_codes
from app.organization.roles import active_business_role_codes
from app.organization.units import create_organization_unit, ensure_organization_foundation


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_direct_org_grant_inherits_only_through_selected_parent_subtree() -> None:
    """验证组织父级授权只派生到显式下级，不放宽到父级或兄弟组织。"""

    with _test_session() as db:
        fixture = _fixture(db)
        role = fixture["org_admin_role"]
        profile = fixture["scoped_profile"]
        db.add(
            EmployeeRoleAssignment(
                id="grant_direct",
                tenant_id="tenant_a",
                employee_profile_id=profile.id,
                business_role_id=role.id,
                scope_type="org_unit",
                scope_id=fixture["division"].id,
                include_descendants=True,
                granted_by_user_id="tenant_owner",
            )
        )
        db.commit()

        checks = {
            fixture["division"].id: True,
            fixture["department"].id: True,
            fixture["root"].id: False,
            fixture["sibling"].id: False,
        }
        for organization_id, expected in checks.items():
            assert has_governance_permission(
                db,
                tenant_id="tenant_a",
                user_id="scoped_admin",
                permission_code="organization.manage",
                target_org_unit_id=organization_id,
            ) is expected

        grants = resolve_permission_grants(
            db,
            tenant_id="tenant_a",
            user_id="scoped_admin",
        )
        organization_grant = next(
            grant
            for grant in grants
            if grant.permission_code == "organization.manage"
        )
        assert organization_grant.source_kind == "direct_role"
        assert organization_grant.scope.organization_unit_ids == frozenset(
            {fixture["division"].id, fixture["department"].id}
        )
        assert organization_grant.granted_by_user_id == "tenant_owner"


def test_position_governance_role_uses_position_org_without_entering_business_roles() -> None:
    """验证岗位可带入组织治理权限，但治理角色不会进入 SOP 业务角色候选。"""

    with _test_session() as db:
        fixture = _fixture(db)
        role = fixture["org_admin_role"]
        profile = fixture["scoped_profile"]
        position = Position(
            id="position_department_admin",
            tenant_id="tenant_a",
            org_unit_id=fixture["department"].id,
            code="DEPARTMENT_ADMIN",
            name="部门治理岗",
            position_type_code="management",
        )
        db.add(position)
        db.add(
            PositionAssignment(
                id="position_assignment_admin",
                tenant_id="tenant_a",
                employee_profile_id=profile.id,
                position_id=position.id,
            )
        )
        db.add(
            PositionRoleBinding(
                id="position_role_admin",
                tenant_id="tenant_a",
                position_id=position.id,
                business_role_id=role.id,
                scope_mode="position_org",
            )
        )
        db.commit()

        grants = resolve_permission_grants(
            db,
            tenant_id="tenant_a",
            user_id="scoped_admin",
        )
        assert any(
            grant.permission_code == "organization.manage"
            and grant.source_kind == "position_role"
            and grant.scope.scope_id == fixture["department"].id
            for grant in grants
        )
        assert active_business_role_codes(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
        ) == []
        assert user_permission_codes(
            db,
            tenant_id="tenant_a",
            user_id="scoped_admin",
        ) == []


def test_position_role_binding_checks_permission_before_forged_position_lookup() -> None:
    """验证无权成员伪造岗位绑定时先返回 403，不把资源查找异常泄漏为 500。"""

    with _test_session() as db:
        _fixture(db)
        scoped_user = db.get(User, "scoped_admin")
        assert scoped_user is not None
        with pytest.raises(HTTPException) as denied:
            create_position_role_binding_endpoint(
                PositionRoleBindingCreate(
                    tenant_id="tenant_a",
                    position_id="forged_position",
                    business_role_id="forged_role",
                ),
                scoped_user,
                db,
            )
        assert denied.value.status_code == 403


def test_platform_admin_compatibility_grants_governance_but_never_business_permission() -> None:
    """验证旧 admin 身份只映射租户治理权限，不获得任何 SOP 业务权限。"""

    with _test_session() as db:
        _fixture(db)

        codes = governance_permission_codes(
            db,
            tenant_id="tenant_a",
            user_id="tenant_owner",
        )

        assert "authorization.manage" in codes
        assert "organization.manage" in codes
        assert user_permission_codes(
            db,
            tenant_id="tenant_a",
            user_id="tenant_owner",
        ) == []
        assert all(
            grant.source_kind == "platform_admin_compat"
            for grant in resolve_permission_grants(
                db,
                tenant_id="tenant_a",
                user_id="tenant_owner",
            )
        )


def test_role_assignment_api_rejects_freeform_foreign_and_invalid_tenant_scopes() -> None:
    """验证保存入口拒绝自由 scope、跨租户组织和不完整租户范围。"""

    with _test_session() as db:
        fixture = _fixture(db)
        owner = fixture["owner"]
        profile = fixture["scoped_profile"]

        for request, expected_code in (
            (
                EmployeeRoleAssignmentCreate(
                    tenant_id="tenant_a",
                    employee_profile_id=profile.id,
                    role_code="governance_org_admin",
                    scope_type="tenant",
                    scope_id="*",
                    include_descendants=False,
                    grant_reason="测试非法租户范围",
                    effective_until=utc_now() + timedelta(days=30),
                ),
                "INVALID_TENANT_ROLE_SCOPE",
            ),
            (
                EmployeeRoleAssignmentCreate(
                    tenant_id="tenant_a",
                    employee_profile_id=profile.id,
                    role_code="governance_org_admin",
                    scope_type="org_unit",
                    scope_id=fixture["foreign_root"].id,
                    include_descendants=True,
                    grant_reason="测试跨租户组织范围",
                    effective_until=utc_now() + timedelta(days=30),
                ),
                "INVALID_ORGANIZATION_ROLE_SCOPE",
            ),
        ):
            with pytest.raises(HTTPException) as denied:
                create_employee_role_assignment(request, owner, db)
            assert denied.value.status_code == 422
            assert denied.value.detail["code"] == expected_code

        with pytest.raises(ValueError, match="INVALID_ROLE_SCOPE_TYPE"):
            validate_role_assignment_scope(
                db,
                tenant_id="tenant_a",
                scope_type="freeform",
                scope_id="anything",
                include_descendants=True,
            )


def test_scoped_org_admin_api_lists_and_writes_only_selected_subtree() -> None:
    """验证组织 API 的返回集合和写目标都应用同一结构化子树授权。"""

    with _test_session() as db:
        fixture = _fixture(db)
        role = fixture["org_admin_role"]
        profile = fixture["scoped_profile"]
        scoped_user = db.get(User, "scoped_admin")
        db.add(
            EmployeeRoleAssignment(
                id="grant_api_scope",
                tenant_id="tenant_a",
                employee_profile_id=profile.id,
                business_role_id=role.id,
                scope_type="org_unit",
                scope_id=fixture["division"].id,
                include_descendants=True,
                granted_by_user_id="tenant_owner",
            )
        )
        db.commit()

        roots = list_organization_unit_children(
            "tenant_a",
            None,
            scoped_user,
            db,
        )
        children = list_organization_unit_children(
            "tenant_a",
            fixture["division"].id,
            scoped_user,
            db,
        )
        assert [row.id for row in roots] == [fixture["division"].id]
        assert [row.id for row in children] == [fixture["department"].id]

        created = create_organization_unit_endpoint(
            OrganizationUnitCreate(
                tenant_id="tenant_a",
                parent_id=fixture["department"].id,
                code="TEAM_A",
                name="团队甲",
                unit_type_code="team",
            ),
            scoped_user,
            db,
        )
        assert created.parent_id == fixture["department"].id

        with pytest.raises(HTTPException) as outside_read:
            get_organization_unit_summary(
                "tenant_a",
                fixture["sibling"].id,
                scoped_user,
                db,
            )
        assert outside_read.value.status_code == 403
        with pytest.raises(HTTPException) as outside_write:
            create_organization_unit_endpoint(
                OrganizationUnitCreate(
                    tenant_id="tenant_a",
                    parent_id=fixture["sibling"].id,
                    code="TEAM_OUTSIDE",
                    name="范围外团队",
                    unit_type_code="team",
                ),
                scoped_user,
                db,
            )
        assert outside_write.value.status_code == 403


@pytest.mark.mysql
def test_governance_scope_matrix_runs_after_mysql_head_migration(
    mysql_database_url: str,
) -> None:
    """在隔离 MySQL 8.4 迁移到 head 后重放治理子树允许与拒绝矩阵。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = mysql_database_url
    command.upgrade(config, "head")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            fixture = _fixture(db)
            db.add(
                EmployeeRoleAssignment(
                    id="grant_mysql_scope",
                    tenant_id="tenant_a",
                    employee_profile_id=fixture["scoped_profile"].id,
                    business_role_id=fixture["org_admin_role"].id,
                    scope_type="org_unit",
                    scope_id=fixture["division"].id,
                    include_descendants=True,
                    granted_by_user_id="tenant_owner",
                )
            )
            db.commit()

            assert has_governance_permission(
                db,
                tenant_id="tenant_a",
                user_id="scoped_admin",
                permission_code="organization.manage",
                target_org_unit_id=fixture["department"].id,
            )
            assert not has_governance_permission(
                db,
                tenant_id="tenant_a",
                user_id="scoped_admin",
                permission_code="organization.manage",
                target_org_unit_id=fixture["sibling"].id,
            )
    finally:
        engine.dispose()


def test_scoped_org_admin_creates_member_only_inside_granted_subtree() -> None:
    """范围管理员可在授权子树创建成员并建立主归属，不能写入兄弟组织。"""

    with _test_session() as db:
        fixture = _fixture(db)
        scoped_user = db.get(User, "scoped_admin")
        assert scoped_user is not None
        db.add(
            EmployeeRoleAssignment(
                id="grant_member_create_scope",
                tenant_id="tenant_a",
                employee_profile_id=fixture["scoped_profile"].id,
                business_role_id=fixture["org_admin_role"].id,
                scope_type="org_unit",
                scope_id=fixture["division"].id,
                include_descendants=True,
                granted_by_user_id="tenant_owner",
            )
        )
        db.commit()

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_a",
                username="department_member",
                password="test-only",
                employee_id="E_DEPARTMENT",
                employee_name="部门成员",
                initial_org_unit_id=fixture["department"].id,
            ),
            scoped_user,
            db,
        )

        assignment = db.exec(
            select(MemberOrgAssignment).where(
                MemberOrgAssignment.tenant_id == "tenant_a",
                MemberOrgAssignment.employee_profile_id == created.employee_profile_id,
            )
        ).one()
        assert assignment.org_unit_id == fixture["department"].id
        assert assignment.is_primary is True

        with pytest.raises(HTTPException) as denied:
            create_user(
                UserCreateRequest(
                    tenant_id="tenant_a",
                    username="sibling_member",
                    password="test-only",
                    employee_id="E_SIBLING",
                    initial_org_unit_id=fixture["sibling"].id,
                ),
                scoped_user,
                db,
            )
        assert denied.value.status_code == 403


def _fixture(db: Session) -> dict[str, object]:
    """创建两租户、三层组织、兼容管理员和范围管理员匿名夹具。"""

    db.add(Tenant(id="tenant_a", name="匿名企业"))
    db.add(Tenant(id="tenant_b", name="其他企业"))
    owner = User(
        id="tenant_owner",
        tenant_id="tenant_a",
        username="tenant_owner",
        role="admin",
        password_hash="test-only",
    )
    scoped_user = User(
        id="scoped_admin",
        tenant_id="tenant_a",
        username="scoped_admin",
        role="member",
        password_hash="test-only",
    )
    profile = EmployeeProfile(
        id="profile_scoped_admin",
        tenant_id="tenant_a",
        user_id=scoped_user.id,
        employee_id="E_SCOPE",
        employee_name="范围管理员",
    )
    db.add(owner)
    db.add(scoped_user)
    db.add(profile)
    root = ensure_organization_foundation(db, "tenant_a")
    foreign_root = ensure_organization_foundation(db, "tenant_b")
    division = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=root.id,
        code="DIVISION_A",
        name="事业部甲",
        unit_type_code="division",
    )
    department = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=division.id,
        code="DEPARTMENT_A",
        name="部门甲",
        unit_type_code="department",
    )
    sibling = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=root.id,
        code="DIVISION_B",
        name="事业部乙",
        unit_type_code="division",
    )
    ensure_builtin_governance_catalog(db, "tenant_a")
    db.commit()
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_a",
            BusinessRole.role_code == "governance_org_admin",
        )
    ).one()
    return {
        "owner": owner,
        "scoped_profile": profile,
        "org_admin_role": role,
        "root": root,
        "division": division,
        "department": department,
        "sibling": sibling,
        "foreign_root": foreign_root,
    }


def _test_session() -> Session:
    """创建加载完整模型的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
