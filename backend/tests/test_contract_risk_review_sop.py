"""
@Time       : 2026/07/22 17:28
@Author     : zhanglp8181
@File       : test_contract_risk_review_sop.py
@CallChain  : pytest → 合同风险审查发布定义 → 初筛工具/法务人工复核/终态
@Description: 验证低风险报告、高风险真人复核、动作权限和受控数字员工执行闭环。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION,
    CONTRACT_RISK_REVIEW_SKILL_ID,
    LEGAL_CONTRACT_REVIEWER_ROLE,
    LEGAL_CONTRACT_RISK_ANALYST_ROLE,
    _contract_risk_review_deterministic_content,
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
    """编译零告警的合同风险审查确定性发布定义。"""

    return compile_legacy_skill_card(_contract_risk_review_deterministic_content({}))


def test_contract_review_normalizes_type_and_maps_explicit_tool_arguments() -> None:
    """验证合同类型归一，且初筛请求只包含用户提供的类型和合同文本。"""

    definition = _definition()
    slots = normalize_slot_values(
        definition,
        {
            "contract_type": "软件采购合同",
            "contract_content": "双方应保护商业秘密，保密义务持续三年。",
        },
    )
    plan = plan_next_action(
        definition,
        current_node_id="node_assess_contract_risk",
        slots=slots,
    )

    assert definition.meta_model_version == 3
    assert definition.diagnostics == ()
    assert slots["contract_type"] == "software_procurement"
    assert plan.operation_name == "contract.risk_assess"
    assert plan.operation_arguments == {
        "contract_type": "software_procurement",
        "contract_content": "双方应保护商业秘密，保密义务持续三年。",
    }


def test_contract_review_routes_low_medium_high_insufficient_and_failure() -> None:
    """验证业务风险、材料不足和传输失败分别进入冻结分支。"""

    expected_nodes = {
        "low": "node_contract_risk_report",
        "medium": "node_contract_risk_report",
        "high": "node_high_risk_legal_review",
    }
    for risk_level, node_id in expected_nodes.items():
        plan = plan_next_action(
            _definition(),
            current_node_id="node_assess_contract_risk",
            slots={},
            tool_results={
                "contract_risk": {
                    "status": "succeeded",
                    "data": {"status": "assessed", "risk_level": risk_level},
                }
            },
        )
        assert plan.next_node_id == node_id

    insufficient = plan_next_action(
        _definition(),
        current_node_id="node_assess_contract_risk",
        slots={},
        tool_results={
            "contract_risk": {
                "status": "succeeded",
                "data": {"status": "insufficient", "risk_level": "unknown"},
            }
        },
    )
    failed = plan_next_action(
        _definition(),
        current_node_id="node_assess_contract_risk",
        slots={},
        tool_results={"contract_risk": {"status": "failed", "data": {}}},
    )

    assert insufficient.next_node_id == "node_contract_assessment_insufficient"
    assert failed.next_node_id == "node_contract_assessment_failure"


def test_high_risk_review_uses_non_approval_outcomes_and_atomic_permissions() -> None:
    """验证高风险节点由真实法务角色办理，结果语义不是批准或拒绝。"""

    review_node = next(
        node for node in _definition().nodes if node.node_id == "node_high_risk_legal_review"
    )

    assert review_node.config.candidate_role_codes == (LEGAL_CONTRACT_REVIEWER_ROLE,)
    assert review_node.config.completion_policy.claim_required is True
    assert review_node.config.exclude_initiator is True
    assert review_node.config.allowed_outcomes == ("reviewed", "needs_information")
    assert review_node.config.action_permissions == {
        "claim": "legal.contract_review.claim",
        "outcome:reviewed": "legal.contract_review.complete",
        "outcome:needs_information": "legal.contract_review.request_information",
    }
    assert [option.label for option in review_node.config.outcome_options] == [
        "提交复核意见",
        "要求补充材料",
    ]
    assert all(option.comment_required for option in review_node.config.outcome_options)


def test_high_risk_human_outcomes_resume_to_distinct_terminals() -> None:
    """验证复核完成与要求补材料分别收口，不会调用第二个工具或伪装审批通过。"""

    reviewed = plan_next_action(
        _definition(),
        current_node_id="node_high_risk_legal_review",
        slots={},
        work_items={"status": "completed", "outcome": "reviewed"},
    )
    needs_information = plan_next_action(
        _definition(),
        current_node_id="node_high_risk_legal_review",
        slots={},
        work_items={"status": "completed", "outcome": "needs_information"},
    )

    assert reviewed.next_node_id == "node_high_risk_review_completed"
    assert needs_information.next_node_id == "node_review_information_required"


def test_contract_review_seed_freezes_tool_roles_and_reviewer_assignment() -> None:
    """验证风险工具、数字员工 execute 职责和真人复核任职随版本幂等落库。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(select(Skill).where(Skill.skill_id == CONTRACT_RISK_REVIEW_SKILL_ID)).one()
        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == CONTRACT_RISK_REVIEW_SKILL_ID,
                SkillVersion.version == CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION,
            )
        ).one()
        tool = db.exec(select(Tool).where(Tool.name == "contract.risk_assess")).one()
        analyst_role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == LEGAL_CONTRACT_RISK_ANALYST_ROLE)
        ).one()
        reviewer_role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == LEGAL_CONTRACT_REVIEWER_ROLE)
        ).one()
        legal_agent = db.exec(select(AgentProfile).where(AgentProfile.name == "法务")).one()
        reviewer_profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == "approver_demo")
        ).one()

        assert skill.version == CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION
        assert version.meta_model_version == 3
        assert compile_legacy_skill_card(version.content_json).diagnostics == ()
        assert tool.allowed_skills_json == [CONTRACT_RISK_REVIEW_SKILL_ID]
        assert tool.required_permission_code == "legal.contract_risk.assess"
        assert tool.permission_authorization_mode == "workflow_delegated"
        assert analyst_role.permissions_json == ["legal.contract_risk.assess"]
        assert reviewer_role.permissions_json == [
            "legal.contract_review.claim",
            "legal.contract_review.complete",
            "legal.contract_review.request_information",
        ]
        assert (
            db.exec(
                select(AgentRoleBinding).where(
                    AgentRoleBinding.agent_id == legal_agent.id,
                    AgentRoleBinding.business_role_id == analyst_role.id,
                )
            )
            .one()
            .assignment_mode
            == "execute"
        )
        assert (
            db.exec(
                select(EmployeeRoleAssignment).where(
                    EmployeeRoleAssignment.employee_profile_id == reviewer_profile.id,
                    EmployeeRoleAssignment.business_role_id == reviewer_role.id,
                )
            )
            .one()
            .status
            == "active"
        )
