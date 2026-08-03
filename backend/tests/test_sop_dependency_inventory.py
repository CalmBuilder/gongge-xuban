"""
@Time       : 2026/07/29 15:10
@Author     : zhanglp8181
@File       : test_sop_dependency_inventory.py
@CallChain  : pytest → 当前发布 SOP → M3/M4/M5 既有事实 → 业务依赖预检
@Description: 验证参与者、数字员工、工具和知识依赖只读复核，并区分可执行路径与绑定告警。
"""

from __future__ import annotations

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    ManagementAuditLog,
    MemberOrgAssignment,
    OrganizationUnit,
    Position,
    PositionAssignment,
    Skill,
    User,
)
from app.db.seed import seed_demo_data
from app.organization.roles import bind_position_business_role
from app.sop_runtime.dependency_inventory import (
    DependencyReadiness,
    build_sop_dependency_assessment,
)
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


def test_seeded_representative_sops_have_executable_dependency_paths() -> None:
    """种子人工与自动代表 SOP 必须各有一条完整数字员工执行路径。"""

    with _seeded_session() as db:
        expense = _assessment(db, "expense_over_limit_approval")
        graph = _assessment(db, "skill_graph_visual_demo")

        assert expense.readiness in {
            DependencyReadiness.READY,
            DependencyReadiness.ATTENTION_REQUIRED,
        }
        assert expense.human_task_count == 2
        assert expense.tool_operation_count == 6
        assert expense.knowledge_task_count == 1
        assert expense.executable_agent_count >= 1
        assert len(expense.human_participants) == 2
        assert all(item.eligible_candidate_count > 0 for item in expense.human_participants)
        assert expense.agent_paths
        assert all(item.resource_binding_ids for item in expense.agent_paths)

        assert graph.readiness is DependencyReadiness.READY
        assert graph.human_task_count == 0
        assert graph.tool_operation_count == 1
        assert graph.knowledge_task_count == 1
        assert graph.bound_agent_count == 1
        assert graph.executable_agent_count == 1
        assert graph.agent_paths[0].executable is True


def test_dependency_assessment_blocks_human_role_without_effective_candidate() -> None:
    """角色目录存在但没有有效真人时必须阻断，不能把目录完整误报为人员覆盖。"""

    with _seeded_session() as db:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == "expense_finance_approver"
            )
        ).one()
        assignments = db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.business_role_id == role.id
            )
        ).all()
        for assignment in assignments:
            assignment.status = "inactive"
            db.add(assignment)
        db.commit()

        assessment = _assessment(db, "expense_over_limit_approval")
        participant = next(
            item
            for item in assessment.human_participants
            if role.role_code in item.role_codes
        )

        assert participant.eligible_candidate_count == 0
        assert "PARTICIPANT_NO_ELIGIBLE_CANDIDATE" in participant.issue_codes
        assert "PARTICIPANT_NO_ELIGIBLE_CANDIDATE" in assessment.issue_codes
        assert assessment.readiness is DependencyReadiness.BLOCKED


def test_dependency_assessment_checks_each_active_initiator_org_context() -> None:
    """动态组织范围必须逐个验证当前主组织，不能用全租户有人承担角色代替覆盖。"""

    with _seeded_session() as db:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == "expense_department_approver"
            )
        ).one()
        role_assignment = db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.business_role_id == role.id,
                EmployeeRoleAssignment.status == "active",
            )
        ).first()
        assert role_assignment is not None
        role_holder = db.get(EmployeeProfile, role_assignment.employee_profile_id)
        assert role_holder is not None
        other_profile = db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.status == "active",
                EmployeeProfile.id != role_holder.id,
            )
        ).first()
        assert other_profile is not None
        first_org = OrganizationUnit(
            id="org_context_first",
            tenant_id="tenant_demo",
            code="ORG-CONTEXT-FIRST",
            name="第一发起组织",
            unit_type_code="department",
            tree_path="/org_context_first/",
            depth=0,
        )
        second_org = OrganizationUnit(
            id="org_context_second",
            tenant_id="tenant_demo",
            code="ORG-CONTEXT-SECOND",
            name="第二发起组织",
            unit_type_code="department",
            tree_path="/org_context_second/",
            depth=0,
        )
        db.add(first_org)
        db.add(second_org)
        db.add(
            MemberOrgAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=role_holder.id,
                org_unit_id=first_org.id,
            )
        )
        db.add(
            MemberOrgAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=other_profile.id,
                org_unit_id=second_org.id,
            )
        )
        db.commit()

        assessment = _assessment(db, "expense_over_limit_approval")
        participant = next(
            item
            for item in assessment.human_participants
            if role.role_code in item.role_codes
        )

        assert participant.context_count == 2
        assert participant.covered_context_count == 0
        assert participant.uncovered_org_unit_ids == (
            "org_context_first",
            "org_context_second",
        )
        assert "PARTICIPANT_CONTEXT_UNCOVERED" in participant.issue_codes
        assert assessment.readiness is DependencyReadiness.BLOCKED


def test_dependency_assessment_explains_position_derived_human_candidate() -> None:
    """岗位默认角色必须作为候选来源被覆盖报告解释，且无需复制真人直接授权。"""

    with _seeded_session() as db:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == "expense_finance_approver"
            )
        ).one()
        assignments = db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.business_role_id == role.id
            )
        ).all()
        for assignment in assignments:
            assignment.status = "inactive"
            db.add(assignment)
        profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.status == "active")
        ).first()
        assert profile is not None
        organization = OrganizationUnit(
            id="org_finance_test",
            tenant_id="tenant_demo",
            code="ORG-FINANCE-TEST",
            name="财务测试组织",
            unit_type_code="department",
            tree_path="/org_finance_test/",
            depth=0,
        )
        position = Position(
            id="position_finance_manager_test",
            tenant_id="tenant_demo",
            org_unit_id=organization.id,
            code="POS-FIN-MANAGER-TEST",
            name="财务部门经理测试岗",
            position_type_code="management",
        )
        db.add(organization)
        db.add(position)
        db.add(
            PositionAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=profile.id,
                position_id=position.id,
            )
        )
        bind_position_business_role(
            db,
            tenant_id="tenant_demo",
            position_id=position.id,
            business_role_id=role.id,
        )
        db.commit()

        assessment = _assessment(db, "expense_over_limit_approval")
        participant = next(
            item
            for item in assessment.human_participants
            if role.role_code in item.role_codes
        )

        assert participant.eligible_candidate_count >= 1
        assert participant.source_counts["position_role"] >= 1
        assert "PARTICIPANT_NO_ELIGIBLE_CANDIDATE" not in participant.issue_codes


def test_dependency_assessment_explains_agent_resource_without_execution_role() -> None:
    """Agent 只装载 SOP 资源但失去执行角色时必须显示不完整路径并阻断。"""

    with _seeded_session() as db:
        skill = db.exec(
            select(Skill).where(Skill.skill_id == "expense_over_limit_approval")
        ).one()
        agent_id = _assessment(db, skill.skill_id).agent_paths[0].agent_id
        resource_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
                AgentResourceBinding.status == "active",
                AgentResourceBinding.agent_id == agent_id,
            )
        ).one()
        role_bindings = db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.agent_id == resource_binding.agent_id,
                AgentRoleBinding.status == "active",
            )
        ).all()
        for binding in role_bindings:
            binding.status = "inactive"
            db.add(binding)
        db.commit()

        assessment = _assessment(db, skill.skill_id)

        assert assessment.readiness is DependencyReadiness.BLOCKED
        assert assessment.executable_agent_count == 0
        assert len(assessment.agent_paths) == 1
        assert assessment.agent_paths[0].executable is False
        assert "AGENT_EXECUTION_PERMISSION_REQUIRED" in assessment.agent_paths[0].issue_codes


def test_dependency_assessment_blocks_missing_participant_role_without_writes() -> None:
    """发布后角色停用必须阻断新实例预检，且报告本身不能修改任何事实。"""

    with _seeded_session() as db:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == "expense_department_approver"
            )
        ).one()
        role.status = "inactive"
        db.add(role)
        db.commit()
        before_bindings = db.exec(select(AgentResourceBinding)).all()

        first = _assessment(db, "expense_over_limit_approval")
        second = _assessment(db, "expense_over_limit_approval")

        assert first == second
        assert first.readiness is DependencyReadiness.BLOCKED
        assert "PARTICIPANT_ROLE_NOT_ACTIVE" in first.issue_codes
        assert db.exec(select(AgentResourceBinding)).all() == before_bindings


def test_dependency_assessment_blocks_skill_without_active_agent_binding() -> None:
    """没有活动数字员工入口的发布 SOP 不能被误报为业务可执行。"""

    with _seeded_session() as db:
        skill = db.exec(
            select(Skill).where(Skill.skill_id == "skill_graph_visual_demo")
        ).one()
        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).all()
        for binding in bindings:
            binding.status = "inactive"
            db.add(binding)
        db.commit()

        assessment = _assessment(db, skill.skill_id)

        assert assessment.readiness is DependencyReadiness.BLOCKED
        assert assessment.bound_agent_count == 0
        assert assessment.executable_agent_count == 0
        assert "ACTIVE_AGENT_BINDING_REQUIRED" in assessment.issue_codes


def test_dependency_assessment_does_not_treat_open_gallery_pool_as_execution_agent() -> None:
    """开放广场发布关系只能证明资源公开，不能冒充可聊天数字员工执行路径。"""

    with _seeded_session() as db:
        skill = db.exec(
            select(Skill).where(Skill.skill_id == "after_sales_refund")
        ).one()
        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).all()
        for binding in bindings:
            agent = db.get(AgentProfile, binding.agent_id)
            if agent is not None and not agent.is_overall:
                binding.status = "inactive"
                db.add(binding)
        db.commit()

        assessment = _assessment(db, skill.skill_id)

        assert assessment.readiness is DependencyReadiness.BLOCKED
        assert assessment.bound_agent_count == 0
        assert assessment.executable_agent_count == 0
        assert "ACTIVE_AGENT_BINDING_REQUIRED" in assessment.issue_codes


def test_low_risk_graph_sop_enforces_tool_and_knowledge_paths_with_audit(
    monkeypatch,
) -> None:
    """无人工代表 SOP 必须同时证明工具/知识允许与拒绝，并留存脱敏审计摘要。"""

    class FakeClient:
        """返回固定价格数据，避免代表性授权测试访问外部服务。"""

        def __init__(self, *args: object, **kwargs: object):
            """接受 HTTP 客户端构造参数但不建立网络连接。"""

        def __enter__(self):
            """返回当前模拟客户端。"""

            return self

        def __exit__(self, *args: object) -> None:
            """退出上下文时无需释放网络资源。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """返回可供 Runtime 消费的低风险价格查询结果。"""

            return httpx.Response(
                200,
                json={"price": 100, "currency": "CNY"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _seeded_session() as db:
        user = db.exec(select(User).where(User.role == "admin")).first()
        platform_demo = db.get(AgentProfile, "agent_tenant_demo_platform_demo")
        hr = db.exec(select(AgentProfile).where(AgentProfile.name == "人事")).one()
        assert user is not None
        assert platform_demo is not None

        allowed_tool = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(
                name="product.price_query",
                arguments={"product_name": "测试商品"},
            ),
            active_skill_id="skill_graph_visual_demo",
            agent_id=platform_demo.id,
            actor_user_id=user.id,
        )
        denied_tool = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(
                name="product.price_query",
                arguments={"product_name": "测试商品"},
            ),
            active_skill_id="skill_graph_visual_demo",
            agent_id=hr.id,
            actor_user_id=user.id,
        )
        allowed_ids, allowed_versions = AgentLoop(db)._accessible_knowledge_scope(
            "tenant_demo",
            user.id,
            platform_demo.id,
            audit=True,
        )
        user.membership_status = "suspended"
        db.add(user)
        db.flush()
        denied_ids, denied_versions = AgentLoop(db)._accessible_knowledge_scope(
            "tenant_demo",
            user.id,
            platform_demo.id,
            audit=True,
        )
        db.commit()
        audit_rows = db.exec(
            select(ManagementAuditLog)
            .where(ManagementAuditLog.action == "knowledge.search")
            .order_by(ManagementAuditLog.created_at)
        ).all()

        assert allowed_tool.success is True
        assert denied_tool.success is False
        assert denied_tool.error is not None
        assert denied_tool.error.code == "NOT_ALLOWED"
        assert allowed_ids
        assert allowed_versions
        assert denied_ids == []
        assert denied_versions == []
        assert [row.outcome for row in audit_rows] == ["success", "denied"]
        assert all("query" not in row.detail_json for row in audit_rows)


def _assessment(db: Session, skill_id: str):
    """读取指定发布头并通过正式依赖预检服务生成结果。"""

    skill = db.exec(select(Skill).where(Skill.skill_id == skill_id)).one()
    return build_sop_dependency_assessment(
        db,
        skill=skill,
        compiled_definition=compile_legacy_skill_card(skill.content_json),
    )


def _seeded_session() -> Session:
    """创建隔离 SQLite 会话并写入与新租户一致的正式种子数据。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    seed_demo_data(db)
    db.commit()
    return db
