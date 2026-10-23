"""
@Time       : 2026/10/17
@Author     : zhanglp8181
@File       : test_final_f3_model_quality_registration.py
@CallChain  : pytest → MQ-I1预登记 → 配置摘要/合同冻结/无网络门禁
@Description: 验证模型质量预登记只冻结脱敏摘要，不启动业务provider、浏览器或模型请求。
"""

from __future__ import annotations

import json

import final_f3_model_quality_registration as registration


def configuration() -> dict[str, str]:
    """提供只在测试内使用的合成模型配置，不代表真实供应商。"""
    return {
        "DEMO_MODEL_BASE_URL": "https://model.example.test/v1",
        "DEMO_MODEL_NAME": "synthetic-quality-model",
        "DEMO_MODEL_API_KEY": "synthetic-key-not-output",
    }


def test_contract_hashes_configuration_without_persisting_secret() -> None:
    """当前模型配置只产生host/model/config摘要，合同明确排除供应商效果。"""
    contract = registration._contract(configuration())
    value = contract.private_data()
    encoded = json.dumps(value, ensure_ascii=False)
    assert "synthetic-quality-model" not in encoded
    assert "synthetic-key-not-output" not in encoded
    assert value["provider"]["provider_kind"] == "model_api"
    assert value["provider"]["real_vendor_qualified"] is False
    assert value["provider"]["remote_readback_required"] is False
    assert len(value["cases"]) == 16


def test_prepare_is_no_network_and_writes_only_safe_pre_registration(monkeypatch, tmp_path) -> None:
    """MQ-I1预登记不启动模型/业务provider/浏览器且输出合同与来源安全摘要。"""
    monkeypatch.setattr(registration, "_configuration", configuration)
    monkeypatch.setattr(registration, "snapshot", lambda _root: {"synthetic.py": "a" * 64})
    result = registration.prepare(run_id="mq-i1-synthetic", output_root=tmp_path)
    assert result == 0
    output = tmp_path / "mq-i1-synthetic"
    receipt = json.loads((output / "preparation.safe.json").read_text())
    assert receipt["preparation_passed"] is True
    assert receipt["network_calls"] == 0
    assert receipt["business_provider_calls"] == 0
    assert receipt["real_model_quality_attempted"] is False
    assert receipt["vendor_sandbox_required"] is False
    assert (output / "model-quality-contract.safe.json").is_file()


def test_prepare_without_model_configuration_stays_pending(monkeypatch, tmp_path) -> None:
    """配置缺失只产生待验收状态，不以空配置生成模型质量通过。"""
    monkeypatch.setattr(registration, "_configuration", lambda: None)
    monkeypatch.setattr(registration, "snapshot", lambda _root: {"synthetic.py": "a" * 64})
    result = registration.prepare(run_id="mq-i1-missing", output_root=tmp_path)
    assert result == 2
    receipt = json.loads((tmp_path / "mq-i1-missing" / "preparation.safe.json").read_text())
    assert receipt["preparation_passed"] is False
    assert receipt["missing_condition"] == "F3_MODEL_CONFIGURATION_MISSING_OR_UNSAFE"
    assert not (tmp_path / "mq-i1-missing" / "model-quality-contract.safe.json").exists()
