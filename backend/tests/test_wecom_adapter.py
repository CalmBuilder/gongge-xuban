"""
@Time       : 2026/08/10 13:42
@Author     : zhanglp8181
@File       : test_wecom_adapter.py
@CallChain  : pytest → WeComAdapter → httpx MockTransport
@Description: 验证企业微信 token 缓存、提前失效刷新、错误归一化和敏感信息隔离。
"""

from __future__ import annotations

import httpx

from app.connectors.wecom import WeComAdapter


def test_application_info_reuses_cached_token_without_exposing_credentials() -> None:
    """连续读取同一应用只获取一次 token，返回投影不含 Secret 和 access_token。"""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """按固定官方路径返回 token 和应用详情。"""

        calls.append(request.url.path)
        if request.url.path.endswith("/gettoken"):
            assert request.url.params["corpid"] == "corp-a"
            assert request.url.params["corpsecret"] == "secret-a"
            return httpx.Response(
                200,
                json={"errcode": 0, "errmsg": "ok", "access_token": "access-a", "expires_in": 7200},
            )
        assert request.url.params["access_token"] == "access-a"
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "errmsg": "ok",
                "agentid": 1000002,
                "name": "集成测试",
                "description": "只读探测",
                "close": 0,
                "home_url": "https://example.invalid",
                "allow_userinfos": {"user": [{"userid": "private-user"}]},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WeComAdapter(client)
    first = adapter.application_info(
        corp_id="corp-a", corp_secret="secret-a", agent_id="1000002"
    )
    second = adapter.application_info(
        corp_id="corp-a", corp_secret="secret-a", agent_id="1000002"
    )

    assert first.success is True
    assert second.success is True
    assert first.data == {
        "agent_id": "1000002",
        "name": "集成测试",
        "description": "只读探测",
        "enabled": True,
        "home_url": "https://example.invalid",
    }
    assert first.granted_scopes == frozenset({"application:read"})
    assert calls == ["/cgi-bin/gettoken", "/cgi-bin/agent/get", "/cgi-bin/agent/get"]
    assert "secret-a" not in repr(first)
    assert "access-a" not in repr(first)
    assert "private-user" not in repr(first)


def test_invalid_cached_token_is_refreshed_only_once() -> None:
    """遇到官方 token 失效码时清缓存并重试一次，持续失败时停止而非循环。"""

    token_count = 0
    agent_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """首次 token 对应失效响应，刷新后的调用成功。"""

        nonlocal token_count, agent_count
        if request.url.path.endswith("/gettoken"):
            token_count += 1
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "access_token": f"access-{token_count}",
                    "expires_in": 7200,
                },
            )
        agent_count += 1
        if request.url.params["access_token"] == "access-1":
            return httpx.Response(200, json={"errcode": 40014, "errmsg": "invalid token"})
        return httpx.Response(
            200,
            json={"errcode": 0, "agentid": 1000002, "name": "应用", "close": 0},
        )

    adapter = WeComAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.application_info(
        corp_id="corp-a", corp_secret="secret-a", agent_id="1000002"
    )

    assert result.success is True
    assert token_count == 2
    assert agent_count == 2


def test_provider_error_uses_errcode_and_never_returns_errmsg() -> None:
    """上游拒绝只暴露稳定数值代码，不把可能变化或夹带信息的 errmsg 向上传播。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        """模拟凭据被拒绝且错误正文包含敏感回显。"""

        return httpx.Response(
            200,
            json={"errcode": 40001, "errmsg": "bad secret secret-a"},
        )

    adapter = WeComAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.application_info(
        corp_id="corp-a", corp_secret="secret-a", agent_id="1000002"
    )

    assert result.success is False
    assert result.error_code == "WECOM_40001"
    assert result.data == {}
    assert "secret-a" not in repr(result)
    assert "bad secret" not in repr(result)


def test_untrusted_ip_is_a_distinct_non_reauth_error() -> None:
    """企业可信 IP 拒绝保留 60020 稳定代码，供服务层标记 degraded 而非误判 Secret。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """先签发 token，再模拟 agent/get 的可信 IP 拒绝。"""

        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "access-a", "expires_in": 7200},
            )
        return httpx.Response(200, json={"errcode": 60020, "errmsg": "unsafe ip"})

    adapter = WeComAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.application_info(
        corp_id="corp-a", corp_secret="secret-a", agent_id="1000002"
    )

    assert result.success is False
    assert result.error_code == "WECOM_60020"


def test_http_rate_limit_records_bounded_retry_time() -> None:
    """HTTP 429 被转换为带有限重试时间的降级结果。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """为 token 请求返回成功，为应用读取返回限流。"""

        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "access-a", "expires_in": 7200},
            )
        return httpx.Response(429, headers={"retry-after": "999999"})

    adapter = WeComAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.application_info(
        corp_id="corp-a", corp_secret="secret-a", agent_id="1000002"
    )

    assert result.success is False
    assert result.error_code == "WECOM_RATE_LIMITED"
    assert result.rate_limited_until is not None


def test_send_text_uses_fixed_target_and_returns_safe_receipt() -> None:
    """回发只调用固定端点，收件人留在 adapter 边界且回执不包含 access token。"""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """签发 token 后验证企业微信文本消息信封。"""

        calls.append(request.url.path)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "access-a", "expires_in": 7200},
            )
        body = request.read().decode()
        assert "external-user-a" in body
        assert "验收通过" in body
        assert request.url.params["access_token"] == "access-a"
        return httpx.Response(200, json={"errcode": 0, "msgid": "remote-message-1"})

    adapter = WeComAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.send_text(
        corp_id="corp-a",
        corp_secret="secret-a",
        agent_id="1000002",
        recipient_ref="external-user-a",
        content="验收通过",
    )

    assert result.success is True
    assert result.data == {"message_id": "remote-message-1", "invalid_user_count": 0}
    assert calls == ["/cgi-bin/gettoken", "/cgi-bin/message/send"]
    assert "external-user-a" not in repr(result)
    assert "access-a" not in repr(result)


def test_send_timeout_is_unknown_and_never_retried() -> None:
    """POST 发出后超时必须标记未知，adapter 不得自动重复外部写。"""

    send_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """令 token 请求成功并在唯一一次发送时模拟 read timeout。"""

        nonlocal send_count
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "access-a", "expires_in": 7200},
            )
        send_count += 1
        raise httpx.ReadTimeout("timeout", request=request)

    adapter = WeComAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.send_text(
        corp_id="corp-a",
        corp_secret="secret-a",
        agent_id="1000002",
        recipient_ref="external-user-a",
        content="验收通过",
    )

    assert result.success is False
    assert result.error_code == "WECOM_DELIVERY_UNKNOWN"
    assert send_count == 1
