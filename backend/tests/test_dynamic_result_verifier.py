"""
@Time       : 2026/08/04 03:26
@Author     : zhanglp8181
@File       : test_dynamic_result_verifier.py
@CallChain  : pytest → DynamicResultVerifier → verification evidence
@Description: 验证结果不能靠模型文字自证，必须逐项引用已完成步骤。
"""

from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.dynamic_tasks.result_verifier import DynamicTaskResult, verify_dynamic_result


def _plan() -> NormalizedPlan:
    """返回带一条可验证成功标准的两步计划。"""

    return NormalizedPlan(
        goal="生成简报",
        success_criteria=(
            SuccessCriterion(id="brief_ready", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(step_key="query_contract", title="查询合同", kind="tool.read"),
            PlanStep(
                step_key="answer",
                title="形成简报",
                kind="answer",
                depends_on=("query_contract",),
            ),
        ),
    )


def test_result_requires_completed_step_evidence_for_every_criterion() -> None:
    """验证不存在或未完成的引用都会使 verification 明确失败。"""

    invalid = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 风险简报",
            criterion_evidence={"brief_ready": ("invented_step",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
    )
    valid = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 风险简报",
            criterion_evidence={"brief_ready": ("query_contract",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
    )

    assert invalid["passed"] is False
    assert invalid["invalid_step_refs"] == ["invented_step"]
    assert valid["passed"] is True


def test_result_requires_declared_operation_values_in_markdown() -> None:
    """引用步骤但只写占位语不能通过，真实值及显式空配置必须进入交付正文。"""

    required = {
        "query_contract": {
            "name": "共格·序伴连接器测试",
            "enabled": True,
            "home_url": "",
        }
    }
    placeholder = verify_dynamic_result(
        DynamicTaskResult(
            markdown="应用名称为步骤返回值，状态与主页地址同上。",
            criterion_evidence={"brief_ready": ("query_contract",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
        required_evidence_by_step=required,
    )
    factual = verify_dynamic_result(
        DynamicTaskResult(
            markdown="应用名称：共格·序伴连接器测试；状态：已启用；主页地址：未配置。",
            criterion_evidence={"brief_ready": ("query_contract",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
        required_evidence_by_step=required,
    )

    assert placeholder["passed"] is False
    assert placeholder["missing_result_evidence"] == [
        "query_contract:enabled",
        "query_contract:home_url",
        "query_contract:name",
    ]
    assert factual["passed"] is True
    assert factual["missing_result_evidence"] == []


def test_answer_only_plan_may_cite_its_delivery_step_without_faking_external_evidence() -> None:
    """纯生成任务可由交付步骤证明正文，但未知步骤仍不能冒充工具或知识回执。"""

    plan = NormalizedPlan(
        goal="形成操作规范",
        success_criteria=(
            SuccessCriterion(id="document_ready", type="assertion", spec={"required": True}),
        ),
        steps=(PlanStep(step_key="write_playbook", title="形成规范", kind="answer"),),
    )
    accepted = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 售后升级处理\n\n## 输入\n订单号\n\n## 步骤\n核验订单。",
            criterion_evidence={"document_ready": ("write_playbook",)},
        ),
        plan=plan,
        completed_step_keys={"write_playbook"},
    )
    rejected = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 售后升级处理",
            criterion_evidence={"document_ready": ("invented_tool_receipt",)},
        ),
        plan=plan,
        completed_step_keys={"write_playbook"},
    )

    assert accepted["passed"] is True
    assert rejected["invalid_step_refs"] == ["invented_tool_receipt"]
