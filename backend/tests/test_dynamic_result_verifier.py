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
