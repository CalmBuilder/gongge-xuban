"""
@Time       : 2026/09/10
@Author     : zhanglp8181
@File       : test_vertical_ability_browser_runtime.py
@CallChain  : pytest → browser_runtime → OpenCLI 状态回执/Profile 选择 → 浏览器会话路由
@Description: 验证多 Profile 状态不会被顶层歧义误判，并要求连接器明确选择目标浏览器会话。
"""

from __future__ import annotations

import json

import pytest

from app.vertical_abilities import browser_runtime


class _StatusResponse:
    """提供状态读取测试所需的最小 HTTP 响应上下文。"""

    def __init__(self, payload: dict[str, object]) -> None:
        """保存待返回的状态 JSON。"""

        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_StatusResponse":
        """返回自身以模拟 urllib 响应上下文。"""

        return self

    def __exit__(self, *_args: object) -> None:
        """结束测试响应上下文，不需要释放外部资源。"""

    def read(self, _limit: int) -> bytes:
        """返回固定 OpenCLI 状态响应。"""

        return self._body


class _StatusOpener:
    """记录请求并返回固定状态响应的测试 opener。"""

    def __init__(self, response: _StatusResponse) -> None:
        """保存测试响应实例。"""

        self.response = response
        self.request = None

    def open(self, request, *, timeout: float) -> _StatusResponse:
        """记录状态请求并返回响应，验证调用方提供了有限超时。"""

        self.request = request
        assert timeout == 2
        return self.response


def test_read_status_keeps_connected_profiles_when_top_level_route_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCLI 顶层 extensionConnected 为 false 时仍应保留各 Profile 的真实连接状态。"""

    response = _StatusResponse(
        {
            "ok": True,
            "extensionConnected": False,
            "profileRequired": True,
            "profiles": [
                {"contextId": "9etrfpdg", "extensionConnected": True, "extensionVersion": "1.0.24"},
                {"contextId": "hwxkuxvt", "extensionConnected": True, "extensionVersion": "1.0.24"},
                {"contextId": "broken", "extensionConnected": False},
            ],
        }
    )
    opener = _StatusOpener(response)
    monkeypatch.setattr(browser_runtime.urllib.request, "build_opener", lambda _handler: opener)

    status = browser_runtime.read_opencli_bridge_status()

    assert opener.request is not None
    request_headers = {key.casefold(): value for key, value in opener.request.headers.items()}
    assert request_headers["x-opencli"] == "1"
    assert status.extension_connected is False
    assert status.profile_required is True
    assert [(item.context_id, item.extension_connected) for item in status.profiles] == [
        ("9etrfpdg", True),
        ("hwxkuxvt", True),
        ("broken", False),
    ]


def test_require_bridge_requires_explicit_profile_when_multiple_profiles_are_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个已连接 Profile 同时存在时不能猜账号，必须返回可操作的选择错误。"""

    monkeypatch.setattr(
        browser_runtime,
        "read_opencli_bridge_status",
        lambda: browser_runtime.BrowserBridgeStatus(
            daemon_running=True,
            extension_connected=False,
            profile_required=True,
            profiles=(
                browser_runtime.BrowserBridgeProfileStatus("9etrfpdg", True, "1.0.24"),
                browser_runtime.BrowserBridgeProfileStatus("hwxkuxvt", True, "1.0.24"),
            ),
        ),
    )

    with pytest.raises(browser_runtime.BrowserRuntimeError) as error:
        browser_runtime.require_opencli_browser_bridge("xiaohongshu")

    assert error.value.code == "VERTICAL_ABILITY_BROWSER_PROFILE_REQUIRED"
    assert "9etrfpdg" in error.value.message
    assert "--opencli-profile" in error.value.message


def test_require_bridge_accepts_the_explicit_connected_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接器明确指定已连接 Profile 后应放行固定平台命令。"""

    status = browser_runtime.BrowserBridgeStatus(
        daemon_running=True,
        extension_connected=False,
        profile_required=True,
        profiles=(
            browser_runtime.BrowserBridgeProfileStatus("9etrfpdg", True, "1.0.24"),
            browser_runtime.BrowserBridgeProfileStatus("hwxkuxvt", True, "1.0.24"),
        ),
    )
    monkeypatch.setattr(browser_runtime, "read_opencli_bridge_status", lambda: status)

    assert browser_runtime.require_opencli_browser_bridge(
        "xiaohongshu",
        opencli_profile=" 9etrfpdg ",
    ) == status


def test_require_bridge_rejects_an_explicit_disconnected_or_unknown_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 Profile 不存在或未连接时应分别返回选择提示，不能退回另一个账号。"""

    status = browser_runtime.BrowserBridgeStatus(
        daemon_running=True,
        extension_connected=False,
        profiles=(browser_runtime.BrowserBridgeProfileStatus("9etrfpdg", False),),
    )
    monkeypatch.setattr(browser_runtime, "read_opencli_bridge_status", lambda: status)

    with pytest.raises(browser_runtime.BrowserRuntimeError) as disconnected:
        browser_runtime.require_opencli_browser_bridge("xiaohongshu", opencli_profile="9etrfpdg")
    assert disconnected.value.code == "VERTICAL_ABILITY_BROWSER_EXTENSION_NOT_CONNECTED"

    with pytest.raises(browser_runtime.BrowserRuntimeError) as unknown:
        browser_runtime.require_opencli_browser_bridge("xiaohongshu", opencli_profile="missing")
    assert unknown.value.code == "VERTICAL_ABILITY_BROWSER_PROFILE_REQUIRED"


def test_normalize_opencli_profile_rejects_control_characters() -> None:
    """Profile 参数只能是单个可传给子进程的标识，不能携带换行或超长内容。"""

    with pytest.raises(browser_runtime.BrowserRuntimeError) as error:
        browser_runtime.normalize_opencli_profile("profile\n--unexpected")

    assert error.value.code == "VERTICAL_ABILITY_BROWSER_PROFILE_INVALID"
