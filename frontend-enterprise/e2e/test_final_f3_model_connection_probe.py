"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : test_final_f3_model_connection_probe.py
@CallChain  : pytest → 只读模型诊断安全投影 → 固定字段与本地旁路
@Description: 纯函数测试不读取真实配置、不创建SDK客户端，也不访问供应商。
"""

from __future__ import annotations

from final_f3_model_connection_probe import _account_status, _receipt_projection


def test_receipt_projection_keeps_safe_call_classes_without_raw_values() -> None:
    """归档投影保留请求类别与稳定状态，不复制提示词、模型名或端点。"""
    rows = [{
        "request_class": "file_input", "status": "failed", "provider_status_code": 400,
        "provider_error_category": "invalid_request", "duration_ms": 2.5,
        "request_sha256": "a" * 64, "model_sha256": "b" * 64,
        "message": "PRIVATE_PROVIDER_BODY_CANARY", "base_url": "PRIVATE_URL_CANARY",
    }]
    observed = _receipt_projection(rows)
    assert observed == [{
        "request_class": "file_input", "status": "failed", "provider_status_code": 400,
        "provider_error_category": "invalid_request", "duration_ms": 2.5,
    }]


def test_non_deepseek_account_stage_is_explicitly_not_applicable() -> None:
    """非DeepSeek供应商沿项目流程跳过余额接口且不触发HTTP网络。"""
    assert _account_status("https://models.example.org/v1", "TEST_ONLY_FAKE_KEY") == {
        "status": "not_applicable", "http_status": None, "available": None,
    }
