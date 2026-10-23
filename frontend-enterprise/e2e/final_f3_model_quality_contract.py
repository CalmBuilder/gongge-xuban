"""
@Time       : 2026/10/17
@Author     : zhanglp8181
@File       : final_f3_model_quality_contract.py
@CallChain  : F3模型质量验收 → 冻结模型配置/人工标签 → SDK旁证与业务来源校验
@Description: 为模型API建立独立的质量验收合同；不把外部业务效果或供应商sandbox纳入模型质量门禁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any


_HASH = re.compile(r"[a-f0-9]{64}")
_CASE_ID = re.compile(r"[a-z][a-z0-9_-]{2,79}")
_BRANCHES = {"sop", "dynamic_task"}
_EXPECTED_OUTCOMES = {"answer", "no_answer", "degraded_answer"}
_SCENARIOS = {
    "normal",
    "knowledge_old",
    "knowledge_new",
    "knowledge_unknown",
    "memory_freeze",
    "memory_correction",
    "memory_clear",
    "feedback_improvement",
}


class ModelQualityContractError(ValueError):
    """只携带稳定拒绝码，不回显模型端点、提示词、回答或人工标签正文。"""

    def __init__(self, code: str) -> None:
        """保存供测试与安全摘要使用的固定错误类别。"""
        self.code = code
        super().__init__(code)


class ModelQualityResultError(ValueError):
    """只携带稳定结果校验码，不公开实际模型响应或业务内容。"""

    def __init__(self, code: str) -> None:
        """保存结果门禁的固定错误类别。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FrozenModelQualityContract:
    """保存严格规范JSON及其摘要，冻结后不再接受调用方原对象变化。"""

    canonical_json: str = field(repr=False)
    contract_sha256: str

    def private_data(self) -> dict[str, Any]:
        """返回独立副本，调用方修改副本不会改变冻结合同。"""
        try:
            value = json.loads(self.canonical_json)
        except (TypeError, ValueError):
            raise ModelQualityContractError("F3_MODEL_QUALITY_FROZEN_INVALID") from None
        if not isinstance(value, dict):
            raise ModelQualityContractError("F3_MODEL_QUALITY_FROZEN_INVALID")
        return value


def _object(value: object, code: str = "F3_MODEL_QUALITY_OBJECT_REQUIRED") -> dict[str, Any]:
    """要求有限JSON对象，不从空值或自由文本推断质量合同。"""
    if not isinstance(value, dict):
        raise ModelQualityContractError(code)
    return value


def _keys(value: dict[str, Any], expected: set[str]) -> None:
    """冻结对象字段集合，防止用额外字段偷偷恢复供应商效果要求。"""
    if set(value) != expected:
        raise ModelQualityContractError("F3_MODEL_QUALITY_SCHEMA_FIELDS_INVALID")


def _hash(value: object, code: str = "F3_MODEL_QUALITY_HASH_REQUIRED") -> str:
    """接受已脱敏SHA-256摘要，不接受端点、密钥或正文。"""
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ModelQualityContractError(code)
    return value


def _text(value: object, code: str = "F3_MODEL_QUALITY_TEXT_REQUIRED") -> str:
    """接受非空人工标签或固定枚举文本，不输出其内容。"""
    if not isinstance(value, str) or not value.strip():
        raise ModelQualityContractError(code)
    return value


def _canonical(value: object) -> str:
    """生成不含NaN、稳定键序和无空白的合同JSON。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ModelQualityContractError("F3_MODEL_QUALITY_JSON_INVALID") from None


def _validate_provider(value: object) -> dict[str, Any]:
    """冻结模型API身份摘要，并明确不要求业务供应商资格。"""
    provider = _object(value)
    _keys(provider, {
        "provider_kind", "sdk_kind", "model_name_sha256", "base_url_host_sha256",
        "config_fingerprint_sha256", "real_model_required", "sdk_observation_required",
        "business_effect_required", "remote_readback_required", "remote_cleanup_required",
        "vendor_sandbox_required", "real_vendor_qualified",
    })
    if provider["provider_kind"] != "model_api" or provider["sdk_kind"] != "openai_compatible":
        raise ModelQualityContractError("F3_MODEL_QUALITY_MODEL_PROVIDER_REQUIRED")
    for key in ("model_name_sha256", "base_url_host_sha256", "config_fingerprint_sha256"):
        _hash(provider[key])
    booleans = (
        "real_model_required", "sdk_observation_required", "business_effect_required",
        "remote_readback_required", "remote_cleanup_required", "vendor_sandbox_required",
        "real_vendor_qualified",
    )
    if any(type(provider[key]) is not bool for key in booleans):
        raise ModelQualityContractError("F3_MODEL_QUALITY_BOOLEAN_REQUIRED")
    if (provider["real_model_required"] is not True
            or provider["sdk_observation_required"] is not True
            or any(provider[key] is not False for key in (
                "business_effect_required", "remote_readback_required",
                "remote_cleanup_required", "vendor_sandbox_required", "real_vendor_qualified",
            ))):
        raise ModelQualityContractError("F3_MODEL_QUALITY_VENDOR_EFFECTS_EXCLUDED")
    return provider


def _validate_cases(value: object) -> list[dict[str, Any]]:
    """冻结两条消费者分支的人工标签和期望场景，禁止空集合代替质量样本。"""
    if not isinstance(value, list) or not value:
        raise ModelQualityContractError("F3_MODEL_QUALITY_CASES_REQUIRED")
    seen: set[str] = set()
    branches: set[str] = set()
    scenarios: set[str] = set()
    result: list[dict[str, Any]] = []
    for case in value:
        row = _object(case, "F3_MODEL_QUALITY_CASE_REQUIRED")
        _keys(row, {
            "case_id", "branch", "scenario", "human_label_sha256", "expected_outcome",
            "source_expectation_sha256", "version_expectation_sha256",
        })
        case_id = row["case_id"]
        if (not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id)
                or case_id in seen):
            raise ModelQualityContractError("F3_MODEL_QUALITY_CASE_ID_INVALID")
        branch, scenario = row["branch"], row["scenario"]
        if branch not in _BRANCHES or scenario not in _SCENARIOS:
            raise ModelQualityContractError("F3_MODEL_QUALITY_CASE_ENUM_INVALID")
        _hash(row["human_label_sha256"])
        if row["expected_outcome"] not in _EXPECTED_OUTCOMES:
            raise ModelQualityContractError("F3_MODEL_QUALITY_EXPECTED_OUTCOME_INVALID")
        _hash(row["source_expectation_sha256"])
        _hash(row["version_expectation_sha256"])
        seen.add(case_id)
        branches.add(branch)
        scenarios.add(scenario)
        result.append(dict(row))
    if branches != _BRANCHES or not _SCENARIOS.issubset(scenarios):
        raise ModelQualityContractError("F3_MODEL_QUALITY_COVERAGE_INCOMPLETE")
    return result


def _validate_evidence(value: object) -> dict[str, Any]:
    """冻结实际SDK和业务来源证据要求，不允许模型自评或远端效果替代。"""
    evidence = _object(value)
    _keys(evidence, {
        "request_digest_rule_sha256", "response_digest_rule_sha256", "sdk_identity_rule_sha256", "usage_required",
        "source_binding_required", "version_binding_required", "citation_check_required",
        "human_label_review_required", "model_self_attestation_allowed",
        "business_effect_claim_allowed", "remote_readback_required", "cleanup_required",
    })
    for key in ("request_digest_rule_sha256", "response_digest_rule_sha256", "sdk_identity_rule_sha256"):
        _hash(evidence[key])
    boolean_keys = (
        "usage_required", "source_binding_required", "version_binding_required",
        "citation_check_required", "human_label_review_required", "model_self_attestation_allowed",
        "business_effect_claim_allowed", "remote_readback_required", "cleanup_required",
    )
    if any(type(evidence[key]) is not bool for key in boolean_keys):
        raise ModelQualityContractError("F3_MODEL_QUALITY_BOOLEAN_REQUIRED")
    if (any(evidence[key] is not True for key in (
            "usage_required", "source_binding_required", "version_binding_required",
            "citation_check_required", "human_label_review_required",
        )) or any(evidence[key] is not False for key in (
            "model_self_attestation_allowed", "business_effect_claim_allowed",
            "remote_readback_required", "cleanup_required",
        ))):
        raise ModelQualityContractError("F3_MODEL_QUALITY_EVIDENCE_POLICY_INVALID")
    return evidence


def validate_model_quality_contract(value: object) -> FrozenModelQualityContract:
    """冻结独立模型质量合同；结构通过不授予真实供应商或业务效果资格。"""
    contract = _object(value)
    _keys(contract, {"schema_version", "acceptance_track", "provider", "cases", "evidence"})
    if contract["schema_version"] != "final-f3-model-quality-v1":
        raise ModelQualityContractError("F3_MODEL_QUALITY_SCHEMA_UNSUPPORTED")
    if contract["acceptance_track"] != "model_quality":
        raise ModelQualityContractError("F3_MODEL_QUALITY_TRACK_REQUIRED")
    provider = _validate_provider(contract["provider"])
    cases = _validate_cases(contract["cases"])
    evidence = _validate_evidence(contract["evidence"])
    canonical = _canonical({
        "schema_version": contract["schema_version"],
        "acceptance_track": contract["acceptance_track"],
        "provider": provider,
        "cases": cases,
        "evidence": evidence,
    })
    return FrozenModelQualityContract(
        canonical_json=canonical,
        contract_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def safe_model_quality_summary(contract: FrozenModelQualityContract) -> dict[str, Any]:
    """只返回合同摘要和质量轨道边界，不返回模型、端点或标签正文。"""
    value = contract.private_data()
    provider = value["provider"]
    return {
        "contract_sha256": contract.contract_sha256,
        "schema_version": value["schema_version"],
        "acceptance_track": value["acceptance_track"],
        "case_count": len(value["cases"]),
        "branches": sorted({row["branch"] for row in value["cases"]}),
        "scenarios": sorted({row["scenario"] for row in value["cases"]}),
        "provider_kind": provider["provider_kind"],
        "real_model_required": provider["real_model_required"],
        "real_model_quality": True,
        "real_vendor_qualified": False,
        "business_effect_required": False,
        "remote_readback_required": False,
        "remote_cleanup_required": False,
    }


def _usage(value: object) -> dict[str, int | None]:
    """校验真实SDK usage摘要，允许供应商未返回某一计数但拒绝自由文本。"""
    usage = _object(value, "F3_MODEL_QUALITY_USAGE_REQUIRED")
    if set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}:
        raise ModelQualityResultError("F3_MODEL_QUALITY_USAGE_SCHEMA_INVALID")
    if any(item is not None and (type(item) is not int or item < 0) for item in usage.values()):
        raise ModelQualityResultError("F3_MODEL_QUALITY_USAGE_INVALID")
    return usage


def validate_model_quality_result(value: object, *, expected_case_id: str) -> dict[str, Any]:
    """校验一条实际模型质量结果，禁止业务效果、自评成功和缺失来源版本绑定。"""
    result = _object(value, "F3_MODEL_QUALITY_RESULT_REQUIRED")
    expected = {
        "case_id", "sdk_receipt_valid", "request_sha256", "response_sha256",
        "sdk_identity_sha256", "usage", "source_binding_valid", "version_binding_valid",
        "citation_check_passed", "human_label_reviewed", "business_effect_claimed",
        "remote_readback_performed", "remote_cleanup_performed", "quality_decision",
    }
    if set(result) != expected:
        raise ModelQualityResultError("F3_MODEL_QUALITY_RESULT_SCHEMA_INVALID")
    if result["case_id"] != expected_case_id:
        raise ModelQualityResultError("F3_MODEL_QUALITY_RESULT_CASE_MISMATCH")
    for key in ("request_sha256", "response_sha256", "sdk_identity_sha256"):
        if not isinstance(result[key], str) or not _HASH.fullmatch(result[key]):
            raise ModelQualityResultError("F3_MODEL_QUALITY_RESULT_HASH_INVALID")
    _usage(result["usage"])
    booleans = (
        "sdk_receipt_valid", "source_binding_valid", "version_binding_valid",
        "citation_check_passed", "human_label_reviewed", "business_effect_claimed",
        "remote_readback_performed", "remote_cleanup_performed",
    )
    if any(type(result[key]) is not bool for key in booleans):
        raise ModelQualityResultError("F3_MODEL_QUALITY_RESULT_BOOLEAN_INVALID")
    if (result["sdk_receipt_valid"] is not True
            or result["source_binding_valid"] is not True
            or result["version_binding_valid"] is not True
            or result["citation_check_passed"] is not True
            or result["human_label_reviewed"] is not True
            or result["business_effect_claimed"] is not False
            or result["remote_readback_performed"] is not False
            or result["remote_cleanup_performed"] is not False):
        raise ModelQualityResultError("F3_MODEL_QUALITY_RESULT_POLICY_INVALID")
    if result["quality_decision"] not in {"pass", "fail", "blocked"}:
        raise ModelQualityResultError("F3_MODEL_QUALITY_DECISION_INVALID")
    return dict(result)
