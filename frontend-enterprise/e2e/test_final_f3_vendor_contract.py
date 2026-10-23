"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f3_vendor_contract.py
@CallChain  : pytest → F3私有manifest边界 → 原profile绑定/冻结/安全解析
@Description: 无网络正式反例；覆盖缺项、旧schema、假资格、布尔计数及未对账重发拒绝。
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from final_f3_preparation import digest
from final_f3_vendor_contract import (
    VendorContractError,
    load_private_vendor_manifest,
    parse_business_readback,
    parse_submission_receipt,
    safe_vendor_contract_summary,
    validate_vendor_manifest,
)
from test_final_f3_preparation import declared_profile, declared_project_provider_profile


def declared_manifest() -> dict:
    """仅生成合成无网络声明；摘要完整也不能宣称供应商身份已验证。"""
    return {
        "schema_version": "final-f3-vendor-contract-v1",
        "evidence_origin": "vendor_sandbox",
        "provider": {
            "name": "PRIVATE_VENDOR_CANARY",
            "documentation_sha256": "a" * 64,
            "authorization_sha256": "b" * 64,
            "minimal_scope_sha256": "c" * 64,
        },
        "objects": {
            "namespace": "f3_owned", "synthetic_only": True,
            "schema": {"type": "object", "properties": {
                "object_id": {"type": "string"}, "amount": {"type": "integer"},
            }, "required": ["object_id", "amount"], "additionalProperties": False},
        },
        "submission": {
            "accepted_http_statuses": [202], "accepted_statuses": ["accepted"],
            "task_id_field": "taskId", "status_field": "status", "result_field": "result",
        },
        "idempotency": {"mode": "request_key", "remote_scope": "f3_owned",
                        "retention_seconds": 86400},
        "readback": {"method": "GET", "read_only": True, "independent": True,
                     "object_id_field": "object_id", "effect_count_field": "effect_count",
                     "business_schema_sha256": "d" * 64},
        "cleanup": {"method": "DELETE", "owned_namespace_only": True},
        "absence": {"method": "GET", "read_only": True, "independent": True,
                    "absent_http_statuses": [404], "body_field": None, "absent_values": []},
        "controls": {
            "normal": {"expected_submit_calls": 1, "expected_effects": 1},
            "safe_error": {"http_status": 422, "error_code_field": "error.code",
                           "error_code": "PRIVATE_VENDOR_ERROR_CANARY",
                           "expected_submit_calls": 1, "expected_effects": 0,
                           "synthetic_trigger_schema_sha256": "e" * 64},
            "unknown": {"readback_before_reissue": True, "blind_resend_calls": 0,
                        "allowed_effect_counts": [0, 1],
                        "reconciliation_contract_sha256": "f" * 64},
        },
    }


def freeze():
    """构造本例冻结声明，只供已有JSON解析，不发送外呼。"""
    return validate_vendor_manifest(declared_manifest(), profile=declared_profile())


def project_manifest() -> dict:
    """构造项目模拟provider合同声明，单独标记为项目合同轨道。"""
    manifest = declared_manifest()
    manifest["schema_version"] = "final-f3-project-provider-contract-v1"
    manifest["evidence_origin"] = "project_provider_contract"
    return manifest


def test_complete_declaration_is_frozen_but_not_qualified() -> None:
    """合法私有声明绑定原profile且只产生待验摘要；修改原输入和副本不改冻结值。"""
    manifest, profile = declared_manifest(), declared_profile()
    frozen = validate_vendor_manifest(manifest, profile=profile)
    summary = safe_vendor_contract_summary(frozen)
    assert summary["structural_contract_complete"] is True
    assert summary["real_provider_qualified"] is False and summary["JRN08_passed"] is False
    assert summary["status_code"] == "F3_VENDOR_DECLARATION_PENDING_QUALIFICATION"
    assert summary["normal_expected_effects"] == 1 and summary["safe_error_expected_effects"] == 0
    assert len(summary["manifest_sha256"]) == len(summary["profile_sha256"]) == 64
    assert summary["profile_sha256"] == digest(profile)
    manifest["objects"]["namespace"] = "changed-after-validation"
    profile["human_labels"]["normal_effect"] = 0
    copy = frozen.private_data()
    copy["objects"]["namespace"] = "changed-private-copy"
    assert frozen.private_data()["objects"]["namespace"] == "f3_owned"
    assert safe_vendor_contract_summary(frozen) == summary
    assert "PRIVATE_VENDOR_CANARY" not in json.dumps(summary)
    assert "PRIVATE_VENDOR_ERROR_CANARY" not in json.dumps(summary)


def test_project_provider_contract_is_passed_without_external_vendor_claim() -> None:
    """项目模拟provider按独立manifest轨道通过，第三方资格字段仍保持false。"""
    frozen = validate_vendor_manifest(
        project_manifest(), profile=declared_project_provider_profile(),
    )
    summary = safe_vendor_contract_summary(frozen)
    assert summary["status_code"] == "F3_PROJECT_PROVIDER_CONTRACT_PASS"
    assert summary["provider_contract_track"] == "project_provider_contract"
    assert summary["project_provider_contract_passed"] is True
    assert summary["real_provider_qualified"] is False


@pytest.mark.parametrize("schema", ["final-f3-vendor-contract-v2", "", True])
def test_unknown_schema_is_not_parsed_as_current(schema: object) -> None:
    """未知或错误schema不按v1猜测，异常仅公开固定类别。"""
    manifest = declared_manifest()
    manifest["schema_version"] = schema
    with pytest.raises(VendorContractError, match="F3_VENDOR_MANIFEST_SCHEMA_UNSUPPORTED"):
        validate_vendor_manifest(manifest, profile=declared_profile())


@pytest.mark.parametrize("block", ["provider", "objects", "submission", "idempotency",
                                   "readback", "cleanup", "absence", "controls"])
def test_required_contract_blocks_are_not_inferred(block: str) -> None:
    """缺少任一业务或清理块不能由profile端点自动补完整资格声明。"""
    manifest = declared_manifest()
    manifest.pop(block)
    with pytest.raises(VendorContractError, match="F3_VENDOR_MANIFEST_REQUIRED_BLOCK"):
        validate_vendor_manifest(manifest, profile=declared_profile())


@pytest.mark.parametrize("location", ["normal", "safe_error", "blind_resend", "retention"])
def test_boolean_cannot_replace_effect_or_retention_counter(location: str) -> None:
    """布尔值不得利用Python整数关系充当一次/零次效果或幂等保留秒数。"""
    manifest = declared_manifest()
    if location in {"normal", "safe_error"}:
        manifest["controls"][location]["expected_effects"] = location == "normal"
    elif location == "blind_resend":
        manifest["controls"]["unknown"]["blind_resend_calls"] = False
    else:
        manifest["idempotency"]["retention_seconds"] = True
    with pytest.raises(VendorContractError, match="F3_VENDOR_COUNTER_INVALID"):
        validate_vendor_manifest(manifest, profile=declared_profile())


@pytest.mark.parametrize("change", [
    "namespace", "task_field", "scope", "retention", "object_schema",
])
def test_declared_identity_fields_and_objects_match_original_profile(change: str) -> None:
    """命名空间、字段映射、远端scope与完整对象schema不得脱离冻结原profile。"""
    manifest = declared_manifest()
    if change == "namespace":
        manifest["objects"]["namespace"] = "unowned_namespace"
    elif change == "task_field":
        manifest["submission"]["task_id_field"] = "other.task"
    elif change == "scope":
        manifest["idempotency"]["remote_scope"] = "other_scope"
    elif change == "retention":
        manifest["idempotency"]["retention_seconds"] = 0
    else:
        manifest["objects"]["schema"]["required"] = ["unknown_property"]
    with pytest.raises(VendorContractError):
        validate_vendor_manifest(manifest, profile=declared_profile())


@pytest.mark.parametrize("control", [
    "safe_error_effect", "no_readback", "blind_resend", "no_absence",
])
def test_errors_unknown_and_cleanup_cannot_drop_original_controls(control: str) -> None:
    """真实错误须零效果，unknown须先读回且不盲发，cleanup须独立缺席读回。"""
    manifest = declared_manifest()
    if control == "safe_error_effect":
        manifest["controls"]["safe_error"]["expected_effects"] = 1
    elif control == "no_readback":
        manifest["controls"]["unknown"]["readback_before_reissue"] = False
    elif control == "blind_resend":
        manifest["controls"]["unknown"]["blind_resend_calls"] = 1
    else:
        manifest["absence"]["independent"] = False
    with pytest.raises(VendorContractError):
        validate_vendor_manifest(manifest, profile=declared_profile())


@pytest.mark.parametrize("condition", [
    "local_origin", "local_endpoint", "vendor_hmac", "poll_auth",
])
def test_local_fixture_and_unimplemented_auth_cannot_be_qualified(condition: str) -> None:
    """受控来源、本地provider、通用vendor HMAC及继承提交鉴权不得冒充原合同。"""
    manifest, profile = declared_manifest(), declared_profile()
    if condition == "local_origin":
        manifest["evidence_origin"] = "local_fixture"
    elif condition == "local_endpoint":
        profile["submit_url"] = "http://127.0.0.1/__e2e/provider"
    elif condition == "vendor_hmac":
        profile.update(completion_path="callback", callback_contract="vendor_hmac")
    else:
        profile["status_auth"] = "bearer"
    with pytest.raises(VendorContractError):
        validate_vendor_manifest(manifest, profile=profile)


def test_original_callback_binding_can_be_declared_without_qualification() -> None:
    """原平台token/Operation/task绑定的声明可准备，但不证明供应商callback可达。"""
    profile = declared_profile()
    profile.update(completion_path="callback", callback_contract="project_token_operation_task",
                   callback_ingress_url="https://ingress.example.org/api/external-tasks")
    summary = safe_vendor_contract_summary(
        validate_vendor_manifest(declared_manifest(), profile=profile)
    )
    assert summary["real_provider_qualified"] is False


def test_nested_original_fields_parse_only_existing_receipt_to_hashes() -> None:
    """冻结字段按原点号路径解析已有JSON，accepted保待验且不公开task/result正文。"""
    manifest, profile = declared_manifest(), declared_profile()
    for field in ["task_id_field", "status_field", "result_field"]:
        value = "reply." + manifest["submission"][field]
        manifest["submission"][field] = value
        profile["reliability_contract"][field] = value
    frozen = validate_vendor_manifest(manifest, profile=profile)
    receipt = parse_submission_receipt(frozen, {"reply": {"taskId": "PRIVATE_TASK_CANARY",
                                      "status": "accepted", "result": "PRIVATE_RESULT_CANARY"}},
                                      http_status=202)
    assert receipt["accepted_observed"] is True and receipt["business_completed"] is False
    assert receipt["real_provider_qualified"] is False
    assert len(receipt["task_id_sha256"]) == 64
    assert "PRIVATE_TASK_CANARY" not in json.dumps(receipt)
    assert "PRIVATE_RESULT_CANARY" not in json.dumps(receipt)


@pytest.mark.parametrize("change", [
    "task_missing", "status_missing", "terminal_status", "http_error",
])
def test_accepted_receipt_requires_actual_task_and_declared_nonterminal_status(change: str) -> None:
    """HTTP受理不能补造task或将完成/失败回执冒称实际观察到异步等待。"""
    payload = {"taskId": "PRIVATE_TASK_CANARY", "status": "accepted"}
    status = 202
    if change == "task_missing":
        payload.pop("taskId")
    elif change == "status_missing":
        payload.pop("status")
    elif change == "terminal_status":
        payload["status"] = "completed"
    else:
        status = 422
    with pytest.raises(VendorContractError, match="F3_VENDOR_ACCEPTED_RECEIPT_MISMATCH"):
        parse_submission_receipt(freeze(), payload, http_status=status)


def test_independent_readback_retains_duplicate_effect_evidence() -> None:
    """整数重复效果按实计数供验收拒绝，不能截断为一次或由本地状态改零。"""
    summary = parse_business_readback(
        freeze(), {"object_id": "PRIVATE_OBJECT_CANARY", "effect_count": 2},
    )
    assert summary["effect_count"] == 2 and summary["real_provider_qualified"] is False
    assert "PRIVATE_OBJECT_CANARY" not in json.dumps(summary)


@pytest.mark.parametrize("counter", [True, False, -1, "1", None])
def test_readback_boolean_and_missing_effects_are_rejected(counter: object) -> None:
    """已有业务读回必须包含实际非负整数效果，布尔或无数据不能被判零效果。"""
    with pytest.raises(VendorContractError, match="F3_VENDOR_COUNTER_INVALID"):
        parse_business_readback(
            freeze(), {"object_id": "PRIVATE_OBJECT_CANARY", "effect_count": counter},
        )


def test_private_file_loader_does_not_expose_content_or_relax_permissions(
    tmp_path: Path,
) -> None:
    """缺文件、公开权限和非法JSON分别固定码；合法600仍只冻结未验声明。"""
    path = tmp_path / "vendor.private.json"
    with pytest.raises(VendorContractError, match="F3_VENDOR_MANIFEST_MISSING"):
        load_private_vendor_manifest(path, profile=declared_profile())
    path.write_text(json.dumps(declared_manifest()))
    path.chmod(0o644)
    with pytest.raises(
        VendorContractError, match="F3_VENDOR_MANIFEST_PRIVATE_PERMISSIONS_REQUIRED",
    ):
        load_private_vendor_manifest(path, profile=declared_profile())
    path.chmod(0o600)
    summary = safe_vendor_contract_summary(
        load_private_vendor_manifest(path, profile=declared_profile())
    )
    assert summary["real_provider_qualified"] is False
    path.write_text("PRIVATE_INVALID_CANARY")
    with pytest.raises(VendorContractError, match="F3_VENDOR_MANIFEST_INVALID_JSON") as caught:
        load_private_vendor_manifest(path, profile=declared_profile())
    assert "PRIVATE_INVALID_CANARY" not in str(caught.value)


def test_extra_fields_and_secret_payload_do_not_enter_public_summary() -> None:
    """未知资格字段拒绝，不允许声明qualified或借任意秘密字段绕开白名单。"""
    manifest = deepcopy(declared_manifest())
    manifest["qualified"] = True
    manifest["api_key"] = "PRIVATE_KEY_CANARY"
    with pytest.raises(VendorContractError, match="F3_VENDOR_MANIFEST_UNDECLARED_FIELD") as caught:
        validate_vendor_manifest(manifest, profile=declared_profile())
    assert "PRIVATE_KEY_CANARY" not in str(caught.value)


@pytest.mark.parametrize("mapped", [False, True])
def test_terminal_status_cannot_be_disguised_as_accepted_with_whitespace(mapped: bool) -> None:
    """原事件入口会strip终态，声明不能借空白把已完成回执登记成异步等待。"""
    manifest, profile = declared_manifest(), declared_profile()
    raw = "DONE" if mapped else "completed "
    manifest["submission"]["accepted_statuses"] = [raw]
    if mapped:
        profile["reliability_contract"]["status_mapping"] = {raw: "completed "}
    with pytest.raises(VendorContractError, match="F3_VENDOR_ACCEPTED_STATUS_REQUIRED"):
        validate_vendor_manifest(manifest, profile=profile)
