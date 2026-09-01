"""
@Time       : 2026/07/27 14:40
@Author     : zhanglp8181
@File       : context_projection.py
@CallChain  : AgentLoop → compact_* → Router/StepAgent/ResponseGenerator 模型输入
@Description: 对会话、SOP 和知识证据做有界投影，同时保持关联证据与原子引用完整。
"""

from __future__ import annotations

from typing import Any

from app.core.conversation_context import ConversationContextSettings, build_conversation_context
from app.knowledge.citations import EMAIL_PATTERN
from app.llm.stage_protocol import TURN_STAGE_MESSAGES_KEY


KNOWLEDGE_HISTORY_LIMIT = 1
KNOWLEDGE_EVIDENCE_LIMIT = 48
KNOWLEDGE_RELATED_CONTENT_LIMIT = 4_800
KNOWLEDGE_CONCEPT_LIMIT = 8
KNOWLEDGE_DOCUMENT_LIMIT = 5
RETRIEVED_KNOWLEDGE_LIMIT = 4
ATTACHMENT_CONTEXT_MARKER = "\n\n上传附件上下文："
CONTROL_ONLY_ATTACHMENT_KEYS = {
    "current_turn_inputs",
    "current_turn_input_read_receipt_ids",
    "current_turn_input_turn_id",
}


def compact_knowledge_context(
    items: list[dict[str, Any]] | None,
    *,
    max_items: int = KNOWLEDGE_HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    selected = [item for item in items if isinstance(item, dict)][-max(1, max_items) :]
    return [_compact_knowledge_result(item) for item in selected]


def compact_step_result(payload: dict[str, Any]) -> dict[str, Any]:
    projected = dict(payload)
    projected.pop("knowledge_results", None)
    projected["retrieved_knowledge"] = compact_knowledge_context(
        payload.get("knowledge_results") if isinstance(payload.get("knowledge_results"), list) else []
    )
    return projected


def compact_conversation_context(
    context: dict[str, object] | None,
    *,
    token_budget: int | None = None,
    settings: ConversationContextSettings | None = None,
) -> dict[str, object]:
    """投影控制阶段上下文，并阻止附件正文绕过Provider外发账本。"""

    context_metadata = context.get("metadata") if isinstance(context, dict) else None
    effective_settings = settings or ConversationContextSettings.from_metadata(context_metadata)
    if token_budget is not None:
        effective_settings = effective_settings.with_token_budget(token_budget)
    if not isinstance(context, dict):
        return build_conversation_context([], settings=effective_settings)
    projected_context = {
        key: value for key, value in context.items() if key not in CONTROL_ONLY_ATTACHMENT_KEYS
    }
    turn_messages = projected_context.setdefault(TURN_STAGE_MESSAGES_KEY, [])
    messages = projected_context.get("messages")
    if not isinstance(messages, list):
        projected_context["messages"] = []
        return projected_context
    projected_context["messages"] = [_control_message(item) for item in messages]
    metadata = projected_context.get("metadata")
    if (
        isinstance(metadata, dict)
        and int(metadata.get("estimated_tokens") or 0) <= effective_settings.token_budget
    ):
        return projected_context
    compacted = build_conversation_context(
        [
            message
            for message in projected_context["messages"]
            if isinstance(message, dict)
        ],
        settings=effective_settings,
    )
    compacted[TURN_STAGE_MESSAGES_KEY] = turn_messages
    return compacted


def _control_message(value: object) -> object:
    """从Router/Step/Reflection消息中剥离附件派生正文与原生图片。"""

    if not isinstance(value, dict):
        return value
    projected = dict(value)
    content = str(projected.get("content") or "")
    if ATTACHMENT_CONTEXT_MARKER in content:
        projected["content"] = content.split(ATTACHMENT_CONTEXT_MARKER, 1)[0]
    projected.pop("images", None)
    return projected


def compact_current_step(
    content: dict[str, Any] | None,
    step_id: str | None,
) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    resolved_step_id = step_id or _optional_text(content.get("start_node_id"))
    node = next(
        (
            item
            for item in _skill_nodes(content)
            if isinstance(item, dict)
            and _optional_text(item.get("node_id") or item.get("step_id")) == resolved_step_id
        ),
        None,
    )
    return _project_node(node) if node else None


def compact_step_skill_context(
    content: dict[str, Any] | None,
    step_id: str | None,
    *,
    skill_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    current_step = compact_current_step(content, step_id)
    current_step_id = _optional_text((current_step or {}).get("node_id"))
    nodes_by_id = {
        _optional_text(node.get("node_id") or node.get("step_id")): node
        for node in _skill_nodes(content)
        if isinstance(node, dict)
        and _optional_text(node.get("node_id") or node.get("step_id"))
    }
    edges = content.get("edges")
    next_steps: list[dict[str, Any]] = []
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, dict):
            continue
        if _optional_text(edge.get("source_node_id")) != current_step_id:
            continue
        target_id = _optional_text(edge.get("next_node_id") or edge.get("target_node_id"))
        target_node = nodes_by_id.get(target_id)
        if not target_node:
            continue
        projected_step = _project_step_agent_node(target_node)
        transition = _project_transition(edge)
        if transition:
            projected_step["transition"] = transition
        next_steps.append(projected_step)
    return _without_empty(
        {
            "current_step": _project_step_agent_node(current_step or {}),
            "next_steps": next_steps,
        }
    )


def compact_router_decision(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    projected = _without_empty(
        {
            key: payload.get(key)
            for key in (
                "decision",
                "selected_task_id",
                "target_skill_id",
                "target_step_id",
                "confidence",
                "user_intent",
                "reason",
                "clarification_question",
                "slot_hints",
            )
        }
    )
    return projected or None


def compact_step_router_decision(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    projected = _without_empty(
        {
            "decision": payload.get("decision"),
            "user_intent": _short_text(payload.get("user_intent"), 300),
        }
    )
    return projected or None


def compact_response_step_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    projected = _without_empty(
        {
            key: payload.get(key)
            for key in ("reply", "next_step_id", "is_step_completed", "handoff")
        }
    )
    return projected or None


def compact_citation_hints(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: item.get(key)
            for key in (
                "label",
                "kind",
                "title",
                "source_path",
                "section_path",
            )
            if item.get(key) not in (None, "")
        }
        for item in citations
        if isinstance(item, dict)
    ]


def compact_memory_context(items: list[dict[str, Any]] | None) -> str:
    if not isinstance(items, list):
        return ""
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _short_text(item.get("content"), 1_000)
        if content and content not in lines:
            lines.append(content)
    return "\n".join(f"- {line}" for line in lines)


def compact_pending_tasks(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    tasks: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        task = _without_empty(
            {
                "task_id": item.get("task_id"),
                "status": item.get("status"),
                "skill_id": item.get("skill_id") or item.get("target_skill_id"),
                "step_id": item.get("step_id") or item.get("target_step_id"),
                "slots": item.get("slots") or item.get("slot_hints"),
                "intent_summary": _short_text(
                    item.get("intent_summary") or item.get("user_intent"), 300
                ),
                "source_message": _short_text(item.get("source_message"), 500),
                "resume_policy": item.get("resume_policy"),
            }
        )
        if task:
            tasks.append(task)
    return tasks


def compact_deferred_intents(
    items: list[dict[str, Any]] | None,
    *,
    selected_task_id: str | None = None,
) -> list[str]:
    if not isinstance(items, list):
        return []
    intents: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if selected_task_id and str(item.get("task_id") or "") == selected_task_id:
            continue
        if str(item.get("status") or "pending") != "pending":
            continue
        intent = _short_text(
            item.get("intent_summary")
            or item.get("user_intent")
            or item.get("source_message"),
            300,
        )
        if intent and intent not in intents:
            intents.append(intent)
    return intents


def compact_awaiting_input(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    projected = _without_empty(
        {
            "skill_id": value.get("skill_id"),
            "step_id": value.get("step_id"),
            "expected_fields": value.get("expected_fields"),
            "question_summary": _short_text(value.get("question_summary"), 500),
        }
    )
    return projected or None


def _compact_knowledge_result(item: dict[str, Any]) -> dict[str, Any]:
    query = item.get("query")
    if isinstance(query, dict):
        query = query.get("query")
    return _without_empty(
        {
            "query": _short_text(query, 500),
            "retrieved_knowledge": _compact_retrieved_knowledge(item),
        }
    )


def _compact_retrieved_knowledge(item: dict[str, Any]) -> list[dict[str, Any]]:
    """投影检索结果，并把同一关联组作为一条连续证据交给控制模型。"""

    candidates = _compact_evidence_candidates(item)
    for value in _dict_items(item.get("selected_concepts"), KNOWLEDGE_CONCEPT_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("name"), 180),
                "source": _short_text(value.get("source_path") or value.get("concept_id"), 300),
                "summary": _short_text(value.get("summary"), 300),
                "content": _short_text(value.get("content") or value.get("content_md"), 600),
            }
        )
    for value in _dict_items(item.get("selected_documents"), KNOWLEDGE_DOCUMENT_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("filename"), 180),
                "source": _short_text(value.get("filename"), 180),
                "summary": _short_text(value.get("summary"), 600),
            }
        )
    for value in _dict_items(item.get("selected_buckets"), KNOWLEDGE_DOCUMENT_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title"), 180),
                "summary": _short_text(value.get("summary"), 600),
            }
        )
    for value in _dict_items(item.get("okf_citations"), KNOWLEDGE_EVIDENCE_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("label"), 180),
                "source": _short_text(
                    value.get("source_path") or value.get("path") or value.get("uri"),
                    300,
                ),
            }
        )

    compacted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        projected = _without_empty(candidate)
        identity = "|".join(
            str(projected.get(key) or "")
            for key in ("source", "title", "content", "summary")
        ).strip("|")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        compacted.append(
            {"label": f"检索到的知识 {len(compacted) + 1}", **projected}
        )
        if len(compacted) >= RETRIEVED_KNOWLEDGE_LIMIT:
            break
    return compacted


def _compact_evidence_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    """按 related_group_id 合并连续证据，并对每组施加独立字符预算。"""

    evidence = _dict_items(item.get("evidence_pack"), KNOWLEDGE_EVIDENCE_LIMIT)
    if not evidence:
        evidence = _dict_items(item.get("chunks"), KNOWLEDGE_EVIDENCE_LIMIT)
    ordered_groups: list[list[dict[str, Any]]] = []
    group_index: dict[str, int] = {}
    for index, value in enumerate(evidence):
        metadata = (
            value.get("metadata")
            if isinstance(value.get("metadata"), dict)
            else {}
        )
        group_id = str(
            value.get("related_group_id")
            or metadata.get("related_group_id")
            or ""
        ).strip()
        key = f"related:{group_id}" if group_id else f"single:{index}"
        if key not in group_index:
            group_index[key] = len(ordered_groups)
            ordered_groups.append([])
        ordered_groups[group_index[key]].append(value)

    candidates: list[dict[str, Any]] = []
    for group in ordered_groups:
        value = group[0]
        content = "\n\n".join(
            str(part.get("content") or part.get("excerpt") or "").strip()
            for part in group
            if str(part.get("content") or part.get("excerpt") or "").strip()
        )
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("label"), 180),
                "source": _short_text(
                    value.get("section_path")
                    or value.get("source_path")
                    or value.get("source_ref"),
                    300,
                ),
                "summary": _short_text(value.get("summary"), 300),
                "content": _short_text(
                    content,
                    KNOWLEDGE_RELATED_CONTENT_LIMIT if len(group) > 1 else 800,
                ),
            }
        )
    return candidates


def _dict_items(value: object, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:limit]


def _skill_nodes(content: dict[str, Any]) -> list[dict[str, Any]]:
    value = content.get("nodes")
    if not isinstance(value, list):
        value = content.get("steps")
    return [item for item in value or [] if isinstance(item, dict)]


def _project_node(node: dict[str, Any]) -> dict[str, Any]:
    projected = {"node_id": node.get("node_id") or node.get("step_id")}
    projected.update(
        {
            key: node.get(key)
            for key in (
                "type",
                "name",
                "instruction",
                "optional",
                "condition",
                "expected_user_info",
                "slot_value_contract",
                "allowed_actions",
                "knowledge_scope",
                "retry_policy",
            )
        }
    )
    projected["slot_value_contract"] = (
        node.get("slot_value_contract") or _slot_value_contract(node)
    )
    return _without_empty(projected)


def _project_step_agent_node(node: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    return _without_empty(
        {
            "node_id": node.get("node_id") or node.get("step_id"),
            "type": node.get("type"),
            "instruction": node.get("instruction"),
            "expected_user_info": node.get("expected_user_info"),
            "slot_value_contract": node.get("slot_value_contract") or _slot_value_contract(node),
            "allowed_actions": node.get("allowed_actions"),
            "knowledge_scope": node.get("knowledge_scope"),
        }
    )


def _slot_value_contract(node: dict[str, Any]) -> dict[str, list[str]]:
    """从节点冻结别名中只投影规范枚举值，避免把历史同义词继续教给模型。"""

    expected_fields = {
        str(field).strip()
        for field in node.get("expected_user_info", [])
        if str(field).strip()
    }
    metadata = node.get("metadata")
    raw_aliases = metadata.get("value_aliases") if isinstance(metadata, dict) else None
    if not isinstance(raw_aliases, dict):
        return {}
    contract: dict[str, list[str]] = {}
    for slot_name, aliases in raw_aliases.items():
        if slot_name not in expected_fields or not isinstance(aliases, dict):
            continue
        canonical_values = sorted(
            {
                str(value).strip()
                for value in aliases.values()
                if isinstance(value, str) and value.strip()
            }
        )
        if canonical_values:
            contract[slot_name] = canonical_values
    return contract


def _project_transition(edge: dict[str, Any]) -> dict[str, Any]:
    return _without_empty(
        {
            key: edge.get(key)
            for key in (
                "condition",
                "label",
            )
        }
    )


def _short_text(value: object, limit: int) -> str:
    """截断长文本，但若边界落在邮箱中间则延伸到该原子值末尾。"""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    boundary = limit
    token_start = text.rfind(" ", 0, limit) + 1
    token_end = text.find(" ", limit)
    if token_end < 0:
        token_end = len(text)
    boundary_token = text[token_start:token_end].strip(
        "，。；：、,;:()（）[]【】<>"
    )
    if EMAIL_PATTERN.fullmatch(boundary_token):
        boundary = token_end
    return text[:boundary].rstrip() + "..."


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }
