"""
@Time       : 2026/08/04 03:25
@Author     : zhanglp8181
@File       : result_verifier.py
@CallChain  : DynamicTaskAgent completion → DynamicResultVerifier → ExecutionResult
@Description: 以计划成功标准和已完成步骤证据机械验证动态任务最终结果。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.dynamic_tasks.planning import NormalizedPlan


class DynamicTaskResult(BaseModel):
    """表示可发布 Markdown 结果及每条成功标准的权威步骤证据引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    markdown: str = Field(min_length=1, max_length=200_000)
    criterion_evidence: dict[str, tuple[str, ...]]
    pending_questions: tuple[str, ...] = ()


def verify_dynamic_result(
    result: DynamicTaskResult,
    *,
    plan: NormalizedPlan,
    completed_step_keys: set[str],
) -> dict[str, object]:
    """要求每条成功标准都有已完成步骤证据，拒绝模型以文字自证完成。"""

    required_ids = {criterion.id for criterion in plan.success_criteria}
    supplied_ids = set(result.criterion_evidence)
    missing = sorted(required_ids - supplied_ids)
    unknown = sorted(supplied_ids - required_ids)
    empty = sorted(
        criterion_id
        for criterion_id in required_ids
        if not result.criterion_evidence.get(criterion_id)
    )
    invalid_refs = sorted(
        {
            ref
            for refs in result.criterion_evidence.values()
            for ref in refs
            if ref not in completed_step_keys
        }
    )
    passed = not (missing or unknown or empty or invalid_refs)
    return {
        "passed": passed,
        "criterion_ids": sorted(required_ids),
        "missing_criteria": missing,
        "unknown_criteria": unknown,
        "empty_criteria": empty,
        "invalid_step_refs": invalid_refs,
        "completed_step_keys": sorted(completed_step_keys),
    }
