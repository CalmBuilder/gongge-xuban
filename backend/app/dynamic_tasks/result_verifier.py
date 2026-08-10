"""
@Time       : 2026/08/04 03:25
@Author     : zhanglp8181
@File       : result_verifier.py
@CallChain  : DynamicTaskAgent completion → DynamicResultVerifier → ExecutionResult
@Description: 以计划成功标准和已完成步骤证据机械验证动态任务最终结果。
"""

from __future__ import annotations

from collections.abc import Mapping

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
    required_evidence_by_step: Mapping[str, Mapping[str, object]] | None = None,
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
    missing_result_evidence = _missing_result_evidence(
        result.markdown,
        required_evidence_by_step or {},
    )
    passed = not (missing or unknown or empty or invalid_refs or missing_result_evidence)
    return {
        "passed": passed,
        "criterion_ids": sorted(required_ids),
        "missing_criteria": missing,
        "unknown_criteria": unknown,
        "empty_criteria": empty,
        "invalid_step_refs": invalid_refs,
        "missing_result_evidence": missing_result_evidence,
        "completed_step_keys": sorted(completed_step_keys),
    }


def _missing_result_evidence(
    markdown: str,
    required_evidence_by_step: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """验证能力声明的关键回执值真实进入交付正文，拒绝 step 占位语冒充事实。"""

    missing: list[str] = []
    normalized = markdown.casefold()
    for step_key, values in required_evidence_by_step.items():
        for path, value in values.items():
            if not _markdown_contains_value(normalized, value):
                missing.append(f"{step_key}:{path}")
    return sorted(missing)


def _markdown_contains_value(markdown_casefold: str, value: object) -> bool:
    """按标量类型判断 Markdown 是否表达真实值，并为显式空配置提供中文语义。"""

    if isinstance(value, bool):
        candidates = ("true", "启用", "已启用", "开启") if value else (
            "false",
            "停用",
            "禁用",
            "未启用",
            "关闭",
        )
        return any(candidate in markdown_casefold for candidate in candidates)
    if value is None or value == "":
        return any(
            marker in markdown_casefold
            for marker in ("未配置", "未设置", "为空", "无主页", "无地址")
        )
    if isinstance(value, (str, int, float)):
        return str(value).strip().casefold() in markdown_casefold
    return False
