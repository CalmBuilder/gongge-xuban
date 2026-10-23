"""
@Time       : 2026/10/19
@Author     : zhanglp8181
@File       : test_final_f3_model_quality_mapping.py
@CallChain  : MQ-I2逐案安全收据 → 合同case映射 → SDK/来源/版本/引用/人工标签门禁
@Description: 验证模型质量映射只接受可从安全收据独立推导的结果，不把诊断或调用方自评当成通过。
"""

from __future__ import annotations

import hashlib

import pytest

from final_f3_model_quality_contract import validate_model_quality_contract
from final_f3_model_quality_mapping import map_model_quality_case


def _hash(seed: str) -> str:
    """生成仅用于映射器测试的固定摘要，不承载模型或用户正文。"""
    return hashlib.sha256(seed.encode()).hexdigest()


def _contract() -> object:
    """构造带一个正常SOP case的最小完整合同，仍排除业务供应商效果。"""
    case = {
        "case_id": "sop-normal",
        "branch": "sop",
        "scenario": "normal",
        "human_label_sha256": _hash("label"),
        "expected_outcome": "answer",
        "source_expectation_sha256": _hash("source"),
        "version_expectation_sha256": _hash("version"),
    }
    scenarios = [
        "knowledge_old", "knowledge_new", "knowledge_unknown", "memory_freeze",
        "memory_correction", "memory_clear", "feedback_improvement",
    ]
    cases = [case]
    for scenario in scenarios:
        cases.append({
            "case_id": f"sop-{scenario}",
            "branch": "sop",
            "scenario": scenario,
            "human_label_sha256": _hash(f"label-{scenario}"),
            "expected_outcome": "no_answer" if scenario == "knowledge_unknown" else "answer",
            "source_expectation_sha256": _hash(f"source-{scenario}"),
            "version_expectation_sha256": _hash(f"version-{scenario}"),
        })
    cases.extend([
        {
            "case_id": f"dynamic_task-{scenario}",
            "branch": "dynamic_task",
            "scenario": scenario,
            "human_label_sha256": _hash(f"dta-label-{scenario}"),
            "expected_outcome": "no_answer" if scenario == "knowledge_unknown" else "answer",
            "source_expectation_sha256": _hash(f"dta-source-{scenario}"),
            "version_expectation_sha256": _hash(f"dta-version-{scenario}"),
        }
        for scenario in [
            "normal", "knowledge_old", "knowledge_new", "knowledge_unknown",
            "memory_freeze", "memory_correction", "memory_clear", "feedback_improvement",
        ]
    ])
    return validate_model_quality_contract({
        "schema_version": "final-f3-model-quality-v1",
        "acceptance_track": "model_quality",
        "provider": {
            "provider_kind": "model_api", "sdk_kind": "openai_compatible",
            "model_name_sha256": _hash("model"), "base_url_host_sha256": _hash("host"),
            "config_fingerprint_sha256": _hash("config"), "real_model_required": True,
            "sdk_observation_required": True, "business_effect_required": False,
            "remote_readback_required": False, "remote_cleanup_required": False,
            "vendor_sandbox_required": False, "real_vendor_qualified": False,
        },
        "cases": cases,
        "evidence": {
            "request_digest_rule_sha256": _hash("request-rule"),
            "response_digest_rule_sha256": _hash("response-rule"),
            "sdk_identity_rule_sha256": _hash("identity-rule"), "usage_required": True,
            "source_binding_required": True, "version_binding_required": True,
            "citation_check_required": True, "human_label_review_required": True,
            "model_self_attestation_allowed": False, "business_effect_claim_allowed": False,
            "remote_readback_required": False, "cleanup_required": False,
        },
    })


def _receipts() -> list[dict[str, object]]:
    """提供一条带业务身份和usage的安全SDK完成收据。"""
    return [{
        "call_id": "call-1", "observation_valid": True, "request_sha256": _hash("request"),
        "response_sha256": _hash("response"), "sdk_identity_sha256": _hash("identity"),
        "identity_sha256": {"session_id": _hash("session"), "source_input_id": _hash("source-input"),
                             "instance_id": _hash("instance")},
        "source_sha256": _hash("source"), "diagnostic_scope": None, "status": "completed",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }]


def _criteria(*, complete: bool = True) -> list[dict[str, object]]:
    """提供可从页面和后台事实推导的case、版本与引用标准。"""
    result: list[dict[str, object]] = [{
        "criterion": "case-sdk-identity-binding", "case_id": "sop-normal",
        "stage": "runtime-instance", "identity_sha256": {
            "session_id": _hash("session"), "source_input_id": _hash("source-input"),
            "instance_id": _hash("instance"),
        }, "passed": True,
    }, {
        "criterion": "SOP-normal-business-complete", "passed": complete,
        "version_binding_valid": complete, "citation_check_passed": complete,
        "source_binding_valid": complete,
    }]
    return result


def _archived_criteria() -> list[dict[str, object]]:
    """模拟当前安全归档后的case与身份摘要，保留原始正文不可逆。"""
    return [{
        "criterion": "case-sdk-identity-binding",
        "case_id_sha256": _hash("sop-normal"),
        "identity_sha256": {
            "session_id_sha256": _hash("session"),
            "source_input_id_sha256": _hash("source-input"),
        },
    }, {
        "criterion": "SOP-normal-business-complete", "passed": True,
        "version_binding_valid": True, "citation_check_passed": True,
    }]


def test_mapping_rejects_diagnostic_only_receipts() -> None:
    """只有模型预检scope而没有业务身份时必须阻塞，不能伪造质量通过。"""
    receipts = _receipts()
    receipts[0]["diagnostic_scope"] = {"schema": "model-preflight-v1"}
    receipts[0]["identity_sha256"] = {}
    mapped = map_model_quality_case(
        _contract(), case_id="sop-normal", criteria=_criteria(), sdk_receipts=receipts,
    )
    assert mapped["quality_decision"] == "blocked"
    assert "business_sdk_receipt" in mapped["blocked_reasons"]


def test_mapping_rejects_missing_version_or_citation() -> None:
    """页面完成但没有版本或引用事实时必须阻塞，不能由调用方布尔值放行。"""
    mapped = map_model_quality_case(
        _contract(), case_id="sop-normal", criteria=[{
            "criterion": "case-sdk-identity-binding", "case_id": "sop-normal",
            "stage": "runtime-instance", "identity_sha256": {
                "session_id": _hash("session"), "source_input_id": _hash("source-input"),
                "instance_id": _hash("instance"),
            }, "passed": True,
        }, {
            "criterion": "SOP-normal-business-complete", "passed": True,
        }], sdk_receipts=_receipts(),
    )
    assert mapped["quality_decision"] == "blocked"
    assert {"version_binding", "citation_check"} <= set(mapped["blocked_reasons"])


def test_mapping_requires_frozen_human_label() -> None:
    """人工标签摘要必须来自冻结合同，缺失或篡改不能被模型结果覆盖。"""
    with pytest.raises(ValueError, match="F3_MODEL_QUALITY_MAPPING_HUMAN_LABEL_REQUIRED"):
        map_model_quality_case(
            _contract(), case_id="sop-normal", criteria=_criteria(), sdk_receipts=_receipts(),
            human_label_sha256=_hash("wrong-label"),
        )


def test_mapping_accepts_archived_case_identity_hashes() -> None:
    """安全criterion的case和身份摘要可与业务SDK收据独立关联后才允许通过。"""
    mapped = map_model_quality_case(
        _contract(), case_id="sop-normal", criteria=_archived_criteria(),
        sdk_receipts=_receipts(), human_label_sha256=_hash("label"),
    )
    assert mapped["quality_decision"] == "pass"
