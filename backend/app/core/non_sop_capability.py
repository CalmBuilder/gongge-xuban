"""
@Time       : 2026/08/03 18:40
@Author     : zhanglp8181
@File       : non_sop_capability.py
@CallChain  : AgentLoop 非 SOP 分支 → NonSopCapabilityRouter → GeneralSkill/动态任务 shadow
@Description: 统一普通回答、通用技能和动态任务 shadow 的兼容决策，确保 A 批不改变执行行为。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app import paths
from app.db.models import GeneralSkill, ModelConfig
from app.general_skills.schema import GeneralSkillSelection
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import TURN_STAGE_MESSAGES_KEY, stage_payload, unified_system_prompt
from app.observability.spans import llm_operation


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "non_sop_capability_prompt.md"
NON_SOP_CAPABILITY_OUTPUT = {
    "mode": "answer | dynamic_task | clarify",
    "goal": "string?",
    "success_criteria": "string[]",
    "requires_durable_execution": "boolean",
    "requires_artifact": "boolean",
    "capability_hints": "string[]",
    "clarification": "string?",
    "execution_intent": "none | continue | steer | new_task | cancel",
    "confidence": "number",
    "reason": "string?",
}


class GeneralCapabilitySelector(Protocol):
    """约束现有 GeneralSkillSelector 的最小调用契约。"""

    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillSelection:
        """返回旧链路仍然权威的通用能力选择。"""


class DynamicTaskShadowSelector(Protocol):
    """约束 A 批动态任务 shadow 选择器的最小调用契约。"""

    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        knowledge_capability: dict[str, object],
    ) -> NonSopCapabilityDecision:
        """提出不具备执行权限的动态任务 shadow 决策。"""


class NonSopCapabilityDecision(BaseModel):
    """表达非 SOP 能力模式；A 批中的 dynamic_task 只能用于 shadow 审计。"""

    mode: Literal["answer", "general_skill", "dynamic_task", "clarify"] = "answer"
    selected_general_skill_slug: str | None = None
    goal: str | None = None
    success_criteria: list[str] = Field(default_factory=list)
    requires_durable_execution: bool = False
    requires_artifact: bool = False
    capability_hints: list[str] = Field(default_factory=list)
    knowledge_mode: Literal["auto", "required", "disabled"] = "auto"
    knowledge_query: str | None = None
    clarification: str | None = None
    execution_intent: Literal["none", "continue", "steer", "new_task", "cancel"] = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str | None = None
    degraded: bool = False
    failure_code: str | None = None


@dataclass(frozen=True)
class NonSopCapabilityRouteResult:
    """同时携带旧链路权威决策和不可执行的 shadow 观测结果。"""

    selected_general_skill: GeneralSkill | None
    general_selection: GeneralSkillSelection
    effective_decision: NonSopCapabilityDecision
    shadow_decision: NonSopCapabilityDecision | None
    shadow_attempted: bool
    shadow_duration_ms: float
    execution_created: bool = False

    def audit_payload(self) -> dict[str, object]:
        """生成固定白名单审计字段，禁止输出用户输入、目标、理由或上下文正文。"""

        shadow = self.shadow_decision
        confidence = shadow.confidence if shadow is not None else 0.0
        if confidence >= 0.8:
            confidence_bucket = "high"
        elif confidence >= 0.5:
            confidence_bucket = "medium"
        else:
            confidence_bucket = "low"
        return {
            "effective_mode": self.effective_decision.mode,
            "shadow_mode": shadow.mode if shadow is not None else None,
            "execution_intent": shadow.execution_intent if shadow is not None else "none",
            "knowledge_mode": self.general_selection.knowledge_mode,
            "confidence_bucket": confidence_bucket,
            "requires_durable_execution": bool(
                shadow and shadow.requires_durable_execution
            ),
            "requires_artifact": bool(shadow and shadow.requires_artifact),
            "degraded": bool(shadow and shadow.degraded),
            "failure_code": shadow.failure_code if shadow is not None else None,
            "shadow_attempted": self.shadow_attempted,
            "shadow_duration_ms": round(max(0.0, self.shadow_duration_ms), 3),
            "execution_created": self.execution_created,
        }


class LlmDynamicTaskShadowSelector:
    """调用受限超时的模型，仅判断动态任务候选，不执行任何能力。"""

    def __init__(self, timeout_seconds: float) -> None:
        """保存 shadow 专用超时，避免观测调用长期阻塞旧链路。"""

        self.timeout_seconds = max(0.1, float(timeout_seconds))

    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        knowledge_capability: dict[str, object],
    ) -> NonSopCapabilityDecision:
        """基于能力元数据提出 answer/dynamic_task/clarify shadow 决策。"""

        selector_context = copy.deepcopy(
            conversation_context if isinstance(conversation_context, dict) else {}
        )
        selector_context.pop(TURN_STAGE_MESSAGES_KEY, None)
        payload = stage_payload(
            phase="Router / Dynamic Task Shadow",
            user_message=query,
            conversation_context=selector_context,
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data={
                "general_skills": [
                    {
                        "slug": skill.slug,
                        "name": skill.name,
                        "description": skill.description,
                        "requested_tools": list(
                            skill.permissions_json.get("requested_tools", [])
                            if isinstance(skill.permissions_json, dict)
                            else []
                        ),
                    }
                    for skill in general_skills
                    if skill.status == "published"
                ],
                "knowledge_capability": knowledge_capability,
            },
            output_contract=NON_SOP_CAPABILITY_OUTPUT,
        )
        with llm_operation("dynamic_task.route_shadow"):
            raw = LLMClient(
                model_config,
                timeout_seconds=self.timeout_seconds,
            ).generate_json(unified_system_prompt(), payload)
        return NonSopCapabilityDecision.model_validate(raw)


class NonSopCapabilityRouter:
    """在不改变旧行为的前提下，统一计算非 SOP 权威选择和动态任务 shadow。"""

    def __init__(
        self,
        *,
        shadow_enabled: bool,
        execution_enabled: bool = False,
        shadow_selector: DynamicTaskShadowSelector,
        minimum_confidence: float = 0.7,
    ) -> None:
        """冻结 shadow/执行 kill switch、选择器和最低采信置信度。"""

        self.shadow_enabled = bool(shadow_enabled)
        self.execution_enabled = bool(execution_enabled)
        self.shadow_selector = shadow_selector
        self.minimum_confidence = min(1.0, max(0.0, float(minimum_confidence)))

    def decide(
        self,
        *,
        message: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        general_skill_selector: GeneralCapabilitySelector,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        knowledge_capability: dict[str, object],
        forced_general_skill: GeneralSkill | None = None,
    ) -> NonSopCapabilityRouteResult:
        """分别决定 Skill 与执行模式；结构化强制 Skill 不再短路动态任务判断。"""

        selector_context = dict(conversation_context or {})
        selector_context["knowledge_capability"] = knowledge_capability
        if forced_general_skill is not None:
            selection = GeneralSkillSelection(
                use_general_skill=True,
                selected_slug=forced_general_skill.slug,
                confidence=1.0,
                reason="用户通过结构化会话入口显式选择 Skill。",
            )
        else:
            try:
                selection = general_skill_selector.decide(
                    message,
                    general_skills,
                    model_config,
                    selector_context,
                    memory_context,
                )
            except LLMError as exc:
                selection = GeneralSkillSelection(
                    knowledge_mode="auto",
                    reason=f"Capability selection failed: {exc}",
                    degraded=True,
                    failure_code="capability_selection_failed",
                )

        selected_skill = forced_general_skill or self._resolve_selected_general_skill(
            selection, general_skills
        )
        if selection.use_general_skill and selected_skill is None:
            selection = selection.model_copy(
                update={"use_general_skill": False, "selected_slug": None}
            )
        effective = self._effective_decision(selection, selected_skill)
        if (
            self.execution_enabled
            and selected_skill is not None
            and selection.knowledge_mode == "required"
        ):
            effective = NonSopCapabilityDecision(
                mode="dynamic_task",
                goal=message.strip(),
                success_criteria=["依据当前有权访问的企业知识形成可追溯回答"],
                requires_durable_execution=True,
                knowledge_mode="required",
                knowledge_query=selection.knowledge_query or message.strip(),
                confidence=1.0,
                reason="Skill 与必需企业知识必须在受控动态任务中组合消费。",
            )
        if not self.shadow_enabled and not self.execution_enabled:
            return NonSopCapabilityRouteResult(
                selected_general_skill=selected_skill,
                general_selection=selection,
                effective_decision=effective,
                shadow_decision=None,
                shadow_attempted=False,
                shadow_duration_ms=0.0,
            )

        if selected_skill is not None and not self.execution_enabled:
            shadow = effective.model_copy()
            return NonSopCapabilityRouteResult(
                selected_general_skill=selected_skill,
                general_selection=selection,
                effective_decision=effective,
                shadow_decision=shadow,
                shadow_attempted=False,
                shadow_duration_ms=0.0,
            )

        started = perf_counter()
        try:
            shadow = self.shadow_selector.decide(
                message,
                general_skills,
                model_config,
                conversation_context,
                memory_context,
                knowledge_capability,
            )
            shadow = self._validate_shadow(shadow, selection)
        except Exception:
            shadow = NonSopCapabilityDecision(
                mode="answer",
                knowledge_mode=selection.knowledge_mode,
                knowledge_query=selection.knowledge_query,
                degraded=True,
                failure_code="dynamic_shadow_failed",
            )
        duration_ms = (perf_counter() - started) * 1000
        if self.execution_enabled and shadow.mode == "dynamic_task":
            effective = shadow
        return NonSopCapabilityRouteResult(
            selected_general_skill=selected_skill,
            general_selection=selection,
            effective_decision=effective,
            shadow_decision=shadow,
            shadow_attempted=True,
            shadow_duration_ms=duration_ms,
        )

    @staticmethod
    def _resolve_selected_general_skill(
        selection: GeneralSkillSelection,
        general_skills: list[GeneralSkill],
    ) -> GeneralSkill | None:
        """只从服务端提供的已发布候选中解析模型返回的 slug。"""

        if not selection.use_general_skill or not selection.selected_slug:
            return None
        return next(
            (
                skill
                for skill in general_skills
                if skill.status == "published" and skill.slug == selection.selected_slug
            ),
            None,
        )

    @staticmethod
    def _effective_decision(
        selection: GeneralSkillSelection,
        selected_skill: GeneralSkill | None,
    ) -> NonSopCapabilityDecision:
        """把旧选择结果投影为 A 批权威决策，永远不产生 dynamic_task。"""

        return NonSopCapabilityDecision(
            mode="general_skill" if selected_skill is not None else "answer",
            selected_general_skill_slug=(selected_skill.slug if selected_skill else None),
            knowledge_mode=selection.knowledge_mode,
            knowledge_query=selection.knowledge_query,
            confidence=selection.confidence,
            reason=selection.reason,
            degraded=selection.degraded,
            failure_code=selection.failure_code,
        )

    def _validate_shadow(
        self,
        shadow: NonSopCapabilityDecision,
        selection: GeneralSkillSelection,
    ) -> NonSopCapabilityDecision:
        """应用置信度和字段完整性门禁，并以服务端知识选择覆盖模型声明。"""

        normalized = shadow.model_copy(
            update={
                "selected_general_skill_slug": None,
                "knowledge_mode": selection.knowledge_mode,
                "knowledge_query": selection.knowledge_query,
            }
        )
        if normalized.mode == "general_skill":
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": "dynamic_shadow_invalid_mode",
                }
            )
        if normalized.mode == "dynamic_task" and normalized.confidence < self.minimum_confidence:
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": "dynamic_shadow_low_confidence",
                }
            )
        if normalized.mode == "dynamic_task" and (
            not str(normalized.goal or "").strip() or not normalized.success_criteria
        ):
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": "dynamic_shadow_incomplete",
                }
            )
        if normalized.mode == "clarify" and not str(normalized.clarification or "").strip():
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": "dynamic_shadow_incomplete",
                }
            )
        return normalized
