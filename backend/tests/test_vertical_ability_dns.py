"""
@Time       : 2026/09/02 14:40
@Author     : zhanglp8181
@File       : test_vertical_ability_dns.py
@CallChain  : pytest → AdaptiveDNSResolver → system DNS/DoH 结果校验与缓存
@Description: 验证跨平台自适应 DNS 在正常、假地址、异常和污染结果下的安全行为。
"""

from __future__ import annotations

import json
import socket

import pytest

from app.security.dns_resolver import AdaptiveDNSResolver, resolve_host_via_cloudflare_doh


def _record(address: str, port: int) -> list[tuple[object, ...]]:
    """构造 resolver 兼容的最小 getaddrinfo 记录。"""

    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


def test_system_public_answer_is_used_without_doh() -> None:
    """系统 DNS 返回公网地址时应直接使用，并避免额外 DoH 请求。"""

    doh_calls: list[str] = []

    def system_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        """返回一个稳定公网 A 记录。"""

        return _record("93.184.216.34", port)

    def doh_resolver(host: str, port: int) -> tuple[str, ...]:
        """记录不应发生的 DoH 调用。"""

        doh_calls.append(host)
        return ("93.184.216.34",)

    resolver = AdaptiveDNSResolver(
        system_resolver=system_resolver,
        doh_resolver=doh_resolver,
    )

    records = resolver("example.com", 443, type=socket.SOCK_STREAM)

    assert records[0][4][0] == "93.184.216.34"
    assert doh_calls == []


def test_sinkhole_answer_switches_to_doh_and_validates_public_result() -> None:
    """系统 DNS 命中 198.18/15 假地址时应切换 DoH，并只接受公网结果。"""

    def system_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        """模拟当前服务器的 fake-IP DNS 返回。"""

        return _record("198.18.0.153", port)

    resolver = AdaptiveDNSResolver(
        system_resolver=system_resolver,
        doh_resolver=lambda _host, _port: ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
    )

    records = resolver("r.jina.ai", 443, type=socket.SOCK_STREAM)

    assert [record[4][0] for record in records] == [
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    ]


def test_mixed_sinkhole_answers_switch_to_doh_but_mixed_private_answers_fail_closed() -> None:
    """混合假地址应回退 DoH，混合私网地址则必须保留给出网层拒绝。"""

    doh_calls: list[str] = []
    resolver = AdaptiveDNSResolver(
        system_resolver=lambda _host, port, **_kwargs: _record("198.18.0.153", port)
        + _record("93.184.216.34", port),
        doh_resolver=lambda host, _port: doh_calls.append(host) or ("93.184.216.34",),
    )

    records = resolver("mixed.example", 443, type=socket.SOCK_STREAM)
    assert [record[4][0] for record in records] == ["93.184.216.34"]
    assert doh_calls == ["mixed.example"]

    private_doh_calls: list[str] = []
    private_resolver = AdaptiveDNSResolver(
        system_resolver=lambda _host, port, **_kwargs: _record("10.0.0.7", port)
        + _record("198.18.0.153", port),
        doh_resolver=lambda host, _port: private_doh_calls.append(host) or ("93.184.216.34",),
    )

    private_records = private_resolver("mixed-private.example", 443, type=socket.SOCK_STREAM)
    assert [record[4][0] for record in private_records] == ["10.0.0.7", "198.18.0.153"]
    assert private_doh_calls == []


def test_system_dns_error_falls_back_to_doh() -> None:
    """系统 DNS 发生跨平台 OSError 时应使用经过校验的 DoH 结果。"""

    resolver = AdaptiveDNSResolver(
        system_resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("resolver down")),
        doh_resolver=lambda _host, _port: ("93.184.216.34",),
    )

    records = resolver("fallback.example", 443, type=socket.SOCK_STREAM)

    assert records[0][4][0] == "93.184.216.34"


def test_doh_non_public_or_malformed_answer_fails_closed() -> None:
    """DoH 返回私网或非法 IP 时不能把错误结果交给 HTTP 客户端。"""

    def system_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        """模拟假地址触发回退。"""

        return _record("198.18.0.152", port)

    for answer in (("10.0.0.1",), ("not-an-ip",)):
        resolver = AdaptiveDNSResolver(
            system_resolver=system_resolver,
            doh_resolver=lambda _host, _port, value=answer: value,
        )

        with pytest.raises(OSError, match="adaptive DNS resolution failed"):
            resolver("news.ycombinator.com", 443, type=socket.SOCK_STREAM)


def test_cloudflare_doh_json_answers_are_type_filtered_and_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 DoH 响应解析必须过滤 CNAME、校验 A/AAAA 类型并保留公网地址。"""

    payloads = iter(
        [
            json.dumps(
                {
                    "Status": 0,
                    "Answer": [
                        {"type": 5, "data": "origin.example.com."},
                        {"type": 1, "data": "93.184.216.34"},
                    ],
                }
            ).encode(),
            json.dumps(
                {
                    "Status": 0,
                    "Answer": [{"type": 28, "data": "2606:2800:220:1:248:1893:25c8:1946"}],
                }
            ).encode(),
        ]
    )
    requests: list[tuple[str, str, dict[str, str]]] = []

    class FakeResponse:
        """提供固定 HTTP 状态和 DoH JSON 响应。"""

        status = 200

        def read(self, _limit: int) -> bytes:
            """返回当前查询对应的固定响应体。"""

            return next(payloads)

    class FakeConnection:
        """记录固定 DoH 引导地址、Host 和查询路径。"""

        def __init__(self, host: str, port: int, **_kwargs: object) -> None:
            """保存连接目标，模拟 HTTPSConnection 构造。"""

            self.host = host
            self.port = port

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            """记录 DoH 请求元数据。"""

            requests.append((method, path, headers))

        def getresponse(self) -> FakeResponse:
            """返回固定 DoH 响应。"""

            return FakeResponse()

        def close(self) -> None:
            """模拟关闭连接。"""

    monkeypatch.setattr("app.security.dns_resolver.http.client.HTTPSConnection", FakeConnection)

    addresses = resolve_host_via_cloudflare_doh("example.com")

    assert addresses == ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    assert all(item[0] == "GET" for item in requests)
    assert all(item[2]["Host"] == "cloudflare-dns.com" for item in requests)
    assert all("name=example.com" in item[1] for item in requests)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps({"Status": 2, "Answer": []}).encode(),
        json.dumps({"Status": 0, "Answer": [{"type": 1, "data": "10.0.0.1"}]}).encode(),
        json.dumps({"Status": 0, "Answer": {"type": 1, "data": "93.184.216.34"}}).encode(),
    ],
)
def test_cloudflare_doh_invalid_json_answer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    """DoH 返回非法 JSON、失败状态、私网 IP 或错误结构时必须失败关闭。"""

    class FakeResponse:
        """提供待校验的固定 DoH 响应。"""

        status = 200

        def read(self, _limit: int) -> bytes:
            """返回测试参数指定的响应体。"""

            return payload

    class FakeConnection:
        """模拟不依赖真实网络的 DoH 连接。"""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """接受 HTTPSConnection 参数。"""

        def request(self, *_args: object, **_kwargs: object) -> None:
            """模拟提交 DNS 查询。"""

        def getresponse(self) -> FakeResponse:
            """返回固定响应。"""

            return FakeResponse()

        def close(self) -> None:
            """模拟关闭连接。"""

    monkeypatch.setattr("app.security.dns_resolver.http.client.HTTPSConnection", FakeConnection)

    with pytest.raises(OSError):
        resolve_host_via_cloudflare_doh("example.com")


def test_system_private_answer_is_not_silently_replaced_by_doh() -> None:
    """非假地址的私网系统解析结果应交由出网层拒绝，而不能被 DoH 绕过。"""

    doh_calls: list[str] = []
    resolver = AdaptiveDNSResolver(
        system_resolver=lambda _host, port, **_kwargs: _record("10.0.0.7", port),
        doh_resolver=lambda host, _port: doh_calls.append(host) or ("93.184.216.34",),
    )

    records = resolver("internal.example", 443, type=socket.SOCK_STREAM)

    assert records[0][4][0] == "10.0.0.7"
    assert doh_calls == []


def test_resolution_result_is_cached_until_ttl_expires() -> None:
    """相同主机在 TTL 内复用结果，过期后才重新验证系统 DNS。"""

    now = [100.0]
    system_calls: list[str] = []

    def system_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        """记录系统解析次数并返回公网地址。"""

        system_calls.append(host)
        return _record("93.184.216.34", port)

    resolver = AdaptiveDNSResolver(
        cache_ttl_seconds=10,
        system_resolver=system_resolver,
        clock=lambda: now[0],
    )

    resolver("example.com", 443, type=socket.SOCK_STREAM)
    resolver("example.com", 443, type=socket.SOCK_STREAM)
    now[0] = 111.0
    resolver("example.com", 443, type=socket.SOCK_STREAM)

    assert system_calls == ["example.com", "example.com"]
