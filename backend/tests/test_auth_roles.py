"""
@Time       : 2026/07/22 19:40
@Author     : zhanglp8181
@File       : test_auth_roles.py
@CallChain  : pytest → auth API → User/EmployeeProfile/BusinessRole
@Description: 验证平台账号、员工身份和公司业务角色的分层授权边界。
"""

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.auth import (
    LoginRequest,
    MemberCategoryCreateRequest,
    MemberCategoryUpdateRequest,
    TenantDisplayUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
    create_user,
    create_member_category,
    enterprise_context,
    login,
    update_member_category,
    update_tenant_display,
    update_user,
)
from app.db.models import BusinessRole, EmployeeProfile, EmployeeRoleAssignment, Tenant, User
from app.security.auth import create_access_token, get_current_user, hash_password


def test_unknown_login_does_not_create_account() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        try:
            login(LoginRequest(tenant_id="tenant_demo", username="missing", password="secret"), db)
        except HTTPException as error:
            assert error.status_code == 401
            assert error.detail == "Invalid username or password"
        else:
            raise AssertionError("unknown account must not be created during login")

        assert db.exec(select(User)).all() == []


def test_database_role_controls_account_management() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        member_named_admin = User(
            id="user_named_admin",
            tenant_id="tenant_demo",
            username="admin",
            role="member",
            password_hash=hash_password("secret"),
        )
        role_admin = User(
            id="user_role_admin",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(member_named_admin)
        db.add(role_admin)
        db.commit()

        try:
            create_user(
                UserCreateRequest(tenant_id="tenant_demo", username="blocked", password="secret"),
                member_named_admin,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("an admin-looking username must not grant administrator access")

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="created_admin",
                password="secret",
                role="admin",
            ),
            role_admin,
            db,
        )
        assert created.role == "admin"

        updated = update_user(
            created.id,
            UserUpdateRequest(tenant_id="tenant_demo", role="member"),
            role_admin,
            db,
        )
        assert updated.role == "member"


def test_account_management_binds_unique_employee_profile() -> None:
    """验证管理员可维护工号绑定，且租户内不能重复绑定同一工号。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        administrator = User(
            id="administrator",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(administrator)
        db.commit()

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="employee",
                password="secret",
                employee_id="E001",
                employee_name="员工一",
                department_id="FINANCE",
            ),
            administrator,
            db,
        )
        profile = db.exec(select(EmployeeProfile)).one()

        assert created.employee_id == "E001"
        assert created.employee_name == "员工一"
        assert profile.user_id == created.id
        assert profile.department_id == "FINANCE"

        try:
            create_user(
                UserCreateRequest(
                    tenant_id="tenant_demo",
                    username="duplicate",
                    password="secret",
                    employee_id="E001",
                ),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("employee ID must be unique within a tenant")


def test_account_business_roles_are_separate_from_platform_admin_role() -> None:
    """验证账号管理独立维护平台角色和公司业务任职。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        administrator = User(
            id="administrator",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(administrator)
        db.add(
            BusinessRole(
                id="role_finance",
                tenant_id="tenant_demo",
                role_code="finance_expense_specialist",
                name="财务报销专员",
                permissions_json=["expense.quota.read:any"],
            )
        )
        db.commit()

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="finance_employee",
                password="secret",
                role="member",
                employee_id="E100",
                business_role_codes=["finance_expense_specialist"],
            ),
            administrator,
            db,
        )

        assert created.role == "member"
        assert created.business_role_codes == ["finance_expense_specialist"]
        assert db.exec(select(EmployeeRoleAssignment)).one().status == "active"


def test_administrator_cannot_grant_own_business_role() -> None:
    """验证平台管理员不能通过账号管理接口给自己追加业务权限。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        administrator = User(
            id="administrator",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(administrator)
        db.add(
            BusinessRole(
                id="role_finance",
                tenant_id="tenant_demo",
                role_code="finance_expense_specialist",
                name="财务报销专员",
            )
        )
        db.commit()

        try:
            update_user(
                administrator.id,
                UserUpdateRequest(
                    tenant_id="tenant_demo",
                    business_role_codes=["finance_expense_specialist"],
                ),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 400
            assert error.detail == "Cannot change your own employee identity or business roles"
        else:
            raise AssertionError("administrator must not grant own business roles")

        assert db.exec(select(EmployeeRoleAssignment)).all() == []


def test_account_management_validates_category_and_synchronizes_lifecycle() -> None:
    """验证成员类别必须活动，且停用/离职状态同步到员工档案而不删除身份。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        administrator = User(
            id="administrator",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(administrator)
        db.commit()

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="contractor",
                password="secret",
                employee_id="C001",
                member_category_code="contractor",
            ),
            administrator,
            db,
        )
        assert created.membership_status == "active"
        assert created.member_category_code == "contractor"
        assert created.joined_at is not None

        suspended = update_user(
            created.id,
            UserUpdateRequest(
                tenant_id="tenant_demo",
                membership_status="suspended",
            ),
            administrator,
            db,
        )
        profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == created.id)
        ).one()
        assert suspended.membership_status == "suspended"
        assert suspended.left_at is None
        assert profile.status == "suspended"
        assert profile.leave_date is None

        left = update_user(
            created.id,
            UserUpdateRequest(
                tenant_id="tenant_demo",
                membership_status="left",
            ),
            administrator,
            db,
        )
        db.refresh(profile)
        assert left.left_at is not None
        assert profile.status == "left"
        assert profile.leave_date is not None

        try:
            create_user(
                UserCreateRequest(
                    tenant_id="tenant_demo",
                    username="unknown-category",
                    password="secret",
                    member_category_code="invented",
                ),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 400
            assert error.detail == "Unknown member category: invented"
        else:
            raise AssertionError("unknown member categories must be rejected")


def test_suspended_member_cannot_login_or_reuse_existing_token() -> None:
    """验证成员停用后新登录和停用前签发的 token 都立即失效。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        member = User(
            id="member",
            tenant_id="tenant_demo",
            username="member",
            password_hash=hash_password("secret"),
        )
        db.add(member)
        db.commit()
        token = create_access_token(member)

        member.membership_status = "suspended"
        db.add(member)
        db.commit()

        with pytest.raises(HTTPException) as login_error:
            login(
                LoginRequest(
                    tenant_id="tenant_demo",
                    username="member",
                    password="secret",
                ),
                db,
            )
        assert login_error.value.status_code == 403
        assert login_error.value.detail == "Member account is not active"

        with pytest.raises(HTTPException) as token_error:
            get_current_user(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                db,
            )
        assert token_error.value.status_code == 403
        assert token_error.value.detail == "Member account is not active"


def test_enterprise_context_uses_authenticated_member_tenant() -> None:
    """验证运行时企业上下文只从认证成员解析，不接受客户端 tenant 覆盖。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="企业甲"))
        db.add(Tenant(id="tenant_b", name="企业乙"))
        member = User(
            id="member_a",
            tenant_id="tenant_a",
            username="member",
            password_hash=hash_password("secret"),
        )
        db.add(member)
        db.commit()

        context = enterprise_context(member, db)

        assert context.tenant.id == "tenant_a"
        assert context.tenant.name == "企业甲"
        assert context.member.id == "member_a"
        assert context.member.tenant_id == "tenant_a"
        assert context.is_administrator is False


def test_member_category_code_is_immutable_and_updates_use_revision() -> None:
    """验证自定义成员类别只能新增稳定编码，并以 revision 修改名称或停用。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        administrator = User(
            id="administrator",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(administrator)
        db.commit()

        created = create_member_category(
            MemberCategoryCreateRequest(
                tenant_id="tenant_demo",
                code="partner_employee",
                name="合作方员工",
            ),
            administrator,
            db,
        )
        assert created.code == "partner_employee"
        assert created.is_builtin is False
        assert created.revision == 0

        updated = update_member_category(
            "partner_employee",
            MemberCategoryUpdateRequest(
                tenant_id="tenant_demo",
                name="生态伙伴员工",
                status="inactive",
                sort_order=120,
                revision=0,
            ),
            administrator,
            db,
        )
        assert updated.code == "partner_employee"
        assert updated.name == "生态伙伴员工"
        assert updated.status == "inactive"
        assert updated.revision == 1

        with pytest.raises(HTTPException) as revision_error:
            update_member_category(
                "partner_employee",
                MemberCategoryUpdateRequest(
                    tenant_id="tenant_demo",
                    name="旧页面覆盖",
                    status="active",
                    sort_order=100,
                    revision=0,
                ),
                administrator,
                db,
            )
        assert revision_error.value.status_code == 409

        with pytest.raises(HTTPException) as inactive_error:
            create_user(
                UserCreateRequest(
                    tenant_id="tenant_demo",
                    username="partner",
                    password="secret",
                    member_category_code="partner_employee",
                ),
                administrator,
                db,
            )
        assert inactive_error.value.detail == "Inactive member category: partner_employee"


def test_tenant_display_name_can_change_without_changing_stable_id() -> None:
    """验证管理员只能修改企业显示名，普通成员不能借此修改企业上下文。"""

    with _test_session() as db:
        tenant = Tenant(id="tenant_demo", name="原企业名")
        administrator = User(
            id="administrator",
            tenant_id=tenant.id,
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        member = User(
            id="member",
            tenant_id=tenant.id,
            username="member",
            password_hash=hash_password("secret"),
        )
        db.add(tenant)
        db.add(administrator)
        db.add(member)
        db.commit()

        updated = update_tenant_display(
            TenantDisplayUpdateRequest(name="共格示范企业"),
            administrator,
            db,
        )
        assert updated.id == "tenant_demo"
        assert updated.name == "共格示范企业"

        with pytest.raises(HTTPException) as permission_error:
            update_tenant_display(
                TenantDisplayUpdateRequest(name="越权企业名"),
                member,
                db,
            )
        assert permission_error.value.status_code == 403
        assert db.get(Tenant, "tenant_demo").name == "共格示范企业"


def _test_session() -> Session:
    """创建包含全部模型表的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
