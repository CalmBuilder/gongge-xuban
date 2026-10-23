"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f3_model_diagnostic.py
@CallChain  : I1实际模型最小诊断 → 原LLM合法重试 → 全SDK旁证收口
@Description: 允许原合法模型重试但逐条收口完整SDK旁证；诊断不代表业务或供应商质量通过。
"""

from __future__ import annotations


def diagnostic_coverage_passed(receipts: list[dict], health: dict) -> bool:
    """允许原合法重试的全部调用，最后成功且每次SDK均有有效旁证才完成诊断观察。"""
    count = len(receipts)
    return bool(
        count > 0 and health.get("valid") is True
        and all(type(health.get(key)) is int and health[key] == count
                for key in ("started_count", "settled_count", "receipt_count"))
        and type(health.get("in_flight_count")) is int and health["in_flight_count"] == 0
        and all(row.get("observation_valid") is True
                and row.get("status") in {"completed", "failed"} for row in receipts)
        and receipts[-1].get("status") == "completed"
    )
