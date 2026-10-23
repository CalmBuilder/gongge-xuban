"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : test_final_f3_preparation.py
@CallChain  : pytest → F3独立准备入口 → 原工具合同/安全观察与效果断言
@Description: 验证本地替身拒绝、poll/callback准确边界、未知效果先对账及私有内容不公开。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from final_f3_preparation import (
    PreparationError,
    assert_effect_contract,
    load_private_profile,
    model_config_payload_fingerprint,
    safe_profile_summary,
    safe_sdk_receipt,
    validate_profile,
)


def declared_profile() -> dict:
    """构造仅用于无网络测试的供应商声明，不把结构有效称为真实资格。"""
    return {
        "schema_version": "final-f3-sandbox-v1", "provider_kind": "vendor_sandbox",
        "documentation_url": "https://docs.example.org/tasks",
        "submit_url": "https://sandbox.example.org/tasks", "submit_method": "POST",
        "readback_url": "https://sandbox.example.org/effects/{taskId}",
        "cleanup_url": "https://sandbox.example.org/objects/{objectId}",
        "completion_path": "poll", "status_method": "GET", "status_auth": "none",
        "credential_environment_keys": ["F3_PROVIDER_API_KEY"],
        "objects": {"synthetic_only": True, "namespace": "f3_owned", "cleanup_readback": True},
        "human_labels": {"normal_effect": 1, "safe_error": "zero_effect",
                         "timeout_recovery": "readback_before_reissue"},
        "reliability_contract": {
            "risk_class": "external_write", "side_effect": "external",
            "confirmation_policy": "once", "timeout_policy": "unknown",
            "dynamic_task_enabled": True, "execution_mode": "detached",
            "async_strategy": "provider_task",
            "status_url": "https://sandbox.example.org/tasks/{taskId}",
            "idempotency": {"mode": "request_key", "remote_scope": "f3_owned"},
        },
    }


def declared_project_provider_profile() -> dict:
    """构造项目模拟provider合同，明确与第三方sandbox资格轨道分离。"""
    profile = declared_profile()
    profile.update(
        schema_version="final-f3-project-provider-v1",
        provider_kind="project_simulated_provider",
        submit_url="http://127.0.0.1:45678/simulation/tasks",
        readback_url="http://127.0.0.1:45678/simulation/objects/{objectId}",
        cleanup_url="http://127.0.0.1:45678/simulation/objects/{objectId}",
        credential_environment_keys=["F3_SIMULATION_KEY", "F3_SIMULATION_CONTROL_KEY"],
    )
    profile["reliability_contract"]["status_url"] = (
        "http://127.0.0.1:45678/simulation/tasks/{taskId}"
    )
    return profile


@pytest.mark.parametrize("target", [
    "https://127.0.0.1/tasks", "https://localhost/tasks", "https://[::1]/tasks",
    "https://sandbox.example.org/__e2e/tasks",
    "https://key:secret@sandbox.example.org/tasks",
])
def test_local_or_credential_url_cannot_be_real_provider(target: str) -> None:
    """明确本地夹具和URL用户信息不能作为F3真实目标适配。"""
    profile = declared_profile()
    profile["submit_url"] = target
    with pytest.raises(PreparationError):
        validate_profile(profile)


@pytest.mark.parametrize("method,auth", [("POST", "none"), ("GET", "bearer"), ("GET", "api_key")])
def test_poll_does_not_inherit_submission_auth(method: str, auth: str) -> None:
    """供应商要求查询鉴权或非GET时，不能把提交headers继承写入现有poll合同。"""
    profile = declared_profile()
    profile.update(status_method=method, status_auth=auth)
    with pytest.raises(PreparationError, match="F3_ORIGINAL_POLL_GET_AUTH_CONTRACT_MISMATCH"):
        validate_profile(profile)


def test_hmac_callback_cannot_be_declared_supported() -> None:
    """任意供应商HMAC不能冒充当前项目token/Operation/task绑定的callback。"""
    profile = declared_profile()
    profile.update(completion_path="callback", callback_contract="vendor_hmac")
    with pytest.raises(PreparationError, match="F3_VENDOR_HMAC_CALLBACK_UNSUPPORTED"):
        validate_profile(profile)


def test_original_callback_declaration_still_is_not_real_qualification() -> None:
    """原token合同结构兼容后仍需实际供应商与可达回调实测，不自动放行真实旅程。"""
    profile = declared_profile()
    profile.update(completion_path="callback", callback_contract="project_token_operation_task",
                   callback_ingress_url="https://ingress.example.org/api/external-tasks")
    summary = safe_profile_summary(validate_profile(profile))
    assert summary["structural_compatibility_passed"] is True
    assert summary["real_provider_qualified"] is False
    assert summary["JRN08_passed"] is False


@pytest.mark.parametrize("field,value", [("dynamic_task_enabled", False), ("confirmation_policy", "always")])
def test_standard_dta_requires_original_write_approval_contract(field: str, value: object) -> None:
    """SOP可接受的声明不能冒充普通DTA一次批准/已冻结HTTP写资格。"""
    profile = declared_profile()
    profile["reliability_contract"][field] = value
    with pytest.raises(PreparationError, match="F3_STANDARD_DTA_FROZEN_WRITE_CONTRACT_MISMATCH"):
        validate_profile(profile)


def test_missing_cleanup_and_unknown_schema_refused() -> None:
    """缺少独立清理读回或未知schema时拒绝准备，不猜测为当前版本。"""
    profile = declared_profile()
    profile["objects"]["cleanup_readback"] = False
    with pytest.raises(PreparationError, match="F3_INDEPENDENT_CLEANUP_READBACK_REQUIRED"):
        validate_profile(profile)
    profile = declared_profile()
    profile["schema_version"] = "final-f3-sandbox-v2"
    with pytest.raises(PreparationError, match="F3_PROFILE_SCHEMA_UNSUPPORTED"):
        validate_profile(profile)


def test_private_loader_refuses_missing_and_public_permissions(tmp_path: Path) -> None:
    """未提供合同不能skip计通过；可读给其他用户的私有文件拒绝加载。"""
    path = tmp_path / "provider.private.json"
    with pytest.raises(PreparationError, match="F3_VENDOR_PROFILE_MISSING"):
        load_private_profile(path)
    path.write_text(json.dumps(declared_profile()))
    path.chmod(0o644)
    with pytest.raises(PreparationError, match="F3_PRIVATE_PROFILE_PERMISSIONS_REQUIRED"):
        load_private_profile(path)
    path.chmod(0o600)
    assert load_private_profile(path)["provider_kind"] == "vendor_sandbox"


def test_summaries_do_not_expose_private_messages_or_endpoints() -> None:
    """安全观察只输出hash和合法usage，不泄露SDK消息、密钥或配置URL。"""
    summary = safe_profile_summary(validate_profile(declared_profile()))
    receipt = safe_sdk_receipt({"secret": "F3_PRIVATE_CANARY"}, {"content": "F3_BODY_CANARY"},
                               usage={"prompt_tokens": 10, "completion_tokens": -1,
                                      "total_tokens": True})
    text = json.dumps([summary, receipt])
    assert "sandbox.example.org" not in text
    assert "F3_PRIVATE_CANARY" not in text and "F3_BODY_CANARY" not in text
    assert receipt["usage"] == {"prompt_tokens": 10, "completion_tokens": None, "total_tokens": None}


def test_model_payload_fingerprint_is_stable_and_sensitive_to_each_execution_field() -> None:
    """配置摘要对字段次序稳定、对每个实际执行参数变化敏感且不返回私有正文。"""
    payload = {
        "provider": "openai_compatible", "base_url": "https://synthetic.invalid/v1",
        "api_key": "SYNTHETIC_KEY", "model": "synthetic-model", "temperature": 0.0,
        "max_output_tokens": 8192, "extra_body": {"thinking": {"type": "disabled"}},
        "api_timeout_seconds": 180,
    }
    fingerprint = model_config_payload_fingerprint(**payload)
    assert fingerprint == model_config_payload_fingerprint(**dict(reversed(tuple(payload.items()))))
    for field, value in (("provider", "other"), ("base_url", "https://other.invalid/v1"),
                         ("api_key", "OTHER_SYNTHETIC_KEY"), ("model", "other-model"),
                         ("temperature", 0.1), ("max_output_tokens", 4096),
                         ("extra_body", {"thinking": {"type": "enabled"}}),
                         ("api_timeout_seconds", 181)):
        changed = {**payload, field: value}
        assert model_config_payload_fingerprint(**changed) != fingerprint
    assert len(fingerprint) == 64
    with pytest.raises(PreparationError, match="F3_MODEL_CONFIG_PAYLOAD_INVALID"):
        model_config_payload_fingerprint(**{**payload, "api_key": ""})
    with pytest.raises(PreparationError, match="F3_MODEL_CONFIG_PAYLOAD_INVALID"):
        model_config_payload_fingerprint(**{**payload, "api_timeout_seconds": 0})


def test_float_model_settings_fingerprint_normalizes_integer_and_float_values() -> None:
    """浮点型temperature和Settings超时的整数表示须与等值浮点表示绑定相同配置。"""
    payload = {
        "provider": "openai_compatible", "base_url": "https://synthetic.invalid/v1",
        "api_key": "SYNTHETIC_KEY", "model": "synthetic-model", "temperature": 0,
        "max_output_tokens": 8192, "extra_body": {"thinking": {"type": "disabled"}},
        "api_timeout_seconds": 180,
    }

    integer_fingerprint = model_config_payload_fingerprint(**payload)
    float_fingerprint = model_config_payload_fingerprint(
        **{**payload, "temperature": 0.0, "api_timeout_seconds": 180.0},
    )

    assert integer_fingerprint == float_fingerprint


@pytest.mark.parametrize("target", [
    "https://sandbox.example.org/tasks?api_version=v2",
    "https://sandbox.example.org/status?taskId={taskId}",
    "http://sandbox.example.org/tasks",
    "https://192.168.42.10/sandbox/tasks",
])
def test_original_compatible_urls_do_not_fail_structural_preparation(target: str) -> None:
    """合法query/原HTTP合同/企业私网不能由准备层判假；真实来源及出站许可仍待核。"""
    profile = declared_profile()
    profile["submit_url"] = target
    profile["reliability_contract"]["status_url"] = target
    summary = safe_profile_summary(validate_profile(profile))
    assert summary["structural_compatibility_passed"] is True
    assert summary["real_provider_qualified"] is False


def test_project_provider_contract_is_explicit_and_not_vendor_qualification() -> None:
    """项目模拟provider按独立schema通过合同校验，不能被默认sandbox轨道误读。"""
    profile = declared_project_provider_profile()
    summary = safe_profile_summary(validate_profile(profile, project_simulated=True))
    assert summary["provider_contract_track"] == "project_provider_contract"
    assert summary["project_provider_contract_passed"] is True
    assert summary["real_provider_qualified"] is False
    with pytest.raises(PreparationError, match="F3_PROFILE_SCHEMA_UNSUPPORTED"):
        validate_profile(profile)


def test_project_provider_contract_rejects_non_loopback_endpoint() -> None:
    """项目模拟provider不得把第三方地址或外网请求混入规范实现合同。"""
    profile = declared_project_provider_profile()
    profile["submit_url"] = "https://provider.example.org/simulation/tasks"
    with pytest.raises(PreparationError, match="F3_PROJECT_PROVIDER_URL_INVALID"):
        validate_profile(profile, project_simulated=True)


def test_owned_namespace_and_existing_credential_names_are_not_hardcoded() -> None:
    """供应商自有合成命名空间和既有环境键不因非F3前缀而误拒，仍不判真实资格。"""
    profile = declared_profile()
    profile["objects"]["namespace"] = "owned-synthetic-20261004"
    profile["credential_environment_keys"] = ["VENDOR_SANDBOX_API_KEY"]
    assert safe_profile_summary(validate_profile(profile))["real_provider_qualified"] is False


def test_boolean_normal_effect_is_not_an_integer_business_label() -> None:
    """布尔True不能冒充人工冻结的整数一次效果标准。"""
    profile = declared_profile()
    profile["human_labels"]["normal_effect"] = True
    with pytest.raises(PreparationError, match="F3_NORMAL_EFFECT_ONCE_REQUIRED"):
        validate_profile(profile)


@pytest.mark.parametrize("control,calls,effects", [
    ("normal", 1, 1), ("authorization_refused", 0, 0), ("provider_rejected", 1, 0),
    ("unknown", 1, 1), ("unknown", 1, 0),
])
def test_registered_effect_controls_are_distinct(control: str, calls: int, effects: int) -> None:
    """正常一次、权限拒绝零外呼、供应商拒绝零效果与已对账unknown分别符合原目标。"""
    assert_effect_contract(control, calls_before_approval=0, submit_calls=calls,
                           independent_effects=effects, resend_calls=0, readback_sha256="a" * 64)


@pytest.mark.parametrize("change", [
    {"calls_before_approval": 1}, {"resend_calls": 1}, {"readback_sha256": None},
    {"independent_effects": 2}, {"submit_calls": 0}, {"submit_calls": True},
])
def test_unknown_cannot_blindly_retry_or_infer_zero_effect(change: dict) -> None:
    """已发unknown未查询、盲目重发或重复效果必须拒绝；本地错误不是零效果证据。"""
    data = {"calls_before_approval": 0, "submit_calls": 1, "independent_effects": 1,
            "resend_calls": 0, "readback_sha256": "b" * 64}
    data.update(change)
    with pytest.raises(PreparationError):
        assert_effect_contract("unknown", **data)
