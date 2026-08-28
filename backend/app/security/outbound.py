"""
@Time       : 2026/08/28 11:00
@Author     : zhanglp8181
@File       : outbound.py
@CallChain  : Tools API/MCP Client → outbound target policy → HTTPX
@Description: 校验外部 HTTP 目标、阻断私网与元数据地址，并为域名请求固定解析结果以降低 DNS 重绑定风险。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


class OutboundTargetError(ValueError):
    """表示出网目标不是允许的 HTTP/HTTPS 地址。"""


@dataclass(frozen=True, slots=True)
class PinnedOutboundTarget:
    """携带逻辑 URL、固定连接地址和 TLS/Host 元数据的受控请求目标。"""

    logical_url: str
    request_url: str
    headers: dict[str, str]
    extensions: dict[str, object]


def allowed_hosts_from_settings(settings: object) -> frozenset[str]:
    """读取显式出网白名单，并自动加入工具服务基础地址的主机名。"""

    raw = getattr(settings, "tool_outbound_allowed_hosts", "")
    values = set(normalize_allowed_hosts(raw))
    base_url = str(getattr(settings, "normalized_tool_base_url", "") or "")
    try:
        base_host = urlsplit(base_url).hostname
    except ValueError:
        base_host = None
    if base_host:
        values.add(base_host.casefold().rstrip("."))
    return frozenset(values)


def normalize_allowed_hosts(values: str | Iterable[str] | None) -> frozenset[str]:
    """把逗号分隔或数组形式的白名单规范为不含端口的小写主机集合。"""

    if values is None:
        return frozenset()
    if isinstance(values, str):
        candidates: Iterable[str] = values.split(",")
    else:
        candidates = values
    normalized: set[str] = set()
    for value in candidates:
        item = str(value or "").strip().casefold().rstrip(".")
        if not item:
            continue
        if "://" in item:
            try:
                item = urlsplit(item).hostname or ""
            except ValueError:
                item = ""
        elif item.startswith("[") and "]" in item:
            item = item[1 : item.index("]")]
        elif item.count(":") == 1 and not item.startswith("["):
            item = item.rsplit(":", 1)[0]
        if item:
            normalized.add(item.rstrip("."))
    return frozenset(normalized)


def prepare_outbound_request(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    resolver: object | None = None,
) -> PinnedOutboundTarget:
    """验证 HTTP 目标并在可行时把域名请求固定到一次解析得到的地址。"""

    logical_url = str(url or "").strip()
    try:
        parsed = urlsplit(logical_url)
        port = parsed.port
    except ValueError as exc:
        raise OutboundTargetError("出网地址格式或端口无效。") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise OutboundTargetError("出网地址只允许使用 HTTP 或 HTTPS。")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundTargetError("出网地址不允许携带用户信息。")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        raise OutboundTargetError("出网地址缺少主机名。")
    if parsed.fragment:
        raise OutboundTargetError("出网地址不允许携带 fragment。")
    approved = host in normalize_allowed_hosts(allowed_hosts)
    literal = _parse_ip(host)
    if literal is not None:
        if not approved and _is_blocked_address(literal):
            raise OutboundTargetError("出网地址指向受限内网或云元数据地址。")
        return PinnedOutboundTarget(logical_url, logical_url, {}, {})

    # 显式白名单是管理员对该主机的信任边界，可以允许企业内网地址；但仍必须
    # 固定一次解析结果，不能因白名单而重新打开 DNS 重绑定窗口。Host/SNI 保留
    # 逻辑主机名，以兼容企业虚拟主机和 mTLS 入口。
    effective_port = port if port is not None else _default_port(parsed.scheme)
    addresses = _resolve_addresses(host, effective_port, resolver)
    if not addresses:
        raise OutboundTargetError("出网地址无法解析。")
    if not approved and any(_is_blocked_address(address) for address in addresses):
        raise OutboundTargetError("出网地址解析结果包含受限内网或云元数据地址。")

    selected = addresses[0]
    request_url = _replace_hostname(parsed, selected, port)
    headers = {"Host": _host_header(parsed, port)}
    extensions: dict[str, object] = {}
    if parsed.scheme.lower() == "https":
        extensions["sni_hostname"] = host
    return PinnedOutboundTarget(logical_url, request_url, headers, extensions)


def is_same_origin(url: str, base_url: str) -> bool:
    """判断两个 URL 是否具有相同的 HTTP(S) scheme、主机和有效端口。"""

    try:
        left = urlsplit(url)
        right = urlsplit(base_url)
        left_port = left.port if left.port is not None else _default_port(left.scheme)
        right_port = right.port if right.port is not None else _default_port(right.scheme)
    except ValueError:
        return False
    return (
        left.scheme.lower(),
        (left.hostname or "").casefold(),
        left_port,
    ) == (
        right.scheme.lower(),
        (right.hostname or "").casefold(),
        right_port,
    )


def _resolve_addresses(host: str, port: int | None, resolver: object | None) -> list[IPv4Address | IPv6Address]:
    """解析目标主机的全部 TCP 地址，任一受限地址都由上层拒绝。"""

    resolve = resolver if callable(resolver) else socket.getaddrinfo
    try:
        records = resolve(
            host,
            port if port is not None else _default_port("http"),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise OutboundTargetError("出网地址无法解析。") from exc
    addresses: list[IPv4Address | IPv6Address] = []
    for record in records:
        try:
            address = _parse_ip(str(record[4][0]))
        except (IndexError, TypeError, ValueError):
            address = None
        if address is not None and address not in addresses:
            addresses.append(address)
    return addresses


def _parse_ip(value: str) -> IPv4Address | IPv6Address | None:
    """解析普通及 IPv4-mapped IPv6 字面量，无法解析时返回空值。"""

    try:
        parsed = ip_address(value)
    except ValueError:
        return None
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _is_blocked_address(address: IPv4Address | IPv6Address) -> bool:
    """识别 loopback、私网、链路本地、保留、未指定和 CGNAT 地址。"""

    cgnat = IPv4Address("100.64.0.0") <= address <= IPv4Address("100.127.255.255") if isinstance(address, IPv4Address) else False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or cgnat
    )


def _replace_hostname(parsed, address: IPv4Address | IPv6Address, port: int | None) -> str:
    """用固定 IP 重建请求 URL，同时保留原路径、查询和端口。"""

    host = str(address)
    if isinstance(address, IPv6Address):
        host = f"[{host}]"
    netloc = host
    effective_port = port if port is not None else _default_port(parsed.scheme)
    if effective_port is not None and effective_port != _default_port(parsed.scheme):
        netloc = f"{netloc}:{effective_port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _host_header(parsed, port: int | None) -> str:
    """生成连接到固定 IP 时仍发送给上游的原始 Host 头。"""

    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    effective_port = port if port is not None else _default_port(parsed.scheme)
    if effective_port is not None and effective_port != _default_port(parsed.scheme):
        return f"{host}:{effective_port}"
    return host


def _default_port(scheme: str) -> int | None:
    """返回 HTTP/HTTPS 默认端口。"""

    return 443 if scheme.lower() == "https" else 80 if scheme.lower() == "http" else None
