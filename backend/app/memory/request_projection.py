"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : request_projection.py
@CallChain  : Router/DTA → 内存投影信封 → LLMClient 最终SDK
@Description: 服务端投影属性与provider正文分离，修复/重试共享只减不增的选择。
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.memory.projection import MemoryCandidate, MemoryProjectionPolicy, POLICY_VERSION


@dataclass
class MemoryRequestEnvelope:
    """保存在内存/受控上下文的来源选择，不能作为JSON字段外发。"""

    candidates: tuple[MemoryCandidate, ...]
    current_input: str
    policy: MemoryProjectionPolicy = field(default_factory=MemoryProjectionPolicy)
    selected: tuple[MemoryCandidate, ...] | None = None
    validate: Callable[[MemoryCandidate], bool] | None = None
    audits: list[dict[str, Any]] = field(default_factory=list)
    source_input_id: str = ""
    parent_projection_id: str = ""
    derived_values_revoked: bool = False
    revoked_execution_references: dict[str, dict[str, str]] = field(
        default_factory=dict, repr=False,
    )
    revoked_system_prompt: str | None = field(default=None, repr=False)

    def eligible(self) -> tuple[MemoryCandidate, ...]:
        """每次实际发送重新核权限，已冻结选择只减不补。"""
        source = self.candidates if self.selected is None else self.selected
        return tuple(item for item in source if self.validate is None or self.validate(item))

    def snapshot(self) -> dict[str, Any]:
        """记录既有执行事实及独立预算审计，未选择的正文不持久复制。"""
        return {"policy_version": POLICY_VERSION, "policy": {
            "configured_memory_limit": self.policy.configured_memory_limit,
            "max_items": self.policy.max_items,
            "max_fact_characters": self.policy.max_fact_characters,
        }, "selected": [item.snapshot() for item in (self.selected or ())],
            "audits": copy.deepcopy(self.audits), "source_input_id": self.source_input_id,
            "current_input": self.current_input, "projection_id": self.parent_projection_id,
            "derived_values_revoked": self.derived_values_revoked}

    @classmethod
    def restore(cls, snapshot: Mapping[str, Any], *,
                validate: Callable[[MemoryCandidate], bool]) -> MemoryRequestEnvelope:
        """只恢复原选择；策略未知安全失败，当前新增记忆不参与恢复。"""
        if snapshot.get("policy_version") != POLICY_VERSION:
            raise ValueError("MEMORY_PROJECTION_VERSION_UNSUPPORTED")
        selected = tuple(MemoryCandidate.restore(item) for item in snapshot.get("selected", ()))
        return cls(selected, str(snapshot.get("current_input") or ""),
                   MemoryProjectionPolicy(**snapshot["policy"]), selected, validate,
                   source_input_id=str(snapshot.get("source_input_id") or ""),
                   parent_projection_id=str(snapshot.get("projection_id") or ""),
                   derived_values_revoked=snapshot.get("derived_values_revoked") is True)


class MemoryRequestPayload(dict[str, Any]):
    """普通JSON字典携带不可序列化属性，provider只能看见原领域字段。"""

    def __init__(self, payload: Mapping[str, Any], envelope: MemoryRequestEnvelope,
                 *, model_output_fields: tuple[str, ...] = ()) -> None:
        """绑定来源及消费者明确声明的派生槽；这些属性不进入provider字典。"""
        super().__init__(payload)
        self.envelope = envelope
        self.model_output_fields = model_output_fields

    def __deepcopy__(self, memo: dict[int, Any]) -> MemoryRequestPayload:
        """JSON修复复制输入而共享同一个只减不增的选择状态。"""
        result = MemoryRequestPayload(copy.deepcopy(dict(self), memo), self.envelope,
                                      model_output_fields=self.model_output_fields)
        memo[id(self)] = result
        return result


class MemoryContext(tuple):
    """沿现有DTA memory_context参数传递内存信封，不改变planner公共调用形状。"""

    def __new__(cls, values: Sequence[dict[str, Any]], envelope: MemoryRequestEnvelope):
        """保持序列兼容性并绑定服务端来源，不让dict(item)序列化信封。"""
        instance = super().__new__(cls, values)
        instance.envelope = envelope
        return instance


def format_memory_line(item: Mapping[str, Any]) -> str:
    """以安全kind/key标签标注Router事实，保留正文原始空白和内容。"""
    content = str(item.get("content") or "")
    metadata = item.get("metadata")
    key = metadata.get("key") if isinstance(metadata, Mapping) else None
    key = str(key or "").strip()
    kind = str(item.get("kind") or "").strip()
    label = "/".join(part for part in (kind, key) if part) if key else ""
    return f"{label}: {content}" if label else content


def render_memory_payload(payload: Mapping[str, Any], items: Sequence[MemoryCandidate]) -> dict:
    """在Router阶段和DTA重复上下文的所有结构位置替换同一完整选择。"""
    values = [item.provider_payload() for item in items]

    def replace(value: Any) -> Any:
        """仅处理已知memory_context位置，不从自由文本猜测事实。"""
        if isinstance(value, dict):
            return {key: copy.deepcopy(values) if key == "memory_context" else replace(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return copy.deepcopy(value)

    result = replace(dict(payload))
    stage = result.get("_agent_stage")
    if isinstance(stage, dict) and stage.get("phase") == "Router":
        stage["memory"] = "\n".join(
            "- " + format_memory_line(item.provider_payload()) for item in items
        )
    return (MemoryRequestPayload(result, payload.envelope,
                                 model_output_fields=payload.model_output_fields)
            if isinstance(payload, MemoryRequestPayload) else result)
