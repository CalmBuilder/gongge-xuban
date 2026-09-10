"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_platform_runtime.py
@CallChain  : pytest → platform_runtime → 固定 CLI 参数/子进程环境/结构化回执
@Description: 验证平台命令运行时不会拼接 shell、不会污染服务环境，并能处理结构化输出边界。
"""

from __future__ import annotations

import os

import pytest

from app.cancellation import TurnCancellationRequested
from app.vertical_abilities import platform_runtime


def test_host_opencli_uses_fixed_command_and_returns_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式宿主机连接器仍可执行固定 OpenCLI 动作，服务端授权不依赖全局浏览器引用。"""

    captured: dict[str, object] = {}
    monkeypatch.setattr(platform_runtime, "require_opencli_browser_bridge", lambda _platform: None)
    monkeypatch.setattr(platform_runtime, "_find_executable", lambda *_args: "/usr/bin/opencli")

    def fake_run(command: list[str], **kwargs: object) -> bytes:
        """捕获命令和运行参数，返回最小 YAML 结果。"""

        captured["command"] = command
        captured["kwargs"] = kwargs
        return b"items:\n  - title: demo\n    url: https://example.com\n"

    monkeypatch.setattr(platform_runtime, "_run_command", fake_run)
    result = platform_runtime.run_opencli_raw(
        "xiaohongshu",
        "search",
        ["agent reach"],
        credentials={"session_reference": "opencli:xiaohongshu"},
        limit=7,
    )

    assert result == b"items:\n  - title: demo\n    url: https://example.com\n"
    assert captured["command"] == [
        "/usr/bin/opencli",
        "xiaohongshu",
        "search",
        "agent reach",
        "--limit",
        "7",
        "-f",
        "yaml",
    ]
    assert "environment" not in captured["kwargs"]


def test_host_opencli_places_explicit_profile_before_platform_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主机连接器指定 Profile 后必须使用 OpenCLI 全局参数位置，不能把账号参数当平台参数。"""

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        platform_runtime,
        "require_opencli_browser_bridge",
        lambda _platform, **_kwargs: None,
    )
    monkeypatch.setattr(platform_runtime, "_find_executable", lambda *_args: "/usr/bin/opencli")

    def fake_run(command: list[str], **kwargs: object) -> bytes:
        """捕获包含 Profile 的固定命令。"""

        captured["command"] = command
        captured["kwargs"] = kwargs
        return b"items:\n  - title: profile result\n"

    monkeypatch.setattr(platform_runtime, "_run_command", fake_run)
    result = platform_runtime.run_opencli_raw(
        "xiaohongshu",
        "search",
        ["热门新闻"],
        credentials={"session_reference": "opencli:xiaohongshu"},
        limit=1,
        opencli_profile="9etrfpdg",
    )

    assert result == b"items:\n  - title: profile result\n"
    assert captured["command"] == [
        "/usr/bin/opencli",
        "--profile",
        "9etrfpdg",
        "xiaohongshu",
        "search",
        "热门新闻",
        "--limit",
        "1",
        "-f",
        "yaml",
    ]


def test_server_cannot_treat_global_opencli_reference_as_user_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务器适配器必须经用户设备配对调度，不能凭平台字符串探测或借用全局浏览器。"""

    called = False

    def unsafe_direct_call(*_args: object, **_kwargs: object) -> bytes:
        """记录不应到达的全局浏览器调用，并返回可解析固定结果。"""

        nonlocal called
        called = True
        return b"items: []"

    monkeypatch.setattr(platform_runtime, "run_opencli_raw", unsafe_direct_call)
    with pytest.raises(platform_runtime.PlatformRuntimeError) as error:
        platform_runtime.run_opencli(
            "reddit", "search", ["fixture"], credentials={"session_reference": "opencli:reddit"},
        )
    assert error.value.code == "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED"
    assert called is False


def test_host_opencli_reference_must_match_declared_platform() -> None:
    """连接器的一次平台授权不能被另一平台或伪造前缀复用。"""

    with pytest.raises(platform_runtime.PlatformRuntimeError) as error:
        platform_runtime.run_opencli_raw(
            "reddit", "search", ["fixture"],
            credentials={"session_reference": "opencli:xiaohongshu"},
        )
    assert error.value.code == "VERTICAL_ABILITY_CONNECTION_REQUIRED"


def test_opencli_rejects_unknown_action_before_starting_process() -> None:
    """验证调用方不能借能力包运行时执行任意 OpenCLI 子命令。"""

    with pytest.raises(platform_runtime.PlatformRuntimeError) as error:
        platform_runtime.run_opencli(
            "xiaohongshu",
            "delete-all",
            credentials={"session_reference": "opencli:xiaohongshu"},
        )

    assert error.value.code == "VERTICAL_ABILITY_PLATFORM_ACTION_INVALID"


def test_opencli_relay_uses_server_dispatcher_without_local_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 browser_relay 只调用服务端注入的固定调度器，不探测 VM 自身 localhost。"""

    captured: dict[str, object] = {}

    def fail_local_bridge(_platform: str) -> None:
        """若 Relay 误走本机桥接则让测试立即失败。"""

        raise AssertionError("browser_relay 不应探测服务端本机 OpenCLI bridge")

    def dispatch(
        platform_id: str,
        action: str,
        values: list[str],
        limit: int | None,
        **kwargs: object,
    ) -> bytes:
        """捕获服务端发往宿主机的固定命令，并返回 YAML 回执。"""

        captured.update(platform_id=platform_id, action=action, values=values, limit=limit, kwargs=kwargs)
        return b"items:\n  - title: remote relay\n"

    monkeypatch.setattr(platform_runtime, "require_opencli_browser_bridge", fail_local_bridge)
    result = platform_runtime.run_opencli(
        "xiaohongshu",
        "search",
        ["remote"],
        credentials={"session_reference": "relay:varelay_fixture"},
        limit=4,
        relay_executor=dispatch,
    )

    assert result == {"items": [{"title": "remote relay"}]}
    assert captured["platform_id"] == "xiaohongshu"
    assert captured["action"] == "search"
    assert captured["values"] == ["remote"]
    assert captured["limit"] == 4


def test_relay_cancellation_preserves_turn_cancellation_signal() -> None:
    """设备等待被用户取消时须保留取消异常，不能包装成设备离线或 Provider 失败。"""

    def cancelled_dispatch(*_args: object, **_kwargs: object) -> bytes:
        """模拟底层等待收到取消信号。"""

        raise TurnCancellationRequested()

    with pytest.raises(TurnCancellationRequested):
        platform_runtime.run_opencli(
            "reddit", "search", ["fixture"],
            credentials={"session_reference": "relay:fixture"},
            relay_executor=cancelled_dispatch,
        )


def test_twitter_credentials_are_scoped_to_child_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Twitter Cookie 只注入本次子进程环境，不修改 API 服务进程环境。"""

    monkeypatch.delenv("TWITTER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_CT0", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(platform_runtime, "_find_executable", lambda *_args: "/usr/bin/twitter")

    def fake_run(command: list[str], **kwargs: object) -> bytes:
        """捕获子进程环境而不执行真实 Twitter CLI。"""

        captured["command"] = command
        captured["environment"] = kwargs["environment"]
        return b'{"tweets": [{"text": "hello"}]}'

    monkeypatch.setattr(platform_runtime, "_run_command", fake_run)
    result = platform_runtime.run_twitter_cli(
        "search",
        ["runtime"],
        credentials={"auth_token": "fixture-auth-token", "ct0": "fixture-ct0"},
        limit=3,
    )

    assert result == {"tweets": [{"text": "hello"}]}
    assert captured["command"] == ["/usr/bin/twitter", "search", "runtime", "-n", "3"]
    child_environment = captured["environment"]
    assert child_environment["TWITTER_AUTH_TOKEN"] == "fixture-auth-token"
    assert child_environment["TWITTER_CT0"] == "fixture-ct0"
    assert "TWITTER_AUTH_TOKEN" not in os.environ
    assert "TWITTER_CT0" not in os.environ
    assert "fixture-auth-token" not in repr(result)


def test_platform_output_rejects_empty_result_and_accepts_plain_text() -> None:
    """验证空平台回执是结构化错误，字幕等纯文本回执仍可被保留。"""

    with pytest.raises(platform_runtime.PlatformRuntimeError) as error:
        platform_runtime._parse_structured_output(b"  \n")
    assert error.value.code == "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"

    assert platform_runtime._parse_structured_output(b"subtitle line") == "subtitle line"
