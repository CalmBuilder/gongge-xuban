"""
@Time       : 2026/07/27 18:40
@Author     : zhanglp8181
@File       : test_overtime_compensatory_sop.py
@CallChain  : pytest → 加班调休 v2 定义/公共 Mock/种子 → Scheduler
@Description: 验证政策证据、加班资格、调休余额、明确确认和 HR 接管的确定性闭环。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION,
    OVERTIME_COMPENSATORY_SKILL_ID,
    _overtime_compensatory_deterministic_content,
)
from app.db.models import BusinessRole, Skill, SkillVersion, Tool
from app.db.seed import seed_demo_data
from app.public_mock.service import execute_public_mock
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action
from app.sop_runtime.slot_values import canonicalize_slot_keys, normalize_slot_values


def _definition():
    """编译加班调休 v2 的零告警统一元模型定义。"""

    return compile_legacy_skill_card(_overtime_compensatory_deterministic_content({}))


def test_overtime_policy_receipt_preserves_hour_ratio_without_inventing_day_conversion() -> None:
    """验证回执冻结 1:1 小时折算，并区分审批、工作日门槛和法定节假日。"""

    eligible = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E002",
            "leave_type": "compensatory",
            "start_date": "2026-07-30",
            "end_date": "2026-07-30",
            "overtime_date": "2026-07-25",
            "overtime_duration_hours": 4,
            "overtime_day_type": "rest_day",
            "pre_approval_status": "approved",
        },
    )
    no_approval = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E002",
            "overtime_date": "2026-07-25",
            "overtime_duration_hours": 4,
            "overtime_day_type": "rest_day",
            "pre_approval_status": "not_approved",
        },
    )
    too_short = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E002",
            "overtime_date": "2026-07-24",
            "overtime_duration_hours": 1,
            "overtime_day_type": "workday",
            "is_pre_approved": True,
        },
    )
    holiday = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E002",
            "overtime_date": "2026-10-01",
            "overtime_duration_hours": 8,
            "overtime_day_type": "statutory_holiday",
            "is_pre_approved": True,
        },
    )

    assert eligible.overtime_policy_assessment is not None
    assert eligible.overtime_policy_assessment.status == "eligible"
    assert eligible.overtime_policy_assessment.conversion_ratio == "1:1"
    assert eligible.overtime_policy_assessment.credit_unit == "hour"
    assert eligible.overtime_policy_assessment.credited_hours == 4
    assert eligible.request_assessment is not None
    assert eligible.request_assessment.status == "sufficient"
    assert no_approval.overtime_policy_assessment.status == "preapproval_missing"
    assert too_short.overtime_policy_assessment.status == "workday_minimum_not_met"
    assert holiday.overtime_policy_assessment.status == "statutory_holiday"


def test_overtime_credit_assessment_closes_hour_to_day_units() -> None:
    """验证八小时标准日把本次加班小时、申请天数和已入账余额统一为小时比较。"""

    insufficient = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E002",
            "leave_type": "compensatory",
            "start_date": "2026-07-30",
            "end_date": "2026-07-30",
            "overtime_date": "2026-07-25",
            "overtime_duration_hours": 4,
            "overtime_day_type": "rest_day",
            "pre_approval_status": "approved",
        },
    )
    sufficient = execute_public_mock(
        "hr.balance_query",
        {
            "employee_id": "E002",
            "leave_type": "compensatory",
            "start_date": "2026-07-30",
            "end_date": "2026-07-30",
            "overtime_date": "2026-07-25",
            "overtime_duration_hours": 8,
            "overtime_day_type": "rest_day",
            "pre_approval_status": "approved",
        },
    )

    assert insufficient.overtime_credit_assessment.status == "insufficient"
    assert insufficient.overtime_credit_assessment.requested_hours == 8
    assert sufficient.overtime_credit_assessment.status == "sufficient"
    assert sufficient.overtime_credit_assessment.standard_hours_per_day == 8


def test_overtime_definition_freezes_policy_tool_confirmation_and_human_takeover() -> None:
    """验证知识、两类 HR 工具、明确确认和角色候选工作项均为声明式契约。"""

    definition = _definition()
    policy_plan = plan_next_action(
        definition,
        current_node_id="check_overtime_policy",
        slots={"overtime_type": "rest_day", "overtime_hours": 4},
    )
    balance_plan = plan_next_action(
        definition,
        current_node_id="assess_overtime_and_balance",
        slots={
            "employee_id": "E002",
            "leave_type": "compensatory",
            "planned_start_date": "2026-07-30",
            "planned_end_date": "2026-07-30",
            "overtime_date": "2026-07-25",
            "overtime_hours": 4,
            "overtime_type": "rest_day",
            "pre_approval_status": "approved",
        },
    )
    review_node = next(node for node in definition.nodes if node.node_id == "hr_overtime_review")

    assert definition.meta_model_version == 5
    assert definition.diagnostics == ()
    assert policy_plan.action is RuntimeAction.QUERY_KNOWLEDGE
    assert policy_plan.operation_arguments["query_type"] == "policy_check"
    assert "1:1" in policy_plan.operation_arguments["desired_evidence"]
    assert balance_plan.operation_name == "hr.balance_query"
    assert balance_plan.operation_arguments["leave_type"] == "compensatory"
    assert balance_plan.operation_arguments["start_date"] == "2026-07-30"
    assert review_node.config.candidate_role_codes == ("hr_leave_specialist",)


def test_overtime_slot_contract_keeps_only_stable_business_compatibility() -> None:
    """验证 v3 仅保留稳定业务兼容，不再吸收模型偶发键和值作为补丁。"""

    content = _overtime_compensatory_deterministic_content({})

    canonical_slots = canonicalize_slot_keys(
        content,
        {
            "leave_type": "调休",
            "is_pre_approved": "not_approved",
            "overtime_reason": "紧急修复",
            "overtime_duration": 4,
            "overtime_day_type": "rest_day",
        },
    )
    patch_variant_slots = canonicalize_slot_keys(
        content,
        {
            "approval_status": "已审批",
            "pre_approval_status": "已通过",
        },
    )
    assert normalize_slot_values(_definition(), canonical_slots) == {
        "leave_type": "compensatory",
        "pre_approval_status": "not_approved",
        "reason": "紧急修复",
        "overtime_hours": 4,
        "overtime_type": "rest_day",
    }
    assert patch_variant_slots == {
        "approval_status": "已审批",
        "pre_approval_status": "已通过",
    }
    assert normalize_slot_values(_definition(), patch_variant_slots) == {
        "approval_status": "已审批",
        "pre_approval_status": "",
    }


def test_overtime_routes_only_policy_and_balance_eligible_receipts_to_confirmation() -> None:
    """验证零证据、缺审批、法定节假日和余额不足均不能进入提交确认。"""

    definition = _definition()
    no_evidence = plan_next_action(
        definition,
        current_node_id="check_overtime_policy",
        slots={},
        node_outputs={
            "overtime_policy": {
                "status": "succeeded",
                "data": {"outcome": "no_match"},
            }
        },
    )
    eligible = plan_next_action(
        definition,
        current_node_id="assess_overtime_and_balance",
        slots={},
        tool_results={
            "overtime_balance": {
                "status": "succeeded",
                "data": {
                        "overtime_policy_assessment": {"status": "eligible"},
                        "request_assessment": {"status": "sufficient"},
                        "overtime_credit_assessment": {"status": "sufficient"},
                },
            }
        },
    )
    no_approval = plan_next_action(
        definition,
        current_node_id="assess_overtime_and_balance",
        slots={},
        tool_results={
            "overtime_balance": {
                "status": "succeeded",
                "data": {
                        "overtime_policy_assessment": {"status": "preapproval_missing"},
                        "request_assessment": {"status": "sufficient"},
                        "overtime_credit_assessment": {"status": "manual_review"},
                },
            }
        },
    )

    assert no_evidence.next_node_id == "hr_overtime_review"
    assert eligible.next_node_id == "confirm_compensatory_submit"
    assert no_approval.next_node_id == "hr_overtime_review"


def test_overtime_submission_uses_compensatory_type_and_receipt_days() -> None:
    """验证写操作固定调休假种、复用计划日期，并只采用 HR 回执计算天数。"""

    definition = _definition()
    plan = plan_next_action(
        definition,
        current_node_id="submit_compensatory_application",
        slots={
            "employee_id": "E002",
            "employee_name": "演示员工",
            "leave_type": "compensatory",
            "planned_start_date": "2026-07-30",
            "planned_end_date": "2026-07-30",
            "reason": "版本发布",
        },
        tool_results={
            "overtime_balance": {
                "status": "succeeded",
                "data": {"request_assessment": {"requested_days": 1}},
            }
        },
    )

    assert plan.action is RuntimeAction.CALL_TOOL
    assert plan.operation_name == "hr.leave_apply"
    assert plan.operation_arguments["leave_type"] == "compensatory"
    assert plan.operation_arguments["days"] == 1
    assert plan.operation_arguments["start_date"] == "2026-07-30"


def test_seed_publishes_immutable_overtime_version_and_review_permissions() -> None:
    """验证种子保留旧快照、发布新版本并同步工具白名单和 HR 接管权限。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(Skill).where(Skill.skill_id == OVERTIME_COMPENSATORY_SKILL_ID)
        ).one()
        versions = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == OVERTIME_COMPENSATORY_SKILL_ID
            )
        ).all()
        tools = {
            tool.name: tool
            for tool in db.exec(
                select(Tool).where(Tool.name.in_(["hr.balance_query", "hr.leave_apply"]))
            ).all()
        }
        role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == "hr_leave_specialist")
        ).one()

        assert skill.version == OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION
        assert {version.version for version in versions} >= {
            "1.0.0",
            OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION,
        }
        assert OVERTIME_COMPENSATORY_SKILL_ID in tools["hr.balance_query"].allowed_skills_json
        assert OVERTIME_COMPENSATORY_SKILL_ID in tools["hr.leave_apply"].allowed_skills_json
        assert {
            "hr.overtime_review.claim",
            "hr.overtime_review.complete",
            "hr.overtime_review.request_information",
        } <= set(role.permissions_json)
