"""
@Time       : 2026/08/04 01:04
@Author     : zhanglp8181
@File       : planning.py
@CallChain  : DynamicTaskAgent/FormalSopPlanner → normalized plan/proposal → SopExecutionStore
@Description: 定义统一计划、步骤、完整模型提案及正式 SOP 计划投影契约。
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from collections.abc import Mapping
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
            if artifact.get("mime_type") != "text/markdown":
                raise ValueError("首期只允许声明 Markdown Artifact")
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
    steps: tuple[DynamicPlanDraftStep, ...] = Field(min_length=1)

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
    return NormalizedPlan(
        goal=draft.goal,
        success_criteria=draft.success_criteria,
        constraints=draft.constraints,
        assumptions=draft.assumptions,
        steps=tuple(
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
        ),
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
