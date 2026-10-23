"""
@Time       : 2026/09/27
@Author     : zhanglp8181
@File       : time_context.py
@CallChain  : Chat API/AgentLoop/ScheduledTask → time_context → Message/Execution/LLM payload
@Description: 冻结可信输入时间、校验 IANA 时区并生成可持久复用的本地时间事实。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIME_CONTEXT_SCHEMA_VERSION = "1"
UTC_TIMEZONE = "UTC"


def normalize_chat_timezone(value: object) -> tuple[str, str, ZoneInfo]:
    """把客户端声明收敛为合法 IANA 时区；缺失或非法值明确回退 UTC。"""

    candidate = str(value or "").strip()
    if candidate:
        try:
            return candidate, "client", ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return UTC_TIMEZONE, "fallback_utc", ZoneInfo(UTC_TIMEZONE)


def freeze_time_context(
    *,
    source_input_id: str,
    client_timezone: object = None,
    anchor_at_utc: datetime | None = None,
    timezone_source: str | None = None,
) -> dict[str, str]:
    """在服务端一次冻结输入锚点，并返回可写入 JSON 账本的时间事实。"""

    normalized_source_input_id = str(source_input_id or "").strip()
    if not normalized_source_input_id:
        raise ValueError("time_context.source_input_id 不能为空")
    timezone, detected_source, zone = normalize_chat_timezone(client_timezone)
    anchor = _as_utc(anchor_at_utc or datetime.now(UTC))
    source = str(timezone_source or detected_source).strip() or detected_source
    if timezone == UTC_TIMEZONE and detected_source == "fallback_utc":
        source = "fallback_utc"
    return {
        "schema_version": TIME_CONTEXT_SCHEMA_VERSION,
        "source_input_id": normalized_source_input_id,
        "anchor_at_utc": _format_datetime(anchor, utc_marker=True),
        "timezone": timezone,
        "timezone_source": source,
        "local_at": _format_datetime(anchor.astimezone(zone)),
    }


def normalize_persisted_time_context(
    value: Mapping[str, Any] | None,
    *,
    expected_source_input_id: str | None = None,
) -> dict[str, str] | None:
    """验证已持久时间事实，仅返回完整且来源一致的安全投影。"""

    if not isinstance(value, Mapping):
        return None
    required = (
        "schema_version",
        "source_input_id",
        "anchor_at_utc",
        "timezone",
        "timezone_source",
        "local_at",
    )
    if any(not str(value.get(key) or "").strip() for key in required):
        return None
    source_input_id = str(value["source_input_id"]).strip()
    if expected_source_input_id and source_input_id != expected_source_input_id:
        return None
    if str(value["schema_version"]).strip() != TIME_CONTEXT_SCHEMA_VERSION:
        return None
    timezone = str(value["timezone"]).strip()
    try:
        ZoneInfo(timezone)
        anchor = _parse_datetime(str(value["anchor_at_utc"]))
        local_at = _parse_datetime(str(value["local_at"]))
    except (ValueError, ZoneInfoNotFoundError):
        return None
    expected_local = anchor.astimezone(ZoneInfo(timezone))
    if _format_datetime(expected_local) != _format_datetime(local_at):
        return None
    return {
        "schema_version": TIME_CONTEXT_SCHEMA_VERSION,
        "source_input_id": source_input_id,
        "anchor_at_utc": _format_datetime(anchor, utc_marker=True),
        "timezone": timezone,
        "timezone_source": str(value["timezone_source"]).strip(),
        "local_at": _format_datetime(local_at),
    }


def time_context_local_at(value: Mapping[str, Any] | None) -> str | None:
    """返回已验证时间事实的本地时间文本，缺失或篡改时返回空值。"""

    normalized = normalize_persisted_time_context(value)
    return normalized["local_at"] if normalized else None


def _as_utc(value: datetime) -> datetime:
    """将带时区或无时区时间统一转换为 UTC，兼容服务端内部锚点。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: str) -> datetime:
    """解析带明确偏移的 ISO 时间文本，拒绝无时区的持久值。"""

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("持久时间必须带明确偏移")
    return parsed


def _format_datetime(value: datetime, *, utc_marker: bool = False) -> str:
    """按项目时间合同格式化时间，并在需要时使用 UTC 的 Z 标记。"""

    formatted = value.isoformat(timespec="microseconds" if value.microsecond else "seconds")
    if utc_marker and formatted.endswith("+00:00"):
        return f"{formatted[:-6]}Z"
    return formatted
