"""
@Time       : 2026/07/27 22:20
@Author     : zhanglp8181
@File       : test_expense_special_approval.py
@CallChain  : pytest → ApprovalRequestService → 顺序工作项决定与通用审批台账
@Description: 验证超标比例计算、单级/双级路由、顺序约束、决定审计和本人查询。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.approvals import ApprovalRequestError, ApprovalRequestService
from app.db.demo_sop_versions import (
    EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
    EXPENSE_SPECIAL_APPROVAL_VERSION,
    _expense_special_approval_content,
)
from app.db.models import (
    AgentProfile,
    ApprovalRequest,
    ApprovalRequestDecision,
    BusinessRole,
    EmployeeProfile,
    Skill,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    SopWorkItemDecision,
    Tenant,
    Tool,
    User,
)
from app.db.seed import seed_demo_data
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import plan_next_action
from app.sop_runtime.versioning import skill_content_checksum
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


def _session() -> Session:
    """创建共享单连接的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_people(db: Session) -> None:
    """创建申请人、部门负责人和财务负责人演示身份。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    for user_id, employee_id in (
        ("applicant", "E002"),
        ("department_manager", "E001"),
        ("finance_manager", "E003"),
    ):
        db.add(
            User(
                id=user_id,
                tenant_id="tenant_demo",
                username=user_id,
                role="member",
                password_hash="test",
            )
        )
        db.add(
            EmployeeProfile(
                id=f"profile_{user_id}",
                tenant_id="tenant_demo",
                user_id=user_id,
                employee_id=employee_id,
                employee_name=user_id,
            )
        )
    db.commit()


def _create(
    db: Session,
    *,
    original_limit: float,
    claimed_amount: float,
) -> dict[str, object]:
    """创建指定标准和申报金额的超标特批申请。"""

    return ApprovalRequestService(db).create_expense_special_approval(
        tenant_id="tenant_demo",
        actor_user_id="applicant",
        payload={
            "employee_id": "E002",
            "employee_name": "applicant",
            "expense_category": "差旅住宿",
            "original_limit": original_limit,
            "claimed_amount": claimed_amount,
            "over_limit_reason": "展会期间协议酒店满房",
        },
    )


def _seed_runtime(
    db: Session,
    request_number: str,
) -> tuple[SkillVersion, SopInstance]:
    """创建特批业务单关联的版本、实例和唯一创建操作。"""

    version = SkillVersion(
        id="skillver_special",
        tenant_id="tenant_demo",
        skill_id="expense_over_limit_approval",
        version="2.0.0",
        name="超标报销特批",
        content_json={},
        status="published",
    )
    instance = SopInstance(
        id="sopinst_special",
        tenant_id="tenant_demo",
        session_id="session_special",
        skill_id="expense_over_limit_approval",
        skill_version_id=version.id,
        skill_version=version.version,
        definition_checksum="checksum",
        status="waiting",
        current_node_id="department_special_approval",
    )
    execution = SopNodeExecution(
        id="sopnode_create",
        tenant_id="tenant_demo",
        instance_id=instance.id,
        node_id="create_special_approval",
        status="succeeded",
    )
    operation = SopOperation(
        tenant_id="tenant_demo",
        instance_id=instance.id,
        node_execution_id=execution.id,
        operation_name="expense.special_approval_create",
        idempotency_key="special-create",
        status="succeeded",
        result_json={"approval_request_id": request_number},
    )
    db.add(version)
    db.add(instance)
    db.add(execution)
    db.add(operation)
    db.commit()
    return version, instance


def _complete_step(
    db: Session,
    *,
    version: SkillVersion,
    instance: SopInstance,
    step: int,
    actor_user_id: str,
    outcome: str,
) -> SopWorkItem:
    """写入指定顺序步骤的已完成工作项和不可变决定。"""

    node_id = "department_special_approval" if step == 1 else "finance_special_approval"
    execution = SopNodeExecution(
        id=f"sopnode_step_{step}",
        tenant_id="tenant_demo",
        instance_id=instance.id,
        node_id=node_id,
        status="succeeded",
    )
    work_item = SopWorkItem(
        id=f"sopwork_step_{step}",
        tenant_id="tenant_demo",
        instance_id=instance.id,
        node_execution_id=execution.id,
        skill_version_id=version.id,
        node_id=node_id,
        status="completed",
        initiator_user_id="applicant",
        assignee_user_id=actor_user_id,
        outcome=outcome,
        comment=f"第 {step} 级{outcome}",
    )
    decision = SopWorkItemDecision(
        tenant_id="tenant_demo",
        work_item_id=work_item.id,
        actor_user_id=actor_user_id,
        outcome=outcome,
        comment=work_item.comment,
        idempotency_key=f"special-step-{step}",
    )
    db.add(execution)
    db.add(work_item)
    db.add(decision)
    db.commit()
    return work_item


def test_server_calculates_single_and_two_step_routes() -> None:
    """验证服务端以原标准为分母，并把 20% 边界冻结成单级或双级链。"""

    with _session() as db:
        _seed_people(db)
        single = _create(db, original_limit=1000, claimed_amount=1200)
        double = _create(db, original_limit=1000, claimed_amount=1200.01)

        assert single["over_limit_amount"] == 200
        assert single["over_limit_ratio"] == 0.2
        assert single["approval_route"] == "department_only"
        assert single["total_steps"] == 1
        assert double["approval_route"] == "department_finance"
        assert double["total_steps"] == 2

        with pytest.raises(ApprovalRequestError, match="高于原报销标准"):
            _create(db, original_limit=1000, claimed_amount=1000)


def test_two_step_approval_is_ordered_and_appends_both_decisions() -> None:
    """验证双级审批不能越级，部门批准后才允许财务负责人形成最终批准。"""

    with _session() as db:
        _seed_people(db)
        created = _create(db, original_limit=1000, claimed_amount=1300)
        request_number = str(created["approval_request_id"])
        version, instance = _seed_runtime(db, request_number)
        _complete_step(
            db,
            version=version,
            instance=instance,
            step=2,
            actor_user_id="finance_manager",
            outcome="approved",
        )

        with pytest.raises(ApprovalRequestError, match="当前等待"):
            ApprovalRequestService(db).decide_expense_special_approval(
                tenant_id="tenant_demo",
                approval_request_id=request_number,
                expected_step=2,
                expected_outcome="approved",
            )

        _complete_step(
            db,
            version=version,
            instance=instance,
            step=1,
            actor_user_id="department_manager",
            outcome="approved",
        )
        first = ApprovalRequestService(db).decide_expense_special_approval(
            tenant_id="tenant_demo",
            approval_request_id=request_number,
            expected_step=1,
            expected_outcome="approved",
        )
        final = ApprovalRequestService(db).decide_expense_special_approval(
            tenant_id="tenant_demo",
            approval_request_id=request_number,
            expected_step=2,
            expected_outcome="approved",
        )

        assert first["status"] == "pending"
        assert first["current_step"] == 2
        assert final["status"] == "approved"
        assert final["revision"] == 2
        audits = db.exec(
            select(ApprovalRequestDecision).order_by(
                ApprovalRequestDecision.step_number
            )
        ).all()
        assert [item.step_number for item in audits] == [1, 2]
        assert [item.actor_user_id for item in audits] == [
            "department_manager",
            "finance_manager",
        ]


def test_rejection_ends_request_and_query_is_applicant_only() -> None:
    """验证任一级驳回结束申请，且申请单号不能被其他员工用作查询凭证。"""

    with _session() as db:
        _seed_people(db)
        created = _create(db, original_limit=1000, claimed_amount=1100)
        request_number = str(created["approval_request_id"])
        version, instance = _seed_runtime(db, request_number)
        _complete_step(
            db,
            version=version,
            instance=instance,
            step=1,
            actor_user_id="department_manager",
            outcome="rejected",
        )

        rejected = ApprovalRequestService(db).decide_expense_special_approval(
            tenant_id="tenant_demo",
            approval_request_id=request_number,
            expected_step=1,
            expected_outcome="rejected",
        )
        own = ApprovalRequestService(db).query_expense_special_approval(
            tenant_id="tenant_demo",
            actor_user_id="applicant",
            payload={"approval_request_id": request_number},
        )

        assert rejected["status"] == "rejected"
        assert own["status"] == "rejected"
        with pytest.raises(ApprovalRequestError, match="本人发起"):
            ApprovalRequestService(db).query_expense_special_approval(
                tenant_id="tenant_demo",
                actor_user_id="finance_manager",
                payload={"approval_request_id": request_number},
            )
        row = db.exec(select(ApprovalRequest)).one()
        assert row.status == "rejected"


def test_definition_compiles_and_routes_department_before_finance() -> None:
    """验证 v2 定义零告警，并只在部门步骤回写 pending 后进入财务步骤。"""

    definition = compile_legacy_skill_card(_expense_special_approval_content())
    assert definition.diagnostics == ()
    assert definition.skill_version == EXPENSE_SPECIAL_APPROVAL_VERSION
    nodes = {node.node_id: node for node in definition.nodes}
    assert nodes["department_special_approval"].config.candidate_role_codes == (
        "expense_department_approver",
    )
    assert nodes["finance_special_approval"].config.candidate_role_codes == (
        "expense_finance_approver",
    )

    next_step = plan_next_action(
        definition,
        current_node_id="record_department_approve",
        slots={},
        tool_results={
            "department_decision": {
                "status": "succeeded",
                "data": {"status": "pending", "current_step": 2},
            }
        },
    )
    single_done = plan_next_action(
        definition,
        current_node_id="record_department_approve",
        slots={},
        tool_results={
            "department_decision": {
                "status": "succeeded",
                "data": {"status": "approved", "current_step": 1},
            }
        },
    )
    assert next_step.next_node_id == "finance_special_approval"
    assert single_done.next_node_id == "special_application_approved"


def test_seed_publishes_v2_roles_tools_and_builtin_create() -> None:
    """验证种子发布特批 v2、两级角色和固定白名单工具并可实际创建。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == "expense_over_limit_approval",
                SkillVersion.version == EXPENSE_SPECIAL_APPROVAL_VERSION,
            )
        ).one()
        roles = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code.in_(
                    (
                        "expense_special_approval_operator",
                        "expense_department_approver",
                        "expense_finance_approver",
                    )
                )
            )
        ).all()
        tools = db.exec(
            select(Tool).where(Tool.name.like("expense.special_approval%"))
        ).all()
        agent = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "财务",
            )
        ).one()

        assert skill.status == "published"
        assert {role.role_code for role in roles} == {
            "expense_special_approval_operator",
            "expense_department_approver",
            "expense_finance_approver",
        }
        assert len(tools) == 6
        assert {tool.tool_type for tool in tools} == {"builtin"}
        result = ToolExecutor(db).execute(
            "tenant_demo",
            ToolCall(
                name="expense.special_approval_create",
                arguments={
                    "employee_id": "E002",
                    "employee_name": "演示员工",
                    "expense_category": "差旅住宿",
                    "original_limit": 1000,
                    "claimed_amount": 1300,
                    "over_limit_reason": "展会期间酒店涨价",
                },
            ),
            active_skill_id="expense_over_limit_approval",
            agent_id=agent.id,
            actor_user_id="user_demo",
        )

        assert result.success is True
        assert result.data["approval_route"] == "department_finance"
        assert result.data["total_steps"] == 2


def test_seed_derives_org_scoped_v21_without_rewriting_v2() -> None:
    """验证代表版本只收口部门节点，保留旧快照与集中财务范围并可重复 seed。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        versions = db.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id == "expense_over_limit_approval")
            .order_by(SkillVersion.version)
        ).all()
        v2 = next(item for item in versions if item.version == EXPENSE_SPECIAL_APPROVAL_VERSION)
        v21 = next(
            item
            for item in versions
            if item.version == EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION
        )
        old_checksum = v2.content_checksum
        old_content = dict(v2.content_json)
        definition = compile_legacy_skill_card(v21.content_json)
        nodes = {node.node_id: node for node in definition.nodes}

        assert v2.content_json == _expense_special_approval_content()
        assert v2.content_checksum == skill_content_checksum(v2.content_json)
        assert v21.derived_from_version_id == v2.id
        assert v21.status == "published"
        assert definition.diagnostics == ()
        assert nodes[
            "department_special_approval"
        ].config.participant_scope_resolver.value == "initiator_primary_org_subtree"
        assert (
            nodes["finance_special_approval"].config.participant_scope_resolver.value
            == "tenant"
        )
        skill = db.exec(
            select(Skill).where(Skill.skill_id == "expense_over_limit_approval")
        ).one()
        assert skill.version == EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION

        seed_demo_data(db)
        db.commit()
        repeated_versions = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == "expense_over_limit_approval"
            )
        ).all()
        db.refresh(v2)
        assert len(repeated_versions) == len(versions)
        assert v2.content_json == old_content
        assert v2.content_checksum == old_checksum
