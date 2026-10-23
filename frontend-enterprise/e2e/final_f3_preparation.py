"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : final_f3_preparation.py
@CallChain  : F3独立入口 → 项目provider合同/历史外部声明 → 原工具合同/SDK安全观察 → 事前证据
@Description: 校验项目provider合同和历史外部声明的结构与效果计数，不访问远端、不生成任务或把准备当作旅程通过。
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from ipaddress import ip_address
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit


PROJECT_PROVIDER_SCHEMA = "final-f3-project-provider-v1"
PROJECT_PROVIDER_KIND = "project_simulated_provider"


class PreparationError(ValueError):
    """仅携带稳定缺项代码，禁止把私有配置值拼入异常。"""

    def __init__(self, code: str) -> None:
        """保留可公开的拒绝类别，不保留配置正文。"""
        self.code = code
        super().__init__(code)


def digest(value: object) -> str:
    """只对规范JSON作摘要，供配置、人工标签和实际SDK原字节关联。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def model_config_payload_fingerprint(
    *, provider: object, base_url: object, api_key: object, model: object,
    temperature: object, max_output_tokens: object, extra_body: object,
    api_timeout_seconds: object,
) -> str:
    """规范浮点执行字段后摘要模型行与超时，避免等值整数和Settings浮点造成假失配。"""
    if (not isinstance(provider, str) or not provider
            or not isinstance(base_url, str) or not base_url
            or not isinstance(api_key, str) or not api_key
            or not isinstance(model, str) or not model
            or isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or type(max_output_tokens) is not int or max_output_tokens <= 0
            or isinstance(api_timeout_seconds, bool)
            or not isinstance(api_timeout_seconds, (int, float))
            or not math.isfinite(api_timeout_seconds) or api_timeout_seconds <= 0
            or not isinstance(extra_body, Mapping)):
        raise PreparationError("F3_MODEL_CONFIG_PAYLOAD_INVALID")
    return digest({
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "temperature": float(temperature),
        "max_output_tokens": max_output_tokens,
        "extra_body": dict(extra_body),
        "api_timeout_seconds": float(api_timeout_seconds),
    })


def _object(value: object, code: str) -> dict[str, object]:
    """读取明确的JSON对象，拒绝数组或字符串代替合同。"""
    if not isinstance(value, dict):
        raise PreparationError(code)
    return value


def _remote_url(value: object) -> str:
    """沿原HTTP/HTTPS及query合同拒绝明确本地夹具；私网资格与DNS仍交原出站守卫。"""
    if not isinstance(value, str):
        raise PreparationError("F3_REMOTE_URL_REQUIRED")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
        parsed.port
    except ValueError as exc:
        raise PreparationError("F3_REMOTE_URL_INVALID") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise PreparationError("F3_REMOTE_HTTP_WITHOUT_URL_CREDENTIAL_REQUIRED")
    if parsed.fragment:
        raise PreparationError("F3_URL_FRAGMENT_UNSUPPORTED")
    if host == "localhost" or host.endswith(".localhost") or "/__e2e/" in parsed.path:
        raise PreparationError("F3_LOCAL_PROVIDER_NOT_REAL_SANDBOX")
    try:
        literal = ip_address(host)
    except ValueError:
        pass
    else:
        if literal.is_loopback or literal.is_unspecified or literal.is_link_local:
            raise PreparationError("F3_LOCAL_PROVIDER_NOT_REAL_SANDBOX")
    return value


def _project_loopback_url(value: object) -> str:
    """验证项目模拟provider只能指向动态loopback HTTP，不把外部地址混入合同。"""
    if not isinstance(value, str):
        raise PreparationError("F3_PROJECT_PROVIDER_URL_REQUIRED")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise PreparationError("F3_PROJECT_PROVIDER_URL_INVALID") from exc
    if (parsed.scheme != "http" or host != "127.0.0.1" or not port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith("/simulation/")):
        raise PreparationError("F3_PROJECT_PROVIDER_URL_INVALID")
    return value


def validate_profile(value: object, *, project_simulated: bool = False) -> dict[str, object]:
    """验证项目模拟provider或历史外部声明；默认分支只防止把本地服务冒标成第三方资格。"""
    profile = _object(value, "F3_PROFILE_OBJECT_REQUIRED")
    allowed = {"schema_version", "provider_kind", "provider_name", "documentation_url",
               "submit_url", "submit_method", "readback_url", "cleanup_url",
               "reliability_contract", "completion_path", "status_method", "status_auth",
               "callback_contract", "callback_ingress_url", "objects",
               "credential_environment_keys", "human_labels"}
    if profile.keys() - allowed:
        raise PreparationError("F3_PROFILE_UNDECLARED_FIELD")
    expected_schema = PROJECT_PROVIDER_SCHEMA if project_simulated else "final-f3-sandbox-v1"
    expected_kind = PROJECT_PROVIDER_KIND if project_simulated else "vendor_sandbox"
    if profile.get("schema_version") != expected_schema:
        raise PreparationError("F3_PROFILE_SCHEMA_UNSUPPORTED")
    if profile.get("provider_kind") != expected_kind:
        raise PreparationError("F3_PROJECT_PROVIDER_KIND_MISMATCH"
                               if project_simulated else "F3_LOCAL_PROVIDER_NOT_REAL_SANDBOX")
    _remote_url(profile.get("documentation_url"))
    url_validator = _project_loopback_url if project_simulated else _remote_url
    for key in ("submit_url", "readback_url", "cleanup_url"):
        url_validator(profile.get(key))
    contract = _object(profile.get("reliability_contract"), "F3_RUNTIME_CONTRACT_REQUIRED")
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract

    try:
        published = ToolReliabilityContract.model_validate(contract)
    except ValueError as exc:
        raise PreparationError("F3_RUNTIME_TOOL_CONTRACT_INVALID") from exc
    if (published.async_strategy != "provider_task" or published.execution_mode != "detached"
            or published.risk_class != "external_write" or published.timeout_policy != "unknown"
            or published.idempotency.mode == "none"):
        raise PreparationError("F3_ASYNC_IDEMPOTENT_UNKNOWN_CONTRACT_REQUIRED")
    if (published.idempotency.mode != "request_key" or not published.dynamic_task_enabled
            or published.confirmation_policy != "once"
            or profile.get("submit_method") not in {"POST", "PUT", "PATCH", "DELETE"}):
        raise PreparationError("F3_STANDARD_DTA_FROZEN_WRITE_CONTRACT_MISMATCH")
    url_validator(published.status_url)
    completion = profile.get("completion_path")
    if completion == "poll":
        if profile.get("status_method") != "GET" or profile.get("status_auth") != "none":
            raise PreparationError("F3_ORIGINAL_POLL_GET_AUTH_CONTRACT_MISMATCH")
    elif completion == "callback":
        if profile.get("callback_contract") != "project_token_operation_task":
            raise PreparationError("F3_VENDOR_HMAC_CALLBACK_UNSUPPORTED")
        url_validator(profile.get("callback_ingress_url"))
    else:
        raise PreparationError("F3_COMPLETION_PATH_REQUIRED")
    objects = _object(profile.get("objects"), "F3_SYNTHETIC_OBJECT_CONTRACT_REQUIRED")
    namespace = objects.get("namespace")
    if (objects.get("synthetic_only") is not True or not isinstance(namespace, str)
            or not namespace.strip()):
        raise PreparationError("F3_SYNTHETIC_OWNED_NAMESPACE_REQUIRED")
    if objects.get("cleanup_readback") is not True:
        raise PreparationError("F3_INDEPENDENT_CLEANUP_READBACK_REQUIRED")
    credentials = profile.get("credential_environment_keys")
    if not isinstance(credentials, list) or not credentials or not all(
        isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        for key in credentials
    ):
        raise PreparationError("F3_PRIVATE_CREDENTIAL_REFERENCES_REQUIRED")
    labels = _object(profile.get("human_labels"), "F3_HUMAN_LABELS_REQUIRED")
    if not all(key in labels for key in ("normal_effect", "safe_error", "timeout_recovery")):
        raise PreparationError("F3_BUSINESS_LABELS_NOT_FROZEN")
    effect = labels.get("normal_effect")
    if isinstance(effect, bool) or not isinstance(effect, int) or effect != 1:
        raise PreparationError("F3_NORMAL_EFFECT_ONCE_REQUIRED")
    return profile


def load_private_profile(path: Path, *, project_simulated: bool = False) -> dict[str, object]:
    """只读取600私有合同文件；文件缺失和错误内容以稳定类别返回，不回显正文。"""
    if not path.is_file():
        raise PreparationError("F3_VENDOR_PROFILE_MISSING")
    if path.stat().st_mode & 0o077:
        raise PreparationError("F3_PRIVATE_PROFILE_PERMISSIONS_REQUIRED")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PreparationError("F3_PRIVATE_PROFILE_INVALID_JSON") from exc
    return validate_profile(value, project_simulated=project_simulated)


def safe_profile_summary(profile: Mapping[str, object]) -> dict[str, object]:
    """仅公开合同/端点/标签摘要；真实身份与HTTP资格仍须另外实测，不从声明推断通过。"""
    return {
        "provider_contract_track": (
            "project_provider_contract"
            if profile.get("provider_kind") == PROJECT_PROVIDER_KIND else "vendor_sandbox"
        ),
        "project_provider_contract_passed": profile.get("provider_kind") == PROJECT_PROVIDER_KIND,
        "profile_sha256": digest(profile),
        "endpoint_sha256": {key: digest(profile.get(key)) for key in
                            ("submit_url", "readback_url", "cleanup_url")},
        "runtime_contract_sha256": digest(profile.get("reliability_contract")),
        "human_labels_sha256": digest(profile.get("human_labels")),
        "completion_path": profile.get("completion_path"),
        "structural_compatibility_passed": True,
        "real_provider_qualified": False,
        "JRN08_passed": False,
    }


def safe_sdk_receipt(request: object, response: object, *, usage: object) -> dict[str, object]:
    """从实际SDK透传旁证仅取摘要及非负token计数，不保存消息、URL或任何凭据。"""
    counters = usage if isinstance(usage, dict) else {}
    return {
        "request_sha256": digest(request), "response_sha256": digest(response),
        "usage": {key: value if isinstance(value := counters.get(key), int)
                  and not isinstance(value, bool) and value >= 0 else None
                  for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
    }


def assert_effect_contract(
    control: str, *, calls_before_approval: int, submit_calls: int,
    independent_effects: int, resend_calls: int, readback_sha256: str | None,
) -> None:
    """区分正常一次、安全拒绝零效果和unknown先对账；未对账不得由本地失败推断零效果。"""
    counts = (calls_before_approval, submit_calls, independent_effects, resend_calls)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise PreparationError("F3_EFFECT_COUNTER_INVALID")
    if calls_before_approval or resend_calls:
        raise PreparationError("F3_UNAPPROVED_OR_BLIND_RESEND")
    if not isinstance(readback_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", readback_sha256):
        raise PreparationError("F3_INDEPENDENT_READBACK_REQUIRED")
    expected = {"normal": (1, 1), "authorization_refused": (0, 0), "provider_rejected": (1, 0)}
    if control in expected:
        if (submit_calls, independent_effects) != expected[control]:
            raise PreparationError("F3_EFFECT_COUNT_MISMATCH")
    elif control == "unknown":
        if submit_calls != 1 or independent_effects > 1:
            raise PreparationError("F3_UNKNOWN_RECONCILIATION_MISMATCH")
    else:
        raise PreparationError("F3_EFFECT_CONTROL_UNREGISTERED")
