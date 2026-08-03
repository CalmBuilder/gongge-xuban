"""
@Time       : 2026/07/27 21:20
@Author     : zhanglp8181
@File       : test_travel_reimbursement_sop.py
@CallChain  : pytest → 差旅报销 v2 定义/公共 Mock/种子 → Scheduler
@Description: 验证政策证据、住宿标准、发票验真、明确确认和财务复核的确定性闭环。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION,
    TRAVEL_REIMBURSEMENT_SKILL_ID,
    _travel_reimbursement_deterministic_content,
)
from app.db.models import BusinessRole, Skill, SkillVersion, Tool
from app.db.seed import seed_demo_data
from app.public_mock.service import execute_public_mock
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action
from app.sop_runtime.slot_values import canonicalize_slot_keys


def _definition():
    """编译差旅报销 v2 的零告警统一元模型定义。"""

    return compile_legacy_skill_card(_travel_reimbursement_deterministic_content({}))


def test_travel_policy_assessment_freezes_domestic_staff_lodging_rules() -> None:
    """验证境内普通员工住宿档位、晚数、限额和超标金额均由确定性回执产生。"""

    within = execute_public_mock(
        "expense.travel_policy_assess",
        {
            "employee_id": "E002",
            "destination_city": "杭州",
            "trip_start_date": "2026-07-20",
            "trip_end_date": "2026-07-22",
            "expense_category": "lodging",
            "claimed_amount": 700,
            "trip_scope": "domestic",
            "trip_approval_status": "approved",
            "trip_approval_number": "TRIP-DEMO-APPROVED-001",
        },
    )
    over = execute_public_mock(
        "expense.travel_policy_assess",
        {
            "employee_id": "E002",
            "destination_city": "上海",
            "trip_start_date": "2026-07-20",
            "trip_end_date": "2026-07-22",
            "expense_category": "lodging",
            "claimed_amount": 1200,
            "trip_scope": "domestic",
            "trip_approval_status": "approved",
            "trip_approval_number": "TRIP-DEMO-APPROVED-001",
        },
    )

    assert within.status == "within_limit"
    assert within.lodging_nights == 2
    assert within.nightly_limit == 400
    assert within.allowance_limit == 800
    assert over.status == "over_limit"
    assert over.allowance_limit == 1000
    assert over.over_limit_amount == 200


def test_travel_policy_rejects_untrusted_employee_approval_and_late_submission() -> None:
    """验证职级、事前申请和十四天时限均由受控回执判断而非用户声明直接放行。"""

    common = {
        "destination_city": "杭州",
        "trip_start_date": "2026-07-20",
        "trip_end_date": "2026-07-22",
        "expense_category": "lodging",
        "claimed_amount": 700,
        "trip_scope": "domestic",
        "trip_approval_status": "approved",
        "trip_approval_number": "TRIP-DEMO-APPROVED-001",
    }
    unsupported = execute_public_mock(
        "expense.travel_policy_assess",
        {"employee_id": "E001", **common},
    )
    unverified = execute_public_mock(
        "expense.travel_policy_assess",
        {
            "employee_id": "E002",
            **common,
            "trip_approval_number": "TRIP-USER-CLAIMED-001",
        },
    )
    late = execute_public_mock(
        "expense.travel_policy_assess",
        {
            "employee_id": "E002",
            **common,
            "trip_start_date": "2026-06-01",
            "trip_end_date": "2026-06-02",
        },
    )

    assert unsupported.status == "unsupported_employee"
    assert unverified.status == "approval_unverified"
    assert unverified.approval_verified is False
    assert late.status == "late_submission"


def test_travel_slot_aliases_only_keep_true_legacy_contracts() -> None:
    """验证差旅新版本不再为模型偶发审批键、目的地键或发票键增加补丁别名。"""

    content = _travel_reimbursement_deterministic_content({})

    assert canonicalize_slot_keys(
        content,
        {
            "amount": 700,
            "description": "客户拜访",
            "approval_status": "approved",
            "destination": "杭州",
        },
    ) == {
        "claimed_amount": 700,
        "expense_reason": "客户拜访",
        "approval_status": "approved",
        "destination": "杭州",
    }


def test_travel_invoice_verification_checks_authenticity_and_amount() -> None:
    """验证发票查验同时输出真伪、字段完整性和申报金额一致性。"""

    valid = execute_public_mock(
        "invoice.verify",
        {
            "invoice_code": "044001",
            "invoice_number": "12345678",
            "invoice_date": "2026-07-21",
            "amount": 700,
            "expected_amount": 700,
        },
    )
    mismatch = execute_public_mock(
        "invoice.verify",
        {
            "invoice_code": "044001",
            "invoice_number": "12345678",
            "invoice_date": "2026-07-21",
            "amount": 600,
            "expected_amount": 700,
        },
    )

    assert valid.authentic is True
    assert valid.fields_complete is True
    assert valid.amount_matches is True
    assert mismatch.amount_matches is False


def test_travel_definition_freezes_knowledge_tools_confirmation_and_review() -> None:
    """验证知识、两个校验工具、提交工具、确认和角色候选工作项均为声明式契约。"""

    definition = _definition()
    policy_plan = plan_next_action(
        definition,
        current_node_id="query_travel_policy",
        slots={"destination_city": "杭州"},
    )
    assess_plan = plan_next_action(
        definition,
        current_node_id="assess_travel_policy",
        slots={
            "employee_id": "E002",
            "destination_city": "杭州",
            "trip_start_date": "2026-07-20",
            "trip_end_date": "2026-07-22",
            "expense_category": "lodging",
            "claimed_amount": 700,
            "trip_scope": "domestic",
        },
    )
    review_node = next(
        node for node in definition.nodes if node.node_id == "finance_travel_review"
    )

    assert definition.meta_model_version == 5
    assert definition.diagnostics == ()
    assert policy_plan.action is RuntimeAction.QUERY_KNOWLEDGE
    assert "发票验真" in policy_plan.operation_arguments["desired_evidence"]
    assert assess_plan.operation_name == "expense.travel_policy_assess"
    assert assess_plan.operation_arguments["claimed_amount"] == 700
    assert review_node.config.candidate_role_codes == ("finance_expense_specialist",)


def test_travel_routes_only_eligible_and_verified_receipts_to_submission() -> None:
    """验证零证据、超标、未事前批准和发票异常均不能进入提交确认。"""

    definition = _definition()
    no_evidence = plan_next_action(
        definition,
        current_node_id="query_travel_policy",
        slots={},
        node_outputs={
            "travel_policy": {"status": "succeeded", "data": {"outcome": "no_match"}}
        },
    )
    within = plan_next_action(
        definition,
        current_node_id="assess_travel_policy",
        slots={"trip_approval_status": "approved"},
        tool_results={
            "travel_policy_assessment": {
                "status": "succeeded",
                "data": {"status": "within_limit"},
            }
        },
    )
    over = plan_next_action(
        definition,
        current_node_id="assess_travel_policy",
        slots={"trip_approval_status": "approved"},
        tool_results={
            "travel_policy_assessment": {
                "status": "succeeded",
                "data": {"status": "over_limit"},
            }
        },
    )
    verified = plan_next_action(
        definition,
        current_node_id="verify_travel_invoice",
        slots={},
        tool_results={
            "invoice_receipt": {
                "status": "succeeded",
                "data": {
                    "authentic": True,
                    "fields_complete": True,
                    "amount_matches": True,
                    "risk_level": "low",
                },
            }
        },
    )

    assert no_evidence.next_node_id == "finance_travel_review"
    assert within.next_node_id == "collect_travel_documents"
    assert over.next_node_id == "finance_travel_review"
    assert verified.next_node_id == "confirm_travel_submit"


def test_travel_submission_uses_verified_business_fields() -> None:
    """验证提交操作只映射可信身份、本次申报字段和已查验发票号。"""

    plan = plan_next_action(
        _definition(),
        current_node_id="submit_travel_expense",
        slots={
            "employee_id": "E002",
            "employee_name": "演示员工",
            "expense_category": "lodging",
            "claimed_amount": 700,
            "invoice_number": "12345678",
            "invoice_date": "2026-07-21",
            "expense_reason": "客户拜访",
        },
    )

    assert plan.operation_name == "expense.submit"
    assert plan.operation_arguments == {
        "employee_id": "E002",
        "employee_name": "演示员工",
        "category": "lodging",
        "amount": 700,
        "invoice_no": "12345678",
        "expense_date": "2026-07-21",
        "description": "客户拜访",
    }


def test_travel_seed_publishes_immutable_version_and_permissions() -> None:
    """验证种子保留历史版本并发布 v2、工具白名单和财务原子权限。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()
        skill = db.exec(
            select(Skill).where(Skill.skill_id == TRAVEL_REIMBURSEMENT_SKILL_ID)
        ).one()
        versions = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == TRAVEL_REIMBURSEMENT_SKILL_ID
            )
        ).all()
        tools = db.exec(
            select(Tool).where(
                Tool.name.in_(
                    [
                        "expense.travel_policy_assess",
                        "invoice.verify",
                        "expense.submit",
                    ]
                )
            )
        ).all()
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == "finance_expense_specialist"
            )
        ).one()

    assert skill.version == TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION
    assert {version.version for version in versions} >= {
        "1.0.0",
        TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION,
    }
    assert all(TRAVEL_REIMBURSEMENT_SKILL_ID in tool.allowed_skills_json for tool in tools)
    assert "expense.travel_review.complete" in role.permissions_json
