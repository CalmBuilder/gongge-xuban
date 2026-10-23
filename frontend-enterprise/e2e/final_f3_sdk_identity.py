"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f3_sdk_identity.py
@CallChain  : F3自有调用方绑定持久身份 → 原llm_operation → 最终SDK安全旁证
@Description: 按上下文关联实际调用身份及源码摘要；不查询数据库、不猜任务身份或授予业务验收。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import os
import re

from final_f3_preparation import PreparationError, digest, model_config_payload_fingerprint

IDENTITY_FIELDS = frozenset({
    "tenant_id", "user_id", "agent_id", "session_id", "source_input_id", "turn_id",
    "instance_id", "skill_version_id", "knowledge_version_id", "plan_id", "operation_id",
    "attempt_id", "signal_id", "task_id",
})
_identity: ContextVar[dict[str, str]] = ContextVar("f3_sdk_identity", default={})
_handoff: ContextVar[dict[str, object] | None] = ContextVar("f3_sdk_metadata_handoff", default=None)
_model_preflight: ContextVar[dict[str, object] | None] = ContextVar(
    "f3_model_preflight_diagnostic", default=None,
)


@contextmanager
def bind_sdk_identity(*, replace_existing: bool = False, **identity: str) -> Iterator[None]:
    """只接收白名单持久ID；可建立独立任务边界，退出恢复父上下文并拒绝自由正文。"""
    if identity.keys() - (IDENTITY_FIELDS | {"kind", "source_sha256"}):
        raise PreparationError("F3_SDK_IDENTITY_FIELD_UNDECLARED")
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise PreparationError("F3_SDK_IDENTITY_VALUE_INVALID")
    if "kind" in identity and identity["kind"] not in {"sop", "dynamic_task"}:
        raise PreparationError("F3_SDK_KIND_INVALID")
    if "source_sha256" in identity and not re.fullmatch(r"[a-f0-9]{64}", identity["source_sha256"]):
        raise PreparationError("F3_SDK_SOURCE_SHA_INVALID")
    parent = {} if replace_existing else _identity.get()
    token = _identity.set({**parent, **identity})
    try:
        yield
    finally:
        _identity.reset(token)


@contextmanager
def bind_observation_handoff(metadata: dict[str, object] | None) -> Iterator[None]:
    """只向原worker传入脱敏旁证；None用于清除父线程注入的旧快照。"""
    token = _handoff.set(metadata)
    try:
        yield
    finally:
        _handoff.reset(token)


@contextmanager
def model_preflight_request_scope(*, invocation_sha256: str, tenant_id: str,
                                  model_config_id: str) -> Iterator[dict[str, object]]:
    """为独立管理诊断建立请求边界，不伪造Composer、source或turn身份。"""
    if not re.fullmatch(r"[a-f0-9]{64}", invocation_sha256) or not tenant_id or not model_config_id:
        raise PreparationError("F3_MODEL_DIAGNOSTIC_SCOPE_INVALID")
    scope: dict[str, object] = {
        "schema": "model-preflight-v1",
        "invocation_sha256": invocation_sha256,
        "tenant_sha256": digest(tenant_id),
        "admin_actor_sha256": None,
        "model_config_sha256": digest(model_config_id),
        "config_fingerprint_sha256": None,
        "probe_class": None,
    }
    token = _model_preflight.set(scope)
    try:
        yield scope
    finally:
        _model_preflight.reset(token)


def bind_model_preflight_admin(tenant_id: str, current_user: object) -> None:
    """仅在原租户管理员依赖成功后关联实际actor及租户摘要。"""
    scope = _model_preflight.get()
    if scope is None:
        return
    actor_id = getattr(current_user, "id", None)
    actor_tenant = getattr(current_user, "tenant_id", None)
    actor_role = getattr(current_user, "role", None)
    if (not isinstance(actor_id, str) or not actor_id or actor_tenant != tenant_id
            or actor_role != "admin" or scope.get("tenant_sha256") != digest(tenant_id)):
        raise PreparationError("F3_MODEL_DIAGNOSTIC_ADMIN_BINDING_INVALID")
    scope["admin_actor_sha256"] = digest(actor_id)


def bind_model_preflight_config(
    model_config: object, *, expected_payload_sha256: str | None = None,
) -> None:
    """解密实际配置行后与冻结执行字段摘要精确比较，不落盘endpoint或密钥。"""
    scope = _model_preflight.get()
    if scope is None:
        return
    config_id = getattr(model_config, "id", None)
    tenant_id = getattr(model_config, "tenant_id", None)
    encrypted_key = getattr(model_config, "api_key_encrypted", None)
    if (not isinstance(config_id, str) or not config_id
            or not isinstance(tenant_id, str) or not tenant_id
            or scope.get("model_config_sha256") != digest(config_id)
            or scope.get("tenant_sha256") != digest(tenant_id)):
        raise PreparationError("F3_MODEL_DIAGNOSTIC_CONFIG_BINDING_INVALID")
    expected = expected_payload_sha256 or os.environ.get(
        "F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256"
    )
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise PreparationError("F3_MODEL_CONFIG_FROZEN_FINGERPRINT_REQUIRED")
    if not isinstance(encrypted_key, str) or not encrypted_key:
        raise PreparationError("F3_MODEL_DIAGNOSTIC_CONFIG_BINDING_INVALID")
    try:
        from app.security.encryption import decrypt_secret
        from app.config import get_settings

        api_key = decrypt_secret(encrypted_key)
        api_timeout_seconds = get_settings().model_api_timeout_seconds
        fingerprint = model_config_payload_fingerprint(
            provider=getattr(model_config, "provider", None),
            base_url=getattr(model_config, "base_url", None),
            api_key=api_key,
            model=getattr(model_config, "model", None),
            temperature=getattr(model_config, "temperature", None),
            max_output_tokens=getattr(model_config, "max_output_tokens", None),
            extra_body=getattr(model_config, "extra_body_json", None),
            api_timeout_seconds=api_timeout_seconds,
        )
    except Exception:
        raise PreparationError("F3_MODEL_DIAGNOSTIC_CONFIG_PAYLOAD_INVALID") from None
    if fingerprint != expected:
        raise PreparationError("F3_MODEL_DIAGNOSTIC_CONFIG_FROZEN_MISMATCH")
    scope["config_fingerprint_sha256"] = fingerprint


@contextmanager
def model_preflight_probe(probe_class: str) -> Iterator[None]:
    """在原模型探针执行期间标注有限诊断类别，退出后不污染后续业务SDK调用。"""
    if probe_class not in {"model_connection", "native_capabilities"}:
        raise PreparationError("F3_MODEL_DIAGNOSTIC_PROBE_CLASS_INVALID")
    scope = _model_preflight.get()
    previous = scope.get("probe_class") if scope is not None else None
    if scope is not None:
        scope["probe_class"] = probe_class
    try:
        yield
    finally:
        if scope is not None:
            scope["probe_class"] = previous


def model_preflight_diagnostic_scope() -> dict[str, object] | None:
    """返回独立诊断hash白名单；未在模型配置路由内时不增加诊断身份。"""
    scope = _model_preflight.get()
    return dict(scope) if scope is not None else None


def sdk_identity() -> dict[str, object]:
    """读取原阶段和本上下文身份，仅公开摘要/PID/UTC；缺失绑定保持缺失、不借上一轮。"""
    from app.observability.spans import current_llm_operation

    identity = _identity.get()
    captured = _handoff.get()
    row = {
        "pid": os.getpid(), "started_at_utc": datetime.now(UTC).isoformat(),
        "phase_sha256": digest(current_llm_operation()),
        "identity_sha256": {key: digest(value) for key, value in sorted(identity.items())
                            if key in IDENTITY_FIELDS},
        "kind": identity.get("kind"), "source_sha256": identity.get("source_sha256"),
        "business_identity_qualified_by_observer": False,
        "phase_source": "original_current_llm_operation",
        "diagnostic_scope": model_preflight_diagnostic_scope(),
    }
    if captured is not None:
        row.update({key: captured[key] for key in
                    ("phase_sha256", "identity_sha256", "kind", "source_sha256")})
        row["sdk_thread_phase_sha256"] = digest(current_llm_operation())
        row["phase_source"] = "original_caller_observation_handoff"
    return row
