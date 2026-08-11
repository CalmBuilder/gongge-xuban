"""
@Time       : 2026/08/12 02:55
@Author     : zhanglp8181
@File       : remote_source.py
@CallChain  : ImportJob source adapter → SecureHttpsFetcher → pinned HTTPS archive bytes
@Description: 对远程 Skill 源执行固定 revision、逐跳 DNS、TLS 主机名和响应预算校验。
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit


GITHUB_ARCHIVE_HOSTS = frozenset({"github.com", "codeload.github.com"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class GeneralSkillRemoteSourceError(RuntimeError):
    """表示远程来源违反网络边界、固定版本或下载预算。"""

    def __init__(self, error_code: str, detail: str) -> None:
        """保存稳定错误码和不包含 DNS/IP/查询参数的脱敏摘要。"""

        super().__init__(detail)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class RemoteFetchResult:
    """保存最终公开 URL、响应正文和逐跳次数，不暴露解析 IP。"""

    final_url: str
    payload: bytes
    redirect_count: int


class RemoteFetcher(Protocol):
    """定义导入服务所需的最小安全远程抓取端口。"""

    def fetch(
        self,
        source_url: str,
        *,
        allowed_hosts: frozenset[str] | None = None,
    ) -> RemoteFetchResult:
        """按可选 host allowlist 返回有限、已校验的远程正文。"""

        ...


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    """表示一次已固定目标 IP 的 HTTPS 响应。"""

    status: int
    headers: dict[str, str]
    payload: bytes


Resolver = Callable[[str, int], tuple[str, ...]]
Exchange = Callable[[str, str, int, float], _ExchangeResult]


class SecureHttpsFetcher:
    """逐跳解析并固定公开 IP，拒绝 urllib 自动重定向与 DNS 重绑定窗口。"""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        exchange: Exchange | None = None,
        timeout_seconds: float = 120,
        max_redirects: int = 3,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        """注入可测试的 DNS/HTTPS 边界并固定默认下载预算。"""

        self.resolver = resolver or _resolve_host
        self.exchange = exchange or _https_exchange
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes

    def fetch(
        self,
        source_url: str,
        *,
        allowed_hosts: frozenset[str] | None = None,
    ) -> RemoteFetchResult:
        """校验每个 URL 与 DNS 结果，手动跟随有限重定向并整包读取。"""

        current = source_url
        for redirect_count in range(self.max_redirects + 1):
            parsed = _validated_https_url(current, allowed_hosts)
            host = parsed.hostname or ""
            addresses = self.resolver(host, 443)
            public_addresses = _validated_public_addresses(addresses)
            response = self.exchange(
                current,
                public_addresses[0],
                self.max_bytes,
                self.timeout_seconds,
            )
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise GeneralSkillRemoteSourceError(
                        "GENERAL_SKILL_PACKAGE_INVALID", "remote redirect has no location"
                    )
                if redirect_count >= self.max_redirects:
                    raise GeneralSkillRemoteSourceError(
                        "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED",
                        "remote source exceeded redirect limit",
                    )
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise GeneralSkillRemoteSourceError(
                    "GENERAL_SKILL_PACKAGE_INVALID", "remote source did not return a ZIP package"
                )
            return RemoteFetchResult(
                final_url=_redacted_url(current),
                payload=response.payload,
                redirect_count=redirect_count,
            )
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED", "remote source exceeded redirect limit"
        )


def github_archive_url(repository_url: str, revision: str) -> str:
    """把标准 GitHub 仓库 URL 与 40 位 commit SHA 转为不可漂移归档地址。"""

    parsed = _validated_https_url(repository_url, frozenset({"github.com"}))
    parts = [item for item in parsed.path.split("/") if item]
    if len(parts) != 2:
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "GitHub source must identify one repository"
        )
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository or not _is_commit_sha(revision):
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "GitHub source requires a full commit SHA"
        )
    return f"https://github.com/{owner}/{repository}/archive/{revision}.zip"


def _validated_https_url(source_url: str, allowed_hosts: frozenset[str] | None):
    """拒绝非 HTTPS、userinfo、非 443 端口、fragment 和未允许主机。"""

    try:
        parsed = urlsplit(source_url)
        port = parsed.port
    except ValueError as exc:
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "remote source URL is invalid"
        ) from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "remote source must be a plain HTTPS URL"
        )
    if allowed_hosts is not None and hostname not in allowed_hosts:
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "remote redirect host is not allowed"
        )
    return parsed


def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    """解析主机全部 A/AAAA 地址，供连接前统一公开网校验。"""

    try:
        rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "remote source hostname cannot be resolved"
        ) from exc
    return tuple(sorted({str(row[4][0]) for row in rows}))


def _validated_public_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    """要求 DNS 返回非空且全部为 global 地址，混入私网同样 fail-closed。"""

    if not addresses:
        raise GeneralSkillRemoteSourceError(
            "GENERAL_SKILL_PACKAGE_INVALID", "remote source hostname has no address"
        )
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise GeneralSkillRemoteSourceError(
                "GENERAL_SKILL_PACKAGE_INVALID", "remote source resolved an invalid address"
            ) from exc
        if not parsed.is_global:
            raise GeneralSkillRemoteSourceError(
                "GENERAL_SKILL_PACKAGE_INVALID", "remote source resolved a non-public address"
            )
    return tuple(sorted(addresses))


def _https_exchange(
    source_url: str,
    address: str,
    max_bytes: int,
    timeout_seconds: float,
) -> _ExchangeResult:
    """连接已验证 IP，以原 hostname 完成 TLS SNI/证书校验并有限读取响应。"""

    parsed = urlsplit(source_url)
    hostname = parsed.hostname or ""
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    raw_socket = socket.create_connection((address, 443), timeout=timeout_seconds)
    try:
        tls_socket = ssl.create_default_context().wrap_socket(
            raw_socket,
            server_hostname=hostname,
        )
    except Exception:
        raw_socket.close()
        raise
    try:
        peer_address = str(tls_socket.getpeername()[0])
        if peer_address != address or not ipaddress.ip_address(peer_address).is_global:
            raise GeneralSkillRemoteSourceError(
                "GENERAL_SKILL_PACKAGE_INVALID", "remote peer address changed during connect"
            )
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {hostname}\r\n"
            "User-Agent: gongge-xuban-skill-import/1\r\n"
            "Accept: application/zip, application/octet-stream\r\n"
            "Connection: close\r\n\r\n"
        )
        tls_socket.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(tls_socket)
        response.begin()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise GeneralSkillRemoteSourceError(
                "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED",
                "remote package exceeds configured byte limit",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise GeneralSkillRemoteSourceError(
                    "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED",
                    "remote package exceeds configured byte limit",
                )
            chunks.append(chunk)
        headers = {key.lower(): value for key, value in response.headers.items()}
        return _ExchangeResult(response.status, headers, b"".join(chunks))
    finally:
        tls_socket.close()


def _redacted_url(source_url: str) -> str:
    """移除可能携带签名或凭据的 query，只保留公开来源定位。"""

    parsed = urlsplit(source_url)
    return parsed._replace(query="", fragment="").geturl()


def _is_commit_sha(value: str) -> bool:
    """只接受完整 40 位十六进制 commit，拒绝 branch/tag 漂移。"""

    return len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value)
