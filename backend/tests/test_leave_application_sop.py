"""
@Time       : 2026/07/27 16:10
@Author     : zhanglp8181
@File       : test_leave_application_sop.py
@CallChain  : pytest → 请假申请 v2 定义/公共 Mock/种子 → Scheduler
@Description: 验证政策证据、自然日计算、余额、明确确认和待审批回执的确定性闭环。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    LEAVE_APPLICATION_DETERMINISTIC_VERSION,
    LEAVE_APPLICATION_SKILL_ID,
    _leave_application_deterministic_content,
)
from app.db.models import AgentProfile, AgentRoleBinding, BusinessRole, Skill, SkillVersion, Tool
from app.db.seed import seed_demo_data
from app.public_mock.service import execute_public_mock
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action


def _definition():
    """编译请假申请 v2 的零告警统一元模型定义。"""

    return compile_legacy_skill_card(_leave_application_deterministic_content({}))


def test_leave_balance_receipt_calculates_inclusive_calendar_days() -> None:
    """验证 HR 回执按起止日期计算含首尾的自然日，并给出余额充分性枚举。"""

    sufficient = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E001",
            "leave_type": "annual",
            "start_date": "2026-07-27",
            "end_date": "2026-07-28",
            "include_attendance": False,
        },
    )
    insufficient = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E001",
            "leave_type": "annual",
            "start_date": "2026-07-27",
            "end_date": "2026-08-02",
            "include_attendance": False,
        },
    )

    assert sufficient.request_assessment is not None
    assert sufficient.request_assessment.status == "sufficient"
    assert sufficient.request_assessment.requested_days == 2
    assert sufficient.request_assessment.available_days == 5
    assert insufficient.request_assessment is not None
    assert insufficient.request_assessment.status == "insufficient"
    assert insufficient.request_assessment.requested_days == 7


def test_leave_balance_receipt_rejects_invalid_or_unsupported_requests() -> None:
    """验证倒置日期和未结构化假种分别进入稳定的保守枚举。"""

    invalid = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E001",
            "leave_type": "annual",
            "start_date": "2026-07-28",
            "end_date": "2026-07-27",
        },
    )
    unsupported = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E001",
            "leave_type": "marriage",
            "start_date": "2026-07-27",
            "end_date": "2026-07-28",
        },
    )

    assert invalid.request_assessment is not None
    assert invalid.request_assessment.status == "invalid_date"
    assert unsupported.request_assessment is not None
    assert unsupported.request_assessment.status == "manual_review"


def test_leave_application_definition_freezes_policy_balance_and_confirmation() -> None:
    """验证知识查询、余额查询和写操作输入均来自声明式冻结契约。"""

    definition = _definition()
    policy_plan = plan_next_action(
        definition,
        current_node_id="check_leave_policy",
        slots={
            "leave_type": "annual",
            "start_date": "2026-07-27",
            "end_date": "2026-07-28",
        },
    )
    balance_plan = plan_next_action(
        definition,
        current_node_id="query_leave_balance",
        slots={
            "employee_id": "E001",
            "leave_type": "annual",
            "start_date": "2026-07-27",
            "end_date": "2026-07-28",
        },
    )
    confirm_node = next(node for node in definition.nodes if node.node_id == "confirm_leave_submit")

    assert definition.meta_model_version == 5
    assert definition.diagnostics == ()
    assert policy_plan.action is RuntimeAction.QUERY_KNOWLEDGE
    assert policy_plan.operation_arguments["query_type"] == "policy_check"
    assert policy_plan.operation_arguments["desired_evidence"] == "年假申请时限"
    assert balance_plan.operation_name == "hr.balance_query"
    assert balance_plan.operation_arguments == {
        "employee_id": "E001",
        "leave_type": "annual",
        "start_date": "2026-07-27",
        "end_date": "2026-07-28",
    }
    assert confirm_node.config.confirmation_policy is not None


def test_leave_application_routes_policy_and_balance_failures_conservatively() -> None:
    """验证零证据、查询失败、余额不足及非年假均不会进入提交节点。"""

    definition = _definition()
    no_evidence = plan_next_action(
        definition,
        current_node_id="check_leave_policy",
        slots={},
        node_outputs={
            "leave_policy": {
                "status": "succeeded",
                "data": {"outcome": "no_match"},
            }
        },
    )
    balance_failed = plan_next_action(
        definition,
        current_node_id="query_leave_balance",
        slots={},
        tool_results={"leave_balance": {"status": "failed", "data": {}}},
    )
    insufficient = plan_next_action(
        definition,
        current_node_id="query_leave_balance",
        slots={"leave_type": "annual"},
        tool_results={
            "leave_balance": {
                "status": "succeeded",
                "data": {"request_assessment": {"status": "insufficient"}},
            }
        },
    )
    sick_leave = plan_next_action(
        definition,
        current_node_id="select_automatic_leave_path",
        slots={"leave_type": "sick"},
    )

    assert no_evidence.next_node_id == "leave_policy_unavailable"
    assert balance_failed.next_node_id == "leave_balance_failed"
    assert insufficient.next_node_id == "hr_leave_review"
    assert sick_leave.next_node_id == "hr_leave_review"


def test_leave_application_manual_review_uses_role_work_item() -> None:
    """验证余额不足和特殊假种由 HR 角色工作项接续，而不是文字建议后终止。"""

    node = next(node for node in _definition().nodes if node.node_id == "hr_leave_review")

    assert node.config.candidate_role_codes == ("hr_leave_specialist",)
    assert node.config.exclude_initiator is True
    assert node.config.allowed_outcomes == ("reviewed", "needs_information")
    assert node.config.action_permissions["claim"] == "hr.leave_review.claim"


def test_leave_submission_uses_computed_days_and_distinguishes_business_status() -> None:
    """验证提交天数来自余额回执，pending、rejected 和传输失败分别收口。"""

    definition = _definition()
    slots = {
        "employee_id": "E001",
        "employee_name": "演示管理员",
        "leave_type": "annual",
        "start_date": "2026-07-27",
        "end_date": "2026-07-28",
        "reason": "家庭事务",
    }
    receipt = {
        "leave_balance": {
            "status": "succeeded",
            "data": {
                "request_assessment": {
                    "status": "sufficient",
                    "requested_days": 2,
                }
            },
        }
    }
    call_plan = plan_next_action(
        definition,
        current_node_id="submit_leave_application",
        slots=slots,
        tool_results=receipt,
    )
    pending = plan_next_action(
        definition,
        current_node_id="submit_leave_application",
        slots=slots,
        tool_results={
            **receipt,
            "leave_application": {
                "status": "succeeded",
                "data": {"status": "pending", "application_id": "LEAVE-DEMO"},
            },
        },
    )
    rejected = plan_next_action(
        definition,
        current_node_id="submit_leave_application",
        slots=slots,
        tool_results={
            **receipt,
            "leave_application": {
                "status": "succeeded",
                "data": {"status": "rejected"},
            },
        },
    )
    failed = plan_next_action(
        definition,
        current_node_id="submit_leave_application",
        slots=slots,
        tool_results={
            **receipt,
            "leave_application": {"status": "failed", "data": {}},
        },
    )

    assert call_plan.action is RuntimeAction.CALL_TOOL
    assert call_plan.operation_arguments["days"] == 2
    assert pending.next_node_id == "leave_submitted_pending"
    assert rejected.next_node_id == "leave_submission_rejected"
    assert failed.next_node_id == "leave_submission_failed"


def test_seed_publishes_immutable_leave_application_version_and_tool_contracts() -> None:
    """验证种子保留旧快照并发布新版本，同时冻结两个工具的 SOP 授权边界。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(Skill).where(Skill.skill_id == LEAVE_APPLICATION_SKILL_ID)
        ).one()
        versions = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == LEAVE_APPLICATION_SKILL_ID
            )
        ).all()
        tools = {
            tool.name: tool
            for tool in db.exec(
                select(Tool).where(Tool.name.in_(["hr.balance_query", "hr.leave_apply"]))
            ).all()
        }
        hr_agent = db.exec(
            select(AgentProfile).where(AgentProfile.name == "人事")
        ).one()
        leave_role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == "hr_leave_specialist"
            )
        ).one()
        agent_role = db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.agent_id == hr_agent.id,
                AgentRoleBinding.business_role_id == leave_role.id,
            )
        ).one()

        assert skill.version == LEAVE_APPLICATION_DETERMINISTIC_VERSION
        assert {version.version for version in versions} >= {
            "1.0.0",
            LEAVE_APPLICATION_DETERMINISTIC_VERSION,
        }
        assert tools["hr.leave_apply"].required_permission_code == "hr.leave.apply"
        assert tools["hr.leave_apply"].permission_authorization_mode == "workflow_delegated"
        assert LEAVE_APPLICATION_SKILL_ID in tools["hr.leave_apply"].allowed_skills_json
        assert LEAVE_APPLICATION_SKILL_ID in tools["hr.balance_query"].allowed_skills_json
        assert agent_role.assignment_mode == "execute"
