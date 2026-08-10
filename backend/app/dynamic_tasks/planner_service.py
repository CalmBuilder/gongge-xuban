"""
@Time       : 2026/08/04 02:24
@Author     : zhanglp8181
@File       : planner_service.py
@CallChain  : DynamicTaskAgent → DynamicTaskPlanner → LLMClient → NormalizedPlan
@Description: 向模型投影受控能力视图，并把完整计划草案收紧为服务端有界计划。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot
from app.dynamic_tasks.planning import (
    DynamicPlanDraft,
    NormalizedPlan,
    SuccessCriterion,
    normalize_plan_draft,
)


class JsonPlanningClient(Protocol):
    """约束动态规划只使用完整 JSON object 响应。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回完整且可解析的 JSON object，不暴露流式半包。"""


class DynamicTaskPlanner:
    """把受控目标、成功标准和能力模型视图转换为有界规范计划。"""

    def __init__(
        self,
        client: JsonPlanningClient,
        *,
        max_steps: int = 8,
        max_tool_calls: int = 6,
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

    def create_plan(
        self,
        *,
        goal: str,
        success_criteria: Sequence[SuccessCriterion],
        capabilities: Sequence[CapabilitySnapshot],
        input_resources: Sequence[dict[str, object]] = (),
    ) -> NormalizedPlan:
        """生成完整草案并覆盖目标/成功标准，防止模型改写用户任务契约。"""

        executable_capabilities = [
            snapshot
            for snapshot in capabilities
            if snapshot.contract.get("risk_class") in {"read", "external_write"}
            and snapshot.capability_type in {"tool", "connector", "knowledge"}
        ]
        allowed_step_kinds = ["tool.read", "answer", "clarification"]
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
        payload = {
            "goal": goal,
            "success_criteria": [item.model_dump(mode="json") for item in success_criteria],
            "output_contract": _planner_output_contract(allowed_step_kinds),
            "capabilities": [snapshot.model_view for snapshot in executable_capabilities],
            "input_resources": [dict(item) for item in input_resources],
            "limits": {
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "max_tool_calls_semantics": (
                    "tool.read、tool.write、knowledge 与 explore 内部实际能力调用总和"
                ),
                "max_model_calls": self.max_model_calls,
                "max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens,
                "max_total_tokens": self.max_total_tokens,
                "max_runtime_seconds": self.max_runtime_seconds,
                "allowed_step_kinds": allowed_step_kinds,
            },
        }
        raw = self.client.generate_json(_PLANNER_SYSTEM_PROMPT, payload)
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
        )
        _validate_plan_capabilities(plan, executable_capabilities)
        _validate_plan_convergence(plan)
        return plan


_PLANNER_SYSTEM_PROMPT = """你是共格·序伴的受控动态任务规划器。只输出一个完整 JSON object。
你只能使用输入中列出的能力；external_write 只能规划为 tool.write，运行时会冻结参数并等待一次性人工批准。
不得提出执行、删除、权限变更或输入中不存在的能力。步骤种类必须来自 limits.allowed_step_kinds。
draft_id 只用于本次草案依赖，持久 step key 由服务端生成。
必须严格按 output_contract 输出顶层字段，禁止增加 plan/draft/result 等包装层或使用 id 替代 draft_id。
不得输出 tenant、agent、授权结论、凭据、URL、header、预算覆盖或未提供的能力。
计划必须有界、无环并覆盖成功标准；必须且只能有一个最终 answer 步骤，所有 required 步骤都必须是该
answer 的直接或间接前置，answer 之后不得再有步骤。"""

_PLANNER_OUTPUT_CONTRACT = {
    "goal": "原样返回输入 goal",
    "success_criteria": "原样返回输入 success_criteria 数组",
    "constraints": ["string，可为空数组"],
    "assumptions": ["string，可为空数组"],
    "steps": [
        {
            "draft_id": "字母开头的本次草案步骤标识",
            "title": "string",
            "kind": "tool.read | tool.write | knowledge | explore | answer | clarification",
            "required": True,
            "depends_on": ["已声明 draft_id"],
            "capability_refs": ["输入 capabilities 中的 name"],
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
        and item.contract.get("risk_class") == "external_write"
    }
    knowledge_names = {
        item.name for item in capabilities if item.capability_type == "knowledge"
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
                raise ValueError("动态计划引用了未冻结的外部写能力")
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
