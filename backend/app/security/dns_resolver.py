"""
@Time       : 2026/09/02 14:00
@Author     : zhanglp8181
@File       : dns_resolver.py
@CallChain  : 垂直能力适配器/MCP → AdaptiveDNSResolver → 系统 DNS 或 Cloudflare DoH
@Description: 提供跨平台、自适应且不依赖 shell/hosts 文件的出网域名解析能力。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterable
from urllib.parse import quote


CLOUDFLARE_DOH_ADDRESS = "1.1.1.1"
CLOUDFLARE_DOH_HOST = "cloudflare-dns.com"
DEFAULT_DNS_CACHE_TTL_SECONDS = 60.0
_MAX_DOH_RESPONSE_BYTES = 64 * 1024
_SINKHOLE_NETWORK = ipaddress.ip_network("198.18.0.0/15")

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_SocketRecord = tuple[int, int, int, str, tuple[str, int]]


class AdaptiveDNSResolver:
    """先使用系统 DNS，命中代理常见的 198.18/15 假地址时自动切换 DoH。"""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = DEFAULT_DNS_CACHE_TTL_SECONDS,
        system_resolver: object | None = None,
        doh_resolver: object | None = None,
        clock: object | None = None,
    ) -> None:
        """注入系统解析、DoH 解析和时钟端口，便于 Windows/Linux 与单元测试复用。"""

        self.cache_ttl_seconds = max(float(cache_ttl_seconds), 0.0)
        self.system_resolver = system_resolver or socket.getaddrinfo
        self.doh_resolver = doh_resolver or resolve_host_via_cloudflare_doh
        self.clock = clock or time.monotonic
        self._cache: dict[tuple[str, int], tuple[float, tuple[_Address, ...]]] = {}
        self._lock = threading.RLock()

    def __call__(
        self,
        host: str,
        port: int,
        *,
        type: int | None = None,
        **_kwargs: object,
    ) -> list[_SocketRecord]:
        """返回兼容 socket.getaddrinfo 的 TCP 记录，并缓存最终可用地址。"""

        del type
        hostname = str(host or "").strip().casefold().rstrip(".")
        if not hostname:
            raise OSError("hostname is empty")
        target_port = int(port)
        key = (hostname, target_port)
        now = float(self.clock())
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return _records(cached[1], target_port)
            addresses = self._resolve_uncached(hostname, target_port)
            self._cache[key] = (now + self.cache_ttl_seconds, tuple(addresses))
            return _records(tuple(addresses), target_port)

    def resolve_addresses(self, host: str, port: int = 443) -> tuple[str, ...]:
        """解析并返回稳定字符串地址，供启动探测和诊断使用。"""

        records = self(host, port, type=socket.SOCK_STREAM)
        return tuple(str(record[4][0]) for record in records)

    def _resolve_uncached(self, hostname: str, port: int) -> tuple[_Address, ...]:
        """执行系统 DNS 优先、假地址触发 DoH 的单次解析。"""

        try:
            system_addresses = _addresses_from_records(self.system_resolver, hostname, port)
        except OSError:
            system_addresses = ()
        if system_addresses:
            # 非 sinkhole 的私网/保留地址必须交给 outbound 层拒绝，不能通过 DoH
            # 把一个可能被攻击者控制的内部解析结果悄悄替换成公网地址；只有全部
            # 正常或包含 198.18/15 假地址时，才进入下面的判断。
            has_non_sinkhole_blocked = any(
                _is_blocked_non_sinkhole_address(address) for address in system_addresses
            )
            if has_non_sinkhole_blocked or not any(
                _is_sinkhole_address(address) for address in system_addresses
            ):
                return system_addresses
        try:
            doh_addresses = tuple(_parse_public_address(value) for value in self.doh_resolver(hostname, port))
        except (OSError, TypeError, ValueError) as exc:
            raise OSError("adaptive DNS resolution failed") from exc
        if not doh_addresses:
            raise OSError("adaptive DNS returned no address")
        return _deduplicate(doh_addresses)


def resolve_host_via_cloudflare_doh(hostname: str, port: int = 443) -> tuple[str, ...]:
    """通过固定 Cloudflare DoH 引导地址解析 A/AAAA，不依赖操作系统 DNS。"""

    if int(port) <= 0 or int(port) > 65535:
        raise OSError("target port is invalid")
    addresses: list[str] = []
    for record_type, answer_type in (("A", 1), ("AAAA", 28)):
        connection = http.client.HTTPSConnection(
            CLOUDFLARE_DOH_ADDRESS,
            443,
            timeout=5,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "GET",
                f"/dns-query?name={quote(hostname, safe='')}&type={record_type}",
                headers={"Host": CLOUDFLARE_DOH_HOST, "Accept": "application/dns-json"},
            )
            response = connection.getresponse()
            payload = response.read(_MAX_DOH_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            raise OSError("Cloudflare DoH lookup failed") from exc
        finally:
            connection.close()
        if response.status != 200 or len(payload) > _MAX_DOH_RESPONSE_BYTES:
            raise OSError("Cloudflare DoH lookup failed")
        addresses.extend(_parse_doh_json_answers(payload, answer_type))
    if not addresses:
        raise OSError("hostname cannot be resolved by Cloudflare DoH")
    return tuple(_parse_public_address(value).compressed for value in addresses)


_DEFAULT_RESOLVER: AdaptiveDNSResolver | None = None
_DEFAULT_RESOLVER_LOCK = threading.Lock()


def default_adaptive_resolver() -> AdaptiveDNSResolver:
    """返回进程级共享解析器，避免每个能力或每次请求重复建立缓存。"""

    global _DEFAULT_RESOLVER
    if _DEFAULT_RESOLVER is None:
        with _DEFAULT_RESOLVER_LOCK:
            if _DEFAULT_RESOLVER is None:
                _DEFAULT_RESOLVER = AdaptiveDNSResolver()
    return _DEFAULT_RESOLVER


def _addresses_from_records(resolver: object, host: str, port: int) -> tuple[_Address, ...]:
    """把系统解析器返回的 getaddrinfo 记录归一化为去重 IP。"""

    if not callable(resolver):
        raise OSError("system resolver is unavailable")
    try:
        records = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        if isinstance(exc, socket.gaierror):
            return ()
        raise
    addresses: list[_Address] = []
    for record in records or ():
        try:
            value = record[4][0]
            address = ipaddress.ip_address(str(value))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _parse_public_address(value: object) -> _Address:
    """校验 DoH 地址为可公开连接的 A/AAAA 地址，阻止 DoH 污染回流。"""

    address = ipaddress.ip_address(str(value))
    if not address.is_global:
        raise OSError("DoH returned a non-public address")
    return address


def _parse_doh_json_answers(payload: bytes, answer_type: int) -> tuple[str, ...]:
    """解析单次 DoH JSON 响应，只返回目标记录类型且通过公网校验的地址。"""

    try:
        body = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("Cloudflare DoH response is invalid") from exc
    if not isinstance(body, dict) or body.get("Status") != 0:
        raise OSError("hostname cannot be resolved by Cloudflare DoH")
    answers = body.get("Answer") or []
    if not isinstance(answers, list):
        raise OSError("Cloudflare DoH response is invalid")
    addresses: list[str] = []
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != answer_type:
            continue
        value = answer.get("data")
        if not isinstance(value, str) or not value.strip():
            raise OSError("Cloudflare DoH response is invalid")
        addresses.append(_parse_public_address(value.strip()).compressed)
    return tuple(addresses)


def _deduplicate(addresses: Iterable[_Address]) -> tuple[_Address, ...]:
    """按 IPv4 优先并去除重复地址，兼容当前 httpx 连接行为。"""

    unique = list(dict.fromkeys(addresses))
    return tuple(sorted(unique, key=lambda item: (isinstance(item, ipaddress.IPv6Address), str(item))))


def _records(addresses: Iterable[_Address], port: int) -> list[_SocketRecord]:
    """生成 outbound.py 读取的最小 getaddrinfo 记录。"""

    result: list[_SocketRecord] = []
    for address in addresses:
        family = socket.AF_INET6 if isinstance(address, ipaddress.IPv6Address) else socket.AF_INET
        result.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(address), port)))
    return result


def _is_sinkhole_address(address: _Address) -> bool:
    """识别代理/DNS 劫持常用的 RFC 2544 benchmark 假地址段。"""

    return address in _SINKHOLE_NETWORK


def _is_blocked_non_sinkhole_address(address: _Address) -> bool:
    """识别需要保持原样并交给出网层阻断的非 sinkhole 地址。"""

    return not _is_sinkhole_address(address) and not address.is_global
