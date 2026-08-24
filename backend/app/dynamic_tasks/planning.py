"""
@Time       : 2026/08/04 01:04
@Author     : zhanglp8181
@File       : planning.py
@CallChain  : DynamicTaskAgent/FormalSopPlanner → normalized plan/proposal → SopExecutionStore
@Description: 定义统一计划、步骤、完整模型提案及正式 SOP 计划投影契约。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.sop_runtime.definition import CompiledSopDefinition


class PlanReason(StrEnum):
    """计划首建和追加修订允许使用的稳定原因。"""

    INITIAL = "initial"
    TOOL_UNAVAILABLE = "tool_unavailable"
    USER_CONSTRAINT = "user_constraint"
    EVIDENCE_MISSING = "evidence_missing"
    EXTERNAL_CHANGE = "external_change"
    SKILL_ADDED = "skill_added"
    SKILL_COUNTERMANDED = "skill_countermanded"


class ActionKind(StrEnum):
    """Runtime 首批能够验证但尚不代表已授权执行的动作类别。"""

    ANSWER = "answer"
    QUERY_KNOWLEDGE = "query_knowledge"
    CALL_TOOL = "call_tool"
    CREATE_ARTIFACT = "create_artifact"
    WAIT_INPUT = "wait_input"
    WAIT_ATTENTION = "wait_attention"
    WAIT_EVENT = "wait_event"
    REPLAN = "replan"
    COMPLETE = "complete"
    FAIL = "fail"


class PlanningContract(BaseModel):
    """禁止额外字段和就地修改的规划持久契约基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SuccessCriterion(PlanningContract):
    """声明可由 Runtime 或后续验证器核验的稳定成功标准。"""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    type: str = Field(pattern=r"^(schema|artifact|assertion)$")
    spec: dict[str, Any]


class GuidanceSourceKind(StrEnum):
    """限定规划要求能够引用的固定 Guidance 来源类别。"""

    INSTRUCTIONS = "instructions"
    REVIEWED_RESOURCE = "reviewed_resource"


class GuidanceDisposition(StrEnum):
    """声明权威原则在本任务中应用或经解释后不适用。"""

    APPLY = "apply"
    NOT_APPLICABLE = "not_applicable"


class GuidanceRequirementDraft(PlanningContract):
    """保存模型提出、尚未获得服务端身份的 Guidance 应用要求。"""

    skill_ref: str = Field(min_length=1, max_length=256)
    source_kind: GuidanceSourceKind
    source_ref: str = Field(min_length=1, max_length=512)
    # 这是仅供模型草案使用的字段。不要在 Pydantic 层先拒绝截断/损坏的候选 ID，
    # 否则 planner 无法进入一次受限 repair；最终冻结的 GuidanceRequirement 仍使用严格 ID。
    principle_candidate_id: str | None = Field(default=None, min_length=1, max_length=128)
    principle_candidate_id_short: str | None = Field(
        default=None,
        min_length=12,
        max_length=24,
        pattern=r"^[a-fA-F0-9]+$",
    )
    principle: str | None = Field(default=None, min_length=2, max_length=240)
    task_mapping: str = Field(min_length=2, max_length=2000)
    observable_acceptance: str = Field(min_length=2, max_length=2000)
    disposition: GuidanceDisposition = GuidanceDisposition.APPLY

    @model_validator(mode="after")
    def validate_principle_identity(self) -> "GuidanceRequirementDraft":
        """新计划优先使用服务端候选ID，同时兼容旧模型的精确原文短语。"""

        candidate_ids = sum(
            bool(value)
            for value in (self.principle_candidate_id, self.principle_candidate_id_short)
        )
        if candidate_ids == 0 and not self.principle:
            raise ValueError("GuidanceRequirement 必须声明候选ID或精确原则原文")
        return self


class GuidanceRequirement(PlanningContract):
    """冻结来源校验后的 Guidance 要求及服务端稳定身份。"""

    requirement_id: str = Field(pattern=r"^guidreq_[a-f0-9]{24}$")
    skill_use_id: str = Field(min_length=1, max_length=128)
    skill_ref: str = Field(min_length=1, max_length=256)
    source_kind: GuidanceSourceKind
    source_ref: str = Field(min_length=1, max_length=512)
    principle: str = Field(min_length=2, max_length=240)
    task_mapping: str = Field(min_length=2, max_length=2000)
    observable_acceptance: str = Field(min_length=2, max_length=2000)
    disposition: GuidanceDisposition


class PlanStep(PlanningContract):
    """保存服务端稳定 step key 和展示/执行所需的规范步骤事实。"""

    step_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    title: str = Field(min_length=1, max_length=256)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    required: bool = True
    depends_on: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    guidance_skill_use_ids: tuple[str, ...] = ()
    expected_output_schema: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dependencies(self) -> "PlanStep":
        """拒绝步骤依赖自己或重复声明同一前置步骤。"""

        if self.step_key in self.depends_on:
            raise ValueError("步骤不能依赖自身")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on 不得重复")
        if len(set(self.guidance_skill_use_ids)) != len(self.guidance_skill_use_ids):
            raise ValueError("guidance_skill_use_ids 不得重复")
        return self


class NormalizedPlan(PlanningContract):
    """表示可计算 checksum、可追加修订的完整动态计划。"""

    goal: str = Field(min_length=1, max_length=4000)
    success_criteria: tuple[SuccessCriterion, ...] = Field(min_length=1)
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    guidance_requirements: tuple[GuidanceRequirement, ...] = ()
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    expected_artifacts: tuple[dict[str, Any], ...] = ()
    budget: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> "NormalizedPlan":
        """验证 step/criterion 身份唯一且所有依赖只指向当前计划中的步骤。"""

        step_keys = [step.step_key for step in self.steps]
        if len(set(step_keys)) != len(step_keys):
            raise ValueError("step_key 在 execution 计划内必须唯一")
        criterion_ids = [criterion.id for criterion in self.success_criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("成功标准 id 不得重复")
        requirement_ids = [item.requirement_id for item in self.guidance_requirements]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("Guidance requirement_id 不得重复")
        artifact_keys: list[str] = []
        allowed_artifact_fields = {
            "artifact_key",
            "filename",
            "mime_type",
            "content_source",
            "required",
        }
        if len(self.expected_artifacts) > 20:
            raise ValueError("单次执行最多声明 20 个 Artifact")
        for artifact in self.expected_artifacts:
            if set(artifact) - allowed_artifact_fields:
                raise ValueError("Artifact 声明包含未知字段")
            artifact_key = str(artifact.get("artifact_key") or "")
            filename = str(artifact.get("filename") or "")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", artifact_key):
                raise ValueError("Artifact key 无效")
            if not filename or len(filename) > 191 or "/" in filename or "\\" in filename:
                raise ValueError("Artifact filename 无效")
            if artifact.get("mime_type") not in {
                "text/markdown",
                "text/plain",
                "text/csv",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }:
                raise ValueError("Artifact MIME 不受确定性 renderer 支持")
            if artifact.get("content_source", "result.markdown") != "result.markdown":
                raise ValueError("Artifact content source 无效")
            artifact_keys.append(artifact_key)
        if len(set(artifact_keys)) != len(artifact_keys):
            raise ValueError("Artifact key 不得重复")
        known = set(step_keys)
        dependencies: dict[str, set[str]] = {}
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"步骤 {step.step_key} 引用了不存在的依赖")
            dependencies[step.step_key] = set(step.depends_on)
        resolved: set[str] = set()
        while dependencies:
            ready = {key for key, values in dependencies.items() if values <= resolved}
            if not ready:
                raise ValueError("计划步骤依赖必须构成无环图")
            resolved.update(ready)
            dependencies = {key: values for key, values in dependencies.items() if key not in ready}
        return self


class DynamicPlanDraftStep(PlanningContract):
    """表示模型可提议的步骤语义，禁止模型直接决定持久 step key。"""

    draft_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=256)
    kind: str = Field(
        pattern=(
            r"^(tool\.read|tool\.write|tool\.execute|knowledge|explore|answer|clarification)$"
        )
    )
    required: bool = True
    depends_on: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    guidance_skill_refs: tuple[str, ...] = ()
    expected_output_schema: dict[str, Any] = Field(default_factory=dict)


class DynamicPlanDraft(PlanningContract):
    """保存 provider 完整结构化响应中的有界计划草案。"""

    goal: str = Field(min_length=1, max_length=4000)
    success_criteria: tuple[SuccessCriterion, ...] = Field(min_length=1)
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    guidance_requirements: tuple[GuidanceRequirementDraft, ...] = ()
    steps: tuple[DynamicPlanDraftStep, ...] = Field(min_length=1)
    expected_artifacts: tuple[dict[str, Any], ...] = ()

    @model_validator(mode="after")
    def validate_draft_graph(self) -> "DynamicPlanDraft":
        """拒绝重复草案身份、未知依赖和循环依赖。"""

        identities = [step.draft_id for step in self.steps]
        if len(identities) != len(set(identities)):
            raise ValueError("计划草案 draft_id 不得重复")
        known = set(identities)
        dependencies = {step.draft_id: set(step.depends_on) for step in self.steps}
        if any(step_id in values or values - known for step_id, values in dependencies.items()):
            raise ValueError("计划草案依赖不存在或引用自身")
        resolved: set[str] = set()
        while dependencies:
            ready = {key for key, values in dependencies.items() if values <= resolved}
            if not ready:
                raise ValueError("计划草案依赖必须构成无环图")
            resolved.update(ready)
            dependencies = {key: values for key, values in dependencies.items() if key not in ready}
        return self


def normalize_plan_draft(
    draft: DynamicPlanDraft,
    *,
    max_steps: int,
    max_tool_calls: int,
    max_model_calls: int,
    max_input_tokens: int = 120_000,
    max_output_tokens: int = 24_000,
    max_total_tokens: int = 144_000,
    max_runtime_seconds: int = 900,
    guidance_use_ids_by_name: Mapping[str, tuple[str, ...]] | None = None,
    guidance_sources_by_name: Mapping[str, tuple[Mapping[str, str], ...]] | None = None,
    guidance_selection_modes_by_name: Mapping[str, str] | None = None,
) -> NormalizedPlan:
    """为草案生成稳定服务端 step key，并以服务端预算覆盖任何模型暗示。"""

    if (
        max_steps < 1
        or max_tool_calls < 0
        or max_model_calls < 1
        or max_input_tokens < 1
        or max_output_tokens < 1
        or max_total_tokens < max(max_input_tokens, max_output_tokens)
        or max_runtime_seconds < 1
    ):
        raise ValueError("动态计划预算无效")
    if len(draft.steps) > max_steps:
        raise ValueError("动态计划步骤超过服务端预算")
    capability_steps = sum(
        step.kind in {"tool.read", "tool.write", "tool.execute", "knowledge", "explore"}
        for step in draft.steps
    )
    if capability_steps > max_tool_calls:
        raise ValueError("动态计划能力调用步骤超过服务端预算")
    key_by_draft_id = {
        step.draft_id: _stable_step_key(index, step)
        for index, step in enumerate(draft.steps, start=1)
    }
    guidance_mapping = guidance_use_ids_by_name or {}
    all_guidance_use_ids = tuple(
        dict.fromkeys(
            use_id
            for use_ids in guidance_mapping.values()
            for use_id in use_ids
        )
    )
    unknown_guidance = {
        reference
        for step in draft.steps
        for reference in step.guidance_skill_refs
        if reference not in guidance_mapping
    }
    if unknown_guidance:
        raise ValueError("动态计划引用了未加载的指导 Skill")
    _validate_nonblocking_guidance_clarifications(
        draft.steps,
        guidance_sources_by_name or {},
    )
    guidance_requirements = _normalize_guidance_requirements(
        draft.guidance_requirements,
        guidance_use_ids_by_name=guidance_mapping,
        guidance_sources_by_name=guidance_sources_by_name,
        guidance_selection_modes_by_name=guidance_selection_modes_by_name or {},
        planned_step_kinds=tuple(step.kind for step in draft.steps),
    )
    normalized_steps = tuple(
        PlanStep(
            step_key=key_by_draft_id[step.draft_id],
            title=step.title,
            kind=step.kind,
            required=step.required,
            depends_on=tuple(key_by_draft_id[value] for value in step.depends_on),
            capability_refs=step.capability_refs,
            guidance_skill_use_ids=(
                all_guidance_use_ids
                if step.kind
                in {
                    "tool.read",
                    "tool.write",
                    "tool.execute",
                    "knowledge",
                    "explore",
                    "answer",
                }
                else tuple(
                    dict.fromkeys(
                        use_id
                        for name in step.guidance_skill_refs
                        for use_id in guidance_mapping[name]
                    )
                )
            ),
            expected_output_schema=step.expected_output_schema,
        )
        for step in draft.steps
    )
    return NormalizedPlan(
        goal=draft.goal,
        success_criteria=draft.success_criteria,
        constraints=draft.constraints,
        assumptions=draft.assumptions,
        guidance_requirements=guidance_requirements,
        steps=_normalize_terminal_answer_dependencies(normalized_steps),
        expected_artifacts=draft.expected_artifacts,
        budget={
            "max_steps": max_steps,
            "max_tool_calls": max_tool_calls,
            "max_model_calls": max_model_calls,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "max_total_tokens": max_total_tokens,
            "max_runtime_seconds": max_runtime_seconds,
        },
    )


def _normalize_terminal_answer_dependencies(
    steps: tuple[PlanStep, ...],
) -> tuple[PlanStep, ...]:
    """把唯一 required answer 固定为所有 required 步骤的汇聚点，消除模型反向依赖。"""

    answers = [step for step in steps if step.kind == "answer" and step.required]
    if len(answers) != 1:
        return steps
    answer = answers[0]
    required_predecessors = tuple(
        step.step_key for step in steps if step.required and step.step_key != answer.step_key
    )
    normalized: list[PlanStep] = []
    for step in steps:
        if step.step_key == answer.step_key:
            dependencies = tuple(
                dict.fromkeys(
                    dependency
                    for dependency in (*step.depends_on, *required_predecessors)
                    if dependency != answer.step_key
                )
            )
        elif step.required:
            dependencies = tuple(
                dependency for dependency in step.depends_on if dependency != answer.step_key
            )
        else:
            dependencies = step.depends_on
        normalized.append(step.model_copy(update={"depends_on": dependencies}))
    return tuple(normalized)


def _validate_nonblocking_guidance_clarifications(
    steps: tuple[DynamicPlanDraftStep, ...],
    guidance_sources_by_name: Mapping[str, tuple[Mapping[str, str], ...]],
) -> None:
    """固定Skill明确要求非阻塞检查点时，拒绝把假设排序升级成必答澄清。"""

    has_nonblocking_hypothesis_checkpoint = any(
        re.search(
            r"(?:don['’]?t|do\s+not)\s+block\s+on\s+it|(?:不得|不要|无需).{0,30}阻塞",
            str(source.get("content") or "").casefold(),
        )
        for sources in guidance_sources_by_name.values()
        for source in sources
    )
    if not has_nonblocking_hypothesis_checkpoint:
        return
    for step in steps:
        if step.kind != "clarification":
            continue
        title = step.title.casefold()
        if re.search(
            r"(?:确认|选择|排序|优先).{0,30}(?:假设|猜想|hypothes)"
            r"|(?:假设|猜想|hypothes).{0,30}(?:确认|选择|排序|优先)",
            title,
        ):
            raise ValueError("Guidance 明确非阻塞的假设检查点不得规划为 clarification")


def _normalize_guidance_requirements(
    draft_requirements: tuple[GuidanceRequirementDraft, ...],
    *,
    guidance_use_ids_by_name: Mapping[str, tuple[str, ...]],
    guidance_sources_by_name: Mapping[str, tuple[Mapping[str, str], ...]] | None,
    guidance_selection_modes_by_name: Mapping[str, str],
    planned_step_kinds: tuple[str, ...],
) -> tuple[GuidanceRequirement, ...]:
    """将模型声明绑定到固定来源，并执行每个已加载 Skill 的完整性语义。"""

    if guidance_sources_by_name is None:
        if draft_requirements:
            raise ValueError("GuidanceRequirement 缺少服务端权威来源")
        return ()
    loaded_names = set(guidance_use_ids_by_name)
    if set(guidance_sources_by_name) != loaded_names:
        raise ValueError("Guidance 权威来源必须与已加载 Skill 一一对应")
    unknown = {item.skill_ref for item in draft_requirements} - loaded_names
    if unknown:
        raise ValueError("GuidanceRequirement 引用了未加载的指导 Skill")
    normalized: list[GuidanceRequirement] = []
    for skill_ref in sorted(loaded_names):
        use_ids = tuple(dict.fromkeys(guidance_use_ids_by_name[skill_ref]))
        if len(use_ids) != 1 or not use_ids[0]:
            raise ValueError("每个 Guidance skill_ref 必须绑定唯一固定 SkillUse")
        sources = _validated_guidance_sources(guidance_sources_by_name[skill_ref])
        candidates = _guidance_principle_candidates_for_sources(sources)
        requirements = [item for item in draft_requirements if item.skill_ref == skill_ref]
        not_applicable = [
            item for item in requirements if item.disposition == GuidanceDisposition.NOT_APPLICABLE
        ]
        if not_applicable and (len(requirements) != 1 or len(not_applicable) != 1):
            raise ValueError("not_applicable 必须是该 Skill 的唯一 GuidanceRequirement")
        if not not_applicable and not 1 <= len(requirements) <= 3:
            raise ValueError("每个已加载 Skill 必须声明 1..3 条 GuidanceRequirement")
        selected_candidates: list[Mapping[str, Any]] = []
        normalized_for_skill: list[
            tuple[tuple[str, str, int, str], GuidanceRequirement]
        ] = []
        normalized_requirement_ids: set[str] = set()
        for requirement in requirements:
            source_key = (requirement.source_kind.value, requirement.source_ref)
            source = sources.get(source_key)
            candidate_reference = (
                requirement.principle_candidate_id
                or requirement.principle_candidate_id_short
            )
            if candidate_reference:
                candidate = candidates.get(candidate_reference)
                if candidate is None:
                    # 模型有时在长候选目录中截断 ID。只有当它仍是至少 12 位
                    # 十六进制前缀且在已加载 Skill 的全部权威来源中唯一命中时才恢复；
                    # 不做模糊相似度或跨 Skill 猜测。
                    prefix = candidate_reference.removeprefix("guidcand_").casefold()
                    if re.fullmatch(r"[a-f0-9]{12,24}", prefix):
                        matches = [
                            item for candidate_id, item in candidates.items()
                            if candidate_id.removeprefix("guidcand_").startswith(prefix)
                        ]
                        if len(matches) == 1:
                            candidate = matches[0]
                if candidate is None and requirement.principle:
                    exact_principle_matches = [
                        item
                        for item in candidates.values()
                        if _normalized_guidance_text(str(item["principle"]))
                        == _normalized_guidance_text(requirement.principle)
                    ]
                    if len(exact_principle_matches) == 1:
                        candidate = exact_principle_matches[0]
                if candidate is None:
                    raise ValueError("GuidanceRequirement 候选ID不属于已加载权威来源")
                if requirement.principle and _normalized_guidance_text(requirement.principle) != _normalized_guidance_text(
                    str(candidate["principle"])
                ):
                    raise ValueError("GuidanceRequirement 候选ID与原则原文不一致")
                # 候选 ID 由服务端从 source checksum/path 派生，是唯一可信的来源身份。
                # 模型抄写的 source_kind/source_ref 仅作草案字段，不能让一个合法候选
                # 因为元数据配对错误而失败，也不能改变候选实际所属的受管来源。
                source_key = (str(candidate["source_kind"]), str(candidate["source_ref"]))
                source = sources.get(source_key)
                if source is None:
                    raise ValueError("GuidanceRequirement 候选ID不属于已加载权威来源")
                principle = candidate["principle"]
                if requirement.disposition == GuidanceDisposition.APPLY:
                    selected_candidates.append(candidate)
                source_order = int(candidate.get("source_order") or 0)
            else:
                if source is None:
                    raise ValueError("GuidanceRequirement 引用了无效资源 path/checksum")
                principle = _normalized_guidance_text(str(requirement.principle or ""))
                if principle not in _normalized_guidance_text(source["content"]):
                    raise ValueError("GuidanceRequirement principle 不在指定权威来源中")
                source_order = max(0, source["content"].find(principle))
            task_mapping = _normalized_guidance_text(requirement.task_mapping)
            acceptance = _normalized_guidance_text(requirement.observable_acceptance)
            task_mapping, acceptance = _normalize_guidance_gate_mapping(
                principle,
                task_mapping=task_mapping,
                observable_acceptance=acceptance,
            )
            requirement_id = "guidreq_" + canonical_checksum(
                {
                    "skill_ref": skill_ref,
                    "source_checksum": source["source_checksum"],
                    "source_path": source_key[1],
                    "principle": principle,
                    "task_mapping": task_mapping,
                    "observable_acceptance": acceptance,
                    "disposition": requirement.disposition.value,
                }
            )[:24]
            normalized_requirement = GuidanceRequirement(
                requirement_id=requirement_id,
                skill_use_id=use_ids[0],
                skill_ref=skill_ref,
                source_kind=GuidanceSourceKind(source_key[0]),
                source_ref=source_key[1],
                principle=principle,
                task_mapping=task_mapping,
                observable_acceptance=acceptance,
                disposition=requirement.disposition,
            )
            # 模型偶尔会把同一条原则完整重复返回。这里按服务端派生的
            # requirement_id 做幂等去重：完全相同的声明没有新增语义、权限或
            # 审计事实，可以安全折叠；只要 task_mapping/acceptance 等任一冻结
            # 字段不同，派生 ID 也会不同，仍会继续走重复/数量契约校验。
            if requirement_id in normalized_requirement_ids:
                continue
            normalized_requirement_ids.add(requirement_id)
            normalized_for_skill.append(
                (
                    (
                        source_key[0],
                        source_key[1],
                        source_order,
                        requirement_id,
                    ),
                    normalized_requirement,
                )
            )
        _validate_guidance_phase_closure(
            selected_candidates,
            tuple(candidates.values()),
            planned_step_kinds=planned_step_kinds,
        )
        normalized.extend(item for _, item in sorted(normalized_for_skill, key=lambda row: row[0]))
    auto_names = {
        name for name in loaded_names if guidance_selection_modes_by_name.get(name) == "auto"
    }
    rejected_auto_names = {
        item.skill_ref
        for item in normalized
        if item.skill_ref in auto_names
        and item.disposition == GuidanceDisposition.NOT_APPLICABLE
    }
    if rejected_auto_names:
        raise ValueError("每个自动选择的 Guidance 都必须至少有一条 apply")
    return tuple(normalized)


def _normalize_guidance_gate_mapping(
    principle: str,
    *,
    task_mapping: str,
    observable_acceptance: str,
) -> tuple[str, str]:
    """把“无回路不得假设”收敛为宿主安全映射，其他否决门仍要求停止语义。"""

    if not _is_guidance_phase_gate(principle):
        return task_mapping, observable_acceptance
    combined = f"{task_mapping} {observable_acceptance}"
    contradictory = bool(
        re.search(
            r"(?:只|仍|同时|但|而).{0,24}(?:给出|列出|提出|生成|排序).{0,30}"
            r"(?:假设|原因|根因)"
            r"|(?:待验证|候选).{0,12}(?:假设|原因|根因)"
            r"|(?:假设|原因|根因).{0,12}(?:方向|列表|排序)",
            combined,
        )
    )
    hypothesis_gate = bool(re.search(r"hypothes|假设|猜想", principle, re.IGNORECASE))
    has_stop_semantics = bool(
        re.search(r"停止|阻塞|请求|缺少|证据|不得|不进入|不继续", combined)
    )
    if hypothesis_gate and (contradictory or not has_stop_semantics):
        return (
            "当前任务缺少已运行的 red-capable feedback loop；"
            "停止进入假设阶段，并请求建立反馈回路所需的脱敏证据或明确授权。",
            "最终交付必须披露反馈回路缺失和证据/授权请求，"
            "不得输出原因排序、根因候选或待验证假设。",
        )
    if not has_stop_semantics:
        raise ValueError("Guidance 否决门禁必须映射为停止、阻塞或请求前置证据")
    return task_mapping, observable_acceptance


def _validate_guidance_phase_closure(
    selected_candidates: list[Mapping[str, Any]],
    all_candidates: tuple[Mapping[str, Any], ...],
    *,
    planned_step_kinds: tuple[str, ...],
) -> None:
    """拒绝跳过分阶段方法的连续阶段或显式否决门。"""

    selected_by_source: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    all_by_source: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for candidate in selected_candidates:
        key = (str(candidate.get("source_kind") or ""), str(candidate.get("source_ref") or ""))
        selected_by_source.setdefault(key, []).append(candidate)
    for candidate in all_candidates:
        key = (str(candidate.get("source_kind") or ""), str(candidate.get("source_ref") or ""))
        all_by_source.setdefault(key, []).append(candidate)

    for source_key, selected in selected_by_source.items():
        selected_phases = {
            phase
            for candidate in selected
            if (phase := _guidance_phase_number(str(candidate.get("section_path") or "")))
            is not None
        }
        if not selected_phases:
            continue
        highest_phase = max(selected_phases)
        missing = [phase for phase in range(1, highest_phase + 1) if phase not in selected_phases]
        if missing:
            missing_text = ", ".join(str(value) for value in missing)
            raise ValueError(f"Guidance 分阶段方法缺少连续前置阶段: {missing_text}")
        blocking_phases = {
            phase
            for candidate in all_by_source.get(source_key, [])
            if (phase := _guidance_phase_number(str(candidate.get("section_path") or "")))
            is not None
            and phase < highest_phase
            and _is_guidance_phase_gate(str(candidate.get("principle") or ""))
        }
        has_prerequisite_operation = any(
            kind in {"tool.read", "tool.execute", "knowledge", "explore"}
            for kind in planned_step_kinds
        )
        # 上游 diagnosing-bugs 的 Phase 1 否决门不是普通建议：没有已经规划的
        # red-capable 前置操作时，必须把权威 gate 本身冻结进 PlanRevision，不能
        # 只选择“停止/列出尝试”等旁支句子，再由模型自行跳到 Phase 3。这个
        # 宿主门禁吸收 OpenWorker/Hermes 的 eligibility-first 思路，但不执行
        # Skill 正文，也不把某个 Skill 名称硬编码进 Runtime。
        if not has_prerequisite_operation:
            phase_gate_candidates = [
                candidate
                for candidate in all_by_source.get(source_key, [])
                if _is_guidance_phase_gate(str(candidate.get("principle") or ""))
            ]
            selected_gate_keys = {
                (
                    str(candidate.get("source_kind") or ""),
                    str(candidate.get("source_ref") or ""),
                    str(candidate.get("principle") or ""),
                )
                for candidate in selected
            }
            has_selected_gate = any(
                (
                    str(candidate.get("source_kind") or ""),
                    str(candidate.get("source_ref") or ""),
                    str(candidate.get("principle") or ""),
                )
                in selected_gate_keys
                for candidate in phase_gate_candidates
            )
            missing_gate = phase_gate_candidates[0] if phase_gate_candidates else None
            if missing_gate is not None and selected and not has_selected_gate:
                raise ValueError(
                    "Guidance 分阶段方法缺少当前无运行回路的前置否决门: "
                    f"{str(missing_gate.get('principle') or '')[:240]}"
                )
        if blocking_phases and not has_prerequisite_operation:
            phases = ", ".join(str(value) for value in sorted(blocking_phases))
            raise ValueError(f"Guidance 分阶段方法存在未满足前置门禁: {phases}")


def _guidance_phase_number(section_path: str) -> int | None:
    """从固定来源章节路径提取阿拉伯数字 Phase/阶段编号。"""

    matches = re.findall(r"(?:\bphase\s*|阶段\s*)([1-9]\d*)", section_path, re.IGNORECASE)
    return int(matches[-1]) if matches else None


def _is_guidance_phase_gate(principle: str) -> bool:
    """识别固定来源中明确禁止越过当前阶段的否决语句。"""

    normalized = _normalized_guidance_text(principle).casefold()
    return bool(
        re.search(r"do\s+\*{0,2}not\*{0,2}\s+proceed", normalized)
        or re.search(r"\bno\b.{0,100}\bno\s+phase\b", normalized)
        or re.search(r"(?:不得|禁止).{0,60}(?:进入|继续|跳到|开始).{0,30}(?:阶段|phase)", normalized)
    )


def _validated_guidance_sources(
    raw_sources: tuple[Mapping[str, str], ...],
) -> dict[tuple[str, str], dict[str, str]]:
    """校验固定来源类别、资源路径、checksum 和正文，拒绝同键歧义。"""

    sources: dict[tuple[str, str], dict[str, str]] = {}
    for raw in raw_sources:
        kind = str(raw.get("source_kind") or "")
        source_ref = str(raw.get("source_ref") or "")
        checksum = str(raw.get("source_checksum") or "")
        content = str(raw.get("content") or "")
        if kind not in {item.value for item in GuidanceSourceKind}:
            raise ValueError("Guidance 权威来源类别无效")
        if kind == GuidanceSourceKind.INSTRUCTIONS and source_ref != "instructions":
            raise ValueError("instructions 来源 source_ref 无效")
        if kind == GuidanceSourceKind.REVIEWED_RESOURCE and (
            not source_ref
            or source_ref.startswith("/")
            or "\\" in source_ref
            or ".." in source_ref.split("/")
        ):
            raise ValueError("reviewed resource path 无效")
        if re.fullmatch(r"[a-f0-9]{64}", checksum) is None or not content.strip():
            raise ValueError("Guidance 资源 checksum 或正文无效")
        key = (kind, source_ref)
        if key in sources:
            raise ValueError("Guidance 权威来源不得重复")
        sources[key] = {
            "source_checksum": checksum,
            "content": content,
        }
    if not sources:
        raise ValueError("已加载 Skill 缺少 Guidance 权威来源")
    return sources


def _normalized_guidance_text(value: str) -> str:
    """折叠 Unicode 空白但保留大小写和标点，支持精确来源短语匹配。"""

    return " ".join(value.split())


def guidance_principle_candidates(
    raw_sources: tuple[Mapping[str, str], ...],
) -> tuple[dict[str, Any], ...]:
    """从固定Skill来源生成带稳定ID的有界原则候选，避免模型重抄原文漂移。"""

    candidates = _guidance_principle_candidates_for_sources(
        _validated_guidance_sources(raw_sources)
    )
    return tuple(candidates[key] for key in sorted(candidates))


def _guidance_principle_candidates_for_sources(
    sources: Mapping[tuple[str, str], Mapping[str, str]],
) -> dict[str, dict[str, Any]]:
    """按来源和精确短语派生候选身份，并保留方法章节与先后位置。"""

    candidates: dict[str, dict[str, Any]] = {}
    for (source_kind, source_ref), source in sorted(sources.items()):
        seen: set[str] = set()
        heading_stack: list[str] = []
        source_order = 0
        for raw_line in source["content"].splitlines():
            normalized_line = _normalized_guidance_text(raw_line)
            heading = re.match(r"^(#{1,6})\s+(.+)$", normalized_line)
            if heading:
                level = len(heading.group(1))
                heading_text = _normalized_guidance_text(heading.group(2))
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(heading_text)
                continue
            normalized_line = re.sub(
                r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*)",
                "",
                normalized_line,
            )
            phrases = (
                [normalized_line]
                if len(normalized_line) <= 240
                else re.split(r"(?<=[.!?。！？])\s+", normalized_line)
            )
            for phrase in phrases:
                phrase = _normalized_guidance_text(phrase)
                if not 8 <= len(phrase) <= 240 or phrase in seen:
                    continue
                seen.add(phrase)
                source_order += 1
                candidate_id = "guidcand_" + canonical_checksum(
                    {
                        "source_kind": source_kind,
                        "source_ref": source_ref,
                        "source_checksum": source["source_checksum"],
                        "principle": phrase,
                    }
                )[:24]
                candidates[candidate_id] = {
                    "principle_candidate_id": candidate_id,
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "source_checksum": source["source_checksum"],
                    "principle": phrase,
                    "section_path": " > ".join(heading_stack),
                    "source_order": source_order,
                }
    if not candidates:
        raise ValueError("Guidance 权威来源没有可选择的有界原则候选")
    return candidates


def _stable_step_key(index: int, step: DynamicPlanDraftStep) -> str:
    """从步骤位置与规范语义生成跨重试稳定、不可由模型指定的持久 key。"""

    digest = canonical_checksum(
        {
            "draft_id": step.draft_id,
            "title": step.title,
            "kind": step.kind,
            "depends_on": list(step.depends_on),
            "capability_refs": list(step.capability_refs),
            "guidance_skill_refs": list(step.guidance_skill_refs),
        }
    )[:10]
    return f"step_{index:02d}_{digest}"


class RuntimeActionProposal(PlanningContract):
    """保存服务端校验后的单步动作，不接受 tenant、agent、risk 或授权结论。"""

    action_kind: ActionKind
    arguments: dict[str, Any] = Field(default_factory=dict)
    capability_ref: str | None = Field(default=None, max_length=512)
    expected_output_schema: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_capability_reference(self) -> "RuntimeActionProposal":
        """只有实际查询或调用能力的提案可以携带能力引用。"""

        requires_capability = self.action_kind in {
            ActionKind.QUERY_KNOWLEDGE,
            ActionKind.CALL_TOOL,
        }
        if requires_capability != bool((self.capability_ref or "").strip()):
            raise ValueError("能力型动作必须且只能声明 capability_ref")
        forbidden = {"tenant_id", "agent_id", "risk_class", "authorized", "permission"}
        if _contains_forbidden_key(self.arguments, forbidden):
            raise ValueError("模型提案不得覆盖服务端身份、风险或授权")
        return self


class CompletedProviderProposal(PlanningContract):
    """表示 provider 已完整结束且结构解析成功的提案边界。"""

    response_id: str = Field(min_length=1, max_length=512)
    finish_reason: str = Field(pattern=r"^(stop|tool_calls)$")
    proposal: RuntimeActionProposal
    usage: dict[str, Any] = Field(default_factory=dict)


class Planner(Protocol):
    """约束 Formal SOP 与未来动态 Planner 都输出规范计划，而不直接写 Runtime。"""

    def create_plan(self) -> NormalizedPlan:
        """生成完整且可验证的规范计划。"""


class FormalSopPlanner:
    """把冻结 SOP 定义机械投影为规划只读视图，不创建动态 PlanRevision。"""

    def __init__(self, definition: CompiledSopDefinition) -> None:
        """绑定已经发布并校验 checksum 的不可变 SOP 定义。"""

        self.definition = definition

    def create_plan(self) -> NormalizedPlan:
        """按定义节点顺序生成稳定 step key，保持 SOP Runtime 的确定性路由权威。"""

        dependencies: dict[str, list[str]] = {}
        for edge in self.definition.edges:
            dependencies.setdefault(edge.target_node_id, []).append(edge.source_node_id)
        steps = tuple(
            PlanStep(
                step_key=node.node_id,
                title=node.name,
                kind=f"sop.{node.type.value}",
                required=not node.optional,
                depends_on=tuple(dependencies.get(node.node_id, ())),
            )
            for node in self.definition.nodes
        )
        return NormalizedPlan(
            goal=f"执行已发布 SOP：{self.definition.name}",
            success_criteria=(
                SuccessCriterion(
                    id="sop_terminal",
                    type="assertion",
                    spec={"terminal_node_ids": list(self.definition.terminal_node_ids)},
                ),
            ),
            steps=steps,
            budget={"definition_checksum": self.definition.checksum},
        )


def canonical_checksum(value: BaseModel | dict[str, Any]) -> str:
    """对严格 JSON 值生成跨进程稳定 SHA-256，并拒绝 NaN/Infinity。"""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contains_forbidden_key(value: object, forbidden: set[str]) -> bool:
    """递归发现嵌套参数中试图覆盖服务端身份、风险或授权的键。"""

    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or _contains_forbidden_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item, forbidden) for item in value)
    return False
