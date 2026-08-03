"""
@Time       : 2026/07/22 10:45
@Author     : zhanglp8181
@File       : test_work_items_api.py
@CallChain  : pytest → work-items API → SopWorkItemService/任务箱投影
@Description: 验证任务箱可见性、平台管理员无业务旁路和服务端允许动作。
"""

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.work_items import (
    WorkItemCommandRequest,
    claim_work_item,
    get_work_item,
    list_work_items,
    page_work_items,
)
from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    SopInstance,
    SopNodeExecution,
    SopWorkItem,
    Tenant,
    User,
)
from app.sop_runtime.contracts import CompletionMode, WorkItemCompletionPolicy
from app.sop_runtime.definition import HumanTaskConfig, HumanTaskKind
from app.sop_runtime.work_items import SopWorkItemService


def test_task_inbox_exposes_server_actions_only_to_candidate() -> None:
    """验证申请人可查看进度但无处理动作，候选人获得认领动作。"""

    with _test_session() as db:
        applicant, approver, administrator, work_item_id = _seed_work_item(db)

        applicant_items = list_work_items(
            "tenant_demo",
            "pending",
            applicant,
            db,
        )
        approver_items = list_work_items(
            "tenant_demo",
            "pending",
            approver,
            db,
        )
        administrator_items = list_work_items(
            "tenant_demo",
            "pending",
            administrator,
            db,
        )

        assert applicant_items[0].id == work_item_id
        assert applicant_items[0].allowed_actions == []
        assert approver_items[0].allowed_actions == ["claim"]
        assert administrator_items == []


def test_platform_administrator_cannot_open_unrelated_work_item_detail() -> None:
    """验证平台管理员不因 admin 角色获得业务工作项详情权限。"""

    with _test_session() as db:
        _, _, administrator, work_item_id = _seed_work_item(db)

        try:
            get_work_item(work_item_id, "tenant_demo", administrator, db)
        except HTTPException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("platform administrator must not bypass work-item candidates")


def test_claim_endpoint_returns_assignee_actions_from_persisted_state() -> None:
    """验证候选人认领后只由服务端返回释放和结构化结果动作。"""

    with _test_session() as db:
        _, approver, _, work_item_id = _seed_work_item(db)

        claimed = claim_work_item(
            work_item_id,
            WorkItemCommandRequest(
                tenant_id="tenant_demo",
                command_id="claim-from-api",
                expected_revision=0,
            ),
            approver,
            db,
        )

        assert claimed.status == "claimed"
        assert claimed.assignee_user_id == approver.id
        assert claimed.allowed_actions == ["unclaim", "approved", "rejected"]


def test_inbox_hides_action_when_current_employee_permission_is_missing() -> None:
    """验证任务箱动作投影与命令鉴权一致，撤权候选不会看到误导性认领按钮。"""

    with _test_session() as db:
        _, approver, _, work_item_id = _seed_work_item(db)
        work_item = db.get(SopWorkItem, work_item_id)
        role = db.get(BusinessRole, "role_seal_approver")
        assert work_item is not None and role is not None
        work_item.action_permissions_json = {"claim": "seal.application.claim"}
        db.add(work_item)
        db.commit()

        without_permission = list_work_items("tenant_demo", "pending", approver, db)
        assert without_permission[0].allowed_actions == []

        role.permissions_json = ["seal.application.claim"]
        db.add(role)
        db.commit()
        with_permission = list_work_items("tenant_demo", "pending", approver, db)
        assert with_permission[0].allowed_actions == ["claim"]


def test_inbox_filters_by_actor_before_applying_tenant_safety_limit() -> None:
    """其他成员的 500 条新任务不能挤掉当前用户较早但仍活动的候选任务。"""

    with _test_session() as db:
        _, approver, _, work_item_id = _seed_work_item(db)
        for index in range(500):
            db.add(
                SopWorkItem(
                    id=f"unrelated_{index}",
                    tenant_id="tenant_demo",
                    instance_id=f"unrelated_instance_{index}",
                    node_execution_id=f"unrelated_execution_{index}",
                    skill_version_id="unrelated_version",
                    node_id="unrelated_review",
                    status="offered",
                    initiator_user_id="someone_else",
                )
            )
        db.commit()

        rows = list_work_items("tenant_demo", "pending", approver, db)

        assert [row.id for row in rows] == [work_item_id]


def test_task_inbox_page_returns_total_and_stable_pages_after_visibility_filter() -> None:
    """验证任务箱先应用用户可见性，再按稳定顺序返回总数和指定页。"""

    with _test_session() as db:
        applicant, _, _, work_item_id = _seed_work_item(db)
        existing = db.get(SopWorkItem, work_item_id)
        assert existing is not None
        db.add(
            SopWorkItem(
                id="work_item_newer",
                tenant_id="tenant_demo",
                instance_id="instance_test",
                node_execution_id="execution_newer",
                skill_version_id="version_test",
                node_id="human_review_2",
                status="offered",
                initiator_user_id=applicant.id,
                created_at=existing.created_at,
            )
        )
        db.commit()

        first_page = page_work_items(
            tenant_id="tenant_demo",
            view="pending",
            page=1,
            page_size=1,
            current_user=applicant,
            db=db,
        )
        second_page = page_work_items(
            tenant_id="tenant_demo",
            view="pending",
            page=2,
            page_size=1,
            current_user=applicant,
            db=db,
        )

        assert first_page.total == 2
        assert first_page.page == 1
        assert first_page.page_size == 1
        assert [item.id for item in first_page.items] == ["work_item_newer"]
        assert [item.id for item in second_page.items] == [work_item_id]


def _seed_work_item(db: Session) -> tuple[User, User, User, str]:
    """创建申请人、候选审批人、无业务权限管理员和待认领工作项。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    applicant = User(
        id="applicant",
        tenant_id="tenant_demo",
        username="applicant",
        password_hash="hash",
    )
    approver = User(
        id="approver",
        tenant_id="tenant_demo",
        username="approver",
        password_hash="hash",
    )
    administrator = User(
        id="administrator",
        tenant_id="tenant_demo",
        username="administrator",
        role="admin",
        password_hash="hash",
    )
    role = BusinessRole(
        id="role_seal_approver",
        tenant_id="tenant_demo",
        role_code="seal.approver",
        name="用章审批人",
    )
    profile = EmployeeProfile(
        id="profile_approver",
        tenant_id="tenant_demo",
        user_id=approver.id,
        employee_id="E200",
    )
    db.add(applicant)
    db.add(approver)
    db.add(administrator)
    db.add(role)
    db.add(profile)
    db.add(
        EmployeeRoleAssignment(
            tenant_id="tenant_demo",
            employee_profile_id=profile.id,
            business_role_id=role.id,
        )
    )
    instance = SopInstance(
        id="instance_test",
        tenant_id="tenant_demo",
        session_id="session_test",
        skill_id="approval_test",
        skill_version_id="version_test",
        skill_version="1.0.0",
        definition_checksum="a" * 64,
        status="running",
        active_slot_key="foreground:session_test",
        current_node_id="human_review",
    )
    execution = SopNodeExecution(
        id="execution_test",
        tenant_id="tenant_demo",
        instance_id=instance.id,
        node_id="human_review",
        status="running",
    )
    db.add(instance)
    db.add(execution)
    db.commit()
    work_item, _ = SopWorkItemService(db).offer(
        instance,
        execution,
        HumanTaskConfig(
            kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
            capability="human.structured_work_item",
            candidate_role_codes=(role.role_code,),
            completion_policy=WorkItemCompletionPolicy(
                mode=CompletionMode.ANY,
                claim_required=True,
            ),
        ),
        initiator_user_id=applicant.id,
    )
    db.commit()
    return applicant, approver, administrator, work_item.id


def _test_session() -> Session:
    """创建加载全部 SQLModel 表的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
