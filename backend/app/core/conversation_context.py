"""
@Time       : 2026/08/28 15:00
@Author     : zhanglp8181
@File       : conversation_context.py
@CallChain  : AgentLoop → conversation_context → Router/Step/Reflection/Response 模型输入
@Description: 构建可恢复、有界且支持租户安全配置的会话上下文压缩结果。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


DEFAULT_CONTEXT_TOKEN_BUDGET = 32_000
COMPACTION_TRIGGER_RATIO = 0.70
RECENT_ROUND_LIMIT = 6
LONG_SUMMARY_TOKEN_BUDGET = 4_000
MEDIUM_SUMMARY_TOKEN_BUDGET = 4_000
MIN_CONTEXT_TOKEN_BUDGET = 4_096
MAX_CONTEXT_TOKEN_BUDGET = DEFAULT_CONTEXT_TOKEN_BUDGET
MIN_COMPACTION_TRIGGER_RATIO = 0.45
MAX_COMPACTION_TRIGGER_RATIO = 0.95
MAX_RECENT_ROUND_LIMIT = 20
ALLOWED_CONTEXT_ROLES = {"user", "assistant"}
LONG_SUMMARY_PREFIX = "历史的信息可以被总结为："
MEDIUM_SUMMARY_PREFIX = "近期的历史信息总结为："

SummaryBuilder = Callable[[str, str, int], str]


@dataclass(frozen=True, slots=True)
class ConversationContextSettings:
    """保存上下文压缩参数，并把管理端配置限制在可预测的资源边界内。"""

    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET
    compaction_trigger_ratio: float = COMPACTION_TRIGGER_RATIO
    recent_round_limit: int = RECENT_ROUND_LIMIT
    long_summary_token_budget: int = LONG_SUMMARY_TOKEN_BUDGET
    medium_summary_token_budget: int = MEDIUM_SUMMARY_TOKEN_BUDGET

    def normalized(self, *, minimum_token_budget: int = 1) -> "ConversationContextSettings":
        """归一化上下文参数，避免非法值放大内存、提示词或摘要开销。"""

        minimum = max(1, min(int(minimum_token_budget), MAX_CONTEXT_TOKEN_BUDGET))
        token_budget = _bounded_int(
            self.token_budget,
            default=DEFAULT_CONTEXT_TOKEN_BUDGET,
            minimum=minimum,
            maximum=MAX_CONTEXT_TOKEN_BUDGET,
        )
        ratio = _bounded_float(
            self.compaction_trigger_ratio,
            default=COMPACTION_TRIGGER_RATIO,
            minimum=MIN_COMPACTION_TRIGGER_RATIO,
            maximum=MAX_COMPACTION_TRIGGER_RATIO,
        )
        recent_round_limit = _bounded_int(
            self.recent_round_limit,
            default=RECENT_ROUND_LIMIT,
            minimum=1,
            maximum=MAX_RECENT_ROUND_LIMIT,
        )
        summary_maximum = max(1, min(token_budget, LONG_SUMMARY_TOKEN_BUDGET))
        long_summary_token_budget = _bounded_int(
            self.long_summary_token_budget,
            default=LONG_SUMMARY_TOKEN_BUDGET,
            minimum=1,
            maximum=summary_maximum,
        )
        medium_summary_token_budget = _bounded_int(
            self.medium_summary_token_budget,
            default=MEDIUM_SUMMARY_TOKEN_BUDGET,
            minimum=1,
            maximum=summary_maximum,
        )
        return replace(
            self,
            token_budget=token_budget,
            compaction_trigger_ratio=ratio,
            recent_round_limit=recent_round_limit,
            long_summary_token_budget=long_summary_token_budget,
            medium_summary_token_budget=medium_summary_token_budget,
        )

    @classmethod
    def from_ui_config(cls, config: object | None) -> "ConversationContextSettings":
        """从租户 UI 配置读取上下文参数，并应用面向管理端的最低安全上限。"""

        return cls(
            token_budget=_config_value(config, "context_token_budget", DEFAULT_CONTEXT_TOKEN_BUDGET),
            compaction_trigger_ratio=_config_value(
                config, "context_compaction_trigger_ratio", COMPACTION_TRIGGER_RATIO
            ),
            recent_round_limit=_config_value(
                config, "context_recent_round_limit", RECENT_ROUND_LIMIT
            ),
        ).normalized(minimum_token_budget=MIN_CONTEXT_TOKEN_BUDGET)

    @classmethod
    def from_metadata(cls, metadata: object | None) -> "ConversationContextSettings":
        """从已生成上下文的元数据恢复设置，兼容历史的低预算测试和回放上下文。"""

        source = metadata if isinstance(metadata, dict) else {}
        return cls(
            token_budget=source.get("token_budget", DEFAULT_CONTEXT_TOKEN_BUDGET),
            compaction_trigger_ratio=source.get(
                "compaction_trigger_ratio", COMPACTION_TRIGGER_RATIO
            ),
            recent_round_limit=source.get("recent_round_limit", RECENT_ROUND_LIMIT),
            long_summary_token_budget=source.get(
                "long_summary_token_budget", LONG_SUMMARY_TOKEN_BUDGET
            ),
            medium_summary_token_budget=source.get(
                "medium_summary_token_budget", MEDIUM_SUMMARY_TOKEN_BUDGET
            ),
        ).normalized()

    def with_token_budget(self, token_budget: int) -> "ConversationContextSettings":
        """生成仅覆盖预算的副本，供内部投影和兼容调用显式收紧上限。"""

        return replace(self, token_budget=token_budget).normalized()


def build_conversation_context(
    messages: list[dict[str, Any]],
    token_budget: int | None = None,
    *,
    context_state: dict[str, Any] | None = None,
    summary_builder: SummaryBuilder | None = None,
    settings: ConversationContextSettings | None = None,
) -> dict[str, object]:
    """按租户上下文配置构建有界会话历史，并保留可恢复的压缩状态。"""

    effective_settings = settings or ConversationContextSettings()
    if token_budget is not None:
        effective_settings = effective_settings.with_token_budget(token_budget)
    else:
        effective_settings = effective_settings.normalized()
    effective_token_budget = effective_settings.token_budget
    normalized = _normalize_messages(messages)
    state = _normalize_state(context_state)
    unsummarized, summarized_count = _messages_after_cursor(normalized, state)
    projected = _project_messages(state, unsummarized)
    trigger_tokens = max(
        1,
        math.floor(effective_token_budget * effective_settings.compaction_trigger_ratio),
    )
    compacted_now = False

    if _messages_tokens(projected) >= trigger_tokens:
        recent = _recent_rounds(unsummarized, effective_settings.recent_round_limit)
        older_count = len(unsummarized) - len(recent)
        older = unsummarized[:older_count]
        if older:
            previous_history = _joined_existing_history(state)
            state["long_term_summary"] = _summarize(
                "长期历史信息",
                previous_history,
                effective_settings.long_summary_token_budget,
                summary_builder,
            )
            state["medium_term_summary"] = _summarize(
                "近期历史信息",
                _transcript(older),
                effective_settings.medium_summary_token_budget,
                summary_builder,
            )
            state["summarized_through_message_id"] = older[-1]["_message_id"]
            state["compaction_count"] = int(state.get("compaction_count") or 0) + 1
            unsummarized = recent
            summarized_count += len(older)
            compacted_now = True
            projected = _project_messages(state, unsummarized)

    projected = _fit_projected_messages(projected, effective_token_budget)
    summary = _joined_existing_history(state)
    return {
        "messages": projected,
        "compacted_summary": summary,
        "context_state": state,
        "metadata": {
            "token_budget": effective_token_budget,
            "compaction_trigger_ratio": effective_settings.compaction_trigger_ratio,
            "compaction_trigger_tokens": trigger_tokens,
            "estimated_tokens": _messages_tokens(projected),
            "total_messages": len(normalized),
            "included_messages": len(projected),
            "omitted_messages": summarized_count,
            "compacted": bool(summary),
            "compacted_now": compacted_now,
            "long_term_summary": bool(state.get("long_term_summary")),
            "medium_term_summary": bool(state.get("medium_term_summary")),
            "recent_round_limit": effective_settings.recent_round_limit,
            "long_summary_token_budget": effective_settings.long_summary_token_budget,
            "medium_summary_token_budget": effective_settings.medium_summary_token_budget,
            "current_turn_time": normalized[-1].get("_created_at") if normalized else None,
        },
    }


def _config_value(config: object | None, name: str, default: object) -> object:
    """从 ORM 配置或测试替身读取单个配置值。"""

    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default) if config is not None else default


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    """将外部整数配置限制到闭区间；布尔值不作为整数接受。"""

    try:
        parsed = int(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: object, *, default: float, minimum: float, maximum: float) -> float:
    """将外部浮点配置限制到闭区间，并拒绝 NaN/无穷值。"""

    try:
        parsed = float(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _normalize_state(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "long_term_summary": str(source.get("long_term_summary") or "").strip(),
        "medium_term_summary": str(source.get("medium_term_summary") or "").strip(),
        "summarized_through_message_id": str(
            source.get("summarized_through_message_id") or ""
        ).strip(),
        "compaction_count": max(0, int(source.get("compaction_count") or 0)),
    }


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        if role not in ALLOWED_CONTEXT_ROLES or not content:
            continue
        normalized_message: dict[str, Any] = {
            "role": role,
            "content": content,
            "_message_id": str(message.get("id") or f"context_message_{index}"),
            "_created_at": _string_time(message.get("created_at")),
        }
        images = message.get("images")
        if role == "user" and isinstance(images, list) and images:
            normalized_message["images"] = images
        normalized.append(normalized_message)
    return normalized


def _messages_after_cursor(
    messages: list[dict[str, Any]], state: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    cursor = str(state.get("summarized_through_message_id") or "")
    if not cursor:
        return messages, 0
    for index, message in enumerate(messages):
        if message["_message_id"] == cursor:
            return messages[index + 1 :], index + 1
    # A missing cursor means the backing history was replaced; rebuild safely.
    state["long_term_summary"] = ""
    state["medium_term_summary"] = ""
    state["summarized_through_message_id"] = ""
    state["compaction_count"] = 0
    return messages, 0


def _project_messages(
    state: dict[str, Any], recent: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    long_summary = str(state.get("long_term_summary") or "").strip()
    medium_summary = str(state.get("medium_term_summary") or "").strip()
    if long_summary or medium_summary:
        projected.append(
            {
                "role": "user",
                "content": f"{LONG_SUMMARY_PREFIX}\n{long_summary or '暂无长期历史摘要。'}",
            }
        )
    if long_summary or medium_summary:
        projected.append(
            {
                "role": "user",
                "content": f"{MEDIUM_SUMMARY_PREFIX}\n{medium_summary or '暂无近期历史摘要。'}",
            }
        )
    projected.extend(_public_message(message) for message in recent)
    return projected


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in message.items()
        if key in {"role", "content", "images"}
    }


def _recent_rounds(
    messages: list[dict[str, Any]], round_limit: int
) -> list[dict[str, Any]]:
    user_indexes = [
        index for index, message in enumerate(messages) if message.get("role") == "user"
    ]
    if len(user_indexes) <= round_limit:
        return messages
    return messages[user_indexes[-round_limit] :]


def _joined_existing_history(state: dict[str, Any]) -> str:
    return "\n".join(
        value
        for value in (
            str(state.get("long_term_summary") or "").strip(),
            str(state.get("medium_term_summary") or "").strip(),
        )
        if value
    )


def _summarize(
    label: str,
    source: str,
    token_budget: int,
    summary_builder: SummaryBuilder | None,
) -> str:
    if not source.strip():
        return ""
    if summary_builder:
        try:
            summary = str(summary_builder(label, source, token_budget) or "").strip()
            if summary:
                return _trim_text_to_tokens(summary, token_budget)
        except Exception:
            pass
    return _compact_transcript(source, token_budget)


def _transcript(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{message['role']}: {' '.join(str(message['content']).split())}"
        for message in messages
    )


def _compact_transcript(source: str, token_budget: int) -> str:
    lines = [" ".join(line.split()) for line in source.splitlines() if line.strip()]
    selected: list[str] = []
    for line in lines:
        candidate = "\n".join([*selected, line])
        if selected and _estimate_tokens(candidate) > token_budget:
            break
        selected.append(line)
    result = "\n".join(selected)
    return _trim_text_to_tokens(result or source, token_budget)


def _fit_projected_messages(
    messages: list[dict[str, Any]], token_budget: int
) -> list[dict[str, Any]]:
    projected = list(messages)
    summary_count = sum(
        1
        for message in projected[:2]
        if str(message.get("content") or "").startswith(
            (LONG_SUMMARY_PREFIX, MEDIUM_SUMMARY_PREFIX)
        )
    )
    while len(projected) > summary_count + 1 and _messages_tokens(projected) > token_budget:
        projected.pop(summary_count)
    if projected and _messages_tokens(projected) > token_budget:
        last = projected[-1]
        remaining = max(1, token_budget - _messages_tokens(projected[:-1]))
        projected[-1] = _trim_message(last, remaining)
    return projected


def _trim_message(message: dict[str, Any], token_budget: int) -> dict[str, Any]:
    content_budget = max(1, token_budget - _estimate_tokens(str(message["role"])) - 6)
    return {**message, "content": _trim_text_to_tokens(str(message["content"]), content_budget)}


def _trim_text_to_tokens(text: str, token_budget: int) -> str:
    if _estimate_tokens(text) <= token_budget:
        return text
    encoded = text.encode("utf-8")
    byte_budget = max(4, token_budget * 4)
    trimmed = encoded[:byte_budget].decode("utf-8", errors="ignore").rstrip()
    return f"{trimmed}..."


def _messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_message_tokens(message) for message in messages)


def _message_tokens(message: dict[str, Any]) -> int:
    return _estimate_tokens(str(message["role"])) + _estimate_tokens(
        str(message["content"])
    ) + 6


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _string_time(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    text = str(value).strip()
    return text or None
