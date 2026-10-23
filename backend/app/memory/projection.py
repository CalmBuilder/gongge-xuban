"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : projection.py
@CallChain  : MemoryService → Router/DTA → LLMClient 实际请求整形
@Description: 内部可信候选及版本化完整事实选择；费用由实际消费者渲染复算。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.memory.keys import normalize_memory_key_identity

POLICY_VERSION = "memory_projection_v1"
ESTIMATOR_VERSION = "utf8_bytes_div4_message6_v1"
_BLOCKED_METADATA = {
    "token", "secret", "password", "authorization", "api_key", "agent_id",
    "agent_id_source", "source", "tenant_id", "user_id", "session_id", "id",
    "source_sequence", "generation", "capture_job_id", "source_hash",
}


def _integer(value: object, name: str) -> int:
    """仅接受服务端有限非负整数，零是有效关闭预算。"""
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise ValueError(f"INVALID_MEMORY_PROJECTION_{name.upper()}")
    return value


def safe_memory_metadata(value: object) -> dict[str, Any]:
    """保留既有安全标量边界，屏蔽身份来源及非有限浮点数。"""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item for key, item in value.items()
        if str(key).casefold() not in _BLOCKED_METADATA
        and (item is None or type(item) in (str, bool, int, float))
        and not (isinstance(item, float) and not math.isfinite(item))
    }


@dataclass(frozen=True)
class MemoryCandidate:
    """保存服务端读取的完整事实及撤销向量；来源侧带不进入模型。"""

    id: str
    kind: str
    key: str
    content: str
    importance: float = 0.5
    source_sequence: int = 0
    updated_at: str = ""
    scope: tuple[str, str, str] = ("", "", "")
    generations: tuple[int, int, int, int] = (0, 0, 0, 0)
    agent_capture_generation: int = 0
    origin: str = "memory"
    metadata: dict[str, Any] = field(default_factory=dict)

    def provider_payload(self) -> dict[str, Any]:
        """生成完整正文和安全标量元数据，不披露主键、租户和来源版本。"""
        return {"kind": self.kind, "content": self.content,
                "metadata": safe_memory_metadata(self.metadata)}

    def snapshot(self) -> dict[str, Any]:
        """供既有受控执行上下文保存选择，不建立额外正文调试存储。"""
        return asdict(self)

    @classmethod
    def restore(cls, value: Mapping[str, Any]) -> MemoryCandidate:
        """恢复服务端冻结事实；JSON列表归一回不可变撤销向量。"""
        fields = dict(value)
        fields["scope"] = tuple(fields.get("scope", ("", "", "")))
        fields["generations"] = tuple(fields.get("generations", (0, 0, 0, 0)))
        return cls(**fields)


@dataclass(frozen=True)
class MemoryProjectionPolicy:
    """定义唯一v1策略；DTA额外条数/字符边界由实际消费者显式启用。"""

    configured_memory_limit: int = 2048
    max_items: int = 2**31 - 1
    max_fact_characters: int = 2**31 - 1

    def __post_init__(self) -> None:
        """构造时拒绝非法预算，而不是静默转换客户端浮点或字符串。"""
        for name in ("configured_memory_limit", "max_items", "max_fact_characters"):
            _integer(getattr(self, name), name)


@dataclass(frozen=True)
class MemoryProjectionResult:
    """分离模型完整条目和服务端可复算的安全审计。"""

    selected: tuple[MemoryCandidate, ...]
    budget: int
    estimated_cost: int
    nonmemory_cost: int
    input_limit: int
    omissions: dict[str, int]
    candidate_digest: str
    policy_version: str = POLICY_VERSION
    estimator_version: str = ESTIMATOR_VERSION

    def audit(self) -> dict[str, Any]:
        """只输出身份/摘要/费用，不复制正文或敏感元数据到观察日志。"""
        return {
            "policy_version": self.policy_version, "estimator_version": self.estimator_version,
            "candidate_digest": self.candidate_digest, "budget": self.budget,
            "estimated_cost": self.estimated_cost, "nonmemory_cost": self.nonmemory_cost,
            "input_limit": self.input_limit, "omissions": self.omissions,
            "selected": [{"id": item.id, "hash": _hash(item.content),
                          "source_sequence": item.source_sequence, "scope": item.scope,
                          "generations": item.generations, "origin": item.origin,
                          "legacy": item.source_sequence == 0} for item in self.selected],
        }


def _hash(text: str) -> str:
    """计算UTF8内容摘要，避免把原文写入预算审计。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_timestamp(value: str) -> float:
    """固定缺省UTC解释，排序不依赖本机TZ或调用墙钟。"""
    try:
        parsed = datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    except (ValueError, OverflowError):
        return 0.0


def _terms(value: str) -> set[str]:
    """v1仅计算Unicode归一化英文词和中文双字重合，不冒称语义抽取。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    result = set(re.findall(r"[a-z0-9_]+", normalized))
    for phrase in re.findall(r"[\u3400-\u9fff]+", normalized):
        result.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return result


def deduplicate_candidates(items: Sequence[MemoryCandidate]) -> tuple[MemoryCandidate, ...]:
    """按既有规范键和来源序归并同scope/kind；legacy回退到冻结UTC及稳定ID。"""
    latest: dict[tuple[object, ...], MemoryCandidate] = {}
    for item in items:
        identity = (item.scope, item.kind, normalize_memory_key_identity(item.key or item.id),
                    item.origin)
        previous = latest.get(identity)
        freshness = (item.source_sequence, _utc_timestamp(item.updated_at), item.id)
        if previous is None or freshness > (
            previous.source_sequence, _utc_timestamp(previous.updated_at), previous.id,
        ):
            latest[identity] = item
    return tuple(sorted(latest.values(), key=lambda item: (item.scope, item.kind, item.key, item.id)))


def select_memory_projection(
    candidates: Sequence[MemoryCandidate], *, current_input: str, input_limit: int,
    render_cost: Callable[[tuple[MemoryCandidate, ...]], int],
    policy: MemoryProjectionPolicy = MemoryProjectionPolicy(),
    corrected_keys: Sequence[str] = (),
) -> MemoryProjectionResult:
    """按真实包装费用选择整条；过大继续尝试，长期事实先于最近会话回声。"""
    input_limit = _integer(input_limit, "input_limit")
    base = _integer(render_cost(()), "nonmemory_cost")
    reserve = max(64, (input_limit * 2 + 99) // 100)
    budget = max(0, min(policy.configured_memory_limit, input_limit // 8,
                        input_limit - base - reserve))
    unique = deduplicate_candidates(candidates)
    digest = _hash(json.dumps([item.snapshot() for item in unique], ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")))
    query = _terms(current_input)

    def priority(item: MemoryCandidate) -> tuple[object, ...]:
        """固定身份/偏好、词重合、量化importance、来源新鲜度及key/id。"""
        importance = item.importance if math.isfinite(item.importance) else 0.0
        return ({"profile": 0, "preference": 1}.get(item.kind, 2),
                -len(query & _terms(item.content)), -round(max(0, min(1, importance)) * 1000),
                -item.source_sequence, -_utc_timestamp(item.updated_at), item.key, item.id)

    facts = sorted((item for item in unique if item.origin == "memory"), key=priority)
    echoes = sorted((item for item in unique if item.origin == "conversation"),
                    key=lambda item: (-item.source_sequence, item.key, item.id))
    selected: list[MemoryCandidate] = []
    omitted: Counter[str] = Counter()
    corrected = set(corrected_keys)
    for item in [*facts, *echoes]:
        reason = ""
        if item.origin == "memory" and item.kind not in {"profile", "preference", "fact"}:
            reason = "unsupported_kind"
        elif not item.content.strip():
            reason = "empty_content"
        elif item.origin == "memory" and item.key in corrected:
            reason = "current_correction"
        elif item.origin == "memory" and len(item.content) > policy.max_fact_characters:
            reason = "item_over_character_limit"
        elif len(selected) >= policy.max_items:
            reason = "item_limit"
        elif budget == 0 or render_cost(tuple([*selected, item])) - base > budget:
            reason = "item_over_budget"
        if reason:
            omitted[reason] += 1
            if item.kind in {"profile", "preference"} and reason.startswith("item_"):
                omitted["required_fact_omitted"] += 1
            continue
        selected.append(item)
    result_items = tuple([item for item in selected if item.origin == "memory"] + sorted(
        (item for item in selected if item.origin == "conversation"),
        key=lambda item: (item.source_sequence, item.key, item.id),
    ))
    return MemoryProjectionResult(result_items, budget, render_cost(result_items) - base,
                                  base, input_limit, dict(omitted), digest)
