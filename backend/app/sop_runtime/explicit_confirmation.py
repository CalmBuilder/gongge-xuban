"""
@Time       : 2026/07/22 18:35
@Author     : zhanglp8181
@File       : explicit_confirmation.py
@CallChain  : Agent Loop 当前消息 → Coordinator → 明确确认槽位 → Scheduler
@Description: 只从当前轮白名单短语生成副作用操作所需的明确确认值。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from app.sop_runtime.definition import CollectInputNode, CompiledSopDefinition


_CONFIRMATION_SEPARATOR_PATTERN = re.compile(r"[\s，。！？、,.!?;；：:]+")


def normalize_confirmation_phrase(value: str) -> str:
    """统一确认短语的大小写、空白和常见标点，仍坚持整句精确匹配。"""

    return _CONFIRMATION_SEPARATOR_PATTERN.sub("", value.strip().casefold())


def resolve_explicit_confirmation_slots(
    definition: CompiledSopDefinition,
    *,
    current_node_id: str,
    slots: Mapping[str, object],
    user_message: str,
) -> dict[str, object]:
    """清除模型推测的确认槽位，仅接受当前确认节点白名单内的整句消息。"""

    resolved = dict(slots)
    confirmation_nodes = tuple(
        node
        for node in definition.nodes
        if isinstance(node, CollectInputNode) and node.config.confirmation_policy is not None
    )
    for node in confirmation_nodes:
        policy = node.config.confirmation_policy
        if policy is not None:
            resolved.pop(policy.slot_name, None)

    current_node = next(
        (node for node in confirmation_nodes if node.node_id == current_node_id),
        None,
    )
    if current_node is None or current_node.config.confirmation_policy is None:
        return resolved
    normalized_message = normalize_confirmation_phrase(user_message)
    canonical_value = current_node.config.confirmation_policy.phrase_values.get(
        normalized_message
    )
    if canonical_value is not None:
        resolved[current_node.config.confirmation_policy.slot_name] = canonical_value
    return resolved
