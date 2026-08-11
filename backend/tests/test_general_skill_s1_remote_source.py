"""
@Time       : 2026/08/12 03:15
@Author     : zhanglp8181
@File       : test_general_skill_s1_remote_source.py
@CallChain  : pytest → SecureHttpsFetcher/GitHub adapter → injected DNS/HTTPS exchange
@Description: 验证固定 revision、逐跳 SSRF、重定向、下载预算和来源去敏契约。
"""

from __future__ import annotations

from collections import deque
import ssl

import pytest

from app.general_skills.remote_source import (
    GITHUB_ARCHIVE_HOSTS,
    GeneralSkillRemoteSourceError,
    SecureHttpsFetcher,
    _ExchangeResult,
    github_archive_url,
    skillhub_archive_url,
)


class _ExchangeStub:
    """按调用顺序返回预设 HTTPS 响应并记录已固定的 IP。"""

    def __init__(self, *responses: _ExchangeResult) -> None:
        """初始化响应队列和调用记录。"""

        self.responses = deque(responses)
        self.calls: list[tuple[str, str, int, float, str | None]] = []

    def __call__(
        self,
        source_url: str,
        address: str,
        max_bytes: int,
        timeout_seconds: float,
        authorization: str | None,
    ) -> _ExchangeResult:
        """记录 URL/IP/预算并返回下一个预设响应。"""

        self.calls.append((source_url, address, max_bytes, timeout_seconds, authorization))
        return self.responses.popleft()


def test_github_archive_requires_exact_repository_and_full_commit_sha() -> None:
    """验证 GitHub branch、子路径、userinfo 和短 SHA 均不能进入供应链快照。"""

    revision = "84fdeffd12f2ee307994d1eb6feb48173b6e0502"
    assert github_archive_url("https://github.com/mattpocock/skills", revision) == (
        f"https://github.com/mattpocock/skills/archive/{revision}.zip"
    )
    for source, invalid_revision in (
        ("https://github.com/mattpocock/skills/tree/main", revision),
        ("https://user@github.com/mattpocock/skills", revision),
        ("https://github.com/mattpocock/skills", "main"),
        ("https://github.com/mattpocock/skills", "84fdeff"),
    ):
        with pytest.raises(GeneralSkillRemoteSourceError):
            github_archive_url(source, invalid_revision)


def test_skillhub_adapter_accepts_only_slug_or_known_page_hosts() -> None:
    """验证 SkillHub 来源只生成固定供应商端点，来源证据不保存下载 query。"""

    download_url, reference = skillhub_archive_url("customer-support")
    assert download_url == (
        "https://wry-manatee-359.convex.site/api/v1/download?slug=customer-support"
    )
    assert reference == "skillhub:customer-support"
    page_download, page_reference = skillhub_archive_url(
        "https://skillhub.ai/acme/customer-support"
    )
    assert page_download == download_url
    assert page_reference == reference
    for invalid in (
        "https://evil.example/customer-support",
        "https://user:secret@skillhub.ai/acme/customer-support",
        "customer/support",
        "a",
    ):
        with pytest.raises(GeneralSkillRemoteSourceError):
            skillhub_archive_url(invalid)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fc00::1"],
)
def test_fetcher_rejects_any_non_public_dns_answer_before_socket_exchange(address: str) -> None:
    """替换 S0 loopback 可达样本，证明私网/metadata 地址不会触发 HTTPS 请求。"""

    exchange = _ExchangeStub(_ExchangeResult(200, {}, b"payload"))
    fetcher = SecureHttpsFetcher(
        resolver=lambda _host, _port: (address,),
        exchange=exchange,
    )
    with pytest.raises(GeneralSkillRemoteSourceError, match="non-public") as captured:
        fetcher.fetch("https://packages.example.com/skill.zip")
    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"
    assert exchange.calls == []


def test_fetcher_revalidates_dns_and_host_allowlist_on_every_redirect() -> None:
    """验证 GitHub 到 codeload 的合法跳转逐跳解析，第三方跳转在连接前拒绝。"""

    resolved: list[str] = []

    def resolver(host: str, _port: int) -> tuple[str, ...]:
        """记录每一跳主机并返回文档保留之外的公开地址。"""

        resolved.append(host)
        return ("8.8.8.8",)

    exchange = _ExchangeStub(
        _ExchangeResult(302, {"location": "https://codeload.github.com/org/repo/zip/abc"}, b""),
        _ExchangeResult(200, {}, b"zip"),
    )
    result = SecureHttpsFetcher(resolver=resolver, exchange=exchange).fetch(
        "https://github.com/org/repo/archive/abc.zip",
        allowed_hosts=GITHUB_ARCHIVE_HOSTS,
    )
    assert result.payload == b"zip"
    assert result.redirect_count == 1
    assert resolved == ["github.com", "codeload.github.com"]

    rejected_exchange = _ExchangeStub(
        _ExchangeResult(302, {"location": "https://evil.example/steal.zip"}, b""),
    )
    with pytest.raises(GeneralSkillRemoteSourceError, match="host is not allowed"):
        SecureHttpsFetcher(resolver=resolver, exchange=rejected_exchange).fetch(
            "https://github.com/org/repo/archive/abc.zip",
            allowed_hosts=GITHUB_ARCHIVE_HOSTS,
        )
    assert len(rejected_exchange.calls) == 1


def test_fetcher_rejects_mixed_public_and_private_dns_answers() -> None:
    """验证攻击者 DNS 同时返回公网与私网地址时不能挑选公网后放行。"""

    exchange = _ExchangeStub(_ExchangeResult(200, {}, b"zip"))
    fetcher = SecureHttpsFetcher(
        resolver=lambda _host, _port: ("8.8.8.8", "127.0.0.1"),
        exchange=exchange,
    )
    with pytest.raises(GeneralSkillRemoteSourceError, match="non-public"):
        fetcher.fetch("https://packages.example.com/skill.zip")
    assert exchange.calls == []


def test_fetcher_limits_redirects_and_removes_query_from_provenance() -> None:
    """验证重定向有硬上限，成功来源快照不会保留签名 query。"""

    redirect = _ExchangeResult(302, {"location": "/next?secret=token"}, b"")
    exchange = _ExchangeStub(redirect, redirect)
    fetcher = SecureHttpsFetcher(
        resolver=lambda _host, _port: ("8.8.8.8",),
        exchange=exchange,
        max_redirects=1,
    )
    with pytest.raises(GeneralSkillRemoteSourceError, match="redirect limit"):
        fetcher.fetch("https://packages.example.com/start?signature=secret")

    success_exchange = _ExchangeStub(_ExchangeResult(200, {}, b"zip"))
    result = SecureHttpsFetcher(
        resolver=lambda _host, _port: ("8.8.8.8",),
        exchange=success_exchange,
    ).fetch("https://packages.example.com/archive.zip?signature=secret")
    assert result.final_url == "https://packages.example.com/archive.zip"


def test_fetcher_preserves_explicit_download_budget_for_exchange() -> None:
    """验证网络边界接收固定下载字节和超时预算，不能由来源扩大。"""

    exchange = _ExchangeStub(_ExchangeResult(200, {}, b"zip"))
    SecureHttpsFetcher(
        resolver=lambda _host, _port: ("8.8.8.8",),
        exchange=exchange,
        max_bytes=1234,
        timeout_seconds=17,
    ).fetch("https://packages.example.com/archive.zip")
    assert exchange.calls == [
        ("https://packages.example.com/archive.zip", "8.8.8.8", 1234, 17, None)
    ]


def test_fetcher_never_forwards_authorization_to_redirected_host() -> None:
    """验证私有来源授权只发往绑定主机，合法跨主机重定向也不会携带 Token。"""

    exchange = _ExchangeStub(
        _ExchangeResult(302, {"location": "https://codeload.github.com/org/repo.zip"}, b""),
        _ExchangeResult(200, {}, b"zip"),
    )
    result = SecureHttpsFetcher(
        resolver=lambda _host, _port: ("8.8.8.8",),
        exchange=exchange,
    ).fetch(
        "https://github.com/org/repo/archive/abc.zip",
        allowed_hosts=GITHUB_ARCHIVE_HOSTS,
        authorization="Bearer private-token",
        authorization_hosts=frozenset({"github.com"}),
    )

    assert result.payload == b"zip"
    assert exchange.calls[0][-1] == "Bearer private-token"
    assert exchange.calls[1][-1] is None


@pytest.mark.parametrize("failure", [TimeoutError("late"), ssl.SSLError("bad certificate")])
def test_fetcher_converts_transport_failures_to_redacted_domain_error(failure: Exception) -> None:
    """验证超时/TLS 异常不会穿透为 500 或携带底层连接目标。"""

    def failing_exchange(
        _source_url: str,
        _address: str,
        _max_bytes: int,
        _timeout_seconds: float,
        _authorization: str | None,
    ) -> _ExchangeResult:
        """模拟供应商网络在 socket/TLS 阶段失败。"""

        raise failure

    fetcher = SecureHttpsFetcher(
        resolver=lambda _host, _port: ("8.8.8.8",),
        exchange=failing_exchange,
    )
    with pytest.raises(GeneralSkillRemoteSourceError) as captured:
        fetcher.fetch("https://packages.example.com/archive.zip?token=never-log")

    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"
    assert str(captured.value) == "remote source request failed or timed out"
    assert "never-log" not in str(captured.value)
