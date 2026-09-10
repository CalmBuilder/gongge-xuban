"""
@Time       : 2026/09/03 16:35
@Author     : zhanglp8181
@File       : browser_runtime.py
@CallChain  : 网站连接 API → 固定 OpenCLI loopback 状态 → 用户浏览器会话连接
@Description: 读取本机 OpenCLI 浏览器桥接状态，提供跨平台、无副作用的已登录浏览器连接探测。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


_OPENCLI_STATUS_URL = "http://127.0.0.1:19825/status"
_MAX_STATUS_BYTES = 64 * 1024
_SUPPORTED_PLATFORMS = frozenset(
    {
        "twitter",
        "reddit",
        "bilibili",
        "facebook",
        "instagram",
        "xiaohongshu",
        "linkedin",
        "xueqiu",
    }
)


class BrowserRuntimeError(RuntimeError):
    """表示本机浏览器桥接不可用或返回了不可识别的状态。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可由连接 API 直接返回的稳定错误码和提示。"""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BrowserBridgeProfileStatus:
    """描述 OpenCLI 浏览器桥接中的单个 Profile 连接状态。"""

    context_id: str
    extension_connected: bool
    version: str = ""


@dataclass(frozen=True, slots=True)
class BrowserBridgeStatus:
    """描述 OpenCLI loopback daemon、扩展及其浏览器 Profile 的只读状态。"""

    daemon_running: bool
    extension_connected: bool
    version: str = ""
    profile_required: bool = False
    profiles: tuple[BrowserBridgeProfileStatus, ...] = ()


def normalize_opencli_profile(value: str | None) -> str | None:
    """规范化连接器指定的 Profile 标识，并拒绝控制字符和超长参数。"""

    if value is None:
        return None
    profile = str(value).strip()
    if not profile:
        return None
    if len(profile) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in profile):
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_PROFILE_INVALID",
            "OpenCLI 浏览器 Profile 标识无效，请填写 profile list 输出的 ID 或别名。",
        )
    return profile


def read_opencli_bridge_status() -> BrowserBridgeStatus:
    """读取固定 loopback 状态接口，不启动 daemon、不读取浏览器 Cookie。"""

    request = urllib.request.Request(
        _OPENCLI_STATUS_URL,
        headers={"X-OpenCLI": "1"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:
            raw = response.read(_MAX_STATUS_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_BRIDGE_UNAVAILABLE",
            "本机 OpenCLI 浏览器桥接不可用，请打开浏览器并确认扩展已启用。",
        ) from exc
    if len(raw) > _MAX_STATUS_BYTES:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_BRIDGE_RESPONSE_TOO_LARGE",
            "浏览器桥接状态响应超过安全上限。",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_BRIDGE_RESPONSE_INVALID",
            "本机浏览器桥接返回了无法识别的状态。",
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_BRIDGE_UNAVAILABLE",
            "本机 OpenCLI 浏览器桥接尚未就绪。",
        )
    return BrowserBridgeStatus(
        daemon_running=True,
        extension_connected=bool(payload.get("extensionConnected")),
        version=str(payload.get("version") or "")[:64],
        profile_required=bool(payload.get("profileRequired")),
        profiles=_parse_profiles(payload),
    )


def require_opencli_browser_bridge(
    platform_id: str,
    *,
    opencli_profile: str | None = None,
) -> BrowserBridgeStatus:
    """确认指定平台可以使用目标 OpenCLI Profile 的已连接浏览器会话。"""

    normalized_platform = str(platform_id or "").strip().casefold()
    if normalized_platform not in _SUPPORTED_PLATFORMS:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_PLATFORM_UNSUPPORTED",
            "该网站暂未声明浏览器会话连接方式。",
        )
    selected_profile = normalize_opencli_profile(opencli_profile)
    status = read_opencli_bridge_status()
    if selected_profile:
        if status.profiles:
            profile = next(
                (item for item in status.profiles if item.context_id == selected_profile),
                None,
            )
            if profile is None:
                raise BrowserRuntimeError(
                    "VERTICAL_ABILITY_BROWSER_PROFILE_REQUIRED",
                    f"未找到 OpenCLI Profile“{selected_profile}”，请先运行 opencli profile list 查看可用 ID。",
                )
            if not profile.extension_connected:
                raise BrowserRuntimeError(
                    "VERTICAL_ABILITY_BROWSER_EXTENSION_NOT_CONNECTED",
                    f"OpenCLI Profile“{selected_profile}”的浏览器扩展尚未连接，请打开并保持该浏览器窗口。",
                )
            return status
        if not status.extension_connected:
            raise BrowserRuntimeError(
                "VERTICAL_ABILITY_BROWSER_EXTENSION_NOT_CONNECTED",
                f"OpenCLI Profile“{selected_profile}”的浏览器扩展尚未连接，请打开并保持该浏览器窗口。",
            )
        return status
    if status.extension_connected:
        return status
    connected_profiles = tuple(item for item in status.profiles if item.extension_connected)
    if len(connected_profiles) > 1:
        profile_ids = "、".join(item.context_id for item in connected_profiles[:8])
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_PROFILE_REQUIRED",
            f"检测到多个已连接的 OpenCLI 浏览器 Profile（{profile_ids}），请在连接器命令中增加 --opencli-profile <ID> 明确选择目标账号。",
        )
    if len(connected_profiles) == 1:
        return status
    if not status.extension_connected:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_EXTENSION_NOT_CONNECTED",
            "OpenCLI daemon 可访问，但浏览器扩展尚未连接；请在 Chrome/Edge 中启用扩展后重试。",
        )
    return status


def _parse_profiles(payload: dict[str, object]) -> tuple[BrowserBridgeProfileStatus, ...]:
    """从状态回执提取有限 Profile 信息，忽略不完整条目并避免回显原始负载。"""

    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list):
        return ()
    profiles: list[BrowserBridgeProfileStatus] = []
    seen: set[str] = set()
    for raw_profile in raw_profiles[:32]:
        if not isinstance(raw_profile, dict):
            continue
        context_id = raw_profile.get("contextId")
        if not isinstance(context_id, str):
            continue
        normalized_id = context_id.strip()
        if not normalized_id or len(normalized_id) > 128 or normalized_id in seen:
            continue
        seen.add(normalized_id)
        profiles.append(
            BrowserBridgeProfileStatus(
                context_id=normalized_id,
                extension_connected=bool(raw_profile.get("extensionConnected")),
                version=str(raw_profile.get("extensionVersion") or "")[:64],
            )
        )
    return tuple(profiles)
