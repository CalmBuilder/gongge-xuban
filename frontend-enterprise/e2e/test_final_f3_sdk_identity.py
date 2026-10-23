"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : test_final_f3_sdk_identity.py
@CallChain  : pytest → F3调用身份白名单/ContextVar → SDK安全关联
@Description: 只验证身份边界和异常退出后隔离，不把合成ID当本轮真实业务证据。
"""

from __future__ import annotations

import json
import pytest
from types import SimpleNamespace

from final_f3_preparation import (
    PreparationError,
    digest,
    model_config_payload_fingerprint,
)
from final_f3_sdk_identity import (
    bind_model_preflight_admin,
    bind_model_preflight_config,
    bind_sdk_identity,
    model_preflight_probe,
    model_preflight_request_scope,
    sdk_identity,
)


@pytest.mark.parametrize("identity,code", [
    ({"body": "PRIVATE_BODY_CANARY"}, "F3_SDK_IDENTITY_FIELD_UNDECLARED"),
    ({"operation_id": True}, "F3_SDK_IDENTITY_VALUE_INVALID"),
    ({"kind": "unknown_kind"}, "F3_SDK_KIND_INVALID"),
    ({"source_sha256": "PRIVATE_SOURCE_CANARY"}, "F3_SDK_SOURCE_SHA_INVALID"),
])
def test_private_free_text_or_unknown_identity_is_not_public_evidence(identity: dict, code: str):
    """只接受原持久身份和明确kind/SHA；未知字段及错误正文不进入公开异常。"""
    with pytest.raises(PreparationError) as caught, bind_sdk_identity(**identity):
        raise AssertionError("F3_INVALID_IDENTITY_ENTERED")
    assert str(caught.value) == code and sdk_identity()["identity_sha256"] == {}


def test_nested_binding_restores_parent_after_exception() -> None:
    """异常退出子任务后恢复父绑定，父退出后清除全部ID，防止并轮来源串用。"""
    with bind_sdk_identity(operation_id="PRIVATE_PARENT"):
        with pytest.raises(RuntimeError), bind_sdk_identity(operation_id="PRIVATE_CHILD"):
            assert sdk_identity()["identity_sha256"]["operation_id"] == digest("PRIVATE_CHILD")
            raise RuntimeError("PRIVATE_BODY_ERROR")
        assert sdk_identity()["identity_sha256"]["operation_id"] == digest("PRIVATE_PARENT")
    assert sdk_identity()["identity_sha256"] == {}


def test_exact_task_binding_hides_parent_and_restores_it_after_exit() -> None:
    """后台持久任务建立自己的来源边界，不借用外层聊天轮次，退出后恢复父身份。"""
    with bind_sdk_identity(source_input_id="PRIVATE_PARENT"):
        with bind_sdk_identity(replace_existing=True, task_id="PRIVATE_TASK"):
            assert sdk_identity()["identity_sha256"] == {"task_id": digest("PRIVATE_TASK")}
        assert sdk_identity()["identity_sha256"] == {
            "source_input_id": digest("PRIVATE_PARENT")
        }


def test_model_preflight_scope_binds_authenticated_admin_and_loaded_configuration(monkeypatch) -> None:
    """管理预检用独立诊断hash闭合实际管理员、租户、配置和探针，不借Composer身份。"""
    config = SimpleNamespace(
        id="SYNTHETIC_MODEL_CONFIG",
        tenant_id="SYNTHETIC_TENANT",
        provider="openai_compatible",
        base_url="https://synthetic.invalid/v1?private=canary",
        api_key_encrypted="SYNTHETIC_ENCRYPTED_KEY_CANARY",
        model="synthetic-model",
        temperature=0.2,
        max_output_tokens=256,
        extra_body_json={"thinking": {"type": "disabled"}},
    )
    admin = SimpleNamespace(id="SYNTHETIC_ADMIN", tenant_id="SYNTHETIC_TENANT", role="admin")
    monkeypatch.setattr("app.security.encryption.decrypt_secret",
                        lambda value: "SYNTHETIC_DECRYPTED_KEY"
                        if value == "SYNTHETIC_ENCRYPTED_KEY_CANARY" else "")
    monkeypatch.setattr("app.config.get_settings",
                        lambda: SimpleNamespace(model_api_timeout_seconds=180.0))
    expected_fingerprint = model_config_payload_fingerprint(
        provider=config.provider, base_url=config.base_url, api_key="SYNTHETIC_DECRYPTED_KEY",
        model=config.model, temperature=config.temperature,
        max_output_tokens=config.max_output_tokens, extra_body=config.extra_body_json,
        api_timeout_seconds=180,
    )
    with model_preflight_request_scope(
        invocation_sha256="a" * 64,
        tenant_id="SYNTHETIC_TENANT",
        model_config_id="SYNTHETIC_MODEL_CONFIG",
    ):
        bind_model_preflight_admin("SYNTHETIC_TENANT", admin)
        bind_model_preflight_config(config, expected_payload_sha256=expected_fingerprint)
        with model_preflight_probe("native_capabilities"):
            observed = sdk_identity()
        scope = observed["diagnostic_scope"]
        assert observed["identity_sha256"] == {}
        assert scope == {
            "schema": "model-preflight-v1",
            "invocation_sha256": "a" * 64,
            "tenant_sha256": digest("SYNTHETIC_TENANT"),
            "admin_actor_sha256": digest("SYNTHETIC_ADMIN"),
            "model_config_sha256": digest("SYNTHETIC_MODEL_CONFIG"),
            "config_fingerprint_sha256": expected_fingerprint,
            "probe_class": "native_capabilities",
        }
        assert isinstance(scope["config_fingerprint_sha256"], str)
        assert len(scope["config_fingerprint_sha256"]) == 64
        encoded = json.dumps(observed)
        assert "SYNTHETIC_ADMIN" not in encoded
        assert "SYNTHETIC_ENCRYPTED_KEY_CANARY" not in encoded
        assert "private=canary" not in encoded
        assert sdk_identity()["diagnostic_scope"]["probe_class"] is None
    assert sdk_identity()["diagnostic_scope"] is None


def test_model_preflight_fingerprint_includes_actual_application_timeout(monkeypatch) -> None:
    """预注册超时与当前应用设置不同即拒绝模型客户端派发。"""
    config = SimpleNamespace(
        id="SYNTHETIC_MODEL_CONFIG", tenant_id="SYNTHETIC_TENANT",
        provider="openai_compatible", base_url="https://synthetic.invalid/v1",
        api_key_encrypted="SYNTHETIC_ENCRYPTED_KEY", model="synthetic-model",
        temperature=0.0, max_output_tokens=8192,
        extra_body_json={"thinking": {"type": "disabled"}},
    )
    monkeypatch.setattr("app.security.encryption.decrypt_secret", lambda _value: "SYNTHETIC_KEY")
    monkeypatch.setattr("app.config.get_settings",
                        lambda: SimpleNamespace(model_api_timeout_seconds=181))
    frozen = model_config_payload_fingerprint(
        provider=config.provider, base_url=config.base_url, api_key="SYNTHETIC_KEY",
        model=config.model, temperature=config.temperature,
        max_output_tokens=config.max_output_tokens, extra_body=config.extra_body_json,
        api_timeout_seconds=180,
    )

    with model_preflight_request_scope(
        invocation_sha256="d" * 64, tenant_id="SYNTHETIC_TENANT",
        model_config_id="SYNTHETIC_MODEL_CONFIG",
    ), pytest.raises(PreparationError, match="F3_MODEL_DIAGNOSTIC_CONFIG_FROZEN_MISMATCH"):
        bind_model_preflight_config(config, expected_payload_sha256=frozen)

    assert sdk_identity()["diagnostic_scope"] is None


@pytest.mark.parametrize("expected", [None, "c" * 64])
def test_model_preflight_config_requires_frozen_semantic_match(monkeypatch, expected) -> None:
    """缺少或不同的预注册执行字段摘要在模型SDK探针前被拒绝。"""
    config = SimpleNamespace(
        id="SYNTHETIC_CONFIG", tenant_id="SYNTHETIC_TENANT", provider="openai_compatible",
        base_url="https://synthetic.invalid/v1", api_key_encrypted="SYNTHETIC_ENCRYPTED_KEY",
        model="synthetic-model", temperature=0.0, max_output_tokens=8192,
        extra_body_json={"thinking": {"type": "disabled"}},
    )
    monkeypatch.setattr("app.security.encryption.decrypt_secret",
                        lambda value: "SYNTHETIC_DECRYPTED_KEY"
                        if value == "SYNTHETIC_ENCRYPTED_KEY" else "")
    with model_preflight_request_scope(
        invocation_sha256="d" * 64, tenant_id="SYNTHETIC_TENANT",
        model_config_id="SYNTHETIC_CONFIG",
    ):
        with pytest.raises(PreparationError) as caught:
            bind_model_preflight_config(config, expected_payload_sha256=expected)
        assert str(caught.value) == (
            "F3_MODEL_CONFIG_FROZEN_FINGERPRINT_REQUIRED" if expected is None
            else "F3_MODEL_DIAGNOSTIC_CONFIG_FROZEN_MISMATCH"
        )
        assert sdk_identity()["diagnostic_scope"]["config_fingerprint_sha256"] is None


def test_model_preflight_scope_rejects_wrong_tenant_admin() -> None:
    """诊断scope不会把非管理员或跨租户actor记成已验证调用者。"""
    with model_preflight_request_scope(
        invocation_sha256="b" * 64,
        tenant_id="EXPECTED_TENANT",
        model_config_id="EXPECTED_CONFIG",
    ):
        with pytest.raises(PreparationError, match="F3_MODEL_DIAGNOSTIC_ADMIN_BINDING_INVALID"):
            bind_model_preflight_admin(
                "EXPECTED_TENANT",
                SimpleNamespace(id="OTHER_USER", tenant_id="OTHER_TENANT", role="admin"),
            )
