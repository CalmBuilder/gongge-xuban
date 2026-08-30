"""
@Time       : 2026/08/04 03:25
@Author     : zhanglp8181
@File       : result_verifier.py
@CallChain  : DynamicTaskAgent completion → DynamicResultVerifier → ExecutionResult
@Description: 以计划成功标准和已完成步骤证据机械验证动态任务最终结果。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.dynamic_tasks.planning import NormalizedPlan


class EvidenceRef(BaseModel):
    """指向同一 Execution 固定 Extraction 元素的不可伪造证据声明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1, max_length=128)
    extraction_id: str = Field(min_length=1, max_length=128)
    read_operation_id: str = Field(min_length=1, max_length=128)
    slice_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    element_id: str = Field(min_length=1, max_length=128)
    element_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    locator: dict[str, object]


class AnalysisClaim(BaseModel):
    """保存可展示结论、类型、值及其一个或多个精确附件证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    text: str = Field(min_length=1, max_length=4000)
    claim_type: str = Field(pattern=r"^(fact|computed|interpretation)$")
    normalized_value: str | int | float | bool | None = None
    unit: str | None = Field(default=None, max_length=64)
    computation_receipt_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    semantic_review_status: str = Field(pattern=r"^(verified|review|required_gap)$")


class GuidanceApplicationItem(BaseModel):
    """记录一条 Skill 原则如何转化为最终正文中的可观察工作。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str | None = Field(
        default=None,
        pattern=r"^guidreq_[a-f0-9]{24}$",
    )
    principle: str = Field(min_length=2, max_length=240)
    application: str = Field(min_length=4, max_length=800)
    evidence_excerpt: str = Field(min_length=2, max_length=500)


class GuidanceApplication(BaseModel):
    """把固定 Skill Use 与至少三项实际应用绑定，避免仅记录已加载。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_use_id: str = Field(min_length=1, max_length=128)
    items: tuple[GuidanceApplicationItem, ...] = Field(min_length=1, max_length=8)


class DynamicTaskResult(BaseModel):
    """表示可发布 Markdown 结果及每条成功标准的权威步骤证据引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    markdown: str = Field(min_length=1, max_length=200_000)
    criterion_evidence: dict[str, tuple[str, ...]]
    pending_questions: tuple[str, ...] = ()
    claims: tuple[AnalysisClaim, ...] = ()
    guidance_applications: tuple[GuidanceApplication, ...] = ()


def verify_dynamic_result(
    result: DynamicTaskResult,
    *,
    plan: NormalizedPlan,
    completed_step_keys: set[str],
    required_evidence_by_step: Mapping[str, Mapping[str, object]] | None = None,
    attachment_evidence_catalog: Mapping[str, Mapping[str, object]] | None = None,
    computation_evidence_catalog: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
    attachment_evidence_required: bool = False,
    guidance_source_catalog: Mapping[str, str] | None = None,
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
    attachment_evidence_errors = _attachment_evidence_errors(
        result,
        attachment_evidence_catalog or {},
        computation_catalog=computation_evidence_catalog or {},
        required=attachment_evidence_required,
    )
    guidance_application_errors = _guidance_application_errors(
        result,
        plan=plan,
        source_catalog=guidance_source_catalog or {},
    )
    passed = not (
        missing
        or unknown
        or empty
        or invalid_refs
        or missing_result_evidence
        or attachment_evidence_errors
        or guidance_application_errors
    )
    return {
        "passed": passed,
        "criterion_ids": sorted(required_ids),
        "missing_criteria": missing,
        "unknown_criteria": unknown,
        "empty_criteria": empty,
        "invalid_step_refs": invalid_refs,
        "missing_result_evidence": missing_result_evidence,
        "attachment_evidence_errors": attachment_evidence_errors,
        "guidance_application_errors": guidance_application_errors,
        "completed_step_keys": sorted(completed_step_keys),
    }


def _guidance_application_errors(
    result: DynamicTaskResult,
    *,
    plan: NormalizedPlan,
    source_catalog: Mapping[str, str],
) -> list[str]:
    """要求每个已加载 Skill 的原则来自权威正文且确实出现在交付结果中。"""

    if plan.guidance_requirements:
        return _planned_guidance_application_errors(result, plan=plan)

    required_ids = set(source_catalog)
    supplied_ids = [item.skill_use_id for item in result.guidance_applications]
    errors: list[str] = []
    if len(supplied_ids) != len(set(supplied_ids)):
        errors.append("guidance:duplicate_skill_use")
    for missing_id in sorted(required_ids - set(supplied_ids)):
        errors.append(f"{missing_id}:guidance_application_required")
    for unknown_id in sorted(set(supplied_ids) - required_ids):
        errors.append(f"{unknown_id}:guidance_application_unknown")
    markdown = result.markdown.casefold()
    for application in result.guidance_applications:
        source = source_catalog.get(application.skill_use_id)
        if source is None:
            continue
        normalized_source = " ".join(source.casefold().split())
        required_item_count = _required_guidance_item_count(source)
        if len(application.items) < required_item_count:
            errors.append(
                f"{application.skill_use_id}:guidance_items_required:{required_item_count}"
            )
        principles: set[str] = set()
        excerpts: set[str] = set()
        for index, item in enumerate(application.items):
            principle = " ".join(item.principle.casefold().split())
            excerpt = " ".join(item.evidence_excerpt.casefold().split())
            if principle not in normalized_source:
                errors.append(f"{application.skill_use_id}:item_{index}:principle_not_in_skill")
            if excerpt not in " ".join(markdown.split()):
                errors.append(f"{application.skill_use_id}:item_{index}:evidence_not_in_markdown")
            if principle in principles:
                errors.append(f"{application.skill_use_id}:item_{index}:duplicate_principle")
            if excerpt in excerpts:
                errors.append(f"{application.skill_use_id}:item_{index}:duplicate_evidence")
            principles.add(principle)
            excerpts.add(excerpt)
    return sorted(set(errors))


def _planned_guidance_application_errors(
    result: DynamicTaskResult,
    *,
    plan: NormalizedPlan,
) -> list[str]:
    """要求结果逐项回证PlanRevision在执行前冻结的适用Guidance要求。"""

    expected = {
        item.requirement_id: item
        for item in plan.guidance_requirements
        if item.disposition.value == "apply"
    }
    not_applicable_use_ids = {
        item.skill_use_id
        for item in plan.guidance_requirements
        if item.disposition.value == "not_applicable"
    }
    errors: list[str] = []
    supplied_ids: list[str] = []
    supplied_use_ids: list[str] = []
    normalized_markdown = " ".join(result.markdown.casefold().split())
    for application in result.guidance_applications:
        supplied_use_ids.append(application.skill_use_id)
        if application.skill_use_id in not_applicable_use_ids:
            errors.append(f"{application.skill_use_id}:guidance_not_applicable_must_not_apply")
        for item in application.items:
            if item.requirement_id is None:
                errors.append(f"{application.skill_use_id}:guidance_requirement_id_required")
                continue
            supplied_ids.append(item.requirement_id)
            requirement = expected.get(item.requirement_id)
            if requirement is None:
                errors.append(f"{item.requirement_id}:guidance_requirement_unknown")
                continue
            if application.skill_use_id != requirement.skill_use_id:
                errors.append(f"{item.requirement_id}:guidance_skill_use_mismatch")
            if " ".join(item.principle.split()) != requirement.principle:
                errors.append(f"{item.requirement_id}:guidance_principle_mismatch")
            excerpt = " ".join(item.evidence_excerpt.casefold().split())
            if excerpt not in normalized_markdown:
                errors.append(f"{item.requirement_id}:guidance_evidence_not_in_markdown")
    if len(supplied_ids) != len(set(supplied_ids)):
        errors.append("guidance:duplicate_requirement")
    if len(supplied_use_ids) != len(set(supplied_use_ids)):
        errors.append("guidance:duplicate_skill_use")
    for requirement_id in sorted(set(expected) - set(supplied_ids)):
        errors.append(f"{requirement_id}:guidance_requirement_required")
    errors.extend(_guidance_delivery_errors(result, plan=plan))
    errors.extend(_guidance_phase_gate_errors(result, plan=plan))
    return sorted(set(errors))


def _guidance_delivery_errors(
    result: DynamicTaskResult,
    *,
    plan: NormalizedPlan,
) -> list[str]:
    """把冻结的可观察验收收敛为正文门禁，避免只在审计尾注中声称已应用。"""

    has_runtime_prerequisite = any(
        step.kind in {"tool.read", "tool.execute", "tool.destructive", "knowledge", "explore"}
        for step in plan.steps
    )
    body = str(result.markdown or "").split("\nSkill应用记录", 1)[0]
    normalized_body = " ".join(body.casefold().split())
    document_task_context = any(
        marker in " ".join(
            [plan.goal, *(step.title for step in plan.steps)]
        ).casefold()
        for marker in ("agents.md", "文档", "报告", "方案", "规范", "document")
    )
    errors: list[str] = []
    for requirement in plan.guidance_requirements:
        if requirement.disposition.value != "apply":
            continue
        requirement_text = " ".join(
            (
                requirement.principle,
                requirement.task_mapping,
                requirement.observable_acceptance,
            )
        ).casefold()
        requirement_id = requirement.requirement_id
        diagnostic = any(
            marker in requirement_text
            for marker in (
                "feedback loop",
                "red-capable",
                "hypothes",
                "假设",
                "probe",
                "探针",
                "phase",
                "阶段",
            )
        )
        # 没有任何真实运行前置步骤时，阶段门只允许 blocked 披露；该语义由
        # _guidance_phase_gate_errors 独立处理，不要求模型凭空生成假设/探针。
        if diagnostic and has_runtime_prerequisite:
            hypothesis_count = len(
                re.findall(
                    r"(?im)(?:^|\n)\s*(?:[-*]\s*)?(?:H[1-9]\b|假设\s*[1-9一二三四五六七八九]\b)",
                    body,
                )
            )
            if hypothesis_count < 3:
                errors.append(f"{requirement_id}:guidance_hypotheses_required")
            if not re.search(r"单一变量|一次只|控制变量|探针|probe", body, re.IGNORECASE):
                errors.append(f"{requirement_id}:guidance_probe_required")
            if not re.search(
                r"退出条件|停止条件|通过条件|red.{0,80}green|修复后.{0,30}(?:恢复|通过)",
                body,
                re.IGNORECASE | re.DOTALL,
            ):
                errors.append(f"{requirement_id}:guidance_exit_criteria_required")
        completion = any(
            marker in requirement_text
            for marker in (
                "completion criterion",
                "完成标准",
                "完成条件",
                "验收",
                "退出码",
                "exit code",
                "command",
                "命令",
            )
        )
        if completion and not re.search(
            r"完成(?:标准|条件)|验收(?:标准|条件)?|退出码|exit\s*code|通过条件",
            normalized_body,
            re.IGNORECASE,
        ):
            errors.append(f"{requirement_id}:guidance_completion_criteria_required")
        # 只有冻结映射明确指向仓库规范/代码变更，或明确提出“改动行为的测试
        # 覆盖”时才要求逐项测试覆盖。领域里的“退款变更”等业务名词不能触发
        # 代码测试门禁；“文档”本身也只是交付载体，不能要求不存在的回执。
        requirement_blob = " ".join(
            (requirement.principle, requirement.task_mapping, requirement.observable_acceptance)
        ).casefold()
        changed_behavior_context = (
            "agents.md" in requirement_blob
            or "changed behavior" in requirement_blob
            or ("改动行为" in requirement_blob and "测试覆盖" in requirement_blob)
        )
        if (completion or document_task_context) and changed_behavior_context and not re.search(
            r"(?:所有|每个)[^\n]{0,40}(?:(?:改动|变更|修改)[^\n]{0,60}行为|行为[^\n]{0,40}(?:改动|变更|修改))[^\n]{0,80}测试|"
            r"every (?:changed|modified) behavior[^\n]{0,100}(?:test|coverage)",
            body,
            re.IGNORECASE,
        ):
            errors.append(f"{requirement_id}:guidance_changed_behavior_test_coverage_required")
    return errors


def _guidance_phase_gate_errors(
    result: DynamicTaskResult,
    *,
    plan: NormalizedPlan,
) -> list[str]:
    """当冻结原则明确禁止跳过前置证据时，拒绝在无运行步骤的结果中伪造假设阶段。"""

    has_runtime_prerequisite = any(
        step.kind in {"tool.read", "tool.execute", "tool.destructive", "knowledge", "explore"}
        for step in plan.steps
    )
    if has_runtime_prerequisite:
        return []
    structured_hypotheses = re.search(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(?:"
        r"H[1-9]\b|假设\s*[1-9一二三四五六七八九]\b|"
        r"最可能原因(?:\s*[（(].{0,24}[）)])?|"
        r"(?:候选|排序|待验证)假设"
        r")",
        result.markdown,
    )
    if structured_hypotheses is None:
        return []
    errors: list[str] = []
    for requirement in plan.guidance_requirements:
        principle = " ".join(requirement.principle.casefold().split())
        if (
            requirement.disposition.value == "apply"
            and (
                re.search(r"do\s+not\s+proceed\s+to\s+hypothesi[sz]e", principle)
                or re.search(r"\bno\b.{0,100}\bno\s+phase\s*[23]\b", principle)
            )
        ):
            errors.append(f"{requirement.requirement_id}:guidance_phase_gate_bypassed")
    return errors


def _required_guidance_item_count(source: str) -> int:
    """按权威正文中可独立执行的语句数确定1至3项应用下限。"""

    statements = {
        " ".join(item.split())
        for item in re.split(r"[\n.!?。！？]+", source)
        if not item.lstrip().startswith("#")
        and len(" ".join(item.lstrip("*- ").split())) >= 4
    }
    return min(3, max(1, len(statements)))


def _attachment_evidence_errors(
    result: DynamicTaskResult,
    catalog: Mapping[str, Mapping[str, object]],
    *,
    computation_catalog: Mapping[str, tuple[Mapping[str, object], ...]],
    required: bool,
) -> list[str]:
    """校验附件 Claim 引用同一 Execution 冻结元素，且 typed事实值由元素正文支持。"""

    unavailable = catalog.get("__unavailable__")
    if unavailable is not None:
        return ["attachments:evidence_unavailable"]
    if not required and not result.claims:
        return []
    if not catalog:
        return ["attachments:evidence_unavailable"]
    if not result.claims:
        return ["claims:required"]
    errors: list[str] = []
    normalized_markdown = " ".join(result.markdown.casefold().split())
    for claim in result.claims:
        if claim.claim_type == "interpretation" and claim.semantic_review_status == "verified":
            errors.append(f"{claim.claim_id}:interpretation_requires_review")
        disclosure_candidates = [claim.text]
        if claim.normalized_value is not None:
            disclosure_candidates.append(str(claim.normalized_value))
        if not any(
            " ".join(candidate.casefold().split()) in normalized_markdown
            for candidate in disclosure_candidates
            if candidate.strip()
        ):
            errors.append(f"{claim.claim_id}:not_disclosed_in_markdown")
        source_texts: list[str] = []
        for reference in claim.evidence_refs:
            expected = catalog.get(reference.element_id)
            if expected is None:
                errors.append(f"{claim.claim_id}:{reference.element_id}:unknown")
                continue
            source_texts.append(" ".join(str(expected.get("text") or "").casefold().split()))
            for key, actual in (
                ("snapshot_id", reference.snapshot_id),
                ("extraction_id", reference.extraction_id),
                ("read_operation_id", reference.read_operation_id),
                ("slice_checksum", reference.slice_checksum),
                ("element_checksum", reference.element_checksum),
                ("locator", reference.locator),
            ):
                if expected.get(key) != actual:
                    errors.append(f"{claim.claim_id}:{reference.element_id}:{key}")
            normalized = claim.normalized_value
            if claim.claim_type == "computed":
                if not claim.computation_receipt_id:
                    errors.append(f"{claim.claim_id}:computation_receipt_required")
                elif not _computed_claim_supported(
                    claim,
                    reference=reference,
                    catalog=computation_catalog,
                ):
                    errors.append(f"{claim.claim_id}:computation_receipt_invalid")
            if claim.claim_type == "fact" and normalized is not None:
                support = str(expected.get("text") or "").casefold()
                visual_facts = expected.get("visual_fact_values")
                visual_values = (
                    visual_facts.get(claim.claim_id, [])
                    if isinstance(visual_facts, Mapping)
                    else []
                )
                normalized_text = str(normalized).strip().casefold()
                if normalized_text not in support and normalized_text not in visual_values:
                    errors.append(f"{claim.claim_id}:{reference.element_id}:unsupported_value")
        if claim.claim_type == "fact" and claim.normalized_value is None:
            claim_text = " ".join(claim.text.casefold().split())
            if not claim_text or not any(claim_text in support for support in source_texts):
                errors.append(f"{claim.claim_id}:unsupported_text")
    return sorted(set(errors))


def _computed_claim_supported(
    claim: AnalysisClaim,
    *,
    reference: EvidenceRef,
    catalog: Mapping[str, tuple[Mapping[str, object], ...]],
) -> bool:
    """要求computed结论精确命中同Execution成功table.compute中的规范事实回执。"""

    checks = catalog.get(str(claim.computation_receipt_id or ""), ())
    normalized = str(claim.normalized_value).strip()
    for check in checks:
        if (
            check.get("fact_key") == claim.claim_id
            and check.get("snapshot_id") == reference.snapshot_id
            and check.get("element_id") == reference.element_id
            and check.get("slice_checksum") == reference.slice_checksum
            and check.get("status") == "match"
            and str(check.get("computed_value")) == normalized
            and isinstance(check.get("formula_checksum"), str)
            and isinstance(check.get("operation_checksum"), str)
            and isinstance(check.get("computation_checksum"), str)
            and isinstance(check.get("evaluator_policy_checksum"), str)
        ):
            return True
    return False


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
