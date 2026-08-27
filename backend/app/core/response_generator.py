"""
@Time       : 2026/07/22 20:45
@Author     : zhanglp8181
@File       : response_generator.py
@CallChain  : AgentLoop → ResponseGenerator → Runtime直出或LLM回复生成
@Description: 生成用户可见回复，并确保受信 Runtime 控制消息不会再次进入模型。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

from app import paths
from app.core.context_projection import (
    compact_citation_hints,
    compact_current_step,
    compact_knowledge_context,
    compact_response_step_result,
)
from app.db.models import ChatSession, ModelConfig, Skill
from app.knowledge.citations import knowledge_citations_from_results
from app.llm import LLMClient
from app.llm.client import LLMStreamCancelled
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "response_generator_prompt.md"
FALLBACK_REPLY = "抱歉，我暂时无法处理这个问题。您可以换个说法，或者我可以帮您转人工。"
MODEL_FAILURE_SUGGESTION = "请检查模型配置、API Key、网络或模型服务状态后重试。"
TOOL_FAILURE_SUGGESTION = "请检查工具配置、调用参数或外部服务状态后重试。"
RUNTIME_CONTROL_FALLBACK = "流程已被系统规则终止，请查看执行记录或联系管理员。"
GUIDANCE_APPLICATION_CONTRACT = {
    "version": "guidance-application-checkpoint-v1",
    "required_checks": [
        "select_relevant_branches",
        "apply_triggered_pointers_and_information_hierarchy",
        "state_checkable_completion_criteria",
        "preserve_conflicts_safety_and_unresolved_state",
    ],
    "document_guidance_output_shape": [
        "保留输入材料中与触发分支对应的具体路径或文档指针",
        "让每个触发分支与其权威指针相邻，并把步骤与参考资料分层",
        "显式写出完成标准以及材料中的冲突、重复或未决状态",
    ],
    "conditional_principle_application": [
        "如果已加载 guidance 正文或其受审资源定义了术语、词汇表或设计原则，且当前问题确实涉及该原则，使用相关原词并说明它如何改变当前材料中的决策；不要只复述术语。",
        "如果 guidance 明确包含 Design It Twice、替代接口或方案比较要求，针对设计/重构/评审问题列出至少两个清楚标记的候选方案，说明权衡并给出有依据的推荐；材料未提供的事实仍保持未决。",
        "如果 guidance 明确包含 deep module、deletion test、interface is the test surface、seam 或 adapter，针对设计/重构/评审问题分别说明适用的模块深度、删除测试、真实 seam/适配器和接口测试面；不把这些原则套到无关问题。",
    ],
    "no_op_rule": "未触发的 guidance 分支不套用、不编造",
    "visibility": "仅用于内部回复检查，不输出契约、Skill 身份或加载过程",
}


def public_error_detail(value: object, fallback: str = "未知原因") -> str:
    detail = re.sub(r"\s+", " ", str(value or "")).strip()
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", detail)
    detail = re.sub(r"\bpt-[A-Za-z0-9_-]{8,}\b", "pt-***", detail)
    if not detail:
        detail = fallback
    return detail[:500]


def format_runtime_failure_reply(
    title: str,
    detail: object,
    code: str | None = None,
    suggestion: str | None = None,
) -> str:
    normalized_detail = public_error_detail(detail)
    normalized_code = public_error_detail(code, "").strip()
    code_part = f"（{normalized_code}）" if normalized_code else ""
    normalized_detail = normalized_detail.rstrip("。.!！")
    suffix = (suggestion or "请稍后重试，或联系管理员查看执行记录。").strip()
    return f"{title}{code_part}：{normalized_detail}。{suffix}"


def model_failure_suggestion(detail: object) -> str:
    return MODEL_FAILURE_SUGGESTION


def tool_failure_reply(tool_result: ToolResult) -> str:
    error = tool_result.error
    code = error.code if error else None
    detail = error.message if error else "工具未返回可用结果"
    return format_runtime_failure_reply(
        f"工具调用失败：{tool_result.tool_name}",
        detail,
        code,
        TOOL_FAILURE_SUGGESTION,
    )


class ResponseGenerator:
    def requires_model_call(
        self,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        task_results: list[dict[str, object]] | None = None,
        *,
        force_model_call: bool = False,
        conversation_context: dict[str, object] | None = None,
    ) -> bool:
        """在外发附件前机械判断本次回复是否确实会调用模型。"""

        if self._runtime_control_reply(step_result) is not None:
            return False
        has_authoritative_attachment = bool(
            self._authoritative_attachment_evidence(conversation_context)
        )
        if not force_model_call and self._can_use_step_reply_directly(
            step_result, tool_result, task_results
        ) and not has_authoritative_attachment:
            # 附件切片已经由宿主 Runtime 读取并绑定到本轮时，不能因为模型偶尔
            # 将步骤判成 ask_user/clarify 就本地直出。否则浏览器会看到回答，
            # 但 provider dispatch ledger 没有授权与结算；这违背附件的
            # identity/authorization/settlement 契约。Runtime 控制回复在上方
            # 已 fail-closed 返回，不会被迫进入模型。
            return False
        if tool_result and not tool_result.success and not task_results:
            # 工具失败通常直接返回固定错误，不能把没有发生的模型外呼登记为已授权。
            # 但本轮若存在已读取的附件切片，固定错误会掩盖用户真正要求回答的附件事实；
            # 回复阶段必须继续调用模型消费权威切片，失败时由上层把派发组标记为 unknown。
            return bool(self._authoritative_attachment_evidence(conversation_context))
        return True

    def generate(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
        propagate_model_failure: bool = False,
        force_model_call: bool = False,
    ) -> str:
        runtime_control_reply = self._runtime_control_reply(step_result)
        if runtime_control_reply is not None:
            return runtime_control_reply
        if not force_model_call and self._can_use_step_reply_directly(
            step_result, tool_result, task_results
        ):
            return step_result.reply.strip()
        raw_payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
        )
        payload = self._stage_payload(raw_payload, persona_prompt)
        try:
            if (
                tool_result
                and not tool_result.success
                and not task_results
                and not self._authoritative_attachment_evidence(conversation_context)
            ):
                return tool_failure_reply(tool_result)
            with llm_operation("response.generate"):
                text = LLMClient(model_config).generate_text(
                    unified_system_prompt(), payload
                )
            reply = text.strip() or step_result.reply or self._minimal_fallback(router_decision)
            return self._visible_reply_or_fallback(
                reply, session, router_decision, step_result, tool_result, skill
            )
        except Exception as exc:
            if propagate_model_failure:
                raise
            return format_runtime_failure_reply("模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc))

    def generate_stream(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        force_model_call: bool = False,
        propagate_model_failure: bool = False,
    ) -> Iterator[str]:
        """流式生成用户回复，并在 provider 首 token 前及每个 chunk 边界响应取消。"""

        if is_cancelled and is_cancelled():
            raise LLMStreamCancelled("LLM stream cancelled before provider request")
        runtime_control_reply = self._runtime_control_reply(step_result)
        if runtime_control_reply is not None:
            yield from self._cancelable_chunks(runtime_control_reply, is_cancelled)
            return
        if not force_model_call and self._can_use_step_reply_directly(
            step_result, tool_result, task_results
        ):
            yield from self._cancelable_chunks(step_result.reply or "", is_cancelled)
            return
        raw_payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
        )
        payload = self._stage_payload(raw_payload, persona_prompt)
        try:
            if (
                tool_result
                and not tool_result.success
                and not task_results
                and not self._authoritative_attachment_evidence(conversation_context)
            ):
                yield from self._cancelable_chunks(tool_failure_reply(tool_result), is_cancelled)
                return
            if router_decision.decision == "clarify" and step_result.reply:
                yield from self._cancelable_chunks(step_result.reply, is_cancelled)
                return
            with llm_operation("response.generate_stream"):
                client = LLMClient(model_config)
                stream = (
                    client.generate_text_stream(
                        unified_system_prompt(), payload, is_cancelled=is_cancelled
                    )
                    if is_cancelled is not None
                    else client.generate_text_stream(unified_system_prompt(), payload)
                )
                reply_parts: list[str] = []
                has_streamed = False
                for chunk in stream:
                    if not chunk:
                        continue
                    reply_parts.append(chunk)
                    if not has_streamed:
                        preview = "".join(reply_parts).strip()
                        if not preview:
                            continue
                        has_streamed = True
                    yield chunk
            if has_streamed:
                return
            reply = self._visible_reply_or_fallback(
                "".join(reply_parts).strip() or step_result.reply or self._minimal_fallback(router_decision),
                session,
                router_decision,
                step_result,
                tool_result,
                skill,
            )
            yield from self._cancelable_chunks(reply, is_cancelled)
            return
        except LLMStreamCancelled:
            raise
        except Exception as exc:
            if propagate_model_failure:
                raise
            yield from self.chunk_text(
                format_runtime_failure_reply("模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc))
            )

    def chunk_text(self, text: str, chunk_size: int = 8) -> Iterator[str]:
        stripped = text.strip()
        if not stripped:
            return
        for index in range(0, len(stripped), chunk_size):
            yield stripped[index : index + chunk_size]

    def _cancelable_chunks(
        self,
        text: str,
        is_cancelled: Callable[[], bool] | None,
    ) -> Iterator[str]:
        """分块交付本地回复时在每个 chunk 前重查 Turn 取消。"""

        for chunk in self.chunk_text(text):
            if is_cancelled and is_cancelled():
                raise LLMStreamCancelled("local reply delivery cancelled")
            yield chunk

    def _can_use_step_reply_directly(
        self,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        task_results: list[dict[str, object]] | None = None,
    ) -> bool:
        return bool(
            not task_results
            and str(step_result.reply or "").strip()
            and step_result.action in {"ask_user", "clarify"}
            and tool_result is None
            and step_result.tool_call is None
            and step_result.knowledge_query is None
            and not step_result.knowledge_results
        )

    @staticmethod
    def _runtime_control_reply(step_result: StepAgentResult) -> str | None:
        """返回 Runtime 权威文案；空文案使用固定兜底且绝不回落到模型。"""

        if not step_result.is_runtime_control_reply():
            return None
        return str(step_result.reply or "").strip() or RUNTIME_CONTROL_FALLBACK

    def _payload(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        projected_conversation_context = (
            dict(conversation_context) if isinstance(conversation_context, dict) else {}
        )
        loaded_general_skills = projected_conversation_context.pop("loaded_general_skills", [])
        if not isinstance(loaded_general_skills, list):
            loaded_general_skills = []
        authoritative_attachment_evidence = self._authoritative_attachment_evidence(
            projected_conversation_context
        )
        projected_task_results = self._project_task_results(task_results)
        if projected_task_results:
            payload: dict[str, object] = {
                "user_message": message,
                "conversation_context": projected_conversation_context,
                "task_results": projected_task_results,
            }
            if loaded_general_skills:
                payload["loaded_general_skills"] = loaded_general_skills
                payload["guidance_application_contract"] = dict(GUIDANCE_APPLICATION_CONTRACT)
            if authoritative_attachment_evidence:
                payload["authoritative_attachment_evidence"] = authoritative_attachment_evidence
            return payload
        knowledge_context = self._current_knowledge_context(message, session, step_result)
        compact_knowledge = compact_knowledge_context(knowledge_context)
        payload = {
            "user_message": message,
            "conversation_context": projected_conversation_context,
            "current_step": compact_current_step(
                skill.content_json if skill else None, session.active_step_id
            ),
            "progress": self._progress_payload(session, skill, step_result, tool_result),
            "slots": session.slots_json or {},
            "step_summary": compact_response_step_result(
                step_result.model_dump(mode="json")
            ),
            "tool_result": tool_result.model_dump() if tool_result else None,
            "retrieved_knowledge": compact_knowledge,
            "knowledge_citation_hints": compact_citation_hints(
                knowledge_citations_from_results(knowledge_context)
            ),
            "response_rules": skill.content_json.get("response_rules", []) if skill else [],
        }
        if loaded_general_skills:
            payload["loaded_general_skills"] = loaded_general_skills
            payload["guidance_application_contract"] = dict(GUIDANCE_APPLICATION_CONTRACT)
        if authoritative_attachment_evidence:
            payload["authoritative_attachment_evidence"] = authoritative_attachment_evidence
        return payload

    @staticmethod
    def _authoritative_attachment_evidence(
        conversation_context: dict[str, object] | None,
    ) -> list[dict[str, object]]:
        """把本轮已授权读取的附件切片投影到回复阶段，避免模型只看到历史摘要而漏读附件。"""

        if not isinstance(conversation_context, dict):
            return []
        raw_inputs = conversation_context.get("current_turn_inputs")
        if not isinstance(raw_inputs, list):
            return []
        evidence: list[dict[str, object]] = []
        for raw_input in raw_inputs:
            if not isinstance(raw_input, dict):
                continue
            elements: list[dict[str, object]] = []
            raw_elements = raw_input.get("elements")
            if isinstance(raw_elements, list):
                for raw_element in raw_elements:
                    if not isinstance(raw_element, dict):
                        continue
                    element = {
                        key: raw_element[key]
                        for key in (
                            "element_id",
                            "type",
                            "text",
                            "table",
                            "locator",
                            "content_checksum",
                        )
                        if key in raw_element
                    }
                    if element:
                        elements.append(element)
            if not elements:
                continue
            evidence.append(
                {
                    key: raw_input[key]
                    for key in (
                        "filename",
                        "mime_type",
                        "instruction_boundary",
                        "read_receipt_id",
                        "slice_checksum",
                    )
                    if key in raw_input
                }
                | {"elements": elements}
            )
        return evidence

    def _project_task_results(
        self, task_results: list[dict[str, object]] | None
    ) -> list[dict[str, object]]:
        if not isinstance(task_results, list):
            return []
        projected: list[dict[str, object]] = []
        for item in task_results:
            if not isinstance(item, dict):
                continue
            skill_content = item.get("skill_content")
            content = skill_content if isinstance(skill_content, dict) else {}
            raw_step_result = item.get("step_result")
            step_result = raw_step_result if isinstance(raw_step_result, dict) else {}
            knowledge_context = (
                step_result.get("knowledge_results")
                if isinstance(step_result.get("knowledge_results"), list)
                else []
            )
            compact_knowledge = compact_knowledge_context(knowledge_context)
            projected.append(
                {
                    "task": item.get("task") or "当前任务",
                    "current_step": compact_current_step(
                        content, str(item.get("current_step_id") or "") or None
                    ),
                    "slots": item.get("slots") if isinstance(item.get("slots"), dict) else {},
                    "step_summary": compact_response_step_result(step_result),
                    "tool_result": item.get("tool_result"),
                    "retrieved_knowledge": compact_knowledge,
                    "knowledge_citation_hints": compact_citation_hints(
                        knowledge_citations_from_results(knowledge_context)
                    ),
                    "response_rules": content.get("response_rules", []),
                }
            )
        return projected

    def _current_knowledge_context(
        self,
        message: str,
        session: ChatSession,
        step_result: StepAgentResult,
    ) -> list[dict[str, object]]:
        if step_result.knowledge_results:
            return list(step_result.knowledge_results)
        return []

    def _visible_reply_or_fallback(
        self,
        reply: str,
        session: ChatSession,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        skill: Skill | None = None,
    ) -> str:
        completion_ready = self._skill_completion_ready(session, skill, step_result, tool_result)
        completion_fallback = self._completion_fallback() if completion_ready else ""
        prefer_step_reply = bool(step_result.reply and router_decision.decision == "clarify")
        candidates = self._reply_candidates(
            reply,
            step_result.reply or "",
            completion_fallback,
            self._minimal_fallback_for_session(session),
            tool_result,
            completion_ready,
            prefer_step_reply,
        )
        for candidate in candidates:
            stripped = candidate.strip()
            if not stripped:
                continue
            return stripped
        return FALLBACK_REPLY

    def _reply_candidates(
        self,
        model_reply: str,
        step_reply: str,
        completion_fallback: str,
        session_fallback: str,
        tool_result: ToolResult | None,
        completion_ready: bool,
        prefer_step_reply: bool,
    ) -> tuple[str, ...]:
        if prefer_step_reply:
            return (
                step_reply,
                model_reply,
                completion_fallback,
                session_fallback,
                FALLBACK_REPLY,
            )
        if completion_ready:
            return (
                model_reply,
                completion_fallback,
                step_reply,
                session_fallback,
                FALLBACK_REPLY,
            )
        if tool_result is not None:
            return (
                model_reply,
                step_reply,
                completion_fallback,
                session_fallback,
                FALLBACK_REPLY,
            )
        return (
            model_reply,
            step_reply,
            completion_fallback,
            session_fallback,
            FALLBACK_REPLY,
        )

    def _progress_payload(
        self,
        session: ChatSession,
        skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> dict[str, object]:
        if not skill:
            return {
                "missing_current_step_info": [],
                "missing_required_info": [],
                "skill_completion_ready": False,
            }
        return {
            "missing_current_step_info": self._missing_current_step_info(session, skill),
            "missing_required_info": self._missing_required_info(session, skill),
            "skill_completion_ready": self._skill_completion_ready(session, skill, step_result, tool_result),
            "step_completed": step_result.is_step_completed,
        }

    def _skill_completion_ready(
        self,
        session: ChatSession,
        skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if not skill or not step_result.is_step_completed:
            return False
        if tool_result and not tool_result.success:
            return False
        return not self._missing_current_step_info(session, skill) and not self._missing_required_info(session, skill)

    def _missing_current_step_info(self, session: ChatSession, skill: Skill) -> list[str]:
        step = self._current_step(session, skill)
        if not step:
            return []
        return [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._slot_has_value(session.slots_json or {}, str(field))
        ]

    def _missing_required_info(self, session: ChatSession, skill: Skill) -> list[str]:
        return [
            str(field)
            for field in (skill.content_json or {}).get("required_info", [])
            if not self._slot_has_value(session.slots_json or {}, str(field))
        ]

    def _current_step(self, session: ChatSession, skill: Skill) -> dict | None:
        for node in (skill.content_json or {}).get("nodes", []):
            if isinstance(node, dict) and node.get("node_id") == session.active_step_id:
                return {
                    "step_id": node.get("node_id"),
                    "node_id": node.get("node_id"),
                    "name": node.get("name"),
                    "instruction": node.get("instruction"),
                    "expected_user_info": node.get("expected_user_info", []),
                    "allowed_actions": node.get("allowed_actions", []),
                }
        return None

    def _slot_has_value(self, slots: dict, field: str) -> bool:
        value = slots.get(field)
        return value is not None and value != ""

    def _completion_fallback(self) -> str:
        return "已记录完整信息。请问还有其他需要帮助的吗？"

    def _minimal_fallback_for_session(self, session: ChatSession) -> str:
        return "请您再补充一下具体诉求，我会继续帮您处理。"

    def _minimal_fallback(self, router_decision: RouterDecision) -> str:
        if router_decision.decision == "clarify" and router_decision.clarification_question:
            return router_decision.clarification_question
        return FALLBACK_REPLY

    def _system_prompt(self, persona_prompt: str | None) -> str:
        return unified_system_prompt()

    def _stage_payload(
        self, payload: dict[str, object], persona_prompt: str | None
    ) -> dict[str, object]:
        stage_data = {
            key: value
            for key, value in payload.items()
            if key not in {"user_message", "conversation_context"}
        }
        if persona_prompt:
            stage_data = {"employee_identity": persona_prompt.strip(), **stage_data}
        return stage_payload(
            phase="Response Generator",
            user_message=str(payload.get("user_message") or ""),
            conversation_context=payload.get("conversation_context")
            if isinstance(payload.get("conversation_context"), dict)
            else {},
            memory_context=None,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract="只输出最终用户可见的纯文本，不输出 JSON、Markdown 代码围栏、分析过程或内部状态。",
        )
