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
class BrowserBridgeStatus:
    """描述 OpenCLI loopback daemon 和浏览器扩展的只读状态。"""

    daemon_running: bool
    extension_connected: bool
    version: str = ""


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
    )


def require_opencli_browser_bridge(platform_id: str) -> BrowserBridgeStatus:
    """确认指定平台可以使用已连接的 OpenCLI 浏览器会话。"""

    normalized_platform = str(platform_id or "").strip().casefold()
    if normalized_platform not in _SUPPORTED_PLATFORMS:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_PLATFORM_UNSUPPORTED",
            "该网站暂未声明浏览器会话连接方式。",
        )
    status = read_opencli_bridge_status()
    if not status.extension_connected:
        raise BrowserRuntimeError(
            "VERTICAL_ABILITY_BROWSER_EXTENSION_NOT_CONNECTED",
            "OpenCLI daemon 可访问，但浏览器扩展尚未连接；请在 Chrome/Edge 中启用扩展后重试。",
        )
    return status
