"""
@Time       : 2026/07/22 09:27
@Author     : zhanglp8181
@File       : test_organization_roles.py
@CallChain  : pytest → organization API → BusinessRole/EmployeeRoleAssignment
@Description: 验证公司业务角色、权限目录、员工多角色任职和自我提权防护。
"""

from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.organization import (
    AgentRoleBindingCreate,
    BusinessRoleCreate,
    BusinessRoleUpdate,
    EmployeeRoleAssignmentCreate,
    PermissionDefinitionCreate,
    PermissionDefinitionUpdate,
    RoleCategoryCreate,
    RoleCategoryUpdate,
    create_permission_definition,
    create_role_category,
    create_business_role,
    create_agent_role_binding,
    create_employee_role_assignment,
    deactivate_business_role,
    list_permission_definitions,
    list_role_categories,
    list_business_roles,
    list_agent_role_bindings,
    list_employee_role_assignments,
    page_business_roles,
    update_permission_definition,
    update_role_category,
    update_business_role,
)
from app.db.models import (
    AgentProfile,
    BusinessRolePermission,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Tenant,
    User,
    utc_now,
)
from app.security.auth import hash_password


def test_business_role_crud_keeps_code_stable_and_soft_deletes() -> None:
    """验证角色编码创建后不可变，删除操作只停用并保留历史实体。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)

        created = create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="admin_seal_approver",
                name="用章审批人",
                category="administration",
                permissions=[
                    "admin.seal_application.approve",
                    "admin.seal_application.approve",
                ],
            ),
            administrator,
            db,
        )
        updated = update_business_role(
            created.id,
            BusinessRoleUpdate(
                tenant_id="tenant_demo",
                name="印章审批人",
                permissions=[
                    "admin.seal_application.reject",
                    "admin.seal_application.approve",
                ],
            ),
            administrator,
            db,
        )
        deactivated = deactivate_business_role(
            created.id,
            "tenant_demo",
            administrator,
            db,
        )

        assert updated.role_code == "admin_seal_approver"
        assert updated.name == "印章审批人"
        assert updated.permissions == [
            "admin.seal_application.approve",
            "admin.seal_application.reject",
        ]
        assert deactivated.status == "inactive"
        assert list_business_roles("tenant_demo", administrator, db)[0].status == "inactive"


def test_business_role_page_uses_stable_database_pagination_and_global_counts() -> None:
    """验证角色目录数据库分页稳定，并返回不受当前页限制的总数与活动数。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        for index in range(13):
            create_business_role(
                BusinessRoleCreate(
                    tenant_id="tenant_demo",
                    role_code=f"test_role_{index:02d}",
                    name=f"测试角色 {index:02d}",
                ),
                administrator,
                db,
            )

        first = page_business_roles("tenant_demo", 1, 10, administrator, db)
        second = page_business_roles("tenant_demo", 2, 10, administrator, db)

    assert first.total == second.total
    assert first.active_count == first.total
    assert len(first.items) == 10
    assert second.items
    assert {item.id for item in first.items}.isdisjoint(item.id for item in second.items)


def test_permission_catalog_supports_query_selection_and_relational_refill() -> None:
    """验证权限可检索选择，角色详情从规范关系回填而非信任自由文本。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        role = create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="it_support_engineer",
                name="IT 支持工程师",
                category="information_technology",
                permissions=["it.ticket.claim", "it.ticket.resolve"],
            ),
            administrator,
            db,
        )

        queried = list_permission_definitions(
            "tenant_demo",
            "认领",
            "information_technology",
            "active",
            administrator,
            db,
        )
        mappings = db.exec(
            select(BusinessRolePermission).where(
                BusinessRolePermission.business_role_id == role.id
            )
        ).all()

        assert [permission.permission_code for permission in queried] == ["it.ticket.claim"]
        assert role.permissions == ["it.ticket.claim", "it.ticket.resolve"]
        assert len(mappings) == 2
        assert {
            category.code
            for category in list_role_categories("tenant_demo", administrator, db)
        } == {
            "governance",
            "human_resources",
            "finance",
            "administration",
            "information_technology",
            "legal_compliance",
            "cross_functional",
        }


def test_permission_and_category_catalogs_have_stable_codes_and_reference_guards() -> None:
    """验证目录可查询治理，稳定编码不可修改且活动引用会阻止停用。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        category = create_role_category(
            RoleCategoryCreate(
                tenant_id="tenant_demo",
                code="procurement",
                name="采购",
                description="采购申请与供应商协同",
                role_code_prefix="purchase",
            ),
            administrator,
            db,
        )
        permission = create_permission_definition(
            PermissionDefinitionCreate(
                tenant_id="tenant_demo",
                permission_code="purchase.order.approve",
                name="审批采购单",
                category="procurement",
                resource="purchase.order",
                action="approve",
            ),
            administrator,
            db,
        )
        create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="purchase_order_approver",
                name="采购审批人",
                category="procurement",
                permissions=["purchase.order.approve"],
            ),
            administrator,
            db,
        )

        updated = update_permission_definition(
            permission.id,
            PermissionDefinitionUpdate(
                tenant_id="tenant_demo",
                name="采购订单审批",
            ),
            administrator,
            db,
        )
        assert updated.permission_code == "purchase.order.approve"
        assert updated.name == "采购订单审批"

        try:
            update_permission_definition(
                permission.id,
                PermissionDefinitionUpdate(tenant_id="tenant_demo", status="inactive"),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 409
            assert error.detail["code"] == "PERMISSION_DEFINITION_REFERENCED"
        else:
            raise AssertionError("referenced permission must not be deactivated")

        try:
            update_role_category(
                category.id,
                RoleCategoryUpdate(tenant_id="tenant_demo", status="inactive"),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 409
            assert error.detail["code"] == "ROLE_CATEGORY_REFERENCED"
        else:
            raise AssertionError("referenced category must not be deactivated")
        assert category.code == "procurement"


def test_role_rejects_arbitrary_category_and_unknown_permission() -> None:
    """验证角色分类和权限编码只能来自服务端受控目录。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        for request, expected_code in (
            (
                BusinessRoleCreate(
                    tenant_id="tenant_demo",
                    role_code="cross_process_owner",
                    name="流程负责人",
                    category="随意分类",
                ),
                None,
            ),
            (
                BusinessRoleCreate(
                    tenant_id="tenant_demo",
                    role_code="cross_process_owner",
                    name="流程负责人",
                    permissions=["made.up.permission"],
                ),
                "UNKNOWN_PERMISSION_DEFINITIONS",
            ),
        ):
            try:
                create_business_role(request, administrator, db)
            except HTTPException as error:
                assert error.status_code == 422
                if expected_code is not None:
                    assert error.detail["code"] == expected_code
                    assert error.detail["permission_codes"] == ["made.up.permission"]
            else:
                raise AssertionError("arbitrary role governance input must be rejected")


def test_employee_can_hold_multiple_business_roles_without_duplicate_candidate_identity() -> None:
    """验证同一员工可同时持有多个业务角色且每条任职独立保存。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        employee_profile = _seed_employee(db, user_id="employee", employee_id="E100")
        for role_code, role_name in (
            ("cross_department_manager", "部门负责人"),
            ("admin_seal_approver", "用章审批人"),
        ):
            create_business_role(
                BusinessRoleCreate(
                    tenant_id="tenant_demo",
                    role_code=role_code,
                    name=role_name,
                ),
                administrator,
                db,
            )
            create_employee_role_assignment(
                EmployeeRoleAssignmentCreate(
                    tenant_id="tenant_demo",
                    employee_profile_id=employee_profile.id,
                    role_code=role_code,
                    grant_reason="测试员工多角色授权",
                    effective_until=utc_now() + timedelta(days=30),
                ),
                administrator,
                db,
            )

        assignments = list_employee_role_assignments("tenant_demo", administrator, db)

        assert {assignment.role_code for assignment in assignments} == {
            "cross_department_manager",
            "admin_seal_approver",
        }
        assert {assignment.user_id for assignment in assignments} == {"employee"}
        assert len(db.exec(select(EmployeeRoleAssignment)).all()) == 2


def test_active_employee_role_assignment_is_idempotent_and_keeps_original_grant_contract() -> None:
    """验证重复提交活动任职不会改写原授权原因、有效期或产生重复实体。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        employee_profile = _seed_employee(db, user_id="employee", employee_id="E101")
        create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="expense_department_approver",
                name="超标报销部门负责人",
            ),
            administrator,
            db,
        )
        original_until = utc_now() + timedelta(days=30)
        original = create_employee_role_assignment(
            EmployeeRoleAssignmentCreate(
                tenant_id="tenant_demo",
                employee_profile_id=employee_profile.id,
                role_code="expense_department_approver",
                grant_reason="季度备用审批授权",
                effective_until=original_until,
            ),
            administrator,
            db,
        )

        repeated = create_employee_role_assignment(
            EmployeeRoleAssignmentCreate(
                tenant_id="tenant_demo",
                employee_profile_id=employee_profile.id,
                role_code="expense_department_approver",
                grant_reason="不应覆盖原授权依据",
                effective_until=utc_now() + timedelta(days=60),
            ),
            administrator,
            db,
        )

        assert repeated.id == original.id
        assert repeated.grant_reason == "季度备用审批授权"
        assert repeated.effective_until == original.effective_until
        assert len(db.exec(select(EmployeeRoleAssignment)).all()) == 1


def test_expired_employee_role_assignment_can_be_reactivated_with_a_new_contract() -> None:
    """验证已到期但状态未归档的任职可原主键续期，并记录新的授权依据。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        employee_profile = _seed_employee(db, user_id="employee", employee_id="E102")
        create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="expense_department_approver",
                name="超标报销部门负责人",
            ),
            administrator,
            db,
        )
        original = create_employee_role_assignment(
            EmployeeRoleAssignmentCreate(
                tenant_id="tenant_demo",
                employee_profile_id=employee_profile.id,
                role_code="expense_department_approver",
                grant_reason="原季度备用审批授权",
                effective_until=utc_now() + timedelta(days=1),
            ),
            administrator,
            db,
        )
        stored = db.get(EmployeeRoleAssignment, original.id)
        assert stored is not None
        stored.effective_until = utc_now() - timedelta(minutes=1)
        db.add(stored)
        db.commit()

        renewed = create_employee_role_assignment(
            EmployeeRoleAssignmentCreate(
                tenant_id="tenant_demo",
                employee_profile_id=employee_profile.id,
                role_code="expense_department_approver",
                grant_reason="新季度备用审批续期",
                effective_until=utc_now() + timedelta(days=90),
            ),
            administrator,
            db,
        )

        assert renewed.id == original.id
        assert renewed.grant_reason == "新季度备用审批续期"
        assert renewed.effective_until is not None
        assert datetime.fromisoformat(renewed.effective_until) > utc_now()
        assert len(db.exec(select(EmployeeRoleAssignment)).all()) == 1


def test_administrator_cannot_assign_business_role_to_self() -> None:
    """验证平台管理员不能通过组织任职接口给自己的员工档案授权。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        administrator_profile = _seed_employee(
            db,
            user_id=administrator.id,
            employee_id="E001",
        )
        create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="admin_seal_approver",
                name="用章审批人",
            ),
            administrator,
            db,
        )

        try:
            create_employee_role_assignment(
                EmployeeRoleAssignmentCreate(
                    tenant_id="tenant_demo",
                    employee_profile_id=administrator_profile.id,
                    role_code="admin_seal_approver",
                    grant_reason="测试管理员自我授权防护",
                    effective_until=utc_now() + timedelta(days=30),
                ),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 400
            assert error.detail == "Cannot change your own business roles"
        else:
            raise AssertionError("administrator must not grant a business role to self")


def test_agent_can_hold_multiple_roles_without_becoming_human_candidate() -> None:
    """验证数字员工可绑定多个角色和人类监督者，但绑定实体与员工任职严格分表。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        supervisor = _seed_employee(db, user_id="supervisor", employee_id="E300")
        agent = AgentProfile(
            id="agent_admin_assistant",
            tenant_id="tenant_demo",
            name="行政事务管家",
        )
        db.add(agent)
        db.commit()
        for role_code, role_name in (
            ("admin_seal_assistant", "用章办理助手"),
            ("admin_office_concierge", "行政事务管家"),
        ):
            create_business_role(
                BusinessRoleCreate(
                    tenant_id="tenant_demo",
                    role_code=role_code,
                    name=role_name,
                ),
                administrator,
                db,
            )
            create_agent_role_binding(
                AgentRoleBindingCreate(
                    tenant_id="tenant_demo",
                    agent_id=agent.id,
                    role_code=role_code,
                    assignment_mode="assist",
                    supervisor_employee_profile_id=supervisor.id,
                ),
                administrator,
                db,
            )

        bindings = list_agent_role_bindings("tenant_demo", administrator, db)

        assert {binding.role_code for binding in bindings} == {
            "admin_seal_assistant",
            "admin_office_concierge",
        }
        assert {binding.supervisor_employee_id for binding in bindings} == {"E300"}
        assert db.exec(select(EmployeeRoleAssignment)).all() == []


def test_open_gallery_resource_pool_cannot_hold_business_role() -> None:
    """开放广场目录不是执行主体，组织角色写入口必须拒绝其角色绑定。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        overall = AgentProfile(
            id="agent_overall",
            tenant_id="tenant_demo",
            name="开放广场资源池",
            is_overall=True,
        )
        db.add(overall)
        db.commit()
        create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="platform_demo_operator",
                name="平台演示办理人",
            ),
            administrator,
            db,
        )

        try:
            create_agent_role_binding(
                AgentRoleBindingCreate(
                    tenant_id="tenant_demo",
                    agent_id=overall.id,
                    role_code="platform_demo_operator",
                ),
                administrator,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 422
            assert error.detail == {
                "code": "AGENT_EXECUTION_SUBJECT_REQUIRED",
                "message": "开放广场资源池不是数字员工，不能绑定业务角色。",
            }
        else:
            raise AssertionError("开放广场资源池不得绑定业务角色")


def test_assignment_rejects_invalid_effective_range_and_cross_tenant_profile() -> None:
    """验证任职有效期和租户边界在写入口确定性校验。"""

    with _test_session() as db:
        administrator = _seed_administrator(db)
        db.add(Tenant(id="tenant_other", name="Other"))
        other_profile = EmployeeProfile(
            id="profile_other",
            tenant_id="tenant_other",
            user_id="other_user",
            employee_id="E900",
        )
        db.add(other_profile)
        db.commit()
        create_business_role(
            BusinessRoleCreate(
                tenant_id="tenant_demo",
                role_code="admin_seal_approver",
                name="用章审批人",
            ),
            administrator,
            db,
        )

        for request, expected_detail in (
            (
                EmployeeRoleAssignmentCreate(
                    tenant_id="tenant_demo",
                    employee_profile_id=other_profile.id,
                    role_code="admin_seal_approver",
                    grant_reason="测试无效员工档案",
                    effective_until=utc_now() + timedelta(days=30),
                ),
                "Active employee profile not found",
            ),
            (
                EmployeeRoleAssignmentCreate(
                    tenant_id="tenant_demo",
                    employee_profile_id=_seed_employee(
                        db,
                        user_id="employee",
                        employee_id="E100",
                    ).id,
                    role_code="admin_seal_approver",
                    grant_reason="测试无效授权区间",
                    effective_from=datetime(2026, 8, 1),
                    effective_until=datetime(2026, 7, 1),
                ),
                "Effective until must be later than effective from",
            ),
        ):
            try:
                create_employee_role_assignment(request, administrator, db)
            except HTTPException as error:
                assert error.status_code in {400, 404}
                assert error.detail == expected_detail
            else:
                raise AssertionError("invalid assignment boundary must be rejected")


def _seed_administrator(db: Session) -> User:
    """创建测试租户及平台管理员并提交。"""

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
    return administrator


def _seed_employee(db: Session, *, user_id: str, employee_id: str) -> EmployeeProfile:
    """创建测试员工账号和唯一员工档案并返回档案。"""

    user = db.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            tenant_id="tenant_demo",
            username=user_id,
            password_hash=hash_password("secret"),
        )
        db.add(user)
    profile = EmployeeProfile(
        tenant_id="tenant_demo",
        user_id=user_id,
        employee_id=employee_id,
        employee_name=user_id,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _test_session() -> Session:
    """创建隔离的内存 SQLite 会话，加载全部 SQLModel 表结构。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
