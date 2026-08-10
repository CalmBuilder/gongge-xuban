"""
@Time       : 2026/08/10 13:35
@Author     : zhanglp8181
@File       : wecom.py
@CallChain  : ConnectionService → WeComAdapter → 企业微信服务端 API
@Description: 管理自建应用 access_token 缓存，并以固定端点探测和读取应用身份。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


WECOM_API_BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_APPLICATION_READ_SCOPE = "application:read"
WECOM_APPLICATION_INFO_ACTION = "wecom.application_info"
WECOM_MESSAGE_SEND_ACTION = "wecom.message_send"
WECOM_REPLY_MAX_CHARS = 4000
_TOKEN_RETRY_ERRORS = frozenset({40014, 42001})


@dataclass(frozen=True)
class WeComCallResult:
    """承载不含 Secret/access_token 的企业微信响应和稳定错误代码。"""

    success: bool
    data: dict[str, Any]
    granted_scopes: frozenset[str] = frozenset()
    error_code: str | None = None
    rate_limited_until: datetime | None = None


@dataclass(frozen=True)
class _TokenEntry:
    """保存仅驻留当前进程内存的短期 access_token 及安全过期时间。"""

    token: str = field(repr=False)
    expires_at: datetime


class WeComAdapter:
    """通过固定官方端点访问企业微信，禁止调用方传入任意 URL。"""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: float = 15.0,
        token_expiry_skew_seconds: int = 300,
    ) -> None:
        """接收可替换客户端并配置 token 提前失效窗口，便于协议与并发测试。"""

        self._client = client
        self._timeout_seconds = timeout_seconds
        self._token_expiry_skew_seconds = max(0, token_expiry_skew_seconds)
        self._token_cache: dict[str, _TokenEntry] = {}
        self._token_lock = threading.Lock()

    def application_info(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> WeComCallResult:
        """获取凭据所属自建应用详情，并在 token 提前失效时只刷新重试一次。"""

        cache_key = self._cache_key(corp_id, corp_secret, agent_id)
        token_result = self._access_token(
            cache_key=cache_key,
            corp_id=corp_id,
            corp_secret=corp_secret,
        )
        if isinstance(token_result, WeComCallResult):
            return token_result
        result = self._call_application(token_result.token, agent_id=agent_id)
        if result.error_code not in {f"WECOM_{code}" for code in _TOKEN_RETRY_ERRORS}:
            return result
        self._invalidate_token(cache_key)
        refreshed = self._access_token(
            cache_key=cache_key,
            corp_id=corp_id,
            corp_secret=corp_secret,
        )
        if isinstance(refreshed, WeComCallResult):
            return refreshed
        return self._call_application(refreshed.token, agent_id=agent_id)

    def send_text(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
        recipient_ref: str,
        content: str,
    ) -> WeComCallResult:
        """向已验证入站发送者回发文本，token 失效只刷新一次且超时标记效果未知。"""

        if (
            not recipient_ref.strip()
            or not content.strip()
            or len(content) > WECOM_REPLY_MAX_CHARS
        ):
            return WeComCallResult(False, {}, error_code="WECOM_MESSAGE_INVALID")
        cache_key = self._cache_key(corp_id, corp_secret, agent_id)
        token_result = self._access_token(
            cache_key=cache_key,
            corp_id=corp_id,
            corp_secret=corp_secret,
        )
        if isinstance(token_result, WeComCallResult):
            return token_result
        result = self._call_send_text(
            token_result.token,
            agent_id=agent_id,
            recipient_ref=recipient_ref,
            content=content,
        )
        if result.error_code not in {f"WECOM_{code}" for code in _TOKEN_RETRY_ERRORS}:
            return result
        self._invalidate_token(cache_key)
        refreshed = self._access_token(
            cache_key=cache_key,
            corp_id=corp_id,
            corp_secret=corp_secret,
        )
        if isinstance(refreshed, WeComCallResult):
            return refreshed
        return self._call_send_text(
            refreshed.token,
            agent_id=agent_id,
            recipient_ref=recipient_ref,
            content=content,
        )

    def invalidate_credentials(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> None:
        """在凭据轮换或撤销后清除对应进程缓存，避免继续使用旧 token。"""

        self._invalidate_token(self._cache_key(corp_id, corp_secret, agent_id))

    def _access_token(
        self,
        *,
        cache_key: str,
        corp_id: str,
        corp_secret: str,
    ) -> _TokenEntry | WeComCallResult:
        """复用未到安全过期点的 token，否则在锁内向官方端点获取并缓存。"""

        now = datetime.now(UTC)
        with self._token_lock:
            cached = self._token_cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return cached
            result = self._request_token(corp_id=corp_id, corp_secret=corp_secret)
            if isinstance(result, WeComCallResult):
                return result
            self._token_cache[cache_key] = result
            return result

    def _request_token(
        self,
        *,
        corp_id: str,
        corp_secret: str,
    ) -> _TokenEntry | WeComCallResult:
        """调用 gettoken 并收敛 HTTP、JSON 和企业微信 errcode，不传播敏感请求 URL。"""

        response = self._request(
            "GET",
            f"{WECOM_API_BASE_URL}/gettoken",
            params={"corpid": corp_id, "corpsecret": corp_secret},
        )
        if isinstance(response, WeComCallResult):
            return response
        body = self._json_body(response)
        if body is None:
            return WeComCallResult(False, {}, error_code="WECOM_INVALID_RESPONSE")
        error_code = _wecom_error_code(body)
        if response.status_code >= 400 or error_code != 0:
            return WeComCallResult(False, {}, error_code=f"WECOM_{error_code}")
        token = str(body.get("access_token") or "").strip()
        expires_in = _bounded_expiry(body.get("expires_in"))
        if not token:
            return WeComCallResult(False, {}, error_code="WECOM_INVALID_RESPONSE")
        safe_lifetime = max(1, expires_in - self._token_expiry_skew_seconds)
        return _TokenEntry(
            token=token,
            expires_at=datetime.now(UTC) + timedelta(seconds=safe_lifetime),
        )

    def _call_application(self, token: str, *, agent_id: str) -> WeComCallResult:
        """调用 agent/get 并仅返回运行时所需的结构化应用事实。"""

        response = self._request(
            "GET",
            f"{WECOM_API_BASE_URL}/agent/get",
            params={"access_token": token, "agentid": agent_id},
        )
        if isinstance(response, WeComCallResult):
            return response
        if response.status_code == 429:
            retry_after = _positive_int(response.headers.get("retry-after"))
            return WeComCallResult(
                False,
                {},
                error_code="WECOM_RATE_LIMITED",
                rate_limited_until=datetime.now(UTC).replace(tzinfo=None)
                + timedelta(seconds=retry_after),
            )
        body = self._json_body(response)
        if body is None:
            return WeComCallResult(False, {}, error_code="WECOM_INVALID_RESPONSE")
        error_code = _wecom_error_code(body)
        if response.status_code >= 500:
            return WeComCallResult(False, {}, error_code="WECOM_UNAVAILABLE")
        if response.status_code >= 400 or error_code != 0:
            return WeComCallResult(False, {}, error_code=f"WECOM_{error_code}")
        returned_agent_id = str(body.get("agentid") or "").strip()
        if not returned_agent_id:
            return WeComCallResult(False, {}, error_code="WECOM_INVALID_RESPONSE")
        return WeComCallResult(
            True,
            {
                "agent_id": returned_agent_id,
                "name": str(body.get("name") or "").strip(),
                "description": str(body.get("description") or "").strip(),
                "enabled": int(body.get("close", 1)) == 0,
                "home_url": str(body.get("home_url") or "").strip(),
            },
            granted_scopes=frozenset({WECOM_APPLICATION_READ_SCOPE}),
        )

    def _call_send_text(
        self,
        token: str,
        *,
        agent_id: str,
        recipient_ref: str,
        content: str,
    ) -> WeComCallResult:
        """调用固定 message/send 端点；网络异常按可能已送达处理而非安全重试。"""

        response = self._send_request(
            f"{WECOM_API_BASE_URL}/message/send",
            params={"access_token": token},
            payload={
                "touser": recipient_ref,
                "msgtype": "text",
                "agentid": int(agent_id),
                "text": {"content": content},
                "safe": 0,
                "enable_id_trans": 0,
                "enable_duplicate_check": 1,
                "duplicate_check_interval": 1800,
            },
        )
        if isinstance(response, WeComCallResult):
            return response
        if response.status_code == 429:
            retry_after = _positive_int(response.headers.get("retry-after"))
            return WeComCallResult(
                False,
                {},
                error_code="WECOM_RATE_LIMITED",
                rate_limited_until=datetime.now(UTC).replace(tzinfo=None)
                + timedelta(seconds=retry_after),
            )
        body = self._json_body(response)
        if body is None:
            return WeComCallResult(False, {}, error_code="WECOM_INVALID_RESPONSE")
        error_code = _wecom_error_code(body)
        if response.status_code >= 500:
            return WeComCallResult(False, {}, error_code="WECOM_DELIVERY_UNKNOWN")
        if response.status_code >= 400 or error_code != 0:
            return WeComCallResult(False, {}, error_code=f"WECOM_{error_code}")
        return WeComCallResult(
            True,
            {
                "message_id": str(body.get("msgid") or "").strip(),
                "invalid_user_count": len(
                    [item for item in str(body.get("invaliduser") or "").split("|") if item]
                ),
            },
        )

    def _send_request(
        self,
        url: str,
        *,
        params: dict[str, str],
        payload: dict[str, object],
    ) -> httpx.Response | WeComCallResult:
        """执行可能产生外部效果的 POST，任何网络异常均返回未知而非可安全重试失败。"""

        owned_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        try:
            return client.request("POST", url, params=params, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError):
            return WeComCallResult(False, {}, error_code="WECOM_DELIVERY_UNKNOWN")
        finally:
            if owned_client:
                client.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response | WeComCallResult:
        """执行一次有界 HTTP 调用，捕获异常且不把含凭据 URL 写入错误正文。"""

        owned_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        try:
            return client.request(method, url, params=params)
        except (httpx.TimeoutException, httpx.NetworkError):
            return WeComCallResult(False, {}, error_code="WECOM_UNAVAILABLE")
        finally:
            if owned_client:
                client.close()

    @staticmethod
    def _json_body(response: httpx.Response) -> dict[str, Any] | None:
        """只接受 JSON 对象响应，拒绝 HTML、数组和畸形 JSON。"""

        try:
            body = response.json()
        except ValueError:
            return None
        return dict(body) if isinstance(body, dict) else None

    @staticmethod
    def _cache_key(corp_id: str, corp_secret: str, agent_id: str) -> str:
        """生成不暴露原始身份和 Secret 的内存缓存键。"""

        material = f"{corp_id}\0{agent_id}\0{corp_secret}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _invalidate_token(self, cache_key: str) -> None:
        """在锁内移除指定凭据组合的 token。"""

        with self._token_lock:
            self._token_cache.pop(cache_key, None)


def wecom_account_id(corp_id: str, agent_id: str) -> str:
    """派生不回显 CorpID/AgentID 的稳定应用身份，用于唯一约束和漂移检测。"""

    material = f"{corp_id.strip()}\0{agent_id.strip()}".encode("utf-8")
    return f"wecom_app_{hashlib.sha256(material).hexdigest()[:32]}"


def _wecom_error_code(body: dict[str, Any]) -> int:
    """将不可信 errcode 转换为整数；缺失或畸形统一视为无效响应。"""

    try:
        return int(body["errcode"])
    except (KeyError, TypeError, ValueError):
        return -99999


def _bounded_expiry(value: Any) -> int:
    """把上游有效期限制在 1 秒至 24 小时，防止异常值造成无限缓存。"""

    try:
        return max(1, min(int(value), 86_400))
    except (TypeError, ValueError):
        return 1


def _positive_int(value: str | None) -> int:
    """把 Retry-After 收敛为至少一秒且不超过一天的整数。"""

    try:
        return max(1, min(int(value or "1"), 86_400))
    except ValueError:
        return 1
