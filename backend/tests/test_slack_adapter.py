"""
@Time       : 2026/08/10 18:20
@Author     : zhanglp8181
@File       : test_slack_adapter.py
@CallChain  : pytest/httpx MockTransport → SlackAdapter → normalized provider result
@Description: 回归 Slack 固定端点、Bearer 认证、scope 响应头、429 和异常响应处理。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.connectors.slack import SLACK_API_BASE_URL, SlackAdapter


def test_auth_test_uses_fixed_official_endpoint_and_captures_actual_scopes() -> None:
    """验证健康探测不可由租户改写 URL，且授权事实来自响应头。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """断言出站请求不在 URL 或 body 泄漏 token。"""

        assert str(request.url) == f"{SLACK_API_BASE_URL}/auth.test"
        assert request.headers["authorization"] == "Bearer xoxb-secret"
        assert b"xoxb-secret" not in request.content
        return httpx.Response(
            200,
            headers={"x-oauth-scopes": "channels:read,users:read"},
            json={"ok": True, "team_id": "T-A"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SlackAdapter(client).auth_test("xoxb-secret")

    assert result.success is True
    assert result.granted_scopes == frozenset({"channels:read", "users:read"})


def test_rate_limit_uses_bounded_retry_after_without_marking_reauth() -> None:
    """验证 429 被识别为限流窗口而不是凭据失效。"""

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(429, headers={"retry-after": "120"}, json={"ok": False})
    )
    before = datetime.now(UTC).replace(tzinfo=None)
    with httpx.Client(transport=transport) as client:
        result = SlackAdapter(client).auth_test("token")

    assert result.error_code == "SLACK_RATE_LIMITED"
    assert result.rate_limited_until is not None
    assert (result.rate_limited_until - before).total_seconds() >= 119


def test_invalid_json_and_server_failure_are_stable_errors() -> None:
    """验证 HTML/损坏 JSON 和上游 5xx 不把原始响应正文带入领域错误。"""

    invalid_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, text="<html>secret diagnostics</html>")
    )
    with httpx.Client(transport=invalid_transport) as client:
        invalid = SlackAdapter(client).auth_test("token")
    server_transport = httpx.MockTransport(
        lambda _request: httpx.Response(503, json={"ok": False, "error": "internal_error"})
    )
    with httpx.Client(transport=server_transport) as client:
        unavailable = SlackAdapter(client).auth_test("token")

    assert invalid.error_code == "SLACK_INVALID_RESPONSE"
    assert unavailable.error_code == "SLACK_UNAVAILABLE"


def test_channel_read_sends_only_whitelisted_method_and_channel_parameter() -> None:
    """验证只读适配器不会接受任意方法或 URL，并正确提交频道标识。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """检查 conversations.info 的固定目标与表单字段。"""

        assert str(request.url) == f"{SLACK_API_BASE_URL}/conversations.info"
        assert request.content == b"channel=C123"
        return httpx.Response(200, json={"ok": True, "channel": {"id": "C123"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SlackAdapter(client).conversations_info("token", channel_id="C123")

    assert result.success is True
    assert result.data["channel"] == {"id": "C123"}


def test_oauth_code_exchange_uses_fixed_v2_endpoint_basic_auth_and_hides_token_repr() -> None:
    """验证 OAuth code 只发往固定 v2 endpoint，client secret 不进表单且 token 不进入 repr。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """核对 Basic 认证、回调一致性和最小 code 表单。"""

        assert str(request.url) == f"{SLACK_API_BASE_URL}/oauth.v2.access"
        assert request.headers["authorization"].startswith("Basic ")
        assert b"client-secret" not in request.content
        assert request.content == b"code=temporary-code&redirect_uri=https%3A%2F%2Fapp.test%2Fcallback"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-oauth-secret",
                "scope": "channels:read",
                "team": {"id": "T-OAUTH", "name": "OAuth Workspace"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SlackAdapter(client).exchange_oauth_code(
            code="temporary-code",
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://app.test/callback",
        )

    assert result.success is True
    assert result.account_id == "T-OAUTH"
    assert result.granted_scopes == frozenset({"channels:read"})
    assert "xoxb-oauth-secret" not in repr(result)
