"""
@Time       : 2026/10/19
@Author     : zhanglp8181
@File       : final_f3_model_quality_mapping.py
@CallChain  : MQ-I2安全收据 → 合同case识别 → SDK/来源/版本/引用/人工标签独立映射
@Description: 将安全摘要映射为模型质量结果；诊断scope、缺失事实和自评字段均拒绝放行。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from final_f3_model_quality_contract import (
    FrozenModelQualityContract,
    validate_model_quality_result,
)


_HASH = re.compile(r"[a-f0-9]{64}")
_DIAGNOSTIC_SCOPE = "diagnostic_scope"
_IDENTITY_KEYS = ("session_id", "source_input_id")
_BINDING_IDENTITY_KEYS = frozenset({
    "tenant_id", "user_id", "agent_id", "session_id", "source_input_id", "turn_id",
    "instance_id", "skill_version_id", "knowledge_version_id", "plan_id", "operation_id",
    "attempt_id", "signal_id", "task_id",
})


def _digest(value: object) -> str:
    """对已脱敏JSON摘要计算稳定哈希，不接受或输出模型与用户正文。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _case(contract: FrozenModelQualityContract, case_id: str) -> dict[str, Any]:
    """从冻结合同读取唯一case，拒绝调用方临时改写标签或期望场景。"""
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("F3_MODEL_QUALITY_MAPPING_CASE_REQUIRED")
    value = contract.private_data()
    matches = [row for row in value["cases"] if row.get("case_id") == case_id]
    if len(matches) != 1:
        raise ValueError("F3_MODEL_QUALITY_MAPPING_CASE_UNKNOWN")
    return matches[0]


def _business_receipts(value: object) -> tuple[list[dict[str, Any]], list[str]]:
    """只选无diagnostic scope且有业务身份的SDK收据，并报告固定拒绝类别。"""
    if not isinstance(value, list):
        return [], ["sdk_receipts"]
    selected: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        if row.get(_DIAGNOSTIC_SCOPE) is not None:
            continue
        identity = row.get("identity_sha256")
        if not isinstance(identity, dict) or not identity:
            continue
        selected.append(row)
    reasons: list[str] = []
    if not selected:
        reasons.append("business_sdk_receipt")
        return [], reasons
    for row in selected:
        identity = row.get("identity_sha256")
        usage = row.get("usage")
        if row.get("observation_valid") is not True or row.get("status") != "completed":
            reasons.append("sdk_receipt")
        if not isinstance(row.get("request_sha256"), str) or not _HASH.fullmatch(row["request_sha256"]):
            reasons.append("request_digest")
        if not isinstance(row.get("response_sha256"), str) or not _HASH.fullmatch(row["response_sha256"]):
            reasons.append("response_digest")
        if not isinstance(row.get("source_sha256"), str) or not _HASH.fullmatch(row["source_sha256"]):
            reasons.append("source_digest")
        if (not isinstance(identity, dict)
                or any(not isinstance(identity.get(key), str) or not _HASH.fullmatch(identity[key])
                       for key in _IDENTITY_KEYS)):
            reasons.append("sdk_identity")
        if (not isinstance(usage, dict)
                or set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}
                or any(item is not None and (type(item) is not int or item < 0)
                       for item in usage.values())):
            reasons.append("usage")
    return selected, list(dict.fromkeys(reasons))


def _source_binding(criteria: list[dict[str, Any]], receipts: list[dict[str, Any]], case_id: str) -> bool:
    """比较case绑定行与业务SDK身份摘要，防止只凭页面完成标记通过。"""
    case_digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    bindings = [row for row in criteria
                if row.get("criterion") == "case-sdk-identity-binding"
                and (row.get("case_id") == case_id or row.get("case_id_sha256") == case_digest)]
    if not bindings:
        return False
    receipt_identities = [row.get("identity_sha256") for row in receipts]
    for binding in bindings:
        identity = binding.get("identity_sha256")
        if binding.get("passed") is not None and binding.get("passed") is not True:
            return False
        if not isinstance(identity, dict):
            return False
        normalized: dict[str, str] = {}
        for key, value in identity.items():
            base_key = key[:-7] if key.endswith("_sha256") else key
            if (base_key not in _BINDING_IDENTITY_KEYS or not isinstance(value, str)
                    or not _HASH.fullmatch(value)):
                return False
            normalized[base_key] = value
        if not normalized or not any(
                all(receipt.get(key) == value for key, value in normalized.items())
                for receipt in receipt_identities if isinstance(receipt, dict)):
            return False
    return True


def _criterion_passed(criteria: list[dict[str, Any]]) -> bool:
    """要求至少一条非诊断质量标准通过，拒绝把预检或身份行当业务质量。"""
    excluded = {"case-sdk-identity-binding", "actual-model-connection-diagnostic",
                "actual-model-native-capabilities"}
    candidates = [row for row in criteria
                  if isinstance(row.get("criterion"), str)
                  and row["criterion"] not in excluded
                  and isinstance(row.get("passed"), bool)]
    return bool(candidates) and all(row["passed"] is True for row in candidates)


def _version_binding(criteria: list[dict[str, Any]]) -> bool:
    """只接受来源记录明确写出的版本绑定，不从case名称或调用方布尔值推断。"""
    return any(row.get("passed") is True and row.get("version_binding_valid") is True
               for row in criteria if isinstance(row, dict))


def _citation_check(criteria: list[dict[str, Any]]) -> bool:
    """要求质量记录明确报告引用检查通过，unknown也必须有显式零引用结论。"""
    return any(row.get("passed") is True and row.get("citation_check_passed") is True
               for row in criteria if isinstance(row, dict))


def _usage(receipts: list[dict[str, Any]]) -> dict[str, int | None]:
    """汇总业务SDK usage为稳定安全标量，缺失计数保留为None。"""
    totals: dict[str, int | None] = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
    for row in receipts:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            return {key: None for key in totals}
        for key in totals:
            value = usage.get(key)
            if value is None:
                totals[key] = None
            elif totals[key] is not None:
                totals[key] += value
    return totals


def map_model_quality_case(
    contract: FrozenModelQualityContract,
    *,
    case_id: str,
    criteria: object,
    sdk_receipts: object,
    human_label_sha256: str | None = None,
) -> dict[str, Any]:
    """从安全证据独立生成case结果；缺任一必需事实时返回blocked而非自评pass。"""
    expected = _case(contract, case_id)
    if human_label_sha256 is not None and human_label_sha256 != expected["human_label_sha256"]:
        raise ValueError("F3_MODEL_QUALITY_MAPPING_HUMAN_LABEL_REQUIRED")
    criteria_rows = criteria if isinstance(criteria, list) else []
    criteria_rows = [row for row in criteria_rows if isinstance(row, dict)]
    receipts, blocked = _business_receipts(sdk_receipts)
    if not _source_binding(criteria_rows, receipts, case_id):
        blocked.append("source_binding")
    if not _version_binding(criteria_rows):
        blocked.append("version_binding")
    if not _citation_check(criteria_rows):
        blocked.append("citation_check")
    if human_label_sha256 is None:
        blocked.append("human_label")
    if not _criterion_passed(criteria_rows):
        blocked.append("quality_criterion")
    blocked = list(dict.fromkeys(blocked))
    request_digest = _digest(sorted(row.get("request_sha256") for row in receipts))
    response_digest = _digest(sorted(row.get("response_sha256") for row in receipts))
    identity_digest = _digest(sorted(_digest(row.get("identity_sha256")) for row in receipts))
    result = {
        "case_id": case_id,
        "sdk_receipt_valid": not any(item in blocked for item in {
            "business_sdk_receipt", "sdk_receipt", "request_digest", "response_digest",
            "source_digest", "sdk_identity", "usage",
        }),
        "request_sha256": request_digest,
        "response_sha256": response_digest,
        "sdk_identity_sha256": identity_digest,
        "usage": _usage(receipts),
        "source_binding_valid": "source_binding" not in blocked,
        "version_binding_valid": "version_binding" not in blocked,
        "citation_check_passed": "citation_check" not in blocked,
        "human_label_reviewed": "human_label" not in blocked,
        "business_effect_claimed": False,
        "remote_readback_performed": False,
        "remote_cleanup_performed": False,
        "quality_decision": "pass" if not blocked else "blocked",
    }
    if not blocked:
        validate_model_quality_result(result, expected_case_id=case_id)
    return {
        "contract_sha256": contract.contract_sha256,
        "case_id": case_id,
        "quality_decision": result["quality_decision"],
        "blocked_reasons": blocked,
        "sdk_receipt_count": len(receipts),
        "result": result,
    }
