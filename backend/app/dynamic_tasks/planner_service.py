"""
@Time       : 2026/08/04 02:24
@Author     : zhanglp8181
@File       : planner_service.py
@CallChain  : DynamicTaskAgent → DynamicTaskPlanner → LLMClient → NormalizedPlan
@Description: 向模型投影受控能力视图，并把完整计划草案收紧为服务端有界计划。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot
from app.dynamic_tasks.planning import (
    DynamicPlanDraft,
    NormalizedPlan,
    SuccessCriterion,
    _guidance_phase_number,
    _is_guidance_phase_gate,
    guidance_principle_candidates,
    normalize_plan_draft,
)
from app.observability.spans import llm_operation


class JsonPlanningClient(Protocol):
    """约束动态规划只使用完整 JSON object 响应。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回完整且可解析的 JSON object，不暴露流式半包。"""


class DynamicTaskPlannerError(ValueError):
    """表示计划在有界修复后仍无法满足服务端结构或语义契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可跨 Agent Loop 传播的稳定错误码和截断诊断。"""

        super().__init__(message[:1000])
        self.code = code


class DynamicGuidanceSelection(BaseModel):
    """保存模型从无正文目录提出、再由服务端校验的动态指导选择。"""

    selected_skill_names: tuple[str, ...] = Field(default=(), max_length=3)
    reason: str = Field(min_length=1, max_length=1000)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_unique_names(self) -> "DynamicGuidanceSelection":
        """拒绝重复或空白名称，避免同一固定修订重复消耗预算。"""

        if any(not value.strip() for value in self.selected_skill_names):
            raise ValueError("动态指导 Skill 名称不能为空")
        if len(set(self.selected_skill_names)) != len(self.selected_skill_names):
            raise ValueError("动态指导 Skill 不得重复")
        return self


class DynamicTaskPlanner:
    """把受控目标、成功标准和能力模型视图转换为有界规范计划。"""

    def __init__(
        self,
        client: JsonPlanningClient,
        *,
        max_steps: int = 10,
        max_tool_calls: int = 9,
        max_model_calls: int = 12,
        max_input_tokens: int = 120_000,
        max_output_tokens: int = 24_000,
        max_total_tokens: int = 144_000,
        max_runtime_seconds: int = 900,
        explore_enabled: bool = False,
    ) -> None:
        """冻结服务端预算；任何 provider 输出都不能扩大这些上限。"""

        self.client = client
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_model_calls = max_model_calls
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_total_tokens = max_total_tokens
        self.max_runtime_seconds = max_runtime_seconds
        self.explore_enabled = explore_enabled

    def select_guidance_skills(
        self,
        *,
        goal: str,
        success_criteria: Sequence[SuccessCriterion],
        catalog: Sequence[CapabilitySnapshot],
    ) -> DynamicGuidanceSelection:
        """仅用无正文目录选择 model-allowed 指导 Skill，并拒绝模型虚构引用。"""

        eligible = [
            item
            for item in catalog
            if item.capability_type == "general_skill"
            and item.contract.get("invocation_policy") == "model_allowed"
        ]
        if not eligible:
            return DynamicGuidanceSelection(
                selected_skill_names=(),
                reason="没有允许模型自动选择的指导 Skill。",
            )
        catalog_rows = [
            {
                key: item.model_view[key]
                for key in (
                    "id",
                    "slug",
                    "name",
                    "description",
                    "usage_mode",
                    "revision_id",
                    "revision_number",
                )
                if key in item.model_view
            }
            for item in eligible
        ]
        with llm_operation("general_skill.select"):
            raw = self.client.generate_json(
                _GUIDANCE_SELECTOR_SYSTEM_PROMPT,
                {
                "goal": goal,
                "success_criteria": [item.model_dump(mode="json") for item in success_criteria],
                "skill_catalog": catalog_rows,
                "output_contract": {
                    "selected_skill_names": ["skill_catalog 中的 slug，最多 3 个"],
                    "reason": "选择原因；不选择时也必须说明",
                },
                },
            )
        selection = DynamicGuidanceSelection.model_validate(raw)
        allowed_names = {item.name for item in eligible}
        if not set(selection.selected_skill_names) <= allowed_names:
            raise ValueError("动态指导选择引用了目录外或 user-only Skill")
        return selection

    def create_plan(
        self,
        *,
        goal: str,
        success_criteria: Sequence[SuccessCriterion],
        capabilities: Sequence[CapabilitySnapshot],
        input_resources: Sequence[dict[str, object]] = (),
        loaded_guidance: Sequence[dict[str, object]] = (),
        memory_context: Sequence[dict[str, object]] = (),
    ) -> NormalizedPlan:
        """生成完整草案并覆盖目标/成功标准，防止模型改写用户任务契约。"""

        executable_capabilities = [
            snapshot
            for snapshot in capabilities
            if snapshot.contract.get("risk_class")
            in {"read", "local_write", "execute", "external_write"}
            and snapshot.capability_type in {"tool", "connector", "knowledge"}
            and _goal_authorizes_capability(goal, snapshot)
        ]
        allowed_step_kinds = ["answer", "clarification"]
        if any(
            snapshot.capability_type in {"tool", "connector"}
            and snapshot.contract.get("risk_class") == "read"
            for snapshot in executable_capabilities
        ):
            allowed_step_kinds.insert(0, "tool.read")
        if any(snapshot.capability_type == "knowledge" for snapshot in executable_capabilities):
            allowed_step_kinds.insert(1, "knowledge")
        if self.explore_enabled and any(
            snapshot.capability_type == "tool"
            and snapshot.contract.get("explore_safe") is True
            for snapshot in executable_capabilities
        ):
            allowed_step_kinds.insert(-1, "explore")
        if any(
            snapshot.contract.get("risk_class") == "external_write"
            for snapshot in executable_capabilities
        ):
            allowed_step_kinds.insert(-2, "tool.write")
        if any(
            snapshot.contract.get("risk_class") == "local_write"
            for snapshot in executable_capabilities
        ) and "tool.write" not in allowed_step_kinds:
            allowed_step_kinds.insert(-2, "tool.write")
        if any(
            snapshot.contract.get("risk_class") == "execute"
            for snapshot in executable_capabilities
        ):
            allowed_step_kinds.insert(-2, "tool.execute")
        payload = {
            "goal": goal,
            "success_criteria": [item.model_dump(mode="json") for item in success_criteria],
            "output_contract": _planner_output_contract(allowed_step_kinds),
            "capabilities": [
                _planner_capability_view(snapshot)
                for snapshot in executable_capabilities
            ],
            "loaded_guidance": _planner_guidance_catalog(loaded_guidance),
            "memory_context": [dict(item) for item in memory_context],
            "input_resources": [dict(item) for item in input_resources],
            "input_resource_contract": (
                "input_resources 由平台在每个模型步骤前通过受管 input.read Operation 自动读取；"
                "公式、表格及其他确定性复核由平台在 answer 前自动调用内建 input.* / "
                "table.compute；不得为附件新增 tool.read 步骤、capability_ref，"
                "也不得询问用户提供、授权或确认这些平台内建能力。"
            ),
            "limits": {
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "max_tool_calls_semantics": (
                    "tool.read、tool.write、tool.execute、knowledge 与 explore 内部实际能力调用总和"
                ),
                "max_model_calls": self.max_model_calls,
                "max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens,
                "max_total_tokens": self.max_total_tokens,
                "max_runtime_seconds": self.max_runtime_seconds,
                "allowed_step_kinds": allowed_step_kinds,
            },
        }
        guidance_use_ids_by_name = {
            str(item.get("name") or ""): tuple(
                str(use_id)
                for use_id in item.get("skill_use_ids", ())
                if str(use_id).strip()
            )
            for item in loaded_guidance
            if str(item.get("name") or "").strip()
        }
        (
            guidance_sources_by_name,
            guidance_selection_modes_by_name,
        ) = _guidance_source_contract(loaded_guidance)
        payload["guidance_principle_candidates"] = _planner_guidance_candidate_catalog(
            guidance_sources_by_name
        )
        repair_payload = dict(payload)
        last_error: ValueError | ValidationError | None = None
        for attempt in range(3):
            with llm_operation("dynamic_task.plan"):
                raw = _normalize_model_display_fields(
                    self.client.generate_json(_PLANNER_SYSTEM_PROMPT, repair_payload)
                )
            loaded_skill_contract = [
                {"skill_ref": name}
                for name in guidance_use_ids_by_name
                if str(name).strip()
            ]
            guidance_requirements_incomplete = (
                bool(guidance_use_ids_by_name)
                and not _repair_covers_loaded_skills(
                    raw.get("guidance_requirements"),
                    loaded_skill_contract,
                )
            )
            if (
                attempt == 1
                and guidance_use_ids_by_name
                and (
                    guidance_requirements_incomplete
                    or (
                        last_error is not None
                        and "每个已加载 Skill 必须声明" in str(last_error)
                    )
                )
            ):
                # DeepSeek 等 provider 偶尔会在完整计划重试时仍遗漏整个
                # guidance_requirements 数组。这里增加一次只修复 Guidance 的
                # 受限回路：不替模型决定步骤、能力或权限，只把候选 ID、任务映射
                # 和可观察验收补回原草案，随后仍由 normalize_plan_draft 全量校验。
                raw = _repair_missing_guidance_requirements(
                    self.client,
                    raw,
                    goal=goal,
                    success_criteria=success_criteria,
                    loaded_guidance=loaded_guidance,
                    candidate_catalog=payload["guidance_principle_candidates"],
                    failure_message=str(last_error),
                )
                # 修复器本身也可能因 provider 超时或返回半截 JSON 而交回原草案。
                # 在进入身份修复前再做一次宿主后置检查，确保“已加载 Skill 但
                # requirements 为空”不会绕过权威候选兜底；如果候选目录确实为空，
                # 保持原样并由 normalize_plan_draft fail-closed。
                if not _repair_covers_loaded_skills(
                    raw.get("guidance_requirements"),
                    loaded_skill_contract,
                ):
                    fallback = _deterministic_guidance_requirement_fallback(
                        loaded_skill_contract,
                        _guidance_candidate_options(payload["guidance_principle_candidates"]),
                        goal=goal,
                    )
                    if fallback is not None:
                        raw = dict(raw)
                        raw["guidance_requirements"] = fallback
            raw = _repair_guidance_identity_fields(
                self.client,
                raw,
                candidate_catalog=payload["guidance_principle_candidates"],
            )
            if (
                attempt == 1
                and guidance_use_ids_by_name
                and not _repair_covers_loaded_skills(
                    raw.get("guidance_requirements"),
                    loaded_skill_contract,
                )
            ):
                # 某些 provider 在第二次完整计划或身份修复后仍会把整个字段丢掉。
                # 这里再做一次纯宿主兜底：只从同一冻结候选目录为每个已加载 Skill
                # 绑定一条 apply 要求，不改写步骤、能力、权限或预算；候选为空时
                # 继续 fail-closed，不能把缺失要求当作无 Skill 计划。
                fallback = _deterministic_guidance_requirement_fallback(
                    loaded_skill_contract,
                    _guidance_candidate_options(payload["guidance_principle_candidates"]),
                    goal=goal,
                )
                if fallback is not None:
                    raw = dict(raw)
                    raw["guidance_requirements"] = fallback
            # 与 OpenWorker/Hermes 的目录外调用边界一致：无 Skill 的计划不能消费
            # 模型自造的指导名称，但一次 repair 仍无法纠正时也不应让普通任务整轮失败。
            # 这里只清除 guidance 字段，不放宽 capability、tool 或权限校验；任何已加载
            # Skill 的未知引用仍继续走 fail-closed 语义。
            if (
                attempt == 1
                and not guidance_use_ids_by_name
                and last_error is not None
                and "未加载的指导 Skill" in str(last_error)
            ):
                raw = _strip_unloaded_guidance_fields(raw)
            if (
                attempt == 1
                and last_error is not None
                and (
                    "缺少当前无运行回路的前置否决门" in str(last_error)
                    or "缺少连续前置阶段" in str(last_error)
                )
            ):
                raw = _repair_guidance_phase_continuity(
                    raw,
                    candidate_catalog=payload["guidance_principle_candidates"],
                )
            if attempt == 1 and guidance_use_ids_by_name:
                # 某些 provider 在第二轮会同时修正 requirement 身份，导致上一轮的
                # phase 错误不再保留在 last_error；仍需在 normalize 前做一次纯候选
                # 连续性收敛。无缺口时返回原草案，有缺口时只补权威前置阶段/否决门，
                # 不改变步骤、能力、权限或预算。
                raw = _repair_guidance_phase_continuity(
                    raw,
                    candidate_catalog=payload["guidance_principle_candidates"],
                )
            if attempt >= 1:
                # 最后一轮 repair 仍可能把报告章节拆成多个 answer。这个形状错误不应
                # 让真实附件任务随机失败；只合并终态，不放宽能力、依赖或 Skill 来源校验。
                raw = _repair_terminal_answer_steps(raw)
            try:
                draft = DynamicPlanDraft.model_validate(raw).model_copy(
                    update={
                        "goal": goal,
                        "success_criteria": tuple(success_criteria),
                    }
                )
                plan = normalize_plan_draft(
                    draft,
                    max_steps=self.max_steps,
                    max_tool_calls=self.max_tool_calls,
                    max_model_calls=self.max_model_calls,
                    max_input_tokens=self.max_input_tokens,
                    max_output_tokens=self.max_output_tokens,
                    max_total_tokens=self.max_total_tokens,
                    max_runtime_seconds=self.max_runtime_seconds,
                    guidance_use_ids_by_name=guidance_use_ids_by_name,
                    guidance_sources_by_name=guidance_sources_by_name,
                    guidance_selection_modes_by_name=guidance_selection_modes_by_name,
                )
                plan = _strip_platform_owned_attachment_steps(
                    plan,
                    has_input_resources=bool(input_resources),
                )
                referenced_guidance = {
                    reference for step in draft.steps for reference in step.guidance_skill_refs
                }
                if referenced_guidance != set(guidance_use_ids_by_name):
                    raise ValueError("已加载指导 Skill 必须且只能由计划步骤显式引用")
                _validate_plan_capabilities(plan, executable_capabilities)
                _validate_clarification_semantics(plan)
                _validate_guidance_step_alignment(
                    plan,
                    input_resource_names={
                        str(item.get("filename") or "").strip().casefold()
                        for item in input_resources
                        if str(item.get("filename") or "").strip()
                    },
                )
                _validate_attachment_read_separation(
                    plan,
                    has_input_resources=bool(input_resources),
                )
                _validate_plan_convergence(plan)
                return plan
            except (ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == 1 and _is_planner_contract_echo(raw):
                    # provider 偶尔会把宿主的 output_contract 原样回显为结果对象，
                    # 既没有 steps 又带有 schema 之外的顶层字段。仅对此确定形状
                    # 再给一次“只返回草案”的有界修复；不把缺失步骤猜成能力调用，
                    # 最终仍由 DynamicPlanDraft、能力、依赖和 Guidance 校验收紧。
                    repair_payload = {
                        **payload,
                        "repair": {
                            "attempt": 2,
                            "failure_code": "DYNAMIC_PLAN_SEMANTIC_INVALID",
                            "failure_message": str(exc)[:1000],
                            "instruction": (
                                "上一轮把输入的 output_contract/schema 回显成了结果，禁止再次回显。"
                                "只返回一个完整 DynamicPlanDraft JSON：顶层必须有 goal、success_criteria、"
                                "steps；steps 必须是实际计划数组，至少包含一个唯一最终 answer，"
                                "不得返回 output_contract、limits、capabilities、input_resources 或其他宿主投影字段。"
                                "不要新增未经列出的能力、权限、工具或澄清；若任务只需分析/文档，使用一个 answer。"
                                "loaded_guidance 非空时仍须按候选填写 guidance_requirements，并在 answer 的"
                                "guidance_skill_refs 逐字引用已加载 Skill 名称；服务端会继续校验所有身份、依赖、预算和来源。"
                            ),
                        },
                    }
                    continue
                if attempt >= 1:
                    break
                repair_payload = {
                    **payload,
                    "repair": {
                        "attempt": 1,
                        "failure_code": "DYNAMIC_PLAN_SEMANTIC_INVALID",
                        "failure_message": str(exc)[:1000],
                        "instruction": (
                            "重新生成完整计划；只能按 capabilities[].allowed_step_kind 使用能力，"
                            "每个能力步骤必须且只能逐字复制一个 capabilities[].name 到"
                            " capability_refs，禁止使用显示名、handler、别名或自行缩写；"
                            "非能力步骤的 capability_refs 必须为空，并保持唯一最终 answer。"
                            "附件由平台自动读取，禁止为 input_resources 新增 tool.read。"
                            "撰写文档、规范、报告属于 answer，不代表调用 tool.write；"
                            "任务不需要外部事实或副作用时应只规划 answer。"
                            "若 loaded_guidance 非空，guidance_requirements 必须按 output_contract"
                            " 重新完整生成：每条都必须从 guidance_principle_candidates 复制"
                            " principle_candidate_id 或对应的 16 位 principle_candidate_id_short（不可省略、不可为 null、不可同时填写）、"
                            "source_kind 和 source_ref，禁止重抄或"
                            "改写 principle；每个 Skill"
                            " 只能给 1..3 条 apply，或在 forced 时给唯一 not_applicable；"
                            "必须根据 section_path/source_order 保持 Phase 先后与前置门禁，"
                            "用户未授权执行时不得跳过前置条件进入后续阶段；"
                            "若 task_mapping 或 observable_acceptance 指定要读取某个 Skill 资源文件，"
                            "必须在 steps 中加入 tool.read/knowledge 前置步骤，标题逐字包含该文件名，"
                            "并让最终 answer 依赖该步骤；这不是附件 input.read，也不需要向用户索取授权。"
                            "每个 auto 选择的 Skill 都必须至少有一条 apply。步骤只能引用已加载"
                            " Skill 的 name，不得自造 requirement_id 或 skill_use_id。"
                            "若 loaded_guidance 为空，本轮没有任何可用指导 Skill；"
                            "guidance_requirements、guidance_skill_refs 必须全部为空，"
                            "不得自行创造 Skill 名称、SkillUse 或原则。"
                            "如果 guidance_principle_candidates 中的 ID 在复制时不完整，"
                            "不要猜测或修补该 ID；改用对应来源中的 principle 原文，"
                            "将 principle_candidate_id 设为 null，并保持 source_kind/source_ref"
                            "逐字正确。服务端仍会按固定来源校验，禁止自行改写原则。"
                        ),
                    },
                }
        assert last_error is not None
        raise DynamicTaskPlannerError(
            "DYNAMIC_PLAN_SEMANTIC_INVALID",
            str(last_error),
        ) from last_error


_PLANNER_SYSTEM_PROMPT = """你是共格·序伴的受控动态任务规划器。只输出一个完整 JSON object。
你只能使用输入中列出的能力；local_write/external_write 只能规划为 tool.write，execute 只能规划为
tool.execute，运行时会冻结参数并等待一次性人工批准。
不得提出执行、删除、权限变更或输入中不存在的能力。步骤种类必须来自 limits.allowed_step_kinds。
每项能力只能用于它声明的 allowed_step_kind；“写文档/写方案”是 answer，不是 tool.write。
每个能力步骤必须且只能在 capability_refs 中逐字复制一个 capabilities[].name；禁止使用显示名、
handler、别名、自然语言名称或自行缩写。无能力步骤的 capability_refs 必须为空。
input_resources 是平台自动读取的受管输入，不是 capabilities；公式、表格及其他确定性复核也由平台
在 answer 前自动执行内建 input.* / table.compute。禁止为附件虚构 tool.read 或 capability_ref，
禁止以缺少这些内建能力为由生成 clarification；clarification 只能询问真实缺失的业务信息。
如果 goal 已经给出事实、约束和输出要求，不得为“确认交付要求/输出格式”增加 clarification；应直接规划 answer。
澄清 title 必须明确请求/等待用户提供、确认、选择或授权；不得把读取、分析、设计或撰写等
Agent 内部工作伪装成 clarification。
draft_id 只用于本次草案依赖，持久 step key 由服务端生成。
必须严格按 output_contract 输出顶层字段，禁止增加 plan/draft/result 等包装层或使用 id 替代 draft_id。
不得输出 tenant、agent、授权结论、凭据、URL、header、预算覆盖或未提供的能力。
计划必须有界、无环并覆盖成功标准；必须且只能有一个最终 answer 步骤，所有 required 步骤都必须是该
answer 的直接或间接前置，answer 之后不得再有步骤。
loaded_guidance 非空时，每个已加载 Skill 必须在 guidance_requirements 中声明 1..3 条实际应用；
每条 requirement 必须实际填写 principle_candidate_id 或 principle_candidate_id_short（不可省略、不可为 null、不可同时填写），
并从 guidance_principle_candidates 复制稳定身份、source_kind 和 source_ref，
不得自行重抄或改写原则；task_mapping 和 observable_acceptance 必须描述本任务中的映射与可观察验收。
候选的 section_path 与 source_order 是固定 Skill 原文的方法结构：若来源以 Phase/阶段组织，
必须保持其先后与前置门禁，不得只选后续阶段而跳过“先/必须/不得/直到”类前置条件。
在没有 Phase 前置门禁时，1..3 条要求应尽量覆盖互补的工作面，而不是重复同一抽象定义：
优先选择能分别约束职责/接口、真实变化接缝与适配器、验证/测试面、替换/回退或方案比较的操作性原则；
不要把术语定义、宣传句或宽泛价值判断当作唯一要求。task_mapping 必须说明该原则对应本任务的哪个
具体决策或验收问题；若候选不足以覆盖某个工作面，不得自行补写原则，应诚实保留未覆盖项。
当用户不授权执行前置操作时，应选择前置门禁并说明所需证据/授权，而不是跳到后续结论。
只有 forced Skill 确实不适用时，才能为该 Skill 返回唯一
not_applicable 声明；每个 auto Skill 都必须至少有一条 apply。
计划是控制面契约，不是最终回答正文：默认 constraints、assumptions 和
expected_artifacts 都返回空数组，只有输入明确要求且确实必要时才填写；不要从材料、附件或
常见工程经验臆造假设、文件或交付物。无能力、无附件、无 loaded_guidance 的普通分析/写作任务
必须只返回一个 answer 步骤。每个 title 保持简短，steps 尽量少于 4 个，禁止复制附件全文或
在计划中展开最终文档内容；优先输出短而完整、可校验的结构，避免因冗长控制面响应被截断。"""

_GUIDANCE_IDENTITY_REPAIR_SYSTEM_PROMPT = """你是共格·序伴的 Guidance 身份修复器。只输出一个 JSON object。
输入包含模型刚刚生成、但缺少 principle_candidate_id 的 GuidanceRequirement，以及服务端投影的
候选选项。对每个缺失项，从同一 skill_ref/source_kind/source_ref 的候选中选择一个最贴合
task_mapping 与 observable_acceptance 的候选。只返回候选的整数 candidate_index，宿主负责映射
到权威 ID；不得创造、改写或猜测任何 ID。
输出格式必须是：{"identities":[{"index":0,"candidate_index":12}]}。
每个 index 只能出现一次；无法安全选择时不要输出该 index，宿主会拒绝计划。"""

_GUIDANCE_REQUIREMENT_REPAIR_SYSTEM_PROMPT = """你是共格·序伴的 Guidance 要求修复器。只输出一个 JSON object。
宿主已经生成了完整动态计划，但它遗漏了已加载 Skill 的 GuidanceRequirement。你只能修复
guidance_requirements 字段，不能修改 goal、success_criteria、steps、能力、权限或预算。
每个 loaded_skill_contract 中的 Skill 必须声明 1..3 条 apply；只有 selection_mode=forced
且确实不适用时，才能声明唯一 not_applicable。每条 apply 必须从 candidate_options 逐字复制
同一候选的 principle_candidate_id（或 16 位短 ID）、skill_ref、source_kind、source_ref，
并填写本任务具体的 task_mapping 与可观察 observable_acceptance；禁止自造原则、ID、SkillUse
或 source。若来源按 Phase/阶段组织，候选必须保持前置阶段和否决门连续。"""


def _guidance_candidate_options(candidate_catalog: object) -> list[dict[str, object]]:
    """把宿主投影的候选目录压平成只读选项，供身份/要求修复共同使用。"""

    candidate_options: list[dict[str, object]] = []
    if not isinstance(candidate_catalog, list):
        return candidate_options
    for source in candidate_catalog:
        if not isinstance(source, dict):
            continue
        skill_ref = str(source.get("skill_ref") or "")
        for source_block in source.get("sources", ()):
            if not isinstance(source_block, dict):
                continue
            source_kind = str(source_block.get("source_kind") or "")
            source_ref = str(source_block.get("source_ref") or "")
            for section in source_block.get("sections", ()):
                if not isinstance(section, dict):
                    continue
                section_path = str(section.get("section_path") or "")
                for candidate in section.get("candidates", ()):
                    if not isinstance(candidate, dict):
                        continue
                    candidate_options.append(
                        {
                            "candidate_index": len(candidate_options),
                            "skill_ref": skill_ref,
                            "source_kind": source_kind,
                            "source_ref": source_ref,
                            "principle_candidate_id": str(
                                candidate.get("principle_candidate_id") or ""
                            ),
                            "principle_candidate_id_short": str(
                                candidate.get("principle_candidate_id_short") or ""
                            ),
                            "section_path": section_path,
                            "source_order": candidate.get("source_order"),
                            "principle": str(candidate.get("principle") or ""),
                        }
                    )
    return candidate_options


def _repair_missing_guidance_requirements(
    client: JsonPlanningClient,
    raw: dict,
    *,
    goal: str,
    success_criteria: Sequence[SuccessCriterion],
    loaded_guidance: Sequence[dict[str, object]],
    candidate_catalog: object,
    failure_message: str,
) -> dict:
    """只修复完整计划遗漏的 GuidanceRequirement，并把其他计划字段原样保留。"""

    candidate_options = _guidance_candidate_options(candidate_catalog)
    loaded_skill_contract: list[dict[str, object]] = []
    for loaded in loaded_guidance:
        name = str(loaded.get("name") or "").strip()
        if not name:
            continue
        raw_blocks = loaded.get("skills")
        block = raw_blocks[0] if isinstance(raw_blocks, list) and len(raw_blocks) == 1 else {}
        loaded_skill_contract.append(
            {
                "skill_ref": name,
                "selection_mode": str(
                    loaded.get("selection_mode")
                    or (block.get("selection_mode") if isinstance(block, Mapping) else "")
                    or "forced"
                ),
                "skill_use_ids": [
                    str(value)
                    for value in loaded.get("skill_use_ids", ())
                    if str(value).strip()
                ],
            }
        )
    repair_payload = {
        "goal": goal,
        "success_criteria": [item.model_dump(mode="json") for item in success_criteria],
        "loaded_skill_contract": loaded_skill_contract,
        "candidate_options": candidate_options,
        "current_guidance_requirements": raw.get("guidance_requirements", []),
        "failure_message": failure_message[:1000],
        "output_contract": {
            "guidance_requirements": [
                {
                    "skill_ref": "loaded_skill_contract 中的 skill_ref",
                    "source_kind": "candidate_options 中的 source_kind",
                    "source_ref": "candidate_options 中的 source_ref",
                    "principle_candidate_id": (
                        "candidate_options 中逐字复制的完整 ID 或对应短 ID"
                    ),
                    "task_mapping": "本任务的具体映射",
                    "observable_acceptance": "可观察验收",
                    "disposition": "apply | not_applicable",
                }
            ]
        },
    }
    for repair_attempt in range(2):
        with llm_operation("general_skill.requirement_repair"):
            repaired = client.generate_json(
                _GUIDANCE_REQUIREMENT_REPAIR_SYSTEM_PROMPT,
                repair_payload,
            )
        requirements = repaired.get("guidance_requirements") if isinstance(repaired, dict) else None
        if _repair_covers_loaded_skills(requirements, loaded_skill_contract):
            repaired_plan = dict(raw)
            repaired_plan["guidance_requirements"] = requirements
            return repaired_plan
        if repair_attempt == 0:
            repair_payload = {
                **repair_payload,
                "failure_message": (
                    "上一次 Guidance 专用修复仍未为每个 loaded_skill_contract 提供 1..3 条要求；"
                    "本次必须返回完整 guidance_requirements 数组。"
                ),
                "previous_repair_requirements": requirements,
            }
    fallback = _deterministic_guidance_requirement_fallback(
        loaded_skill_contract,
        candidate_options,
        goal=goal,
    )
    if fallback is not None:
        repaired_plan = dict(raw)
        repaired_plan["guidance_requirements"] = fallback
        return repaired_plan
    return raw


def _repair_covers_loaded_skills(
    requirements: object,
    loaded_skill_contract: list[dict[str, object]],
) -> bool:
    """确认受限修复至少为每个已加载 Skill 返回1至3条要求，避免空修复误放行。"""

    if not isinstance(requirements, list):
        return False
    loaded_names = {
        str(item.get("skill_ref") or "")
        for item in loaded_skill_contract
        if str(item.get("skill_ref") or "").strip()
    }
    counts: dict[str, int] = {}
    for item in requirements:
        if not isinstance(item, dict):
            return False
        skill_ref = str(item.get("skill_ref") or "")
        counts[skill_ref] = counts.get(skill_ref, 0) + 1
    return set(counts) == loaded_names and all(1 <= count <= 3 for count in counts.values())


def _deterministic_guidance_requirement_fallback(
    loaded_skill_contract: list[dict[str, object]],
    candidate_options: list[dict[str, object]],
    *,
    goal: str,
) -> list[dict[str, object]] | None:
    """在两次模型修复仍为空时按权威候选首项补回每个Skill的一条apply要求。"""

    fallback: list[dict[str, object]] = []
    for contract in loaded_skill_contract:
        skill_ref = str(contract.get("skill_ref") or "").strip()
        choices = [
            item
            for item in candidate_options
            if str(item.get("skill_ref") or "").strip() == skill_ref
            and str(item.get("principle_candidate_id") or "").strip()
        ]
        if not skill_ref or not choices:
            return None
        candidate = choices[0]
        fallback.append(
            {
                "skill_ref": skill_ref,
                "source_kind": candidate["source_kind"],
                "source_ref": candidate["source_ref"],
                "principle_candidate_id": candidate["principle_candidate_id"],
                "task_mapping": f"将该权威Guidance原则映射到本任务：{goal[:240]}",
                "observable_acceptance": "最终交付物逐项体现该原则，并可由结果验证器检查",
                "disposition": "apply",
            }
        )
    return fallback

_GUIDANCE_SELECTOR_SYSTEM_PROMPT = """你是共格·序伴的动态任务指导选择器。只输出一个 JSON object。
你只能按 goal、success_criteria 和无正文 skill_catalog 判断是否需要指导；不得虚构名称、请求正文、工具或权限。
只选择能实质改变任务执行纪律的 Skill，最多 3 个；没有必要时返回空数组。"""

_PLANNER_OUTPUT_CONTRACT = {
    "goal": "原样返回输入 goal",
    "success_criteria": "原样返回输入 success_criteria 数组",
    "constraints": ["string，可为空数组"],
    "assumptions": ["string，可为空数组"],
    "expected_artifacts": [
        {
            "artifact_key": (
                "建议返回字母开头的 ASCII 稳定标识；"
                "非法或重复值由服务端按位置生成，不得在步骤中引用"
            ),
            "filename": "无路径分隔符的下载文件名",
            "mime_type": "text/markdown | text/plain | DOCX MIME | XLSX MIME",
            "content_source": "result.markdown",
            "required": True,
        }
    ],
    "guidance_requirements": [
        {
            "skill_ref": "输入 loaded_guidance 中的 name",
            "source_kind": "instructions | reviewed_resource",
            "source_ref": "instructions 或 reviewed_resources[].path",
            "principle_candidate_id": (
                "逐字复制 guidance_principle_candidates[].sources[].sections[]"
                ".candidates[].principle_candidate_id 或同一候选的 principle_candidate_id_short"
            ),
            "principle_candidate_id_short": (
                "可选：逐字复制同一候选的 16 位十六进制 principle_candidate_id_short；"
                "与 principle_candidate_id、principle 三者只能填一个"
            ),
            "task_mapping": "该原则在当前任务中的具体映射",
            "observable_acceptance": "可从计划交付物观察的验收条件",
            "disposition": "apply | not_applicable",
        }
    ],
    "steps": [
        {
            "draft_id": "字母开头的本次草案步骤标识",
            "title": "简短展示标题，最多 256 个字符；不得把步骤说明或 Skill 正文塞入标题",
            "kind": (
                "tool.read | tool.write | tool.execute | knowledge | explore | answer | "
                "clarification"
            ),
            "required": True,
            "depends_on": ["已声明 draft_id"],
            "capability_refs": ["能力步骤逐字复制且只复制一个 capabilities[].name；否则空数组"],
            "guidance_skill_refs": ["输入 loaded_guidance 中的 name"],
            "expected_output_schema": {},
        }
    ],
}


def _planner_output_contract(allowed_step_kinds: list[str]) -> dict[str, object]:
    """把当前实际可执行步骤种类写入模型输出契约，避免无知识能力时仍规划检索。"""

    contract = dict(_PLANNER_OUTPUT_CONTRACT)
    steps = [dict(_PLANNER_OUTPUT_CONTRACT["steps"][0])]
    steps[0]["kind"] = " | ".join(allowed_step_kinds)
    contract["steps"] = steps
    return contract


def _repair_guidance_identity_fields(
    client: JsonPlanningClient,
    raw: dict,
    *,
    candidate_catalog: object,
) -> dict:
    """为缺失候选身份执行一次最小、只读的候选选择，不替模型改写原则。"""

    raw_requirements = raw.get("guidance_requirements")
    if not isinstance(raw_requirements, list):
        return raw
    candidate_options: list[dict[str, object]] = []
    if isinstance(candidate_catalog, list):
        for source in candidate_catalog:
            if not isinstance(source, dict):
                continue
            skill_ref = str(source.get("skill_ref") or "")
            for source_block in source.get("sources", ()):
                if not isinstance(source_block, dict):
                    continue
                source_kind = str(source_block.get("source_kind") or "")
                source_ref = str(source_block.get("source_ref") or "")
                for section in source_block.get("sections", ()):
                    if not isinstance(section, dict):
                        continue
                    for candidate in section.get("candidates", ()):
                        if not isinstance(candidate, dict):
                            continue
                        candidate_options.append(
                            {
                                "candidate_index": len(candidate_options),
                                "skill_ref": skill_ref,
                                "source_kind": source_kind,
                                "source_ref": source_ref,
                                "principle_candidate_id": str(
                                    candidate.get("principle_candidate_id") or ""
                                ),
                                "principle": str(candidate.get("principle") or ""),
                            }
                        )

    def _candidate_for_reference(reference: str) -> dict[str, object] | None:
        """按完整ID或唯一截断前缀返回权威候选，避免把模型幻造ID直接交给规范化器。"""

        normalized = reference.removeprefix("guidcand_").casefold()
        if not re.fullmatch(r"[a-f0-9]{12,64}", normalized):
            return None
        matches = [
            item
            for item in candidate_options
            if str(item.get("principle_candidate_id") or "")
            .removeprefix("guidcand_")
            .casefold()
            .startswith(normalized)
        ]
        return matches[0] if len(matches) == 1 else None

    # DeepSeek 等模型有时会正确复制候选 ID，却漏掉同一候选的
    # skill_ref/source_kind/source_ref。不能让 Pydantic 在一次受限 repair
    # 之前直接拒绝这种响应；这些字段属于服务端权威来源身份，应由候选
    # 反查补齐，而不是接受模型自报值。若候选无法唯一反查，继续保持
    # fail-closed，后续 schema/来源校验会拒绝计划。
    hydrated_requirements: list[object] = []
    hydration_changed = False
    missing: list[dict[str, object]] = []
    for index, item in enumerate(raw_requirements):
        if not isinstance(item, dict):
            hydrated_requirements.append(item)
            continue
        reference = str(
            item.get("principle_candidate_id")
            or item.get("principle_candidate_id_short")
            or ""
        ).strip()
        principle = str(item.get("principle") or "").strip()
        selected = _candidate_for_reference(reference) if reference else None
        exact_matches = (
            [
                option
                for option in candidate_options
                if str(option.get("principle") or "").strip() == principle
            ]
            if principle
            else []
        )
        # 原文相同但来源不同不能凭第一条候选猜来源；交给受限 repair，仍无法唯一
        # 选择时由后续来源校验 fail-closed，保持 Skill 身份契约确定性。
        exact_principle = exact_matches[0] if len(exact_matches) == 1 else None
        authoritative = selected or exact_principle
        hydrated = dict(item)
        if authoritative is not None:
            for field in ("skill_ref", "source_kind", "source_ref"):
                if not str(hydrated.get(field) or "").strip():
                    hydrated[field] = authoritative.get(field)
                    hydration_changed = True
        hydrated_requirements.append(hydrated)
        valid = bool(selected or exact_principle)
        if reference and selected and principle and str(selected.get("principle") or "").strip() != principle:
            valid = False
        if valid:
            continue
        missing.append(
            {
                "index": index,
                "skill_ref": str(hydrated.get("skill_ref") or ""),
                "source_kind": str(hydrated.get("source_kind") or ""),
                "source_ref": str(hydrated.get("source_ref") or ""),
                "task_mapping": str(hydrated.get("task_mapping") or ""),
                "observable_acceptance": str(hydrated.get("observable_acceptance") or ""),
            }
        )
    if not missing:
        if not hydration_changed:
            return raw
        repaired = dict(raw)
        repaired["guidance_requirements"] = hydrated_requirements
        return repaired
    with llm_operation("general_skill.requirement_identity"):
        selection = client.generate_json(
            _GUIDANCE_IDENTITY_REPAIR_SYSTEM_PROMPT,
            {
                "requirements": missing,
                "candidate_options": candidate_options,
                "output_contract": {
                    "identities": [
                        {
                            "index": "输入 requirements 中的整数 index",
                            "candidate_index": "candidate_options 中的整数 candidate_index",
                        }
                    ]
                },
            },
        )
    identities = selection.get("identities") if isinstance(selection, dict) else None
    if not isinstance(identities, list):
        return raw
    repaired = dict(raw)
    repaired_requirements = [
        dict(item) if isinstance(item, dict) else item for item in hydrated_requirements
    ]
    for identity in identities:
        if not isinstance(identity, dict):
            continue
        index = identity.get("index")
        if not isinstance(index, int) or not 0 <= index < len(repaired_requirements):
            continue
        item = repaired_requirements[index]
        if not isinstance(item, dict):
            continue
        candidate_index = identity.get("candidate_index")
        reference = ""
        if isinstance(candidate_index, int) and 0 <= candidate_index < len(candidate_options):
            selected = candidate_options[candidate_index]
            reference = str(selected.get("principle_candidate_id") or "").strip()
        if not reference:
            reference = str(
                identity.get("principle_candidate_id")
                or identity.get("principle_candidate_id_short")
                or ""
            ).strip()
        if not reference:
            continue
        item["principle_candidate_id"] = reference
        item["principle_candidate_id_short"] = None
        item["principle"] = None
    repaired["guidance_requirements"] = repaired_requirements
    return repaired


def _guidance_source_contract(
    loaded_guidance: Sequence[dict[str, object]],
) -> tuple[dict[str, tuple[dict[str, str], ...]], dict[str, str]]:
    """从固定 Skill prompt block 提取 instructions 与受审资源的权威规划来源。"""

    sources_by_name: dict[str, tuple[dict[str, str], ...]] = {}
    modes_by_name: dict[str, str] = {}
    for loaded in loaded_guidance:
        name = str(loaded.get("name") or "").strip()
        raw_blocks = loaded.get("skills")
        if not name or not isinstance(raw_blocks, list) or len(raw_blocks) != 1:
            raise ValueError("loaded_guidance 必须为每个 Skill 提供唯一固定正文块")
        block = raw_blocks[0]
        if not isinstance(block, dict):
            raise ValueError("loaded_guidance 固定正文块无效")
        instructions = str(block.get("instructions") or "")
        if not instructions.strip():
            raise ValueError("loaded_guidance instructions 不能为空")
        sources: list[dict[str, str]] = [
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": hashlib.sha256(instructions.encode()).hexdigest(),
                "content": instructions,
            }
        ]
        reviewed_resources = block.get("reviewed_resources", [])
        if not isinstance(reviewed_resources, list):
            raise ValueError("loaded_guidance reviewed_resources 无效")
        for resource in reviewed_resources:
            if not isinstance(resource, dict):
                raise ValueError("loaded_guidance reviewed resource 无效")
            if _is_executable_guidance_resource(resource):
                continue
            sources.append(
                {
                    "source_kind": "reviewed_resource",
                    "source_ref": str(resource.get("path") or ""),
                    "source_checksum": str(
                        resource.get("content_checksum") or resource.get("checksum") or ""
                    ),
                    "content": str(resource.get("content") or ""),
                }
            )
        if name in sources_by_name:
            raise ValueError("loaded_guidance name 不得重复")
        sources_by_name[name] = tuple(sources)
        selection_mode = str(
            loaded.get("selection_mode") or block.get("selection_mode") or "forced"
        )
        if selection_mode not in {"auto", "forced"}:
            raise ValueError("loaded_guidance selection_mode 无效")
        modes_by_name[name] = selection_mode
    return sources_by_name, modes_by_name


def _planner_guidance_catalog(
    loaded_guidance: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """规划阶段只投影Skill身份与来源目录，原则正文由稳定候选ID单独提供。"""

    catalog: list[dict[str, object]] = []
    for loaded in loaded_guidance:
        raw_blocks = loaded.get("skills")
        block = raw_blocks[0] if isinstance(raw_blocks, list) and len(raw_blocks) == 1 else {}
        resources = block.get("reviewed_resources") if isinstance(block, Mapping) else []
        instructions = str(block.get("instructions") or "") if isinstance(block, Mapping) else ""
        catalog.append(
            {
                "name": str(loaded.get("name") or ""),
                "description": str(loaded.get("description") or ""),
                "skill_use_ids": [
                    str(value)
                    for value in loaded.get("skill_use_ids", ())
                    if str(value).strip()
                ],
                "selection_mode": str(
                    loaded.get("selection_mode")
                    or (block.get("selection_mode") if isinstance(block, Mapping) else "")
                    or "forced"
                ),
                "sources": [
                    {
                        "source_kind": "instructions",
                        "source_ref": "instructions",
                        "source_checksum": hashlib.sha256(instructions.encode()).hexdigest(),
                        "media_type": "text/markdown",
                    },
                    *[
                        {
                            "source_kind": "reviewed_resource",
                            "source_ref": str(resource.get("path") or ""),
                            "source_checksum": str(
                                resource.get("content_checksum")
                                or resource.get("checksum")
                                or ""
                            ),
                            "media_type": str(resource.get("media_type") or ""),
                        }
                        for resource in (resources if isinstance(resources, list) else [])
                        if isinstance(resource, Mapping)
                    ],
                ],
            }
        )
    return catalog


def _planner_guidance_candidate_catalog(
    guidance_sources_by_name: Mapping[str, tuple[dict[str, str], ...]],
) -> list[dict[str, object]]:
    """按来源和章节压缩原则目录，避免为每个候选重复checksum与长章节路径。"""

    catalog: list[dict[str, object]] = []
    for skill_ref, sources in sorted(guidance_sources_by_name.items()):
        grouped_sources: dict[tuple[str, str, str], dict[str, object]] = {}
        grouped_sections: dict[
            tuple[str, str, str], dict[str, list[dict[str, object]]]
        ] = {}
        all_candidates = guidance_principle_candidates(sources)
        visible_candidates = [
            candidate
            for candidate in all_candidates
            if not _is_introductory_guidance_candidate(candidate)
        ] or list(all_candidates)
        for candidate in visible_candidates:
            source_key = (
                str(candidate["source_kind"]),
                str(candidate["source_ref"]),
                str(candidate["source_checksum"]),
            )
            source = grouped_sources.setdefault(
                source_key,
                {
                    "source_kind": source_key[0],
                    "source_ref": source_key[1],
                    "source_checksum": source_key[2],
                    "sections": [],
                },
            )
            sections = grouped_sections.setdefault(source_key, {})
            section_path = str(candidate.get("section_path") or "")
            sections.setdefault(section_path, []).append(
                {
                    "principle_candidate_id": candidate["principle_candidate_id"],
                    "principle_candidate_id_short": str(
                        candidate["principle_candidate_id"]
                    ).removeprefix("guidcand_")[:16],
                    "source_order": candidate["source_order"],
                    "principle": candidate["principle"],
                }
            )
            source["sections"] = [
                {
                    "section_path": path,
                    "candidates": sorted(
                        values,
                        key=lambda item: (
                            int(item["source_order"]),
                            str(item["principle_candidate_id"]),
                        ),
                    ),
                }
                for path, values in sorted(
                    sections.items(),
                    key=lambda item: min(
                        int(candidate_item["source_order"])
                        for candidate_item in item[1]
                    ),
                )
            ]
        catalog.append(
            {
                "skill_ref": skill_ref,
                "sources": [grouped_sources[key] for key in sorted(grouped_sources)],
            }
        )
    return catalog


def _is_introductory_guidance_candidate(candidate: Mapping[str, object]) -> bool:
    """把 Skill 开场定义/宣传句排除出 Planner 候选，保留可观察的操作性规则。"""

    if str(candidate.get("section_path") or "").strip():
        return False
    principle = " ".join(str(candidate.get("principle") or "").split())
    # Skill frontmatter 是来源身份/路由元数据，不是本轮任务可应用的方法原则。
    # 保留 guidance_principle_candidates() 的完整结果以兼容旧计划；这里只在新计划
    # 投影给模型的候选目录中排除它，避免模型把 ``name:``/``description:`` 当成指导。
    if re.match(
        r"^(?:name|description|license|version|metadata|disable-model-invocation|"
        r"user-invocable|allowed-tools|model)\s*:",
        principle,
        flags=re.IGNORECASE,
    ):
        return True
    return re.match(
        r"^(?:Reference for\b|The packaging differs\b|This (?:skill|document)\b|Use when\b)",
        principle,
        flags=re.IGNORECASE,
    ) is not None


def _is_executable_guidance_resource(resource: Mapping[str, object]) -> bool:
    """识别Skill包中只供阅读的代码/脚本资源，禁止将其语句提升为规划原则。"""

    path = str(resource.get("path") or "").strip().casefold()
    media_type = str(resource.get("media_type") or "").strip().casefold()
    executable_suffixes = (
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".bat",
        ".cmd",
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".rs",
        ".sql",
        ".vba",
    )
    executable_media = {
        "application/javascript",
        "application/sql",
        "text/javascript",
        "text/x-python",
        "text/x-shellscript",
        "text/x-c",
        "text/x-c++",
        "text/x-java-source",
    }
    return path.endswith(executable_suffixes) or media_type in executable_media


def _normalize_model_display_fields(raw: dict) -> dict:
    """收紧展示标题并为交付物生成稳定身份，不修补执行语义错误。"""

    normalized = dict(raw)
    raw_artifacts = raw.get("expected_artifacts")
    if isinstance(raw_artifacts, list):
        artifacts: list[object] = []
        used_keys: set[str] = set()
        for index, raw_artifact in enumerate(raw_artifacts, start=1):
            if not isinstance(raw_artifact, dict):
                artifacts.append(raw_artifact)
                continue
            artifact = dict(raw_artifact)
            artifact_key = str(artifact.get("artifact_key") or "")
            if (
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", artifact_key) is None
                or artifact_key in used_keys
            ):
                artifact_key = f"artifact_{index:02d}"
                suffix = index
                while artifact_key in used_keys:
                    suffix += 1
                    artifact_key = f"artifact_{suffix:02d}"
                artifact["artifact_key"] = artifact_key
            used_keys.add(artifact_key)
            artifacts.append(artifact)
        normalized["expected_artifacts"] = artifacts
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        return normalized
    answer_only_steps = [
        item
        for item in raw_steps
        if isinstance(item, dict) and item.get("kind") == "answer"
    ]
    if len(answer_only_steps) > 1 and len(answer_only_steps) == len(raw_steps):
        final_answer = dict(answer_only_steps[-1])
        final_answer["depends_on"] = []
        final_answer["required"] = True
        raw_steps = [final_answer]
    steps: list[object] = []
    answer_indexes = [
        index
        for index, item in enumerate(raw_steps)
        if isinstance(item, dict) and item.get("kind") == "answer"
    ]
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            steps.append(raw_step)
            continue
        step = dict(raw_step)
        title = step.get("title")
        if isinstance(title, str) and len(title) > 256:
            step["title"] = f"{title[:255].rstrip()}…"
        if step.get("kind") == "answer" and len(answer_indexes) == 1:
            step["required"] = True
        steps.append(step)
    normalized["steps"] = steps
    return normalized


def _is_planner_contract_echo(raw: Mapping[str, object]) -> bool:
    """识别 provider 回显宿主 output_contract 且缺少真实 steps 的确定形状。"""

    return not isinstance(raw.get("steps"), list) and isinstance(
        raw.get("output_contract"), Mapping
    ) and "steps" in raw["output_contract"]


def _planner_capability_view(snapshot: CapabilitySnapshot) -> dict[str, object]:
    """向规划器披露做出合法步骤选择所需的最小类别，不暴露审计或连接侧带。"""

    risk_class = str(snapshot.contract.get("risk_class") or "")
    allowed_step_kind = {
        "read": "knowledge" if snapshot.capability_type == "knowledge" else "tool.read",
        "local_write": "tool.write",
        "external_write": "tool.write",
        "execute": "tool.execute",
    }.get(risk_class, "")
    return {
        **snapshot.model_view,
        "capability_type": snapshot.capability_type,
        "risk_class": risk_class,
        "allowed_step_kind": allowed_step_kind,
    }


def _strip_unloaded_guidance_fields(raw: Mapping[str, object]) -> dict[str, object]:
    """在无已加载 Skill 的 repair 末次清除模型伪造的指导字段。

    该收敛只影响 guidance_skill_refs 和 guidance_requirements，不改变目标、步骤类别、
    capability_refs 或成功标准；因此未知 Skill 不能借机变成能力调用，也不会污染无 Skill
    基线。已加载 Skill 的未知引用不进入本函数，仍由服务端严格拒绝。
    """

    normalized = dict(raw)
    normalized["guidance_requirements"] = []
    raw_steps = raw.get("steps")
    if isinstance(raw_steps, list):
        steps: list[object] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping):
                steps.append(raw_step)
                continue
            step = dict(raw_step)
            step["guidance_skill_refs"] = []
            steps.append(step)
        normalized["steps"] = steps
    return normalized


def _force_guidance_phase_gate(
    raw: Mapping[str, object],
    *,
    candidate_catalog: object,
) -> dict[str, object]:
    """在末次有界 repair 中冻结一个权威阶段否决门，不凭模型选词放宽方法契约。

    当计划没有任何显式运行步骤、但已加载 Skill 的固定来源存在多个等价 Phase 1 gate
    时，模型可能在不同重试中选到旁支句子，造成正常任务被随机拒绝。宿主只从同一受管
    候选目录选择第一个 gate，替换该 Skill 的一条 apply requirement，并把映射收敛为
    停止/请求证据；不增加能力、步骤或权限，也不执行 Skill 正文。
    """

    raw_requirements = raw.get("guidance_requirements")
    # 模型在 repair 轮可能把 requirements 整体省略，或忘记唯一已加载 Skill
    # 的 skill_ref。单 Skill 场景仍有明确的宿主来源身份，可以安全补出一个
    # 最小 apply 槽；多 Skill 场景不猜测归属，继续 fail-closed。
    if not isinstance(raw_requirements, list):
        raw_requirements = []
    raw_steps = raw.get("steps")
    if isinstance(raw_steps, list) and any(
        isinstance(step, Mapping)
        and str(step.get("kind") or "") in {"tool.read", "tool.execute", "knowledge", "explore"}
        for step in raw_steps
    ):
        return dict(raw)
    if not isinstance(candidate_catalog, list):
        return dict(raw)

    repaired = dict(raw)
    repaired_requirements = [
        dict(item) if isinstance(item, Mapping) else item for item in raw_requirements
    ]
    for source_block in candidate_catalog:
        if not isinstance(source_block, Mapping):
            continue
        skill_ref = str(source_block.get("skill_ref") or "").strip()
        if not skill_ref:
            continue
        candidates: list[dict[str, object]] = []
        all_candidates: list[dict[str, object]] = []
        for source in source_block.get("sources", ()):
            if not isinstance(source, Mapping):
                continue
            for section in source.get("sections", ()):
                if not isinstance(section, Mapping):
                    continue
                for candidate in section.get("candidates", ()):
                    if not isinstance(candidate, Mapping):
                        continue
                    candidate_entry = {
                        **dict(candidate),
                        "source_kind": str(source.get("source_kind") or ""),
                        "source_ref": str(source.get("source_ref") or ""),
                        "section_path": str(section.get("section_path") or ""),
                    }
                    all_candidates.append(candidate_entry)
                    if _is_guidance_phase_gate(str(candidate.get("principle") or "")):
                        candidates.append(candidate_entry)
        if not candidates:
            continue
        skill_indices = [
            index
            for index, item in enumerate(repaired_requirements)
            if isinstance(item, Mapping)
            and str(item.get("skill_ref") or "").strip() == skill_ref
            and str(item.get("disposition") or "apply") == "apply"
        ]
        if not skill_indices and len(candidate_catalog) == 1:
            unbound_indices = [
                index
                for index, item in enumerate(repaired_requirements)
                if isinstance(item, Mapping)
                and not str(item.get("skill_ref") or "").strip()
                and str(item.get("disposition") or "apply") == "apply"
            ]
            if unbound_indices:
                for index in unbound_indices:
                    repaired_requirements[index] = {
                        **dict(repaired_requirements[index]),
                        "skill_ref": skill_ref,
                    }
                skill_indices = unbound_indices
        if not skill_indices and len(candidate_catalog) == 1:
            repaired_requirements.append(
                {
                    "skill_ref": skill_ref,
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle_candidate_id": "",
                    "principle_candidate_id_short": None,
                    "principle": None,
                    "task_mapping": "",
                    "observable_acceptance": "",
                    "disposition": "apply",
                }
            )
            skill_indices = [len(repaired_requirements) - 1]
        if not skill_indices:
            continue
        selected_principles = {
            str(repaired_requirements[index].get("principle") or "").strip()
            for index in skill_indices
            if isinstance(repaired_requirements[index], Mapping)
        }
        if any(str(candidate.get("principle") or "") in selected_principles for candidate in candidates):
            continue
        gate = sorted(
            candidates,
            key=lambda item: (
                int(item.get("source_order") or 0),
                str(item.get("principle_candidate_id") or ""),
            ),
        )[0]
        target_index = skill_indices[0]
        target = dict(repaired_requirements[target_index])
        target.update(
            {
                "source_kind": str(gate.get("source_kind") or "instructions"),
                "source_ref": str(gate.get("source_ref") or "instructions"),
                "principle_candidate_id": str(gate.get("principle_candidate_id") or ""),
                "principle_candidate_id_short": None,
                "principle": None,
                "task_mapping": (
                    "当前没有已运行的 red-capable feedback loop；停止进入假设阶段，"
                    "请求建立回路所需的脱敏证据或明确授权。"
                ),
                "observable_acceptance": (
                    "正文明确披露反馈回路缺失和证据/授权请求，不输出原因排序、"
                    "根因候选或待验证假设。"
                ),
                "disposition": "apply",
            }
        )
        kept_requirements: list[object] = []
        for index, item in enumerate(repaired_requirements):
            if index not in skill_indices or index == target_index:
                kept_requirements.append(target if index == target_index else item)
                continue
            if not isinstance(item, Mapping):
                kept_requirements.append(item)
                continue
            reference = str(
                item.get("principle_candidate_id")
                or item.get("principle_candidate_id_short")
                or item.get("principle")
                or ""
            ).strip()
            matched = next(
                (
                    candidate
                    for candidate in all_candidates
                    if reference
                    and (
                        reference == str(candidate.get("principle_candidate_id") or "")
                        or str(candidate.get("principle_candidate_id") or "")
                        .removeprefix("guidcand_")
                        .startswith(reference.removeprefix("guidcand_"))
                        or reference == str(candidate.get("principle") or "")
                    )
                ),
                None,
            )
            section_path = str(matched.get("section_path") or "") if matched else ""
            phase_match = re.search(r"(?:\bphase\s*|阶段\s*)([2-9]\d*)", section_path, re.I)
            if phase_match:
                # 没有 red-capable 前置操作时，任何 Phase 2+ 要求都会把
                # 模型带入后续阶段；保持 fail-closed，交给 answer 只披露阻塞。
                continue
            kept_requirements.append(item)
        repaired_requirements = kept_requirements
    repaired["guidance_requirements"] = repaired_requirements
    return repaired


def _repair_guidance_phase_continuity(
    raw: Mapping[str, object],
    *,
    candidate_catalog: object,
) -> dict[str, object]:
    """在末次 repair 中补齐已选阶段的权威连续前置候选。"""

    if not isinstance(candidate_catalog, list):
        return dict(raw)
    raw_requirements = raw.get("guidance_requirements")
    if not isinstance(raw_requirements, list):
        return dict(raw)
    raw_steps = raw.get("steps")
    has_prerequisite_operation = isinstance(raw_steps, list) and any(
        isinstance(step, Mapping)
        and str(step.get("kind") or "")
        in {"tool.read", "tool.execute", "knowledge", "explore"}
        for step in raw_steps
    )
    if not has_prerequisite_operation:
        return _force_guidance_phase_gate(raw, candidate_catalog=candidate_catalog)

    candidates: list[dict[str, object]] = []
    for source_block in candidate_catalog:
        if not isinstance(source_block, Mapping):
            continue
        skill_ref = str(source_block.get("skill_ref") or "").strip()
        for source in source_block.get("sources", ()):
            if not isinstance(source, Mapping):
                continue
            source_kind = str(source.get("source_kind") or "")
            source_ref = str(source.get("source_ref") or "")
            for section in source.get("sections", ()):
                if not isinstance(section, Mapping):
                    continue
                section_path = str(section.get("section_path") or "")
                for candidate in section.get("candidates", ()):
                    if not isinstance(candidate, Mapping):
                        continue
                    candidates.append(
                        {
                            **dict(candidate),
                            "skill_ref": skill_ref,
                            "source_kind": source_kind,
                            "source_ref": source_ref,
                            "section_path": section_path,
                        }
                    )
    if not candidates:
        return dict(raw)

    def candidate_for_requirement(item: Mapping[str, object]) -> dict[str, object] | None:
        """按完整/短候选身份或唯一原则返回权威候选。"""

        reference = str(
            item.get("principle_candidate_id")
            or item.get("principle_candidate_id_short")
            or ""
        ).strip().removeprefix("guidcand_").casefold()
        if reference:
            matches = [
                candidate
                for candidate in candidates
                if str(candidate.get("principle_candidate_id") or "")
                .removeprefix("guidcand_")
                .casefold()
                .startswith(reference)
            ]
            if len(matches) == 1:
                return matches[0]
        principle = " ".join(str(item.get("principle") or "").split()).casefold()
        if principle:
            matches = [
                candidate
                for candidate in candidates
                if " ".join(str(candidate.get("principle") or "").split()).casefold()
                == principle
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    repaired = dict(raw)
    repaired_requirements = [
        dict(item) if isinstance(item, Mapping) else item for item in raw_requirements
    ]
    selected: dict[tuple[str, str], list[dict[str, object]]] = {}
    for item in repaired_requirements:
        if not isinstance(item, Mapping) or str(item.get("disposition") or "apply") != "apply":
            continue
        candidate = candidate_for_requirement(item)
        if candidate is None:
            continue
        key = (str(candidate.get("source_kind") or ""), str(candidate.get("source_ref") or ""))
        selected.setdefault(key, []).append(candidate)

    for source_key, selected_candidates in selected.items():
        selected_phases = {
            phase
            for candidate in selected_candidates
            if (phase := _guidance_phase_number(str(candidate.get("section_path") or "")))
            is not None
        }
        if not selected_phases:
            continue
        highest_phase = max(selected_phases)
        selected_ids = {
            str(candidate.get("principle_candidate_id") or "") for candidate in selected_candidates
        }
        for phase in range(1, highest_phase + 1):
            if phase in selected_phases:
                continue
            phase_candidates = [
                candidate
                for candidate in candidates
                if (
                    str(candidate.get("source_kind") or ""),
                    str(candidate.get("source_ref") or ""),
                ) == source_key
                and _guidance_phase_number(str(candidate.get("section_path") or "")) == phase
            ]
            if not phase_candidates:
                continue
            chosen = min(
                phase_candidates,
                key=lambda item: (
                    int(item.get("source_order") or 0),
                    str(item.get("principle_candidate_id") or ""),
                ),
            )
            candidate_id = str(chosen.get("principle_candidate_id") or "")
            if not candidate_id or candidate_id in selected_ids:
                continue
            repaired_requirements.append(
                {
                    "skill_ref": str(chosen.get("skill_ref") or ""),
                    "source_kind": str(chosen.get("source_kind") or ""),
                    "source_ref": str(chosen.get("source_ref") or ""),
                    "principle_candidate_id": candidate_id,
                    "principle_candidate_id_short": None,
                    "principle": None,
                    "task_mapping": f"按固定来源顺序补齐 Phase {phase} 前置阶段。",
                    "observable_acceptance": f"交付物明确记录 Phase {phase} 的阶段结果或门禁。",
                    "disposition": "apply",
                }
            )
            selected_ids.add(candidate_id)
            selected_phases.add(phase)
    repaired["guidance_requirements"] = repaired_requirements
    return repaired


def _repair_terminal_answer_steps(raw: Mapping[str, object]) -> dict[str, object]:
    """把 repair 草案收敛为一个终态 answer，并保留前置步骤和指导引用。

    模型有时会把“报告结构”和“交付报告”都标成 answer。服务端契约只有一个最终汇聚点，
    因此这里仅在第二次、有界 repair 中删除重复 answer：保留最后一个 answer，转移被删除
    answer 的前置依赖和 Skill 引用，并让所有 required 非 answer 成为最终 answer 的前置。
    任何能力、权限、步骤类别和成功标准都不在此函数中新增。
    """

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        return dict(raw)
    step_items = [dict(item) if isinstance(item, Mapping) else item for item in raw_steps]
    answer_indexes = [
        index
        for index, item in enumerate(step_items)
        if isinstance(item, Mapping) and str(item.get("kind") or "") == "answer"
    ]
    if len(answer_indexes) <= 1:
        return dict(raw)

    answer_index = answer_indexes[-1]
    final_raw = dict(step_items[answer_index])
    final_key = str(final_raw.get("draft_id") or final_raw.get("step_key") or "answer")
    removed: dict[str, tuple[str, ...]] = {}
    guidance_refs: list[str] = [
        str(item)
        for item in final_raw.get("guidance_skill_refs", ())
        if str(item).strip()
    ]
    for index in answer_indexes[:-1]:
        item = step_items[index]
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("draft_id") or item.get("step_key") or "").strip()
        if not key:
            continue
        removed[key] = tuple(
            str(dep)
            for dep in item.get("depends_on", ())
            if str(dep).strip() and str(dep) != key
        )
        guidance_refs.extend(
            str(ref)
            for ref in item.get("guidance_skill_refs", ())
            if str(ref).strip()
        )

    def expand_dependency(value: str, seen: set[str] | None = None) -> tuple[str, ...]:
        """展开被移除 answer 的前置，避免保留悬空 draft_id。"""

        visited = set(seen or ())
        if value in visited:
            return ()
        visited.add(value)
        parents = removed.get(value)
        if parents is None:
            return (value,)
        expanded: list[str] = []
        for parent in parents:
            for dependency in expand_dependency(parent, visited):
                if dependency not in expanded:
                    expanded.append(dependency)
        return tuple(expanded)

    final_dependencies: list[str] = []
    for dependency in final_raw.get("depends_on", ()):
        for expanded in expand_dependency(str(dependency)):
            if expanded != final_key and expanded not in final_dependencies:
                final_dependencies.append(expanded)
    removed_keys = set(removed)
    for item in step_items:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("draft_id") or item.get("step_key") or "").strip()
        if not key or key in removed_keys or key == final_key:
            continue
        if bool(item.get("required", True)) and key not in final_dependencies:
            final_dependencies.append(key)
    final_raw["required"] = True
    final_raw["depends_on"] = final_dependencies
    final_raw["guidance_skill_refs"] = list(dict.fromkeys(guidance_refs))

    repaired_steps: list[object] = []
    for item in step_items:
        if not isinstance(item, Mapping):
            repaired_steps.append(item)
            continue
        key = str(item.get("draft_id") or item.get("step_key") or "").strip()
        if key in removed_keys or key == final_key:
            continue
        rewritten = dict(item)
        dependencies: list[str] = []
        for dependency in item.get("depends_on", ()):
            for expanded in expand_dependency(str(dependency)):
                if expanded != key and expanded not in dependencies:
                    dependencies.append(expanded)
        rewritten["depends_on"] = dependencies
        repaired_steps.append(rewritten)
    repaired_steps.append(final_raw)
    repaired = dict(raw)
    repaired["steps"] = repaired_steps
    return repaired


def _goal_authorizes_capability(goal: str, snapshot: CapabilitySnapshot) -> bool:
    """对需显式意图的副作用能力做确定性预过滤，审批不能替代用户授权。"""

    if not _goal_matches_capability_applicability(goal, snapshot):
        return False
    intent = str(snapshot.contract.get("requires_explicit_goal_intent") or "")
    managed_workspace = snapshot.audit_view.get("managed_workspace")
    if (
        not intent
        and isinstance(managed_workspace, Mapping)
        and snapshot.contract.get("risk_class") in {"local_write", "execute"}
    ):
        normalized = " ".join(goal.casefold().split())
        write_intent = re.search(
            r"(?:修改|编辑|重构|修复|实现|新增|删除|提交|落盘|写入|改动)"
            r".{0,32}(?:代码|文件|仓库|模块|接口|测试|配置|实现|工作区)"
            r"|(?:代码|文件|仓库|模块|接口|测试|配置|实现|工作区)"
            r".{0,32}(?:修改|编辑|重构|修复|实现|新增|删除|提交|落盘|写入|改动)"
            r"|(?:modify|edit|refactor|fix|implement|add|delete|commit|write)"
            r".{0,32}(?:code|file|repository|module|api|test|config|workspace)"
            r"|(?:修改|重构|修复|实现).{0,8}(?:并)?(?:验证|测试)",
            normalized,
        )
        execute_intent = re.search(
            r"(?:运行|执行|跑).{0,16}(?:测试|检查|命令|脚本|构建)"
            r"|(?:run|execute).{0,16}(?:test|check|command|script|build)"
            r"|(?:修改|重构|修复|实现).{0,8}(?:并)?(?:验证|测试)",
            normalized,
        )
        if snapshot.contract.get("risk_class") == "execute":
            return bool(execute_intent)
        return bool(write_intent)
    if not intent:
        return True
    normalized = goal.casefold()
    if intent == "skill_proposal":
        positive_pattern = (
            r"(?:安装|导入|采用).{0,24}(?:skill|技能)"
            r"|(?:skill|技能).{0,24}(?:安装|导入|采用)"
            r"|(?:保存|沉淀|创建|新增|提交|发布|提议|建议).{0,40}"
            r"(?:为|成|一个|新的).{0,12}(?:skill|技能)"
            r"|(?:保存|沉淀|创建|新增|提交|发布|提议|建议).{0,24}(?:skill|技能)"
            r"|(?:propose|install|import|adopt|publish|save as|create)"
            r".{0,40}skill"
            r"|skill.{0,40}(?:proposal|install|import|adoption|publication)"
        )
        negative_pattern = (
            r"(?:不要|不准|禁止|无需|无须|别|不得|拒绝|取消).{0,12}"
            r"(?:安装|导入|采用|保存|沉淀|创建|新增|提交|发布|提议|建议)?"
            r".{0,20}(?:skill|技能)"
            r"|(?:do not|don't|must not|never|without).{0,32}"
            r"(?:propose|install|import|adopt|publish|save|create).{0,20}skill"
        )
        clauses = [
            item.strip()
            for item in re.split(r"[，,；;。]|(?:但是|但|however|but)", normalized)
            if item.strip()
        ]
        return any(
            re.search(positive_pattern, clause)
            and not re.search(negative_pattern, clause)
            for clause in clauses
        )
    return False


def _goal_matches_capability_applicability(
    goal: str,
    snapshot: CapabilitySnapshot,
) -> bool:
    """按发布快照中的领域或代码意图过滤能力，未声明时保持历史兼容。"""

    applicability = snapshot.contract.get("applicability")
    if not isinstance(applicability, Mapping):
        return True
    mode = str(applicability.get("mode") or "")
    normalized_goal = " ".join(goal.casefold().split())
    if mode == "goal_scoped":
        terms = [
            str(item).strip().casefold()
            for key in ("domains", "aliases")
            for item in applicability.get(key, ())
            if str(item).strip()
        ]
        return any(_goal_contains_scope_term(normalized_goal, term) for term in terms)
    if mode == "agent_workspace":
        intents = {str(item) for item in applicability.get("intents", ())}
        return any(_goal_has_workspace_intent(normalized_goal, intent) for intent in intents)
    return False


def _goal_contains_scope_term(goal: str, term: str) -> bool:
    """中文范围词按子串匹配，ASCII范围词按稳定单词边界匹配。"""

    if term.isascii():
        return re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", goal) is not None
    return term in goal


def _goal_has_workspace_intent(goal: str, intent: str) -> bool:
    """识别通用代码工作区的检查、修改和执行意图，不依赖具体文件路径。"""

    code_object = r"(?:代码|仓库|模块|接口|实现|文件|测试|配置|code|repository|repo|module|api|implementation|file|test|config)"
    inspect_action = r"(?:检查|审查|评审|分析|诊断|排查|调试|查看|读取|理解|inspect|review|analy[sz]e|diagnose|debug|read|understand)"
    change_action = r"(?:修改|编辑|重构|修复|实现|新增|删除|提交|落盘|写入|改动|modify|edit|refactor|fix|implement|add|delete|commit|write)"
    execute_action = r"(?:运行|执行|跑|run|execute)"
    if intent == "code_inspect":
        return bool(
            re.search(rf"{inspect_action}.{{0,32}}{code_object}|{code_object}.{{0,32}}{inspect_action}", goal)
        )
    if intent == "code_change":
        return bool(
            re.search(rf"{change_action}.{{0,32}}{code_object}|{code_object}.{{0,32}}{change_action}", goal)
        )
    if intent == "code_execute":
        return bool(
            re.search(
                rf"{execute_action}.{{0,16}}(?:测试|检查|命令|脚本|构建|test|check|command|script|build)",
                goal,
            )
        )
    return False


def _validate_plan_capabilities(
    plan: NormalizedPlan,
    capabilities: Sequence[CapabilitySnapshot],
) -> None:
    """拒绝步骤引用未冻结、类别错误或运行时无法执行的能力。"""

    tool_names = {
        item.name
        for item in capabilities
        if item.capability_type in {"tool", "connector"}
        and item.contract.get("risk_class") == "read"
    }
    write_names = {
        item.name
        for item in capabilities
        if item.capability_type in {"tool", "connector"}
        and item.contract.get("risk_class") in {"local_write", "external_write"}
    }
    execute_names = {
        item.name
        for item in capabilities
        if item.capability_type == "tool" and item.contract.get("risk_class") == "execute"
    }
    knowledge_names = {
        item.name for item in capabilities if item.capability_type == "knowledge"
    }
    required_knowledge_names = {
        item.name
        for item in capabilities
        if item.capability_type == "knowledge"
        and item.contract.get("required_for_answer") is True
    }
    explore_names = {
        item.name
        for item in capabilities
        if item.capability_type == "tool"
        and item.contract.get("risk_class") == "read"
        and item.contract.get("explore_safe") is True
    }
    for step in plan.steps:
        refs = set(step.capability_refs)
        if step.kind == "tool.read":
            if not refs or not refs <= tool_names:
                raise ValueError("动态计划引用了未冻结的只读工具能力")
            continue
        if step.kind == "tool.write":
            if not refs or not refs <= write_names:
                raise ValueError("动态计划引用了未冻结的写能力")
            continue
        if step.kind == "tool.execute":
            if not refs or not refs <= execute_names:
                raise ValueError("动态计划引用了未冻结的执行能力")
            continue
        if step.kind == "knowledge":
            if refs != {"knowledge.search"} or not refs <= knowledge_names:
                raise ValueError("动态计划引用了未冻结的知识能力")
            continue
        if step.kind == "explore":
            if not refs or not refs <= explore_names:
                raise ValueError("动态计划引用了未显式发布为 explore-safe 的工具能力")
            continue
        if refs:
            raise ValueError("非能力步骤不得声明 capability_refs")
    planned_knowledge_names = {
        ref
        for step in plan.steps
        if step.kind == "knowledge"
        for ref in step.capability_refs
    }
    if not required_knowledge_names <= planned_knowledge_names:
        raise ValueError("required knowledge 必须作为回答前置步骤")


def _validate_attachment_read_separation(
    plan: NormalizedPlan,
    *,
    has_input_resources: bool,
) -> None:
    """拒绝把平台受管附件读取伪装成任意业务工具步骤。"""

    if not has_input_resources:
        return
    for step in plan.steps:
        if step.kind == "clarification":
            title = step.title.casefold()
            if re.search(
                r"(?:input\.(?:read|inspect|search)|table\.(?:compute|profile|compare))",
                title,
            ) or _is_platform_owned_attachment_step(step):
                raise ValueError(
                    "平台内建附件读取/计算由Runtime自动执行，"
                    "不得伪装成用户澄清或分析步骤"
                )
            continue
        if step.kind not in {"tool.read", "tool.execute", "explore", "knowledge"}:
            continue
        if _is_platform_owned_attachment_step(step):
            raise ValueError("受管附件由平台 input.read 自动读取，不得借用业务工具分析附件")


def _is_platform_owned_attachment_step(step: object) -> bool:
    """识别模型误把受管附件读取规划成业务读工具的步骤。"""

    if getattr(step, "kind", None) != "tool.read":
        return False
    title = str(getattr(step, "title", "")).casefold()
    attachment_object = (
        r"(?:附件|图片|图像|文件|文档|表格|pdf|csv|xlsx|docx|pptx|image|file|document)"
    )
    read_action = r"(?:读取|解析|分析|核对|检查|提取|查看|识别|read|parse|analy[sz]e|inspect|extract)"
    return bool(
        re.search(
            rf"(?:{read_action}.{{0,20}}{attachment_object}|"
            rf"{attachment_object}.{{0,20}}{read_action})",
            title,
        )
    )


def _strip_platform_owned_attachment_steps(
    plan: NormalizedPlan,
    *,
    has_input_resources: bool,
) -> NormalizedPlan:
    """把平台已拥有的附件读取步骤确定性消解，并重接其余步骤依赖。"""

    if not has_input_resources:
        return plan
    removed = {
        step.step_key
        for step in plan.steps
        if _is_platform_owned_attachment_step(step)
    }
    if not removed or not any(step.kind == "answer" for step in plan.steps):
        return plan
    dependencies = {step.step_key: tuple(step.depends_on) for step in plan.steps}

    def expand(step_key: str, trail: set[str]) -> list[str]:
        """将被消解节点展开为仍需保留的上游依赖，避免留下悬空边。"""

        if step_key not in removed:
            return [step_key]
        if step_key in trail:
            raise ValueError("平台附件读取步骤依赖出现循环")
        values: list[str] = []
        for dependency in dependencies.get(step_key, ()):
            values.extend(expand(dependency, trail | {step_key}))
        return values

    rewritten = []
    for step in plan.steps:
        if step.step_key in removed:
            continue
        new_dependencies: list[str] = []
        for dependency in step.depends_on:
            for candidate in expand(dependency, set()):
                if candidate not in new_dependencies and candidate != step.step_key:
                    new_dependencies.append(candidate)
        rewritten.append(step.model_copy(update={"depends_on": tuple(new_dependencies)}))
    return plan.model_copy(update={"steps": tuple(rewritten)})


def _validate_clarification_semantics(plan: NormalizedPlan) -> None:
    """澄清只能表达真实用户输入缺口，禁止模型把内部分析包装成等待步骤。"""

    user_input_markers = re.compile(
        r"(?:请用户|请求|等待|需要|请|补充|确认|选择|提供|澄清|授权|回答|输入)"
        r"|(?:ask|request|wait|need|provide|confirm|choose|clarify|authorize|answer|input)",
        re.IGNORECASE,
    )
    internal_work_markers = re.compile(
        r"(?:结构|数据来源|分析|解析|设计|撰写|生成|形成|整理|核对|检查|评审)"
        r"|(?:交付要求|交付标准|输出要求|结果要求|任务要求)"
        r"|(?:structure|source|analy[sz]e|parse|design|draft|generate|review)",
        re.IGNORECASE,
    )
    explicit_user_choice = re.compile(
        r"(?:请用户|请求用户|等待用户|用户提供|用户确认|是否|能否|授权|补充|选择)"
        r"|(?:ask the user|user (?:provide|confirm|choose)|whether|authorize)",
        re.IGNORECASE,
    )
    for step in plan.steps:
        if step.kind != "clarification":
            continue
        title = step.title.strip()
        if not user_input_markers.search(title):
            raise ValueError("澄清步骤必须明确请求真实用户输入，不得代表Agent内部工作")
        if internal_work_markers.search(title) and not explicit_user_choice.search(title):
            raise ValueError("澄清步骤不得把结构、分析或数据来源等Agent内部工作包装成用户等待")


def _validate_guidance_step_alignment(
    plan: NormalizedPlan,
    *,
    input_resource_names: set[str] | None = None,
) -> None:
    """冻结要求实际读取文件时，必须存在命名该文件的读取步骤。

    Skill 原则经常把“按需读取某文档”作为最终文档应呈现的上下文指针，
    这不等于本轮规划必须真的打开该文档。只有出现“读取 X 后/之前/作为输入”
    这类明确的执行前置语义时，才要求冻结 read 步骤，避免把待交付文档中的
    引用误判为本轮缺失工具调用。
    """

    input_names = input_resource_names or set()
    read_titles = "\n".join(
        step.title.casefold()
        for step in plan.steps
        if step.kind in {"tool.read", "knowledge", "explore"}
    )
    for requirement in plan.guidance_requirements:
        if requirement.disposition.value != "apply":
            continue
        contract = f"{requirement.principle} {requirement.task_mapping}"
        explicit_read = re.search(
            r"(?:\bread\b|读取|读|查阅)\s+[`'\"]?[^\s`'\"]+[`'\"]?\s*"
            r"(?:后|再|之前|前|作为(?:输入|依据)|才能|before|prior|first)",
            contract,
            re.IGNORECASE,
        )
        if explicit_read is None:
            continue
        file_refs = {
            match.casefold()
            for match in re.findall(
                r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:md|py|ts|tsx|js|json|ya?ml)",
                contract,
                re.IGNORECASE,
            )
        }
        missing = {ref for ref in file_refs if ref not in read_titles and ref not in input_names}
        if missing:
            missing_text = ", ".join(sorted(missing))[:240]
            raise ValueError(f"Guidance 文件读取要求缺少对应的冻结 read 步骤: {missing_text}")


def _validate_plan_convergence(plan: NormalizedPlan) -> None:
    """拒绝没有唯一结果汇聚点或会绕过 required 步骤的动态计划。"""

    answer_steps = [step for step in plan.steps if step.kind == "answer"]
    if len(answer_steps) != 1 or not answer_steps[0].required:
        raise ValueError("动态计划必须且只能包含一个 required answer 终止步骤")
    answer = answer_steps[0]
    dependents: dict[str, set[str]] = {step.step_key: set() for step in plan.steps}
    dependencies = {step.step_key: set(step.depends_on) for step in plan.steps}
    for step in plan.steps:
        for dependency in step.depends_on:
            dependents[dependency].add(step.step_key)
    if dependents[answer.step_key]:
        raise ValueError("动态计划 answer 必须是最终步骤")
    ancestors: set[str] = set()
    pending = list(answer.depends_on)
    while pending:
        step_key = pending.pop()
        if step_key in ancestors:
            continue
        ancestors.add(step_key)
        pending.extend(dependencies[step_key])
    missing = {
        step.step_key
        for step in plan.steps
        if step.required and step.step_key != answer.step_key and step.step_key not in ancestors
    }
    if missing:
        raise ValueError("动态计划 required 步骤必须汇聚到最终 answer")
