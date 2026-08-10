"""
@Time       : 2026/08/10 16:35
@Author     : zhanglp8181
@File       : slack.py
@CallChain  : ConnectionService → SlackAdapter → Slack Web API
@Description: 以固定官方端点执行 Slack 身份探测和只读会话查询，并归一化错误与限流。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


SLACK_API_BASE_URL = "https://slack.com/api"
SLACK_OAUTH_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"


@dataclass(frozen=True)
class SlackCallResult:
    """承载不含 token 的 Slack 响应、scope 快照和稳定错误信息。"""

    success: bool
    data: dict[str, Any]
    granted_scopes: frozenset[str] = frozenset()
    error_code: str | None = None
    rate_limited_until: datetime | None = None


@dataclass(frozen=True)
class SlackOAuthResult:
    """承载 OAuth code exchange 的稳定账号事实；token 禁止进入 repr 或错误正文。"""

    success: bool
    account_id: str = ""
    account_name: str = ""
    granted_scopes: frozenset[str] = frozenset()
    token: str = field(default="", repr=False)
    error_code: str | None = None


class SlackAdapter:
    """只允许调用代码内白名单方法，禁止租户配置任意 Slack URL。"""

    def __init__(self, client: httpx.Client | None = None, *, timeout_seconds: float = 15.0) -> None:
        """接收可替换 HTTP Client 便于协议测试，生产默认使用严格超时客户端。"""

        self._client = client
        self._timeout_seconds = timeout_seconds

    def auth_test(self, token: str) -> SlackCallResult:
        """验证 token、返回稳定 workspace 身份，并捕获响应中的实际 scope。"""

        return self._call("auth.test", token, {})

    def conversations_info(self, token: str, *, channel_id: str) -> SlackCallResult:
        """读取一个频道的基础信息；调用方须先验证 channels:read 授权。"""

        return self._call("conversations.info", token, {"channel": channel_id})

    def exchange_oauth_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> SlackOAuthResult:
        """按 OAuth v2 固定端点和 HTTP Basic 交换一次性 code，并只返回结构化安装事实。"""

        owned_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        try:
            response = client.post(
                f"{SLACK_API_BASE_URL}/oauth.v2.access",
                auth=(client_id, client_secret),
                data={"code": code, "redirect_uri": redirect_uri},
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return SlackOAuthResult(False, error_code="SLACK_OAUTH_UNAVAILABLE")
        finally:
            if owned_client:
                client.close()
        try:
            body = response.json()
        except ValueError:
            return SlackOAuthResult(False, error_code="SLACK_OAUTH_INVALID_RESPONSE")
        if not isinstance(body, dict) or response.status_code >= 500:
            return SlackOAuthResult(False, error_code="SLACK_OAUTH_INVALID_RESPONSE")
        if response.status_code >= 400 or body.get("ok") is not True:
            return SlackOAuthResult(False, error_code="SLACK_OAUTH_EXCHANGE_FAILED")
        team = body.get("team")
        account_id = str(team.get("id") or "") if isinstance(team, dict) else ""
        account_name = str(team.get("name") or "") if isinstance(team, dict) else ""
        token = str(body.get("access_token") or "")
        scopes = frozenset(
            item.strip() for item in str(body.get("scope") or "").split(",") if item.strip()
        )
        if not account_id or not token:
            return SlackOAuthResult(False, error_code="SLACK_OAUTH_INVALID_RESPONSE")
        return SlackOAuthResult(
            True,
            account_id=account_id,
            account_name=account_name,
            granted_scopes=scopes,
            token=token,
        )

    def _call(self, method: str, token: str, payload: dict[str, str]) -> SlackCallResult:
        """统一处理 Slack JSON 契约、429 Retry-After 和网络故障，且不记录凭据。"""

        owned_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        try:
            response = client.post(
                f"{SLACK_API_BASE_URL}/{method}",
                headers={"Authorization": f"Bearer {token}"},
                data=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return SlackCallResult(False, {}, error_code="SLACK_UNAVAILABLE")
        finally:
            if owned_client:
                client.close()
        scopes = frozenset(
            item.strip()
            for item in response.headers.get("x-oauth-scopes", "").split(",")
            if item.strip()
        )
        if response.status_code == 429:
            retry_after = _positive_int(response.headers.get("retry-after"))
            return SlackCallResult(
                False,
                {},
                granted_scopes=scopes,
                error_code="SLACK_RATE_LIMITED",
                rate_limited_until=datetime.now(UTC).replace(tzinfo=None)
                + timedelta(seconds=retry_after),
            )
        try:
            body = response.json()
        except ValueError:
            return SlackCallResult(False, {}, granted_scopes=scopes, error_code="SLACK_INVALID_RESPONSE")
        if not isinstance(body, dict):
            return SlackCallResult(False, {}, granted_scopes=scopes, error_code="SLACK_INVALID_RESPONSE")
        if response.status_code >= 500:
            return SlackCallResult(False, {}, granted_scopes=scopes, error_code="SLACK_UNAVAILABLE")
        if response.status_code >= 400 or body.get("ok") is not True:
            error = str(body.get("error") or f"http_{response.status_code}")
            return SlackCallResult(False, {}, granted_scopes=scopes, error_code=error)
        return SlackCallResult(True, dict(body), granted_scopes=scopes)


def _positive_int(value: str | None) -> int:
    """把不可信 Retry-After 收敛为至少一秒的有限整数。"""

    try:
        return max(1, min(int(value or "1"), 86_400))
    except ValueError:
        return 1
