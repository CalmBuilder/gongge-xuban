"""
@Time       : 2026/07/22 20:10
@Author     : zhanglp8181
@File       : slot_values.py
@CallChain  : DeterministicSopCoordinator → 槽位键/值归一 → Runtime 调度
@Description: 按不可变发布内容归一模型槽位键，并按规范定义归一稳定业务枚举值。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.sop_runtime.definition import CollectInputNode, CompiledSopDefinition


def canonicalize_slot_keys(
    version_content: Mapping[str, object],
    slots: Mapping[str, object],
) -> dict[str, object]:
    """按版本冻结的键别名归一槽位；规范键已有非空值时确定性保留规范值。"""

    normalized_slots = dict(slots)
    raw_aliases = version_content.get("slot_key_aliases")
    if not isinstance(raw_aliases, Mapping):
        return normalized_slots
    for raw_alias, raw_canonical in raw_aliases.items():
        if not isinstance(raw_alias, str) or not isinstance(raw_canonical, str):
            continue
        alias = raw_alias.strip()
        canonical = raw_canonical.strip()
        if not alias or not canonical or alias == canonical or alias not in normalized_slots:
            continue
        alias_value = normalized_slots.pop(alias)
        canonical_value = normalized_slots.get(canonical)
        if canonical not in normalized_slots or _is_blank_slot_value(canonical_value):
            normalized_slots[canonical] = alias_value
    return normalized_slots


def normalize_slot_values(
    definition: CompiledSopDefinition,
    slots: Mapping[str, object],
) -> dict[str, object]:
    """应用定义期冻结的枚举白名单，布尔值仅在显式 true/false 别名存在时归一。"""

    normalized_slots = dict(slots)
    mappings = _definition_value_aliases(definition)
    for slot_name, aliases in mappings.items():
        value = normalized_slots.get(slot_name)
        if isinstance(value, bool):
            normalized_value = "true" if value else "false"
        elif isinstance(value, str):
            normalized_value = value.strip().casefold()
        else:
            if value is not None:
                normalized_slots[slot_name] = ""
            continue
        if normalized_value in aliases:
            normalized_slots[slot_name] = aliases[normalized_value]
        else:
            normalized_slots[slot_name] = ""
    return normalized_slots


def _is_blank_slot_value(value: object) -> bool:
    """判断规范槽位是否仍为空，避免别名覆盖已确认的非空业务值。"""

    return value is None or (isinstance(value, str) and not value.strip())


def _definition_value_aliases(
    definition: CompiledSopDefinition,
) -> dict[str, dict[str, str]]:
    """汇总所有输入节点的别名，并在异常发布数据出现冲突时确定性拒绝。"""

    mappings: dict[str, dict[str, str]] = {}
    for node in definition.nodes:
        if not isinstance(node, CollectInputNode):
            continue
        for slot_name, aliases in node.config.value_aliases.items():
            existing = mappings.get(slot_name)
            if existing is not None and existing != aliases:
                raise ValueError(f"槽位 {slot_name} 在多个节点声明了冲突的值别名")
            mappings[slot_name] = aliases
    return mappings
