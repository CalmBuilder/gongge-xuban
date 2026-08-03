"""
@Time       : 2026/07/27 00:00
@Author     : zhanglp8181
@File       : test_partner_due_diligence_sop.py
@CallChain  : pytest → 合作方尽调 v2 定义/工具/种子 → Scheduler/组织授权
@Description: 验证专用尽调、内部制度检索、低风险建议和高风险真人复核的统一闭环。
"""

from __future__ import annotations

from copy import deepcopy

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    LEGAL_PARTNER_DUE_DILIGENCE_ANALYST_ROLE,
    LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE,
    PARTNER_DUE_DILIGENCE_SKILL_ID,
    PARTNER_DUE_DILIGENCE_VERSION,
    _partner_due_diligence_content,
    ensure_partner_due_diligence_version,
)
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Skill,
    SkillVersion,
    Tool,
)
from app.db.seed import seed_demo_data
from app.public_mock.service import execute_public_mock
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action
from app.sop_runtime.versioning import skill_content_checksum


PASS_COMPANY = "共格演示科技有限公司"
PASS_CODE = "91370000MA3D3M001X"
RISK_COMPANY = "共格演示风险供应商有限公司"
RISK_CODE = "91370000MA3R15K01X"


def _definition():
    """编译合作方尽调 v2 的零告警统一元模型定义。"""

    return compile_legacy_skill_card(_partner_due_diligence_content({}))


def test_partner_due_diligence_mock_separates_pass_risk_and_unknown_subjects() -> None:
    """验证专用工具按固定演示主体返回通过、风险和未知三类结构化事实。"""

    passed = execute_public_mock(
        "partner.due_diligence_query",
        {"company_name": PASS_COMPANY, "unified_social_credit_code": PASS_CODE},
    )
    risk = execute_public_mock(
        "partner.due_diligence_query",
        {"company_name": RISK_COMPANY, "unified_social_credit_code": RISK_CODE},
    )
    unknown = execute_public_mock(
        "partner.due_diligence_query",
        {
            "company_name": "不存在的演示合作方有限公司",
            "unified_social_credit_code": "91370000MA3N0KN01X",
        },
    )

    assert passed.status == "assessed"
    assert passed.credit_code_match is True
    assert passed.risk_level == "low"
    assert passed.recommendation == "pass"
    assert passed.requires_human_review is False
    assert risk.status == "assessed"
    assert risk.risk_level == "high"
    assert risk.recommendation == "human_review"
    assert risk.requires_human_review is True
    assert {item.code for item in risk.risk_flags} == {
        "ENFORCEMENT_RECORD",
        "DEMO_BLACKLIST_MATCH",
    }
    assert unknown.status == "not_found"
    assert unknown.risk_level == "unknown"
    assert unknown.recommendation == "insufficient"


def test_partner_due_diligence_uses_declared_tool_and_knowledge_queries() -> None:
    """验证外部事实与内部制度分别使用冻结工具参数和声明式知识查询。"""

    definition = _definition()
    tool_plan = plan_next_action(
        definition,
        current_node_id="query_partner_due_diligence",
        slots={
            "enterprise_full_name": PASS_COMPANY,
            "unified_social_credit_code": PASS_CODE,
        },
    )
    knowledge_plan = plan_next_action(
        definition,
        current_node_id="query_partner_policy",
        slots={"enterprise_full_name": PASS_COMPANY},
    )

    assert definition.meta_model_version == 5
    assert definition.diagnostics == ()
    assert tool_plan.operation_name == "partner.due_diligence_query"
    assert tool_plan.operation_arguments == {
        "company_name": PASS_COMPANY,
        "unified_social_credit_code": PASS_CODE,
    }
    assert knowledge_plan.action is RuntimeAction.QUERY_KNOWLEDGE
    assert knowledge_plan.operation_arguments["query_type"] == "policy_check"
    assert f"enterprise_full_name: {PASS_COMPANY}" in str(
        knowledge_plan.operation_arguments["query"]
    )


def test_partner_due_diligence_routes_external_and_policy_results_conservatively() -> None:
    """验证未知主体、工具失败、知识失败、通过和高风险均进入独立冻结分支。"""

    definition = _definition()
    assessed = plan_next_action(
        definition,
        current_node_id="query_partner_due_diligence",
        slots={},
        tool_results={
            "partner_due_diligence": {
                "status": "succeeded",
                "data": {"status": "assessed"},
            }
        },
    )
    unknown = plan_next_action(
        definition,
        current_node_id="query_partner_due_diligence",
        slots={},
        tool_results={
            "partner_due_diligence": {
                "status": "succeeded",
                "data": {"status": "not_found"},
            }
        },
    )
    tool_failed = plan_next_action(
        definition,
        current_node_id="query_partner_due_diligence",
        slots={},
        tool_results={"partner_due_diligence": {"status": "failed", "data": {}}},
    )
    passed = plan_next_action(
        definition,
        current_node_id="query_partner_policy",
        slots={},
        tool_results={
            "partner_due_diligence": {
                "status": "succeeded",
                "data": {"recommendation": "pass", "risk_level": "low"},
            }
        },
        node_outputs={
            "partner_policy": {
                "status": "succeeded",
                "data": {"outcome": "evidence_found"},
            }
        },
    )
    risk = plan_next_action(
        definition,
        current_node_id="query_partner_policy",
        slots={},
        tool_results={
            "partner_due_diligence": {
                "status": "succeeded",
                "data": {"recommendation": "human_review", "risk_level": "high"},
            }
        },
        node_outputs={
            "partner_policy": {
                "status": "succeeded",
                "data": {"outcome": "evidence_found"},
            }
        },
    )
    policy_failed = plan_next_action(
        definition,
        current_node_id="query_partner_policy",
        slots={},
        tool_results={
            "partner_due_diligence": {
                "status": "succeeded",
                "data": {"recommendation": "pass", "risk_level": "low"},
            }
        },
        node_outputs={"partner_policy": {"status": "failed"}},
    )
    policy_no_match = plan_next_action(
        definition,
        current_node_id="query_partner_policy",
        slots={},
        tool_results={
            "partner_due_diligence": {
                "status": "succeeded",
                "data": {"recommendation": "pass", "risk_level": "low"},
            }
        },
        node_outputs={
            "partner_policy": {
                "status": "succeeded",
                "data": {"outcome": "no_match"},
            }
        },
    )

    assert assessed.next_node_id == "query_partner_policy"
    assert unknown.next_node_id == "partner_information_insufficient"
    assert tool_failed.next_node_id == "partner_due_diligence_failed"
    assert passed.next_node_id == "issue_demo_onboarding_recommendation"
    assert risk.next_node_id == "partner_legal_review"
    assert policy_failed.next_node_id == "partner_policy_query_failed"
    assert policy_no_match.next_node_id == "partner_policy_query_failed"


def test_partner_due_diligence_human_review_uses_domain_outcomes() -> None:
    """验证高风险由独立法务角色认领，并以复核或补材料语义恢复。"""

    definition = _definition()
    node = next(item for item in definition.nodes if item.node_id == "partner_legal_review")
    reviewed = plan_next_action(
        definition,
        current_node_id="partner_legal_review",
        slots={},
        work_items={"status": "completed", "outcome": "reviewed"},
    )
    needs_information = plan_next_action(
        definition,
        current_node_id="partner_legal_review",
        slots={},
        work_items={"status": "completed", "outcome": "needs_information"},
    )

    assert node.config.candidate_role_codes == (LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE,)
    assert node.config.allowed_outcomes == ("reviewed", "needs_information")
    assert node.config.action_permissions == {
        "claim": "legal.partner_due_diligence.claim",
        "outcome:reviewed": "legal.partner_due_diligence.complete",
        "outcome:needs_information": "legal.partner_due_diligence.request_information",
    }
    reviewed_option = next(
        option for option in node.config.outcome_options if option.value == "reviewed"
    )
    assert "{subject_name}" in reviewed_option.completion_message
    assert "{enterprise_full_name}" not in reviewed_option.completion_message
    assert reviewed.next_node_id == "partner_review_completed"
    assert needs_information.next_node_id == "partner_review_information_required"


def test_partner_due_diligence_seed_publishes_v2_and_authorization_contract() -> None:
    """验证 v1 不可变派生、专用工具、数字员工执行职责和真人任职完整落库。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(Skill).where(Skill.skill_id == PARTNER_DUE_DILIGENCE_SKILL_ID)
        ).one()
        versions = db.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id == PARTNER_DUE_DILIGENCE_SKILL_ID)
            .order_by(SkillVersion.version)
        ).all()
        tool = db.exec(
            select(Tool).where(Tool.name == "partner.due_diligence_query")
        ).one()
        analyst_role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == LEGAL_PARTNER_DUE_DILIGENCE_ANALYST_ROLE
            )
        ).one()
        reviewer_role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE
            )
        ).one()
        legal_agent = db.exec(select(AgentProfile).where(AgentProfile.name == "法务")).one()
        reviewer = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == "approver_demo")
        ).one()

        assert skill.version == PARTNER_DUE_DILIGENCE_VERSION
        assert [item.version for item in versions] == [
            "1.0.0",
            PARTNER_DUE_DILIGENCE_VERSION,
        ]
        assert versions[1].derived_from_version_id == versions[0].id
        assert versions[1].meta_model_version == 5
        assert compile_legacy_skill_card(versions[1].content_json).diagnostics == ()
        assert tool.allowed_skills_json == [PARTNER_DUE_DILIGENCE_SKILL_ID]
        assert tool.required_permission_code == "legal.partner_due_diligence.query"
        assert tool.permission_authorization_mode == "workflow_delegated"
        assert analyst_role.permissions_json == ["legal.partner_due_diligence.query"]
        assert reviewer_role.permissions_json == [
            "legal.partner_due_diligence.claim",
            "legal.partner_due_diligence.complete",
            "legal.partner_due_diligence.request_information",
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
                    EmployeeRoleAssignment.employee_profile_id == reviewer.id,
                    EmployeeRoleAssignment.business_role_id == reviewer_role.id,
                )
            )
            .one()
            .status
            == "active"
        )
        assert (
            db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.agent_id == legal_agent.id,
                    AgentResourceBinding.resource_type == "tool",
                    AgentResourceBinding.resource_id == tool.id,
                    AgentResourceBinding.status == "active",
                )
            ).one()
            is not None
        )


def test_partner_due_diligence_derives_k11_version_without_overwriting_v22() -> None:
    """验证现有 2.2.0 快照保留，并由证据门禁修复派生新的 2.3.0。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()
        skill = db.exec(
            select(Skill).where(Skill.skill_id == PARTNER_DUE_DILIGENCE_SKILL_ID)
        ).one()
        current = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == PARTNER_DUE_DILIGENCE_SKILL_ID,
                SkillVersion.version == PARTNER_DUE_DILIGENCE_VERSION,
            )
        ).one()
        legacy_content = deepcopy(current.content_json)
        legacy_content["version"] = "2.2.0"
        legacy_definition = compile_legacy_skill_card(legacy_content)
        current.version = "2.2.0"
        current.content_json = legacy_content
        current.content_checksum = skill_content_checksum(legacy_content)
        current.compiled_definition_checksum = legacy_definition.checksum
        skill.version = "2.2.0"
        skill.content_json = legacy_content
        db.add(current)
        db.add(skill)
        db.commit()
        previous_id = current.id

        ensure_partner_due_diligence_version(db)
        db.commit()
        versions = db.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id == PARTNER_DUE_DILIGENCE_SKILL_ID)
            .order_by(SkillVersion.version)
        ).all()
        db.refresh(skill)

    assert [item.version for item in versions] == ["1.0.0", "2.2.0", "2.3.0"]
    assert versions[-1].derived_from_version_id == previous_id
    assert versions[-1].content_json["version"] == "2.3.0"
    assert skill.version == "2.3.0"
