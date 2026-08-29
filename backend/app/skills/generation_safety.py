"""
@Time       : 2026/08/28
@Author     : zhanglp8181
@File       : generation_safety.py
@CallChain  : Skill 生成 API → SkillDistiller/SkillEditor → 模型输入与工具建议响应
@Description: 统一清理 Skill 生成目录、Schema、模型输出中的凭据、端点和服务端引用。
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


GENERATION_REDACTED = "[已隐藏]"
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "base_url",
        "client_secret",
        "credential",
        "credentials",
        "cookie",
        "endpoint",
        "host",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
        "uri",
        "url",
    }
)
_REMOVED_SCHEMA_KEYS = frozenset(
    {"$ref", "const", "default", "enum", "example", "examples", "pattern"}
)
_SECRET_REFERENCE_RE = re.compile(r"\$\{secret\.[^}]+\}", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9][A-Za-z0-9._~+/=-]{3,}")
_API_KEY_RE = re.compile(r"(?i)\b(?:sk|rk|ghp|github_pat)-[A-Za-z0-9][A-Za-z0-9._~-]{7,}\b")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|access[_ -]?token|refresh[_ -]?token|api[_ -]?key|"
    r"password|secret|credential|token)\s*[:=]\s*(?:Bearer\s+)?[^\s,;}\]]+"
)
_PRIVATE_SECRET_RE = re.compile(
    r"(?i)\b(?:private|confidential|secret|access|refresh)[-_ ](?:token|key|credential)\b"
)
_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s<>()\"']+")
_PATH_SECRET_RE = re.compile(
    r"(?i)(/(?:access[_-]?token|api[_-]?key|credential|password|refresh[_-]?token|secret|token)"
    r"(?:/|=|:))[^/?#]+"
)


def sanitize_generation_text(value: str) -> str:
    """清理生成上下文中的服务端引用、令牌赋值、常见 API key 和 URL 凭据。"""

    redacted = _SECRET_REFERENCE_RE.sub("[已隐藏服务端引用]", value)
    redacted = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", redacted)
    redacted = _BEARER_RE.sub("Bearer [已隐藏令牌]", redacted)
    redacted = _API_KEY_RE.sub("[已隐藏 API key]", redacted)
    redacted = _PRIVATE_SECRET_RE.sub(GENERATION_REDACTED, redacted)
    return _URL_RE.sub(lambda match: sanitize_generation_url(match.group(0)), redacted)


def sanitize_generation_url(value: str) -> str:
    """保留可定位端点的 URL 结构，同时移除用户信息、查询值和敏感路径段。"""

    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return GENERATION_REDACTED
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        netloc = f"{GENERATION_REDACTED}@{host}"
    query = urlencode(
        [(key, GENERATION_REDACTED if item else item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)]
    )
    path = _PATH_SECRET_RE.sub(rf"\1{GENERATION_REDACTED}", parsed.path)
    return urlunsplit((parsed.scheme, netloc, path, query, ""))


def sanitize_generation_schema(value: object) -> object:
    """递归保留 Schema 的类型结构，移除示例、枚举、正则、引用和敏感字段。"""

    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = _normalize_key(key)
            if normalized_key in _REMOVED_SCHEMA_KEYS:
                continue
            if normalized_key in _SENSITIVE_KEYS:
                continue
            if normalized_key in {"pattern_properties", "properties"} and isinstance(item, Mapping):
                sanitized[key] = {
                    str(property_name): sanitize_generation_schema(property_value)
                    for property_name, property_value in item.items()
                    if _normalize_key(str(property_name)) not in _SENSITIVE_KEYS
                }
                continue
            if normalized_key == "required" and isinstance(item, (list, tuple)):
                sanitized[key] = [
                    str(name)
                    for name in item
                    if _normalize_key(str(name)) not in _SENSITIVE_KEYS
                ]
                continue
            sanitized[key] = sanitize_generation_schema(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_generation_schema(item) for item in value]
    if isinstance(value, str):
        return sanitize_generation_text(value)
    return value


def sanitize_generation_value(value: object) -> object:
    """递归脱敏模型输出，保留技能业务结构但不回传密钥字段或原始端点凭据。"""

    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = _normalize_key(key)
            if normalized_key in {"input_schema", "output_schema"}:
                sanitized[key] = sanitize_generation_schema(item)
            elif normalized_key in {"url", "endpoint", "uri", "base_url"} and isinstance(item, str):
                sanitized[key] = sanitize_generation_url(item)
            elif normalized_key in _SENSITIVE_KEYS:
                sanitized[key] = GENERATION_REDACTED
            else:
                sanitized[key] = sanitize_generation_value(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_generation_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_generation_text(value)
    return value


def _normalize_key(value: str) -> str:
    """把 Schema 字段统一为可比较的下划线小写形式。"""

    return value.strip().lower().replace("-", "_")
