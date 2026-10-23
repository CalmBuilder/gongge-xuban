"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f3_model_diagnostic.py
@CallChain  : pytest → 实际模型诊断反例 → 原重试次数与完整SDK旁证交叉核对
@Description: 原模型重试可多次；缺行、在途或观察失败不能通过，合成记录不替代真实外呼。
"""

from __future__ import annotations

import pytest

from final_f3_model_diagnostic import diagnostic_coverage_passed


def health(count: int) -> dict:
    """生成完整收口计数的合成标签，固定为非业务资格判据。"""
    return {"valid": True, "started_count": count, "settled_count": count,
            "in_flight_count": 0, "receipt_count": count}


@pytest.mark.parametrize("statuses", [
    ["completed"], ["completed", "completed"], ["failed", "completed"],
])
def test_original_legal_retry_keeps_all_receipts_without_single_sdk_limit(statuses) -> None:
    """合法空响应或瞬时异常后原重试成功允许多SDK，实际每条旁证仍完整覆盖。"""
    receipts = [{"status": status, "observation_valid": True} for status in statuses]
    assert diagnostic_coverage_passed(receipts, health(len(receipts))) is True


@pytest.mark.parametrize("counter", ["started_count", "settled_count", "receipt_count"])
def test_one_remaining_row_cannot_hide_missing_original_sdk_receipt(counter) -> None:
    """原调用计数出现2而只保留1行时，即使健康布尔被误用也必须拒绝。"""
    summary = health(1)
    summary[counter] = 2
    assert diagnostic_coverage_passed([{"status": "completed", "observation_valid": True}],
                                      summary) is False


@pytest.mark.parametrize("change", ["inflight", "unhealthy", "invalid_row", "failed_end"])
def test_incomplete_or_failed_diagnostic_never_passes(change) -> None:
    """在途、观察故障、无效行或未最终成功均拒绝，保留模型真实错误与诊断边界。"""
    summary, receipts = health(1), [{"status": "completed", "observation_valid": True}]
    if change == "inflight":
        summary["in_flight_count"] = 1
    elif change == "unhealthy":
        summary["valid"] = False
    elif change == "invalid_row":
        receipts[0]["observation_valid"] = False
    else:
        receipts[0]["status"] = "failed"
    assert diagnostic_coverage_passed(receipts, summary) is False


def test_empty_diagnostic_does_not_count_as_model_request_or_success() -> None:
    """零SDK和零旁证没有诊断完成事实，不能由空集合判断通过。"""
    assert diagnostic_coverage_passed([], health(0)) is False
