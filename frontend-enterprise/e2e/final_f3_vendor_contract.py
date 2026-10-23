"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f3_vendor_contract.py
@CallChain  : F3项目provider合同/历史声明 → 私有manifest/原工具合同 → 冻结声明与安全解析摘要
@Description: 冻结项目provider合同或历史外部声明并解析已有JSON；不是供应商实现或完整JRN08，不授予第三方资格或外呼。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re

from final_f3_preparation import PreparationError, digest as profile_digest, validate_profile


_BLOCKS = {
    "provider", "objects", "submission", "idempotency", "readback", "cleanup", "absence",
    "controls",
}
_TERMINAL_STATUSES = {
    "completed", "succeeded", "success", "failed", "cancelled", "canceled", "expired",
}


class VendorContractError(ValueError):
    """只携带固定拒绝码，不回显私有manifest、端点或业务正文。"""

    def __init__(self, code: str) -> None:
        """保存可公开的错误类别。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FrozenVendorContract:
    """持有不可变规范JSON及原profile摘要，不代表真实供应商资格。"""

    canonical_json: str = field(repr=False)
    profile_sha256: str

    def private_data(self) -> dict[str, object]:
        """返回独立私有副本，调用方不得用副本修改已冻结声明。"""
        try:
            value = json.loads(self.canonical_json)
        except (ValueError, TypeError):
            raise VendorContractError("F3_VENDOR_FROZEN_CONTRACT_INVALID") from None
        return _object(value)


def _object(value: object) -> dict:
    """要求明确JSON对象，不从空值、数组或自由文本推断缺失合同。"""
    if not isinstance(value, dict):
        raise VendorContractError("F3_VENDOR_MANIFEST_REQUIRED_BLOCK")
    return value


def _keys(value: dict, expected: set[str]) -> None:
    """核固定私有schema字段，拒绝未声明的资格或凭据字段。"""
    if value.keys() - expected:
        raise VendorContractError("F3_VENDOR_MANIFEST_UNDECLARED_FIELD")
    if expected - value.keys():
        raise VendorContractError("F3_VENDOR_MANIFEST_REQUIRED_BLOCK")


def _text(value: object) -> str:
    """只接受非空文本；错误不携带私有名称、路径或状态值。"""
    if not isinstance(value, str) or not value.strip():
        raise VendorContractError("F3_VENDOR_DECLARATION_VALUE_REQUIRED")
    return value


def _sha(value: object) -> str:
    """要求已声明证据的SHA格式，不据此推断证据真实或授权已验证。"""
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise VendorContractError("F3_VENDOR_EVIDENCE_SHA_REQUIRED")
    return value


def _counter(value: object) -> int:
    """效果、调用和保留期计数只接受非负整数，布尔不得冒充整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VendorContractError("F3_VENDOR_COUNTER_INVALID")
    return value


def _canonical(value: object) -> str:
    """冻结严格有限JSON，不调用对象字符串方法或接受NaN等非JSON数据。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        raise VendorContractError("F3_VENDOR_JSON_VALUE_INVALID") from None


def _digest(value: object) -> str:
    """对私有规范JSON作摘要，不输出原供应商或业务值。"""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _path(value: object) -> str:
    """声明原点号字典路径；不新增数组索引、表达式或执行型JSONPath。"""
    path = _text(value)
    if any(not part for part in path.split(".")):
        raise VendorContractError("F3_VENDOR_FIELD_PATH_INVALID")
    return path


def _lookup(payload: object, path: str) -> object:
    """沿冻结的原点号路径读取已有字典，缺失保留为None而不造字段。"""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _codes(value: object, *, minimum: int, maximum: int, allow_empty: bool = False) -> list:
    """核声明的HTTP整数状态码集合，不把布尔、重复或范围外值当真实回执。"""
    if not isinstance(value, list) or (not value and not allow_empty):
        raise VendorContractError("F3_VENDOR_HTTP_STATUS_CONTRACT_INVALID")
    codes = [_counter(item) for item in value]
    if len(codes) != len(set(codes)) or any(code < minimum or code > maximum for code in codes):
        raise VendorContractError("F3_VENDOR_HTTP_STATUS_CONTRACT_INVALID")
    return codes


def _object_schema(value: object) -> dict:
    """核内联对象schema及required归属，仅冻结声明、不执行供应商参数验证。"""
    schema = _object(value)
    properties, required = schema.get("properties"), schema.get("required")
    if (schema.get("type") != "object" or not isinstance(properties, dict) or not properties
            or not isinstance(required, list) or not required
            or any(not isinstance(key, str) or not key.strip() for key in properties)
            or any(not isinstance(item, str) or item not in properties for item in required)
            or len(required) != len(set(required))
            or any(not isinstance(item, dict) or not item for item in properties.values())):
        raise VendorContractError("F3_VENDOR_OBJECT_SCHEMA_INCOMPLETE")
    _canonical(schema)
    return schema


def _read_only_contract(value: dict) -> None:
    """独立查询声明只允许GET或只读POST，不将声明等同实际远端读回。"""
    if (value.get("method") not in {"GET", "POST"} or value.get("read_only") is not True
            or value.get("independent") is not True):
        raise VendorContractError("F3_VENDOR_INDEPENDENT_READBACK_REQUIRED")


def _controls(value: dict) -> None:
    """冻结正常一次、安全错误零效果与unknown先对账，不允许盲目自动重发。"""
    _keys(value, {"normal", "safe_error", "unknown"})
    normal, safe_error, unknown = (
        _object(value[key]) for key in ("normal", "safe_error", "unknown")
    )
    _keys(normal, {"expected_submit_calls", "expected_effects"})
    _keys(safe_error, {"http_status", "error_code_field", "error_code", "expected_submit_calls",
                       "expected_effects", "synthetic_trigger_schema_sha256"})
    _keys(unknown, {"readback_before_reissue", "blind_resend_calls", "allowed_effect_counts",
                    "reconciliation_contract_sha256"})
    actual = (
        _counter(normal["expected_submit_calls"]), _counter(normal["expected_effects"]),
        _counter(safe_error["expected_submit_calls"]), _counter(safe_error["expected_effects"]),
    )
    if actual != (1, 1, 1, 0):
        raise VendorContractError("F3_VENDOR_EXPECTED_EFFECT_MISMATCH")
    _codes([safe_error["http_status"]], minimum=400, maximum=599)
    _path(safe_error["error_code_field"])
    _text(safe_error["error_code"])
    _sha(safe_error["synthetic_trigger_schema_sha256"])
    blind_resends = _counter(unknown["blind_resend_calls"])
    effects = unknown["allowed_effect_counts"]
    if not isinstance(effects, list):
        raise VendorContractError("F3_VENDOR_UNKNOWN_RECONCILIATION_REQUIRED")
    allowed = [_counter(item) for item in effects]
    if (unknown["readback_before_reissue"] is not True or blind_resends
            or sorted(allowed) != [0, 1]):
        raise VendorContractError("F3_VENDOR_UNKNOWN_RECONCILIATION_REQUIRED")
    _sha(unknown["reconciliation_contract_sha256"])


def validate_vendor_manifest(value: object, *, profile: object) -> FrozenVendorContract:
    """冻结外部供应商或项目模拟provider合同；结构通过不授第三方资格。"""
    manifest = _object(value)
    _keys(manifest, _BLOCKS | {"schema_version", "evidence_origin"})
    project_track = manifest["evidence_origin"] == "project_provider_contract"
    expected_schema = (
        "final-f3-project-provider-contract-v1"
        if project_track else "final-f3-vendor-contract-v1"
    )
    if manifest["schema_version"] != expected_schema:
        raise VendorContractError("F3_VENDOR_MANIFEST_SCHEMA_UNSUPPORTED")
    if manifest["evidence_origin"] not in {"vendor_sandbox", "project_provider_contract"}:
        raise VendorContractError("F3_VENDOR_CONTROLLED_SOURCE_NOT_QUALIFICATION")
    try:
        original_profile = validate_profile(profile, project_simulated=project_track)
    except PreparationError as error:
        raise VendorContractError(error.code) from None
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract

    published = ToolReliabilityContract.model_validate(
        original_profile["reliability_contract"]
    )
    provider, objects, submission, idempotency, readback, cleanup, absence, controls = (
        _object(manifest[key]) for key in
        (
            "provider", "objects", "submission", "idempotency", "readback", "cleanup",
            "absence", "controls",
        )
    )
    _keys(provider, {
        "name", "documentation_sha256", "authorization_sha256", "minimal_scope_sha256",
    })
    _text(provider["name"])
    for key in ("documentation_sha256", "authorization_sha256", "minimal_scope_sha256"):
        _sha(provider[key])
    _keys(objects, {"namespace", "synthetic_only", "schema"})
    if (objects["synthetic_only"] is not True
            or objects["namespace"] != original_profile["objects"]["namespace"]):
        raise VendorContractError("F3_VENDOR_OWNED_NAMESPACE_MISMATCH")
    _object_schema(objects["schema"])
    _keys(submission, {"accepted_http_statuses", "accepted_statuses", "task_id_field",
                       "status_field", "result_field"})
    _codes(submission["accepted_http_statuses"], minimum=200, maximum=299)
    for key in ("task_id_field", "status_field", "result_field"):
        if _path(submission[key]) != getattr(published, key):
            raise VendorContractError("F3_VENDOR_ORIGINAL_FIELD_MAPPING_MISMATCH")
    statuses = submission["accepted_statuses"]
    if not isinstance(statuses, list) or not statuses:
        raise VendorContractError("F3_VENDOR_ACCEPTED_STATUS_REQUIRED")
    status_names = [_text(status) for status in statuses]
    if (len(status_names) != len(set(status_names))
            or any(str(published.status_mapping.get(status, status)).strip().lower()
                   in _TERMINAL_STATUSES
                   for status in status_names)):
        raise VendorContractError("F3_VENDOR_ACCEPTED_STATUS_REQUIRED")
    _keys(idempotency, {"mode", "remote_scope", "retention_seconds"})
    if (idempotency["mode"] != published.idempotency.mode
            or idempotency["remote_scope"] != published.idempotency.remote_scope):
        raise VendorContractError("F3_VENDOR_IDEMPOTENCY_SCOPE_MISMATCH")
    if _counter(idempotency["retention_seconds"]) == 0:
        raise VendorContractError("F3_VENDOR_IDEMPOTENCY_RETENTION_REQUIRED")
    _keys(readback, {"method", "read_only", "independent", "object_id_field", "effect_count_field",
                     "business_schema_sha256"})
    _read_only_contract(readback)
    _path(readback["object_id_field"])
    _path(readback["effect_count_field"])
    _sha(readback["business_schema_sha256"])
    _keys(cleanup, {"method", "owned_namespace_only"})
    if (cleanup["method"] not in {"POST", "PUT", "PATCH", "DELETE"}
            or cleanup["owned_namespace_only"] is not True):
        raise VendorContractError("F3_VENDOR_OWNED_CLEANUP_REQUIRED")
    _keys(absence, {"method", "read_only", "independent", "absent_http_statuses", "body_field",
                    "absent_values"})
    _read_only_contract(absence)
    absent_codes = _codes(
        absence["absent_http_statuses"], minimum=200, maximum=599, allow_empty=True,
    )
    absent_values = absence["absent_values"]
    if not isinstance(absent_values, list):
        raise VendorContractError("F3_VENDOR_ABSENCE_MAPPING_REQUIRED")
    if absence["body_field"] is not None:
        _path(absence["body_field"])
        if not absent_values:
            raise VendorContractError("F3_VENDOR_ABSENCE_MAPPING_REQUIRED")
    elif absent_values:
        raise VendorContractError("F3_VENDOR_ABSENCE_MAPPING_REQUIRED")
    if not absent_codes and absence["body_field"] is None:
        raise VendorContractError("F3_VENDOR_ABSENCE_MAPPING_REQUIRED")
    _controls(controls)
    _canonical(original_profile)
    return FrozenVendorContract(_canonical(manifest), profile_digest(original_profile))


def load_private_vendor_manifest(path: Path, *, profile: object) -> FrozenVendorContract:
    """只读取明确私有文件，不寻找凭据、访问业务库或探测供应商。"""
    if not path.is_file():
        raise VendorContractError("F3_VENDOR_MANIFEST_MISSING")
    if path.stat().st_mode & 0o077:
        raise VendorContractError("F3_VENDOR_MANIFEST_PRIVATE_PERMISSIONS_REQUIRED")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        raise VendorContractError("F3_VENDOR_MANIFEST_INVALID_JSON") from None
    return validate_vendor_manifest(value, profile=profile)


def safe_vendor_contract_summary(contract: FrozenVendorContract) -> dict[str, object]:
    """输出固定码、摘要和计数，不公开私有合同或把模拟轨道冒称第三方资格。"""
    private = contract.private_data()
    project_track = private["evidence_origin"] == "project_provider_contract"
    return {
        "status_code": (
            "F3_PROJECT_PROVIDER_CONTRACT_PASS"
            if project_track else "F3_VENDOR_DECLARATION_PENDING_QUALIFICATION"
        ),
        "provider_contract_track": "project_provider_contract" if project_track else "vendor_sandbox",
        "project_provider_contract_passed": project_track,
        "structural_contract_complete": True, "real_provider_qualified": False,
        "JRN08_passed": False,
        "manifest_sha256": _digest(private), "profile_sha256": contract.profile_sha256,
        "namespace_sha256": _digest(private["objects"]["namespace"]),
        "object_schema_sha256": _digest(private["objects"]["schema"]),
        "object_property_count": len(private["objects"]["schema"]["properties"]),
        "idempotency_retention_seconds": private["idempotency"]["retention_seconds"],
        "normal_expected_effects": private["controls"]["normal"]["expected_effects"],
        "safe_error_expected_effects": private["controls"]["safe_error"]["expected_effects"],
        "contract_section_sha256": {key: _digest(private[key]) for key in sorted(_BLOCKS)},
    }


def parse_submission_receipt(
    contract: FrozenVendorContract, payload: object, *, http_status: int,
) -> dict[str, object]:
    """仅按冻结映射解析已有JSON旁证，不制造task或把accepted当业务完成。"""
    submission = contract.private_data()["submission"]
    task = _lookup(payload, submission["task_id_field"])
    status = _lookup(payload, submission["status_field"])
    if (isinstance(http_status, bool) or not isinstance(http_status, int)
            or http_status not in submission["accepted_http_statuses"]
            or isinstance(task, bool) or not isinstance(task, (str, int)) or not str(task).strip()
            or isinstance(status, bool) or str(status) not in submission["accepted_statuses"]):
        raise VendorContractError("F3_VENDOR_ACCEPTED_RECEIPT_MISMATCH")
    return {
        "status_code": "F3_VENDOR_DECLARED_ACCEPTED_OBSERVED",
        "accepted_observed": True, "business_completed": False, "real_provider_qualified": False,
        "task_id_sha256": _digest(str(task)), "status_sha256": _digest(status),
        "result_sha256": _digest(_lookup(payload, submission["result_field"])),
        "payload_sha256": _digest(payload),
    }


def parse_business_readback(
    contract: FrozenVendorContract, payload: object,
) -> dict[str, object]:
    """只解析已有独立读回声明中的整数效果计数，不执行查询或推断远端资格。"""
    readback = contract.private_data()["readback"]
    effect_count = _counter(_lookup(payload, readback["effect_count_field"]))
    object_id = _lookup(payload, readback["object_id_field"])
    if (isinstance(object_id, bool) or not isinstance(object_id, (str, int))
            or not str(object_id).strip()):
        raise VendorContractError("F3_VENDOR_READBACK_OBJECT_ID_REQUIRED")
    return {
        "status_code": "F3_VENDOR_DECLARED_READBACK_PARSED", "effect_count": effect_count,
        "real_provider_qualified": False, "JRN08_passed": False,
        "object_id_sha256": _digest(str(object_id)), "payload_sha256": _digest(payload),
    }
