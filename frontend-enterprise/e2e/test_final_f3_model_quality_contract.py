"""
@Time       : 2026/10/17
@Author     : zhanglp8181
@File       : test_final_f3_model_quality_contract.py
@CallChain  : pytest → F3模型质量合同 → 真实SDK/来源版本旁证与供应商效果隔离
@Description: 先红后绿验证模型质量合同不会把DeepSeek模型API误当成vendor sandbox。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from final_f3_model_quality_contract import (
    ModelQualityContractError,
    ModelQualityResultError,
    safe_model_quality_summary,
    validate_model_quality_contract,
    validate_model_quality_result,
)


def _hash(seed: str) -> str:
    """生成只用于合同测试的固定脱敏摘要。"""
    return (seed * 64)[:64]


def quality_contract() -> dict:
    """构造不含端点、密钥、模型名或业务正文的模型质量合同。"""
    scenarios = (
        "normal", "knowledge_old", "knowledge_new", "knowledge_unknown",
        "memory_freeze", "memory_correction", "memory_clear", "feedback_improvement",
    )
    cases = [
        {"case_id": f"sop-{scenario}", "branch": "sop", "scenario": scenario,
         "human_label_sha256": _hash("a"), "expected_outcome": "answer",
         "source_expectation_sha256": _hash("b"), "version_expectation_sha256": _hash("c")}
        for scenario in scenarios
    ]
    cases.append({
        "case_id": "dta-normal", "branch": "dynamic_task", "scenario": "normal",
        "human_label_sha256": _hash("d"), "expected_outcome": "answer",
        "source_expectation_sha256": _hash("e"), "version_expectation_sha256": _hash("f"),
    })
    return {
        "schema_version": "final-f3-model-quality-v1",
        "acceptance_track": "model_quality",
        "provider": {
            "provider_kind": "model_api", "sdk_kind": "openai_compatible",
            "model_name_sha256": _hash("1"), "base_url_host_sha256": _hash("2"),
            "config_fingerprint_sha256": _hash("3"), "real_model_required": True,
            "sdk_observation_required": True, "business_effect_required": False,
            "remote_readback_required": False, "remote_cleanup_required": False,
            "vendor_sandbox_required": False, "real_vendor_qualified": False,
        },
        "cases": cases,
        "evidence": {
            "request_digest_rule_sha256": _hash("4"),
            "response_digest_rule_sha256": _hash("5"),
            "sdk_identity_rule_sha256": _hash("6"), "usage_required": True,
            "source_binding_required": True, "version_binding_required": True,
            "citation_check_required": True, "human_label_review_required": True,
            "model_self_attestation_allowed": False, "business_effect_claim_allowed": False,
            "remote_readback_required": False, "cleanup_required": False,
        },
    }


def _result(case_id: str = "sop-normal") -> dict:
    """生成一条真实SDK质量结果的安全摘要形状。"""
    return {
        "case_id": case_id, "sdk_receipt_valid": True,
        "request_sha256": _hash("7"), "response_sha256": _hash("8"),
        "sdk_identity_sha256": _hash("9"),
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        "source_binding_valid": True, "version_binding_valid": True,
        "citation_check_passed": True, "human_label_reviewed": True,
        "business_effect_claimed": False, "remote_readback_performed": False,
        "remote_cleanup_performed": False, "quality_decision": "pass",
    }


def test_model_quality_contract_freezes_model_track_without_vendor_effects() -> None:
    """合法模型质量合同只生成模型轨道摘要，并永久保留真实供应商未认证。"""
    source = quality_contract()
    frozen = validate_model_quality_contract(source)
    summary = safe_model_quality_summary(frozen)
    source["provider"]["model_name_sha256"] = _hash("z")
    private = frozen.private_data()
    private["cases"][0]["case_id"] = "changed"
    assert summary["acceptance_track"] == "model_quality"
    assert summary["real_model_quality"] is True
    assert summary["real_vendor_qualified"] is False
    assert summary["business_effect_required"] is False
    assert summary["remote_readback_required"] is False
    assert summary["remote_cleanup_required"] is False
    assert frozen.private_data()["cases"][0]["case_id"] == "sop-normal"


@pytest.mark.parametrize(("path", "value", "error"), [
    (("provider", "provider_kind"), "vendor_sandbox", "F3_MODEL_QUALITY_MODEL_PROVIDER_REQUIRED"),
    (("provider", "remote_readback_required"), True, "F3_MODEL_QUALITY_VENDOR_EFFECTS_EXCLUDED"),
    (("provider", "real_vendor_qualified"), True, "F3_MODEL_QUALITY_VENDOR_EFFECTS_EXCLUDED"),
    (("evidence", "business_effect_claim_allowed"), True, "F3_MODEL_QUALITY_EVIDENCE_POLICY_INVALID"),
])
def test_model_quality_contract_rejects_vendor_effect_or_qualification_fields(path, value, error) -> None:
    """模型质量轨道不能通过字段变形重新引入供应商业务效果验收。"""
    source = quality_contract()
    cursor = source
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ModelQualityContractError, match=error):
        validate_model_quality_contract(source)


@pytest.mark.parametrize("change", ["missing_branch", "missing_scenario", "duplicate_id"])
def test_model_quality_contract_requires_both_consumers_and_fixed_scenarios(change: str) -> None:
    """缺少SOP/DTA或必需质量场景不能由模型最小生成替代。"""
    source = quality_contract()
    if change == "missing_branch":
        source["cases"] = [row for row in source["cases"] if row["branch"] == "sop"]
    elif change == "missing_scenario":
        source["cases"] = [row for row in source["cases"] if row["scenario"] != "memory_clear"]
    else:
        source["cases"].append(deepcopy(source["cases"][0]))
    with pytest.raises(ModelQualityContractError):
        validate_model_quality_contract(source)


def test_model_quality_result_accepts_real_sdk_evidence_without_business_effect() -> None:
    """真实SDK旁证、来源版本和引用通过时，结果仍只代表模型质量。"""
    result = validate_model_quality_result(_result(), expected_case_id="sop-normal")
    assert result["business_effect_claimed"] is False
    assert result["remote_readback_performed"] is False
    assert result["quality_decision"] == "pass"


@pytest.mark.parametrize(("field", "value", "error"), [
    ("business_effect_claimed", True, "F3_MODEL_QUALITY_RESULT_POLICY_INVALID"),
    ("source_binding_valid", False, "F3_MODEL_QUALITY_RESULT_POLICY_INVALID"),
    ("usage", {"prompt_tokens": "10", "completion_tokens": 4, "total_tokens": 14},
     "F3_MODEL_QUALITY_USAGE_INVALID"),
    ("quality_decision", "self_attested_success", "F3_MODEL_QUALITY_DECISION_INVALID"),
])
def test_model_quality_result_rejects_self_attestation_or_missing_evidence(field, value, error) -> None:
    """模型自报成功、业务效果和缺失usage/来源证据不能使质量结果通过。"""
    result = _result()
    result[field] = value
    with pytest.raises(ModelQualityResultError, match=error):
        validate_model_quality_result(result, expected_case_id="sop-normal")
