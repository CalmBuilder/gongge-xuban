"""
@Time       : 2026/08/10 16:20
@Author     : zhanglp8181
@File       : router.py
@CallChain  : AgentLoop → Router → LLMClient → RouterDecision
@Description: 生成并规范化场景技能路由，阻止无显式触发词的演示流程抢占业务请求。
"""

from __future__ import annotations

from typing import Any

from app import paths
from app.core.context_projection import (
    compact_awaiting_input,
    compact_conversation_context,
    compact_pending_tasks,
)
from app.db.models import ChatSession, ModelConfig, Skill
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    ROUTER_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation
from app.session.session_schema import PendingTask, RouterDecision
from app.session.slot_policy import strip_router_generated_message_slots


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "router_prompt.md"


class Router:
    def decide(
        self,
        message: str,
        session: ChatSession,
        available_skills: list[Skill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> RouterDecision:
        payload = stage_payload(
            phase="Router",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data={
                "current_session": _router_session_payload(session),
                "available_skills": _available_skill_payloads(available_skills),
            },
            output_contract=ROUTER_OUTPUT_SCHEMA,
        )
        try:
            with llm_operation("router.scene"):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            decision = RouterDecision.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Router returned invalid JSON schema: {exc}") from exc
        return self._normalize_decision(decision, session, available_skills, message)

    def _normalize_decision(
        self,
        decision: RouterDecision,
        session: ChatSession,
        available_skills: list[Skill],
        message: str,
    ) -> RouterDecision:
        """校验模型返回的技能、任务和演示流程触发边界，产出可执行路由。"""

        self._strip_generated_message_slots(decision)
        decision.general_intent = " ".join(
            str(decision.general_intent or "").split()
        ) or None
        skills = {skill.skill_id: skill for skill in available_skills}
        if decision.target_skill_id and decision.target_skill_id not in skills:
            decision.target_skill_id = None
            decision.target_step_id = None
        if decision.awaiting_input and decision.awaiting_input.skill_id not in {None, *skills.keys()}:
            decision.awaiting_input = None
        if decision.decision == "start_new_task":
            if not decision.target_skill_id or decision.target_skill_id not in skills:
                decision.decision = "clarify"
                decision.target_skill_id = None
                decision.target_step_id = None
                decision.clarification_question = "请问您想办理哪类业务？"
                return decision
            target_skill = skills[decision.target_skill_id]
            if not _demo_skill_explicitly_triggered(target_skill, message):
                decision.decision = "answer_only"
                decision.selected_task_id = None
                decision.target_skill_id = None
                decision.target_step_id = None
                decision.task_frames = []
                decision.pending_tasks = []
                decision.created_tasks = []
                decision.task_updates = []
                decision.clarification_question = None
                decision.reason = "演示流程未命中显式触发词，交回非场景能力路由。"
                return decision
        if decision.decision == "switch_to_pending":
            pending_ids = {
                str(task.get("task_id"))
                for task in (session.pending_tasks_json or [])
                if isinstance(task, dict) and task.get("task_id")
            }
            if not decision.selected_task_id or decision.selected_task_id not in pending_ids:
                decision.decision = "clarify"
                decision.clarification_question = "请问您想继续哪一项待处理任务？"
                return decision
        if decision.decision == "create_pending":
            ordered_tasks = [
                *decision.task_frames,
                *decision.pending_tasks,
                *decision.created_tasks,
            ]
            if ordered_tasks:
                primary = ordered_tasks[0]
                decision.decision = "start_new_task"
                decision.selected_task_id = primary.task_id
                decision.target_skill_id = primary.target_skill_id
                decision.target_step_id = primary.target_step_id
                decision.user_intent = primary.user_intent or decision.user_intent
                decision.slot_hints = {
                    **dict(primary.slot_hints or {}),
                    **dict(decision.slot_hints or {}),
                }
                decision.task_frames = ordered_tasks
                decision.pending_tasks = []
                decision.created_tasks = []
                if not decision.target_skill_id or decision.target_skill_id not in skills:
                    decision.decision = "clarify"
                    decision.selected_task_id = None
                    decision.target_skill_id = None
                    decision.target_step_id = None
                    decision.clarification_question = "请问您想办理哪类业务？"
                    return decision
        if not decision.target_skill_id and session.active_skill_id:
            decision.target_skill_id = session.active_skill_id
        if decision.target_skill_id and not decision.target_step_id:
            target_skill = skills.get(decision.target_skill_id)
            if target_skill:
                decision.target_step_id = _first_node_id(target_skill)
        normalized_tasks = self._normalize_tasks(decision.pending_tasks, skills)
        decision.pending_tasks = normalized_tasks
        legacy_created_tasks = self._normalize_tasks(decision.created_tasks, skills)
        decision.task_frames = self._normalize_turn_task_frames(
            decision,
            self._normalize_tasks(decision.task_frames, skills),
            legacy_created_tasks,
        )
        decision.created_tasks = []
        return decision

    def _strip_generated_message_slots(self, decision: RouterDecision) -> None:
        decision.slot_hints = strip_router_generated_message_slots(decision.slot_hints)
        for task in [*decision.task_frames, *decision.pending_tasks, *decision.created_tasks]:
            task.slot_hints = strip_router_generated_message_slots(task.slot_hints)
        for update in decision.task_updates:
            update.slot_hints = strip_router_generated_message_slots(update.slot_hints)

    def _normalize_tasks(self, tasks, skills: dict[str, Skill]):
        normalized_tasks = []
        for task in tasks:
            if not task.target_skill_id or task.target_skill_id not in skills:
                continue
            if not task.target_step_id:
                target_skill = skills.get(task.target_skill_id)
                if target_skill:
                    task.target_step_id = _first_node_id(target_skill)
            normalized_tasks.append(task)
        return normalized_tasks

    def _normalize_turn_task_frames(
        self,
        decision: RouterDecision,
        task_frames: list[PendingTask],
        legacy_created_tasks: list[PendingTask],
    ) -> list[PendingTask]:
        if decision.decision not in {"continue_active", "start_new_task", "switch_to_pending"}:
            return []
        primary = PendingTask(
            task_id=decision.selected_task_id,
            decision=decision.decision,
            target_skill_id=decision.target_skill_id,
            target_step_id=decision.target_step_id,
            confidence=decision.confidence,
            user_intent=decision.user_intent,
            reason=decision.reason,
            slot_hints=dict(decision.slot_hints or {}),
        )
        ordered = [*task_frames, *legacy_created_tasks]
        first = ordered[0] if ordered else None
        if not first or first.target_skill_id != primary.target_skill_id:
            ordered.insert(0, primary)
        else:
            first.decision = decision.decision
            first.task_id = first.task_id or decision.selected_task_id
            first.target_step_id = first.target_step_id or decision.target_step_id
            first.slot_hints = {
                **dict(decision.slot_hints or {}),
                **dict(first.slot_hints or {}),
            }
        return ordered


def _first_node_id(skill: Skill) -> str | None:
    content = skill.content_json or {}
    start_node_id = content.get("start_node_id")
    if isinstance(start_node_id, str) and start_node_id.strip():
        return start_node_id
    nodes = content.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("node_id"):
                return str(node["node_id"])
    return None


def _demo_skill_explicitly_triggered(skill: Skill, message: str) -> bool:
    """仅允许显式命中触发词的 demo 技能启动，业务技能不增加词法限制。"""

    if str(skill.business_domain or "").strip().casefold() != "demo":
        return True
    normalized_message = " ".join(str(message or "").casefold().split())
    trigger_intents = (skill.content_json or {}).get("trigger_intents")
    if not isinstance(trigger_intents, list):
        return False
    return any(
        normalized_trigger in normalized_message
        for trigger in trigger_intents
        if (normalized_trigger := " ".join(str(trigger).casefold().split()))
    )


def _available_skill_payloads(available_skills: list[Skill]) -> list[dict[str, Any]]:
    return [_skill_payload(skill) for skill in available_skills]


def _skill_payload(skill: Skill) -> dict[str, Any]:
    content = skill.content_json or {}
    return _without_empty(
        {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "business_domain": (
                skill.business_domain
                if str(skill.business_domain or "").strip().casefold() == "demo"
                else None
            ),
            "trigger_intents": content.get("trigger_intents", []),
        }
    )


def _router_session_payload(session: ChatSession) -> dict[str, Any]:
    return _without_empty(
        {
            "active_skill_id": session.active_skill_id,
            "active_step_id": session.active_step_id,
            "slots": session.slots_json or {},
            "pending_tasks": compact_pending_tasks(session.pending_tasks_json),
            "awaiting_input": compact_awaiting_input(session.awaiting_input_json),
            "status": session.status,
        }
    )


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }
