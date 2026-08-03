"""
@Time       : 2026/07/28 20:20
@Author     : zhanglp8181
@File       : test_expense_special_approval_org_scope.py
@CallChain  : pytest → v2.1 定义 → SopWorkItemService → Coordinator 持久恢复
@Description: 验证超标特批部门子树候选、竞争认领、集中财务和跨会话完成闭环。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.demo_sop_versions import (
    EXPENSE_DEPARTMENT_APPROVER_ROLE,
    EXPENSE_FINANCE_APPROVER_ROLE,
    EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
)
from app.db.models import (
    AgentProfile,
    BusinessRole,
    ChatSession,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Skill,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopWorkItem,
    User,
    utc_now,
)
from app.db.seed import seed_demo_data
from app.organization.assignments import (
    assign_member_to_organization,
    assign_member_to_position,
    create_position,
    ensure_assignment_foundation,
)
from app.organization.roles import bind_position_business_role
from app.organization.units import create_organization_unit, ensure_organization_foundation
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.work_items import SopWorkItemService, WorkItemError
from app.tools.tool_schema import ToolResult


def test_org_scoped_expense_approval_reaches_central_finance_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证代表 SOP 的范围、认领互斥、部门到财务推进和新会话恢复。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'expense-org-scope.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    _seed_representative_runtime(engine)

    def execute_approval_step(
        _executor: object,
        _tenant_id: str,
        tool_call: object,
        *_args: object,
        **_kwargs: object,
    ) -> ToolResult:
        """返回与真实特批台账相同的部门 pending、财务 approved 回执。"""

        tool_name = str(getattr(tool_call, "name", ""))
        if tool_name == "expense.special_approval_step1_approve":
            return ToolResult(
                tool_name=tool_name,
                success=True,
                data={
                    "approval_request_id": "SPECIAL-M3C",
                    "status": "pending",
                    "current_step": 2,
                    "total_steps": 2,
                },
            )
        if tool_name == "expense.special_approval_step2_approve":
            return ToolResult(
                tool_name=tool_name,
                success=True,
                data={
                    "approval_request_id": "SPECIAL-M3C",
                    "status": "approved",
                    "current_step": 2,
                    "total_steps": 2,
                },
            )
        raise AssertionError(f"unexpected representative operation: {tool_name}")

    monkeypatch.setattr(
        "app.sop_runtime.coordinator.ToolExecutor.execute",
        execute_approval_step,
    )

    with Session(engine) as db:
        department_item = db.exec(
            select(SopWorkItem).where(
                SopWorkItem.node_id == "department_special_approval"
            )
        ).one()
        service = SopWorkItemService(db)
        assert [item.user_id for item in service.candidates(department_item)] == [
            "department_child",
            "department_same",
        ]
        child_candidate = next(
            item
            for item in service.candidates(department_item)
            if item.user_id == "department_child"
        )
        assert child_candidate.source_types_json == ["position_role"]
        assert department_item.participant_scope_snapshot_json["resolver"] == (
            "initiator_primary_org_subtree"
        )

        service.claim(
            department_item,
            actor_user_id="department_same",
            command_id="m3c-department-claim-winner",
        )
        with pytest.raises(WorkItemError) as race:
            service.claim(
                department_item,
                actor_user_id="department_child",
                command_id="m3c-department-claim-loser",
            )
        assert race.value.code == "WORK_ITEM_ALREADY_CLAIMED"
        service.complete(
            department_item,
            actor_user_id="department_same",
            command_id="m3c-department-complete",
            outcome="approved",
            comment="部门预算与事由属实",
        )
        plan = DeterministicSopCoordinator(db).resume_completed_work_item(department_item)
        db.commit()

        finance_item = db.exec(
            select(SopWorkItem).where(SopWorkItem.node_id == "finance_special_approval")
        ).one()
        assert plan.action.value == "wait_work_item"
        assert finance_item.status == "offered"
        assert finance_item.participant_scope_snapshot_json == {
            "schema_version": 1,
            "resolver": "tenant",
            "root_org_unit_id": None,
            "organization_unit_ids": [],
        }
        assert "finance_central" in {
            item.user_id for item in service.candidates(finance_item)
        }

    with Session(engine) as restarted_db:
        finance_item = restarted_db.exec(
            select(SopWorkItem).where(SopWorkItem.node_id == "finance_special_approval")
        ).one()
        service = SopWorkItemService(restarted_db)
        service.claim(
            finance_item,
            actor_user_id="finance_central",
            command_id="m3c-finance-claim",
        )
        service.complete(
            finance_item,
            actor_user_id="finance_central",
            command_id="m3c-finance-complete",
            outcome="approved",
            comment="财务政策与额度复核通过",
        )
        plan = DeterministicSopCoordinator(restarted_db).resume_completed_work_item(
            finance_item
        )
        restarted_db.commit()

        instance = restarted_db.get(SopInstance, "instance_m3c_expense")
        assert plan.action.value == "complete"
        assert instance is not None
        assert instance.status == "succeeded"


def test_sqlite_concurrent_claim_uses_revision_compare_and_swap(tmp_path: Path) -> None:
    """验证 SQLite 无行锁时两个真实事务竞争也只有一个账号认领成功。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'expense-claim-race.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    SQLModel.metadata.create_all(engine)
    _seed_representative_runtime(engine)
    barrier = Barrier(2)

    def claim(actor_user_id: str) -> str:
        """在独立事务中同时发出带相同 revision 的认领命令。"""

        with Session(engine) as db:
            item = db.exec(
                select(SopWorkItem).where(
                    SopWorkItem.node_id == "department_special_approval"
                )
            ).one()
            barrier.wait()
            try:
                SopWorkItemService(db).claim(
                    item,
                    actor_user_id=actor_user_id,
                    command_id=f"race-{actor_user_id}",
                    expected_revision=0,
                )
                db.commit()
                return "claimed"
            except WorkItemError as error:
                db.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(claim, ("department_same", "department_child"))
        )

    assert sorted(outcomes) == ["WORK_ITEM_ALREADY_CLAIMED", "claimed"]
    with Session(engine) as db:
        item = db.exec(
            select(SopWorkItem).where(
                SopWorkItem.node_id == "department_special_approval"
            )
        ).one()
        assert item.revision == 1
        assert item.assignee_user_id in {"department_same", "department_child"}


def _seed_representative_runtime(engine: object) -> None:
    """写入实际 v2.1 定义所需的组织、角色、会话、实例和首个人工节点。"""

    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()
        roles = {
            role.role_code: role
            for role in db.exec(
                select(BusinessRole).where(
                    BusinessRole.role_code.in_(
                        (
                            EXPENSE_DEPARTMENT_APPROVER_ROLE,
                            EXPENSE_FINANCE_APPROVER_ROLE,
                        )
                    )
                )
            ).all()
        }
        people = (
            ("expense_applicant", "M3C-A", None, "active"),
            (
                "department_same",
                "M3C-D1",
                EXPENSE_DEPARTMENT_APPROVER_ROLE,
                "active",
            ),
            (
                "department_child",
                "M3C-D2",
                EXPENSE_DEPARTMENT_APPROVER_ROLE,
                "active",
            ),
            (
                "department_sibling",
                "M3C-D3",
                EXPENSE_DEPARTMENT_APPROVER_ROLE,
                "active",
            ),
            (
                "department_inactive",
                "M3C-D4",
                EXPENSE_DEPARTMENT_APPROVER_ROLE,
                "inactive",
            ),
            ("department_no_role", "M3C-D5", None, "active"),
            (
                "finance_central",
                "M3C-F1",
                EXPENSE_FINANCE_APPROVER_ROLE,
                "active",
            ),
        )
        for user_id, employee_id, role_code, membership_status in people:
            db.add(
                User(
                    id=user_id,
                    tenant_id="tenant_demo",
                    username=user_id,
                    password_hash="test",
                    membership_status=membership_status,
                )
            )
            profile_id = f"profile_{user_id}"
            db.add(
                EmployeeProfile(
                    id=profile_id,
                    tenant_id="tenant_demo",
                    user_id=user_id,
                    employee_id=employee_id,
                    employee_name=user_id,
                )
            )
            if role_code and user_id != "department_child":
                db.add(
                    EmployeeRoleAssignment(
                        tenant_id="tenant_demo",
                        employee_profile_id=profile_id,
                        business_role_id=roles[role_code].id,
                    )
                )
        db.flush()

        root = ensure_organization_foundation(db, "tenant_demo")
        department = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="M3C_DEPARTMENT",
            name="M3-C 申请部门",
            unit_type_code="department",
        )
        child = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=department.id,
            code="M3C_DEPARTMENT_CHILD",
            name="M3-C 申请部门下级",
            unit_type_code="team",
        )
        ensure_assignment_foundation(db, "tenant_demo")
        child_approver_position = create_position(
            db,
            tenant_id="tenant_demo",
            org_unit_id=child.id,
            code="M3C_CHILD_APPROVER",
            name="下级部门审批岗",
            position_type_code="professional",
        )
        sibling = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="M3C_SIBLING",
            name="M3-C 兄弟部门",
            unit_type_code="department",
        )
        for user_id, org_unit_id in (
            ("expense_applicant", department.id),
            ("department_same", department.id),
            ("department_child", child.id),
            ("department_sibling", sibling.id),
            ("department_inactive", department.id),
            ("department_no_role", department.id),
            ("finance_central", sibling.id),
        ):
            assign_member_to_organization(
                db,
                tenant_id="tenant_demo",
                employee_profile_id=f"profile_{user_id}",
                org_unit_id=org_unit_id,
                effective_from=utc_now() - timedelta(minutes=1),
            )
        assign_member_to_position(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_department_child",
            position_id=child_approver_position.id,
            effective_from=utc_now() - timedelta(minutes=1),
        )
        bind_position_business_role(
            db,
            tenant_id="tenant_demo",
            position_id=child_approver_position.id,
            business_role_id=roles[EXPENSE_DEPARTMENT_APPROVER_ROLE].id,
        )

        skill = db.exec(
            select(Skill).where(Skill.skill_id == "expense_over_limit_approval")
        ).one()
        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.skill_id,
                SkillVersion.version == EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
            )
        ).one()
        definition = compile_legacy_skill_card(version.content_json)
        agent = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "财务",
            )
        ).one()
        chat_session = ChatSession(
            id="session_m3c_expense",
            tenant_id="tenant_demo",
            user_id="expense_applicant",
            agent_id=agent.id,
            active_skill_id=skill.skill_id,
            active_step_id="department_special_approval",
        )
        instance = SopInstance(
            id="instance_m3c_expense",
            tenant_id="tenant_demo",
            session_id=chat_session.id,
            skill_id=skill.skill_id,
            skill_version_id=version.id,
            skill_version=version.version,
            definition_checksum=definition.checksum,
            status="waiting",
            current_node_id="department_special_approval",
            context_json={
                "tool_results": {
                    "special_application": {
                        "status": "succeeded",
                        "data": {
                            "approval_request_id": "SPECIAL-M3C",
                            "status": "pending",
                            "current_step": 1,
                            "total_steps": 2,
                        },
                    }
                }
            },
        )
        execution = SopNodeExecution(
            id="execution_m3c_department",
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_id="department_special_approval",
            status="waiting",
        )
        db.add(chat_session)
        db.add(instance)
        db.add(execution)
        db.flush()
        department_node = next(
            node
            for node in definition.nodes
            if node.node_id == "department_special_approval"
        )
        SopWorkItemService(db).offer(
            instance,
            execution,
            department_node.config,
            initiator_user_id=chat_session.user_id,
        )
        db.commit()
