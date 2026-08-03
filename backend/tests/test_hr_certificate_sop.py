"""
@Time       : 2026/07/22 23:52
@Author     : zhanglp8181
@File       : test_hr_certificate_sop.py
@CallChain  : pytest → 在职证明发布定义 → Scheduler/WorkItem/Tool 分支
@Description: 验证常规证明自动开具、特殊证明人工复核及结构化业务回执闭环。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    HR_CERTIFICATE_DETERMINISTIC_VERSION,
    HR_CERTIFICATE_OPERATOR_ROLE,
    HR_CERTIFICATE_REVIEWER_ROLE,
    HR_CERTIFICATE_SKILL_ID,
    _hr_certificate_deterministic_content,
)
from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Skill,
    SkillVersion,
    Tool,
)
from app.db.seed import seed_demo_data
from app.sop_runtime.definition import CompiledSopDefinition
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import plan_next_action
from app.sop_runtime.slot_values import normalize_slot_values


def _definition() -> CompiledSopDefinition:
    """编译零告警的在职证明确定性发布定义。"""

    return compile_legacy_skill_card(_hr_certificate_deterministic_content({}))


def test_certificate_normalizes_restricted_values_and_requires_confirmation() -> None:
    """验证用户自然语言归一为受限枚举，信息齐全后仍等待明确确认。"""

    definition = _definition()
    slots = normalize_slot_values(
        definition,
        {
            "employee_id": "E002",
            "employee_name": "演示员工",
            "cert_type": "在职证明",
            "purpose": "普通业务",
            "purpose_category": "普通业务",
            "language": "中文",
        },
    )
    plan = plan_next_action(
        definition,
        current_node_id="node_confirm_certificate_request",
        slots=slots,
    )

    assert definition.meta_model_version == 4
    assert definition.diagnostics == ()
    assert slots["cert_type"] == "employment"
    assert slots["purpose_category"] == "routine"
    assert slots["language"] == "zh"
    assert plan.action == "wait_input"
    assert plan.expected_inputs == ("confirmation",)


def test_certificate_regular_path_calls_single_protected_operation() -> None:
    """验证普通在职证明走自动分支，工具参数只来自冻结槽位映射。"""

    definition = _definition()
    routed = plan_next_action(
        definition,
        current_node_id="node_route_certificate_policy",
        slots={"cert_type": "employment", "purpose_category": "routine"},
    )
    tool_plan = plan_next_action(
        definition,
        current_node_id="node_call_certificate_issue",
        slots={
            "employee_id": "E002",
            "employee_name": "演示员工",
            "cert_type": "employment",
            "purpose": "普通业务",
            "language": "zh",
        },
    )

    assert routed.next_node_id == "node_call_certificate_issue"
    assert tool_plan.action == "call_tool"
    assert tool_plan.operation_name == "hr.cert_issue"
    assert tool_plan.operation_arguments == {
        "employee_id": "E002",
        "employee_name": "演示员工",
        "cert_type": "employment",
        "purpose": "普通业务",
        "language": "zh",
    }


def test_certificate_sensitive_paths_use_business_role_work_item() -> None:
    """验证收入、签证和贷款类申请统一进入可认领且排除发起人的复核任务。"""

    definition = _definition()
    review_node = next(
        node for node in definition.nodes if node.node_id == "node_special_certificate_review"
    )
    special_cases = (
        {"cert_type": "income", "purpose_category": "routine"},
        {"cert_type": "employment", "purpose_category": "visa"},
        {"cert_type": "employment", "purpose_category": "loan"},
    )

    for slots in special_cases:
        plan = plan_next_action(
            definition,
            current_node_id="node_route_certificate_policy",
            slots=slots,
        )
        assert plan.next_node_id == "node_special_certificate_review"

    assert review_node.config.candidate_role_codes == ("hr_certificate_reviewer",)
    assert review_node.config.completion_policy.claim_required is True
    assert review_node.config.exclude_initiator is True
    assert review_node.config.action_permissions == {
        "outcome:approved": "hr.certificate_request.approve",
        "outcome:rejected": "hr.certificate_request.reject",
    }


def test_certificate_review_approval_resumes_tool_and_rejection_stops() -> None:
    """验证复核批准恢复统一工具，拒绝直接到拒绝终态且不会调用工具。"""

    approved = plan_next_action(
        _definition(),
        current_node_id="node_special_certificate_review",
        slots={},
        work_items={"status": "completed", "outcome": "approved"},
    )
    rejected = plan_next_action(
        _definition(),
        current_node_id="node_special_certificate_review",
        slots={},
        work_items={"status": "completed", "outcome": "rejected"},
    )

    assert approved.next_node_id == "node_call_certificate_issue"
    assert rejected.next_node_id == "node_certificate_rejected"


def test_certificate_routes_business_receipt_and_transport_failure_separately() -> None:
    """验证 issued、pending、rejected 和工具失败分别落入唯一终态。"""

    expected_terminals = {
        "issued": "node_certificate_issued",
        "pending": "node_certificate_pending",
        "rejected": "node_certificate_rejected",
    }
    for status, terminal_node_id in expected_terminals.items():
        plan = plan_next_action(
            _definition(),
            current_node_id="node_call_certificate_issue",
            slots={},
            tool_results={
                "certificate_issue": {
                    "status": "succeeded",
                    "data": {"status": status},
                }
            },
        )
        assert plan.next_node_id == terminal_node_id

    failure = plan_next_action(
        _definition(),
        current_node_id="node_call_certificate_issue",
        slots={},
        tool_results={"certificate_issue": {"status": "failed", "data": {}}},
    )
    assert failure.next_node_id == "node_certificate_failure"


def test_certificate_seed_freezes_roles_permissions_tool_and_published_version() -> None:
    """验证种子数据把发布版本、工具权限、数字员工职责和真实复核人一起落库。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(Skill).where(Skill.skill_id == HR_CERTIFICATE_SKILL_ID)
        ).one()
        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == HR_CERTIFICATE_SKILL_ID,
                SkillVersion.version == HR_CERTIFICATE_DETERMINISTIC_VERSION,
            )
        ).one()
        tool = db.exec(select(Tool).where(Tool.name == "hr.cert_issue")).one()
        operator_role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == HR_CERTIFICATE_OPERATOR_ROLE
            )
        ).one()
        reviewer_role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == HR_CERTIFICATE_REVIEWER_ROLE
            )
        ).one()
        human_resources_agent = db.exec(
            select(AgentProfile).where(AgentProfile.name == "人事")
        ).one()
        reviewer_profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == "approver_demo")
        ).one()

        assert skill.version == HR_CERTIFICATE_DETERMINISTIC_VERSION
        assert version.meta_model_version == 4
        assert compile_legacy_skill_card(version.content_json).diagnostics == ()
        assert tool.allowed_skills_json == [HR_CERTIFICATE_SKILL_ID]
        assert tool.required_permission_code == "hr.certificate.issue"
        assert tool.permission_authorization_mode == "workflow_delegated"
        assert operator_role.permissions_json == ["hr.certificate.issue"]
        assert reviewer_role.permissions_json == [
            "hr.certificate_request.approve",
            "hr.certificate_request.reject",
        ]
        assert db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.agent_id == human_resources_agent.id,
                AgentRoleBinding.business_role_id == operator_role.id,
            )
        ).one().assignment_mode == "execute"
        assert db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.employee_profile_id == reviewer_profile.id,
                EmployeeRoleAssignment.business_role_id == reviewer_role.id,
            )
        ).one().status == "active"
