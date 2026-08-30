"""
@Time       : 2026/08/28 13:20
@Author     : zhanglp8181
@File       : non_sop_capability.py
@CallChain  : AgentLoop 非 SOP 分支 → NonSopCapabilityRouter → GeneralSkill/动态任务主路由与 shadow
@Description: 统一普通回答、通用技能、动态任务主路由和可选 shadow 观测决策。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app import paths
from app.cancellation import TurnCancellationRequested, raise_if_cancelled
from app.core.context_projection import compact_conversation_context
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
        is_cancelled: Callable[[], bool] | None = None,
    ) -> GeneralSkillSelection:
        """返回旧链路仍然权威的通用能力选择。"""


class DynamicTaskSelector(Protocol):
    """约束动态任务主路由和 shadow 选择器的最小调用契约。"""

    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        knowledge_capability: dict[str, object],
        is_cancelled: Callable[[], bool] | None = None,
    ) -> NonSopCapabilityDecision:
        """提出结构化动态任务决策；调用方负责执行前的能力与风险校验。"""


class DynamicTaskShadowSelector(DynamicTaskSelector, Protocol):
    """保留旧类型名，供只读 shadow 适配器和外部测试兼容使用。"""


class NonSopCapabilityDecision(BaseModel):
    """表达非 SOP 能力模式，dynamic_task 可作为主路由或 shadow 观测结果。"""

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
    """同时携带普通 Skill 选择、动态主路由和可选 shadow 观测结果。"""

    selected_general_skill: GeneralSkill | None
    general_selection: GeneralSkillSelection
    effective_decision: NonSopCapabilityDecision
    shadow_decision: NonSopCapabilityDecision | None
    shadow_attempted: bool
    shadow_duration_ms: float
    execution_created: bool = False
    primary_decision: NonSopCapabilityDecision | None = None
    primary_attempted: bool = False
    primary_duration_ms: float = 0.0

    def audit_payload(self) -> dict[str, object]:
        """生成固定白名单审计字段，禁止输出用户输入、目标、理由或上下文正文。"""

        shadow = self.shadow_decision
        primary = self.primary_decision
        observed = primary or shadow
        confidence = observed.confidence if observed is not None else 0.0
        if confidence >= 0.8:
            confidence_bucket = "high"
        elif confidence >= 0.5:
            confidence_bucket = "medium"
        else:
            confidence_bucket = "low"
        payload = {
            "effective_mode": self.effective_decision.mode,
            "shadow_mode": shadow.mode if shadow is not None else None,
            "execution_intent": observed.execution_intent if observed is not None else "none",
            "knowledge_mode": self.general_selection.knowledge_mode,
            "confidence_bucket": confidence_bucket,
            "requires_durable_execution": bool(
                observed and observed.requires_durable_execution
            ),
            "requires_artifact": bool(observed and observed.requires_artifact),
            "degraded": bool(observed and observed.degraded),
            "failure_code": observed.failure_code if observed is not None else None,
            "shadow_attempted": self.shadow_attempted,
            "shadow_duration_ms": round(max(0.0, self.shadow_duration_ms), 3),
            "execution_created": self.execution_created,
        }
        if primary is not None or self.primary_attempted:
            payload.update(
                {
                    "primary_mode": primary.mode if primary is not None else None,
                    "primary_attempted": self.primary_attempted,
                    "primary_duration_ms": round(max(0.0, self.primary_duration_ms), 3),
                }
            )
        return payload


class LlmDynamicTaskSelector:
    """调用模型生成结构化动态任务决策，不在路由阶段执行任何能力。"""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        phase: str,
        operation: str,
    ) -> None:
        """保存阶段和超时，避免主路由或观测调用长期阻塞聊天链路。"""

        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.phase = phase
        self.operation = operation

    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        knowledge_capability: dict[str, object],
        is_cancelled: Callable[[], bool] | None = None,
    ) -> NonSopCapabilityDecision:
        """基于能力元数据提出 answer/dynamic_task/clarify 决策。"""

        raise_if_cancelled(is_cancelled)
        selector_context = copy.deepcopy(
            conversation_context if isinstance(conversation_context, dict) else {}
        )
        selector_context.pop(TURN_STAGE_MESSAGES_KEY, None)
        payload = stage_payload(
            phase=self.phase,
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
        with llm_operation(self.operation):
            raw = LLMClient(
                model_config,
                timeout_seconds=self.timeout_seconds,
            ).generate_json(
                unified_system_prompt(),
                payload,
                **(
                    {"is_cancelled": is_cancelled}
                    if is_cancelled is not None
                    else {}
                ),
            )
        raise_if_cancelled(is_cancelled)
        return NonSopCapabilityDecision.model_validate(raw)


class LlmDynamicTaskPrimarySelector(LlmDynamicTaskSelector):
    """DynamicTaskAgent 的正式主路由模型选择器。"""

    def __init__(self, timeout_seconds: float) -> None:
        """使用主路由阶段名和独立的阶段观测操作名。"""

        super().__init__(
            timeout_seconds,
            phase="Router / Dynamic Task",
            operation="dynamic_task.route_primary",
        )


class LlmDynamicTaskShadowSelector(LlmDynamicTaskSelector):
    """可选的只读比较路由选择器，永远不直接改变主执行语义。"""

    def __init__(self, timeout_seconds: float) -> None:
        """使用 shadow 专用阶段名和超时。"""

        super().__init__(
            timeout_seconds,
            phase="Router / Dynamic Task Shadow",
            operation="dynamic_task.route_shadow",
        )


class NonSopCapabilityRouter:
    """统一计算非 SOP 权威选择、动态主路由和可选 shadow。"""

    def __init__(
        self,
        *,
        shadow_enabled: bool,
        execution_enabled: bool = False,
        primary_selector: DynamicTaskSelector | None = None,
        shadow_selector: DynamicTaskShadowSelector | None = None,
        minimum_confidence: float = 0.7,
    ) -> None:
        """冻结主路由、shadow 观测开关、兼容选择器和最低采信置信度。"""

        self.shadow_enabled = bool(shadow_enabled)
        self.execution_enabled = bool(execution_enabled)
        legacy_selector_fallback = primary_selector is None and shadow_selector is not None
        self.primary_selector = primary_selector or (
            shadow_selector if self.execution_enabled else None
        )
        self.shadow_selector = (
            shadow_selector
            if (not legacy_selector_fallback or not self.execution_enabled)
            else None
        )
        if self.shadow_enabled and not self.execution_enabled and self.shadow_selector is None:
            self.shadow_selector = primary_selector
        if self.execution_enabled and self.primary_selector is None:
            raise ValueError("execution_enabled=True 时必须提供 primary_selector")
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
        is_cancelled: Callable[[], bool] | None = None,
    ) -> NonSopCapabilityRouteResult:
        """分别决定 Skill 与执行模式；结构化强制 Skill 不再短路动态任务判断。"""

        raise_if_cancelled(is_cancelled)
        selector_context = compact_conversation_context(conversation_context)
        selector_context["knowledge_capability"] = knowledge_capability
        # 没有任何已发布通用 Skill 且成员/Agent 知识交集为空时，选择器没有可消费
        # 的候选，也没有需要判断的知识；跳过一次完整远程调用，避免在 Dynamic
        # shadow/规划前白白消耗 600 秒模型阶段预算。只要存在 Skill 或知识，仍走
        # 原选择器以保持知识自动/必需语义不变。
        if forced_general_skill is not None:
            selection = GeneralSkillSelection(
                use_general_skill=True,
                selected_slug=forced_general_skill.slug,
                confidence=1.0,
                reason="用户通过结构化会话入口显式选择 Skill。",
            )
        elif not general_skills and not bool(knowledge_capability.get("available")):
            selection = GeneralSkillSelection(
                use_general_skill=False,
                knowledge_mode="auto",
                reason="当前没有可用通用 Skill 或企业知识，直接进入普通/动态能力判断。",
            )
        else:
            try:
                selection_kwargs = (
                    {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
                )
                selection = general_skill_selector.decide(
                    message,
                    general_skills,
                    model_config,
                    selector_context,
                    memory_context,
                    **selection_kwargs,
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

        primary: NonSopCapabilityDecision | None = None
        primary_attempted = False
        primary_duration_ms = 0.0
        if self.execution_enabled:
            primary, primary_attempted, primary_duration_ms = self._run_selector(
                self.primary_selector,
                failure_code="dynamic_primary_failed",
                message=message,
                general_skills=general_skills,
                model_config=model_config,
                selector_context=selector_context,
                memory_context=memory_context,
                knowledge_capability=knowledge_capability,
                selection=selection,
                is_cancelled=is_cancelled,
            )
            if primary is not None:
                if primary.mode in {"dynamic_task", "clarify"}:
                    effective = primary
                elif selected_skill is None and effective.mode != "dynamic_task":
                    effective = primary

        shadow: NonSopCapabilityDecision | None = None
        shadow_attempted = False
        shadow_duration_ms = 0.0
        if self.shadow_enabled and self.shadow_selector is not None:
            shadow, shadow_attempted, shadow_duration_ms = self._run_selector(
                self.shadow_selector,
                failure_code="dynamic_shadow_failed",
                message=message,
                general_skills=general_skills,
                model_config=model_config,
                selector_context=selector_context,
                memory_context=memory_context,
                knowledge_capability=knowledge_capability,
                selection=selection,
                is_cancelled=is_cancelled,
            )
        return NonSopCapabilityRouteResult(
            selected_general_skill=selected_skill,
            general_selection=selection,
            effective_decision=effective,
            shadow_decision=shadow,
            shadow_attempted=shadow_attempted,
            shadow_duration_ms=shadow_duration_ms,
            primary_decision=primary,
            primary_attempted=primary_attempted,
            primary_duration_ms=primary_duration_ms,
        )

    def _run_selector(
        self,
        selector: DynamicTaskSelector | None,
        *,
        failure_code: str,
        message: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        selector_context: dict[str, object],
        memory_context: list[dict[str, object]] | None,
        knowledge_capability: dict[str, object],
        selection: GeneralSkillSelection,
        is_cancelled: Callable[[], bool] | None,
    ) -> tuple[NonSopCapabilityDecision | None, bool, float]:
        """执行一次主路由或 shadow 调用并统一记录失败降级事实。"""

        if selector is None:
            return None, False, 0.0
        started = perf_counter()
        try:
            raise_if_cancelled(is_cancelled)
            decision = selector.decide(
                message,
                general_skills,
                model_config,
                selector_context,
                memory_context,
                knowledge_capability,
                **(
                    {"is_cancelled": is_cancelled}
                    if is_cancelled is not None
                    else {}
                ),
            )
            decision = self._validate_decision(decision, selection, failure_code)
        except TurnCancellationRequested:
            raise
        except Exception:
            decision = NonSopCapabilityDecision(
                mode="answer",
                knowledge_mode=selection.knowledge_mode,
                knowledge_query=selection.knowledge_query,
                degraded=True,
                failure_code=failure_code,
            )
        duration_ms = (perf_counter() - started) * 1000
        raise_if_cancelled(is_cancelled)
        return decision, True, duration_ms

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

    def _validate_decision(
        self,
        shadow: NonSopCapabilityDecision,
        selection: GeneralSkillSelection,
        failure_code: str,
    ) -> NonSopCapabilityDecision:
        """应用主路由和 shadow 共用的结构化完整性、置信度和知识范围校验。"""

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
                    "failure_code": failure_code.replace("_failed", "_invalid_mode"),
                }
            )
        if normalized.mode == "dynamic_task" and normalized.confidence < self.minimum_confidence:
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": failure_code.replace("_failed", "_low_confidence"),
                }
            )
        if normalized.mode == "dynamic_task" and (
            not str(normalized.goal or "").strip() or not normalized.success_criteria
        ):
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": failure_code.replace("_failed", "_incomplete"),
                }
            )
        if normalized.mode == "clarify" and not str(normalized.clarification or "").strip():
            return normalized.model_copy(
                update={
                    "mode": "answer",
                    "degraded": True,
                    "failure_code": failure_code.replace("_failed", "_incomplete"),
                }
            )
        return normalized
