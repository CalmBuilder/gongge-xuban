"""
@Time       : 2026/08/04 01:28
@Author     : zhanglp8181
@File       : provider_view.py
@CallChain  : DynamicTaskAgent → ProviderExecutionViewBuilder → LLM provider adapter
@Description: 机械构建合法、脱敏且不受对话压缩覆盖的动态执行 provider 视图。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderExecutionViewError(ValueError):
    """表示 provider 外呼前发现能力、消息序列或安全投影不合法。"""


class ProviderActionCall(BaseModel):
    """表示已经由服务端赋予稳定身份的单个 provider action call。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=512)
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProviderMessage(BaseModel):
    """表示剥离 display/audit/provider sidecar 后的规范消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str | dict[str, Any]
    action_calls: tuple[ProviderActionCall, ...] = ()
    action_call_id: str | None = Field(default=None, max_length=512)
    message_kind: Literal["ordinary", "steering", "execution_context"] = "ordinary"


class ProviderExecutionView(BaseModel):
    """保存一次 provider 请求唯一可见的消息与机械 Execution 事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["dynamic-v1"] = "dynamic-v1"
    execution_context: dict[str, Any]
    messages: tuple[ProviderMessage, ...]
    native_input_parts: tuple[dict[str, Any], ...] = ()


def build_provider_execution_view(
    *,
    execution_context: Mapping[str, object],
    canonical_messages: Sequence[Mapping[str, object]],
    model_capabilities: Mapping[str, object],
    compacted_messages: Sequence[Mapping[str, object]] | None = None,
    interrupted_action_ids: Set[str] | None = None,
    native_input_parts: Sequence[Mapping[str, object]] = (),
) -> ProviderExecutionView:
    """验证 canonical 历史并生成 provider view，压缩仅替换对话而不替换执行事实。"""

    require_dynamic_preflight(model_capabilities)
    context = dict(execution_context)
    if _contains_sensitive_key(context):
        raise ProviderExecutionViewError("ExecutionContextProjection 含禁止的敏感 sidecar。")
    interrupted = {str(value) for value in interrupted_action_ids or set()}
    _normalize_messages(canonical_messages, interrupted_action_ids=interrupted)
    outbound_source = compacted_messages if compacted_messages is not None else canonical_messages
    outbound_interrupted = interrupted if compacted_messages is None else set()
    outbound = _normalize_messages(
        outbound_source,
        interrupted_action_ids=outbound_interrupted,
    )
    context_message = ProviderMessage(
        role="system",
        message_kind="execution_context",
        content={"execution_context": context},
    )
    return ProviderExecutionView(
        execution_context=context,
        messages=(context_message, *outbound),
        native_input_parts=tuple(dict(item) for item in native_input_parts),
    )


def _normalize_messages(
    messages: Sequence[Mapping[str, object]],
    *,
    interrupted_action_ids: set[str],
) -> tuple[ProviderMessage, ...]:
    """按 call/result 顺序白名单投影消息，并在声明中断时先补齐结构化结果。"""

    normalized: list[ProviderMessage] = []
    pending: set[str] = set()
    seen: set[str] = set()
    for raw in messages:
        role = str(raw.get("role") or "")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ProviderExecutionViewError("provider 消息 role 不合法。")
        if role != "tool" and pending:
            interruptible = pending & interrupted_action_ids
            if interruptible != pending:
                raise ProviderExecutionViewError("action 结果尚未补齐，不能插入用户或模型消息。")
            for action_id in sorted(pending):
                normalized.append(
                    ProviderMessage(
                        role="tool",
                        action_call_id=action_id,
                        content={"error": "action_interrupted"},
                    )
                )
            pending.clear()
        if role == "assistant":
            calls = tuple(
                ProviderActionCall.model_validate(item)
                for item in raw.get("action_calls", ())
                if isinstance(item, Mapping)
            )
            for call in calls:
                if call.id in seen:
                    raise ProviderExecutionViewError("provider action identity 重复。")
                seen.add(call.id)
                pending.add(call.id)
            normalized.append(
                ProviderMessage(
                    role="assistant",
                    content=_safe_content(raw.get("content")),
                    action_calls=calls,
                )
            )
            continue
        if role == "tool":
            action_call_id = str(raw.get("action_call_id") or "")
            if not action_call_id or action_call_id not in pending:
                raise ProviderExecutionViewError("发现没有对应 action call 的孤立结果。")
            pending.remove(action_call_id)
            normalized.append(
                ProviderMessage(
                    role="tool",
                    action_call_id=action_call_id,
                    content=_safe_content(raw.get("content")),
                )
            )
            continue
        message_kind = "steering" if raw.get("message_kind") == "steering" else "ordinary"
        normalized.append(
            ProviderMessage(
                role=role,
                content=_safe_content(raw.get("content")),
                message_kind=message_kind,
            )
        )
    if pending:
        interruptible = pending & interrupted_action_ids
        if interruptible == pending:
            for action_id in sorted(pending):
                normalized.append(
                    ProviderMessage(
                        role="tool",
                        action_call_id=action_id,
                        content={"error": "action_interrupted"},
                    )
                )
        else:
            raise ProviderExecutionViewError("provider history 尾部存在未闭合 action call。")
    return tuple(normalized)


def require_dynamic_preflight(capabilities: Mapping[str, object]) -> None:
    """只接受已验证的 dynamic-v1 必需能力，不按 provider 或模型名称推断。"""

    required = (
        capabilities.get("protocol_version") == "dynamic-v1"
        and capabilities.get("sdk_available") is True
        and capabilities.get("credentials_verified") is True
        and capabilities.get("structured_output") is True
        and capabilities.get("tool_calling") is True
    )
    if not required:
        raise ProviderExecutionViewError("模型 dynamic preflight 未通过或已经失效。")


def _safe_content(value: object) -> str | dict[str, Any]:
    """仅允许正文字符串或严格对象进入 provider，拒绝对象内敏感 sidecar。"""

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        content = dict(value)
        if _contains_sensitive_key(content):
            raise ProviderExecutionViewError("provider 正文含禁止的敏感 sidecar。")
        return content
    raise ProviderExecutionViewError("provider 消息 content 必须是字符串或对象。")


def _contains_sensitive_key(value: object) -> bool:
    """递归拒绝凭据、locator、审计和 provider 私有 sidecar 键。"""

    forbidden = {
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "secret",
        "secret_locator",
        "blob_locator",
        "audit",
        "reasoning",
        "usage",
        "source",
        "_openai",
        "_anthropic",
        "_gemini",
    }
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in forbidden or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False
