"""
@Time       : 2026/09/05 18:52
@Author     : zhanglp8181
@File       : test_vertical_ability_content_integrity.py
@CallChain  : pytest → 能力适配器 → MCP/Reader/RSS/V2EX/GitHub 固定回执 → 模型结果投影
@Description: 验证 C2 入口质量、公开条目与 README 正文保真，以及部分失败经原输出合同仍可见。
"""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.tools.mcp_client import _HttpSession
from app.cancellation import TurnCancellationRequested
from app.dynamic_tasks.execution_context import project_result_for_model
from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
)
from app.vertical_abilities.catalog import (
    _GITHUB_REPOSITORY_OUTPUT_SCHEMA,
    _V2EX_OUTPUT_SCHEMA,
)


def _settings() -> SimpleNamespace:
    """只提供固定适配器参数，不加载环境文件、服务或真实凭据。"""

    return SimpleNamespace(
        vertical_ability_exa_mcp_url="https://mcp.exa.ai/mcp",
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
        vertical_ability_jina_api_key="",
    )


def _mock_mcp_response(monkeypatch: pytest.MonkeyPatch, raw: object) -> None:
    """仅替换 HTTP 会话的协议读写，保留 execute_mcp_tool 和结果提取生产链路。"""

    def request(_session: object, method: str, params: dict[str, object]) -> object:
        """初始化返回空确认，工具调用返回预置回执而不访问网络。"""

        if method == "initialize":
            return {}
        assert method == "tools/call"
        assert params["name"] == "web_search_exa"
        return raw

    monkeypatch.setattr(_HttpSession, "_request", request)
    monkeypatch.setattr(_HttpSession, "_notify", lambda *_args: None)


@pytest.mark.parametrize("structured_key", ["structuredContent", "structured_content"])
def test_exa_real_mcp_extraction_preserves_text_beside_empty_results(
    monkeypatch: pytest.MonkeyPatch,
    structured_key: str,
) -> None:
    """经过真实 MCP 调用与提取后，空结构化列表仍可回退到同一响应中的有效来源。"""

    _mock_mcp_response(
        monkeypatch,
        {
            structured_key: {"results": [], "count": 0},
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Title: Public CLI guide\nURL: https://example.org/cli\nHighlights:\nCLI completion."
                    ),
                }
            ],
        },
    )
    result = execute_vertical_adapter("exa.web_search_exa", {"query": "CLI"}, settings=_settings())
    assert result["count"] == 1
    assert result["results"][0]["url"] == "https://example.org/cli"
    assert result["result_state"] == "success_with_data"
    assert result["retrieved_at"]


@pytest.mark.parametrize("wire_format", ["json", "sse"])
def test_exa_http_json_rpc_roundtrip_preserves_mixed_content(
    monkeypatch: pytest.MonkeyPatch,
    wire_format: str,
) -> None:
    """保留 HTTP、JSON-RPC 和结果解析生产链，只用内存传输代替真实外呼。"""

    client_type = httpx.Client
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """按真实 MCP 初始化、通知和调用顺序返回 JSON 或 SSE 格式的混合回执。"""

        payload = json.loads(request.content)
        requests.append(payload["method"])
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        result: object = {}
        if payload["method"] == "tools/call":
            assert payload["params"]["name"] == "web_search_exa"
            result = {
                "structuredContent": {"results": []},
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Title: CLI\nURL: https://example.org/cli\nHighlights:\nCompletion guide"
                        ),
                    }
                ],
            }
        response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if wire_format == "sse":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"event: message\ndata: {json.dumps(response)}\n\n",
            )
        return httpx.Response(200, json=response)

    def client(**kwargs: object) -> httpx.Client:
        """构建不会访问 socket 的 HTTPX 客户端，保留其真实请求与响应行为。"""

        return client_type(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr("app.tools.mcp_client.httpx.Client", client)
    monkeypatch.setattr(
        _HttpSession,
        "_target",
        lambda _self, url: SimpleNamespace(
            request_url=url,
            headers={},
            extensions={},
        ),
    )
    result = execute_vertical_adapter("exa.web_search_exa", {"query": "CLI"}, settings=_settings())
    assert requests == ["initialize", "notifications/initialized", "tools/call"]
    assert result["count"] == 1
    assert result["results"][0]["url"] == "https://example.org/cli"


@pytest.mark.parametrize(
    "raw",
    [
        {"results": []},
        {
            "structuredContent": {"results": []},
            "content": [{"type": "text", "text": "No results found."}],
        },
        {"content": [{"type": "text", "text": "No search results found"}]},
        {"content": [{"type": "text", "text": "results: []"}]},
        {
            "content": [
                {"type": "text", "text": "No results found"},
                {"type": "text", "text": "No results found"},
            ]
        },
    ],
)
def test_exa_explicit_empty_response_is_not_a_synthetic_result(
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
) -> None:
    """明确的无结果提示和空集合均返回 no_results，不制造没有 URL 的来源。"""

    _mock_mcp_response(monkeypatch, raw)
    result = execute_vertical_adapter("exa.web_search_exa", {"query": "CLI"}, settings=_settings())
    assert result["results"] == []
    assert result["count"] == 0
    assert result["result_state"] == "no_results"


@pytest.mark.parametrize(
    ("raw", "error_code"),
    [
        (
            {"content": [{"type": "text", "text": "Service temporarily unavailable"}]},
            "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
        ),
        (
            {"content": [{"type": "text", "text": "Unrecognized provider format"}]},
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
        ),
        (
            {"content": [{"type": "text", "text": "  "}]},
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
        ),
        (
            {"results": [{"title": "error", "text": "upstream failed"}]},
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
        ),
        (
            {"results": [{"title": "bad link", "url": "javascript:alert(1)"}]},
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
        ),
        (
            {"error": {"message": "upstream unavailable"}, "results": []},
            "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
        ),
        (
            {
                "structuredContent": {"results": []},
                "content": [{"type": "text", "text": "Service temporarily unavailable"}],
            },
            "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
        ),
        ({"success": False, "results": []}, "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"),
        ({"status": "error", "results": []}, "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"),
        ({"status_code": 503, "results": []}, "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"),
    ],
)
def test_exa_failed_or_unknown_payload_never_becomes_success(
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
    error_code: str,
) -> None:
    """服务错误、未知格式及缺少合法来源的内容必须进入稳定错误路径。"""

    _mock_mcp_response(monkeypatch, raw)
    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter("exa.web_search_exa", {"query": "CLI"}, settings=_settings())
    assert error.value.code == error_code


def test_exa_valid_source_about_errors_is_not_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源正文讨论报错不等于 Provider 报错；保留有合法 URL 的正常搜索结果。"""

    _mock_mcp_response(
        monkeypatch,
        {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Title: Service temporarily unavailable\nURL: https://example.org/errors\n"
                        "Highlights:\nNo results found can mean an empty query."
                    ),
                }
            ]
        },
    )
    result = execute_vertical_adapter(
        "exa.web_search_exa", {"query": "errors"}, settings=_settings()
    )
    assert result["count"] == 1


def _mock_jina_body(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """把 Reader 的出网层替换为固定文本，不绕过正文识别生产逻辑。"""

    monkeypatch.setattr("app.vertical_abilities.adapters._validate_public_url", lambda *_args: None)
    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes", lambda *_args, **_kwargs: body.encode()
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        " \n\t",
        "\ufeff  ",
        " \ufeff  ",
        "Title: Empty\nURL Source: https://example.org/\n",
        "Title: Empty\nURL Source: https://example.org/\nMarkdown Content:\n  ",
    ],
)
def test_jina_rejects_empty_body_including_nonempty_metadata(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    """元数据不算原文；空字节、纯空白和只有 Reader 头部均不能作为读取成功。"""

    _mock_jina_body(monkeypatch, body)
    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "jina.reader", {"url": "https://example.org/article"}, settings=_settings()
        )
    assert error.value.code == "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_jina_distinguishes_target_http_failure_from_outer_success(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """Reader 外层 HTTP 成功但目标站失败时，保存目标状态码并拒绝作为正文。"""

    _mock_jina_body(
        monkeypatch,
        (
            f"Title: Failure\nWarning: Target URL returned error {status}: Failure\n"
            "Markdown Content:\nFailure page"
        ),
    )
    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "jina.reader", {"url": "https://example.org/article"}, settings=_settings()
        )
    assert error.value.code == "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"
    assert error.value.status_code == status


@pytest.mark.parametrize(
    "body",
    [
        "# Article",
        "Title: Errors guide\nMarkdown Content:\nExamples:\nWarning: Target URL returned error 404: Not Found",
        "Title: Cache\nWarning: Target URL returned error 304: Not Modified\nMarkdown Content:\nCached article",
    ],
)
def test_jina_preserves_valid_body_without_keyword_false_positive(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    """正文引用 HTTP 错误示例或有效缓存状态时不误判，并保留原文、来源和时间。"""

    _mock_jina_body(monkeypatch, body)
    result = execute_vertical_adapter(
        "jina.reader", {"url": "https://example.org/article"}, settings=_settings()
    )
    assert result["content"] == body
    assert result["url"] == "https://example.org/article"
    assert result["retrieved_at"]


@pytest.mark.parametrize(
    "body",
    [None, "", "Reproduce with --completion bash", "详" * 4100],
    ids=["missing", "empty", "body", "truncated"],
)
def test_github_issue_preserves_bounded_body_and_missing_semantics(
    monkeypatch: pytest.MonkeyPatch,
    body: str | None,
) -> None:
    """经过仓库适配器，Issue 正文可追溯且有截断标记，缺失正文不伪造文本。"""

    def github_get(path: str, **_kwargs: object) -> object:
        """分别返回仓库和含正文的 Issue，避免读取真实 GitHub 或凭据。"""

        if "/issues?" in path:
            return [
                {"number": 1, "html_url": "https://github.com/acme/demo/issues/1", "body": body}
            ]
        return {"full_name": "acme/demo", "stargazers_count": 0}

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", github_get)
    result = execute_vertical_adapter(
        "github.public_repo", {"repository": "acme/demo"}, settings=_settings()
    )
    issue = result["issues"][0]
    assert issue["body"] == (body[:4000] if body is not None else None)
    assert issue["body_truncated"] == (body is not None and len(body) > 4000)
    assert issue["comments"] is None
    assert result["repository"]["stars"] == 0
    assert result["repository"]["forks"] is None
    assert result["repository"]["open_issues"] is None


@pytest.mark.parametrize(
    ("extra_chars", "upstream_truncated"),
    [(0, False), (0, True), (81000, False)],
    ids=["complete", "upstream-truncated", "bounded"],
)
def test_github_jina_fallback_preserves_page_body_without_inventing_counts(
    monkeypatch: pytest.MonkeyPatch,
    extra_chars: int,
    upstream_truncated: bool,
) -> None:
    """匿名限流降级后仍返回已取得的仓库正文，列表中未读取的 Issue 详情显式缺失。"""

    page_body = "Title: acme/demo\nMarkdown Content:\n# Cross-platform CLI\nSupports completion."
    page_body += "详" * extra_chars

    def rate_limited(*_args: object, **_kwargs: object) -> object:
        """模拟公开 GitHub API 限流以进入生产降级链。"""

        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR", "Rate limited", status_code=403
        )

    def jina_get(
        arguments: dict[str, object], *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        """返回仓库原文与已知 Issue 列表，但不伪造列表未含的正文和计数。"""

        content = page_body
        if "/issues?" in str(arguments["url"]):
            content = "* ### [Improve CLI](https://github.com/acme/demo/issues/1) Status: Open. #1"
        return {
            "content": content,
            "truncated": upstream_truncated,
            "retrieved_at": "2026-09-05T10:00:00+00:00",
            "provider": "Jina Reader",
        }

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", rate_limited)
    monkeypatch.setattr("app.vertical_abilities.adapters._jina_read", jina_get)
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
        jina_api_key="fixture-not-a-real-key",
    )
    repository = result["repository"]
    assert repository["content"] == page_body[:80000]
    assert repository["content_truncated"] == (upstream_truncated or len(page_body) > 80000)
    assert repository["content_retrieved_at"] == "2026-09-05T10:00:00+00:00"
    assert repository["content_provider"] == "Jina Reader"
    assert all(repository[field] is None for field in ("stars", "forks", "open_issues"))
    assert result["issues"][0]["body"] is None
    assert result["issues"][0]["comments"] is None
    assert "Cross-platform CLI" in json.dumps(result)


def _read_feed_fixture(monkeypatch: pytest.MonkeyPatch, xml: str) -> dict[str, object]:
    """通过真实 RSS 适配器解析固定 XML，禁止该夹具发起实际网络读取。"""

    monkeypatch.setattr("app.vertical_abilities.adapters._validate_public_url", lambda *_args: None)
    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda *_args, **_kwargs: xml.encode(),
    )
    return execute_vertical_adapter(
        "rss.atom",
        {"url": "https://example.org/feed.xml"},
        settings=_settings(),
    )


def test_rss_preserves_full_entry_identity_author_enclosure_and_encoded_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSS 摘要不能覆盖 content:encoded，条目标识、作者和附件仍可供后续下钻。"""

    result = _read_feed_fixture(
        monkeypatch,
        """
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/"
             xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
        <guid>entry-1</guid><title>Article</title><link>https://example.org/article</link>
        <dc:creator>Writer</dc:creator><description>Short summary</description>
        <content:encoded><![CDATA[<p>Full article</p>\n<p>Second paragraph</p>]]></content:encoded>
        <enclosure url="https://example.org/audio.mp3" type="audio/mpeg" length="1234"/>
        </item></channel></rss>
    """,
    )
    entry = result["entries"][0]
    assert entry["id"] == "entry-1"
    assert entry["author"] == "Writer"
    assert entry["summary"] == "Short summary"
    assert entry["content"] == "<p>Full article</p>\n<p>Second paragraph</p>"
    assert entry["content_type"] == "text/html"
    assert entry["content_truncated"] is False
    assert entry["enclosures"] == [
        {
            "url": "https://example.org/audio.mp3",
            "type": "audio/mpeg",
            "length": 1234,
        }
    ]


def test_atom_prefers_alternate_link_and_preserves_self_author_and_full_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atom self 链接仅作为来源元数据，正文地址优先 alternate，正文保留独立于摘要。"""

    result = _read_feed_fixture(
        monkeypatch,
        """
        <feed xmlns="http://www.w3.org/2005/Atom"><entry><id>urn:entry:1</id>
        <title>Article</title><link rel="self" href="https://example.org/entry.atom"/>
        <link rel="alternate" href="/article"/>
        <link rel="enclosure" href="/episode.m4a" type="audio/mp4" length="200"/>
        <author><name>Writer</name><uri>https://example.org/writer</uri></author>
        <summary>Short</summary><content type="text">Full article\nSecond paragraph</content>
        </entry></feed>
    """,
    )
    entry = result["entries"][0]
    assert entry["id"] == "urn:entry:1"
    assert entry["author"] == "Writer"
    assert entry["url"] == "https://example.org/article"
    assert {"rel": "self", "url": "https://example.org/entry.atom"} in entry["links"]
    assert entry["content"] == "Full article\nSecond paragraph"
    assert entry["content_type"] == "text"
    assert entry["enclosures"][0]["url"] == "https://example.org/episode.m4a"


def test_atom_self_only_link_is_not_presented_as_article_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅有 self 地址时明确缺少文章 URL，不把条目 XML 页面冒充原文。"""

    result = _read_feed_fixture(
        monkeypatch,
        """
        <feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Article</title>
        <link rel="self" href="https://example.org/entry.atom"/></entry></feed>
    """,
    )
    assert result["entries"][0]["url"] == ""
    assert result["entries"][0]["links"][0]["rel"] == "self"


def test_atom_xhtml_content_keeps_structure_and_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Atom XHTML 正文保留段落和链接结构，不退化为摘要或无分隔拼接字符串。"""

    result = _read_feed_fixture(
        monkeypatch,
        """
        <feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Article</title>
        <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
        <p>First paragraph</p><p><a href="https://example.org/ref">Reference</a></p>
        </div></content></entry></feed>
    """,
    )
    entry = result["entries"][0]
    assert "First paragraph" in entry["content"]
    assert 'href="https://example.org/ref"' in entry["content"]
    assert "<" in entry["content"]
    assert entry["content_type"] == "xhtml"


def test_rss_body_bounds_are_explicit_and_do_not_change_summary_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新增正文仍受大小上限约束，摘要维持原四千字符上限并明确披露截断。"""

    result = _read_feed_fixture(
        monkeypatch,
        (
            '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
            f"<title>Article</title><description>{'s' * 4100}</description>"
            f"<content:encoded>{'b' * 81000}</content:encoded></item></channel></rss>"
        ),
    )
    entry = result["entries"][0]
    assert len(entry["summary"]) == 4000
    assert entry["summary_truncated"] is True
    assert len(entry["content"]) == 80000
    assert entry["content_truncated"] is True


def test_rss_metadata_urls_cannot_carry_credentials_or_javascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保留链接和附件不意味着信任任意 URL；无效来源不应输出成可点击正文地址。"""

    result = _read_feed_fixture(
        monkeypatch,
        """
        <feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Article</title>
        <link rel="alternate" href="javascript:alert(1)"/>
        <link rel="enclosure" href="https://name:password@example.org/file"/>
        </entry></feed>
    """,
    )
    entry = result["entries"][0]
    assert entry["url"] == ""
    assert not entry.get("enclosures")
    assert "password" not in json.dumps(entry)


@pytest.mark.parametrize(
    "metadata",
    [
        '<link href="https://[invalid-host"/>',
        '<content src="https://[invalid-host"/>',
        '<link rel="enclosure" href="https://example.org/audio" length="²"/>',
    ],
)
def test_atom_invalid_optional_metadata_does_not_discard_valid_entry(
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    """可选链接的畸形主机或非十进制附件长度不能导致整篇已解析条目异常丢失。"""

    result = _read_feed_fixture(
        monkeypatch,
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Valid article</title>'
            '<link rel="alternate" href="https://example.org/article"/>'
            f"{metadata}</entry></feed>"
        ),
    )
    entry = result["entries"][0]
    assert entry["title"] == "Valid article"
    assert entry["url"] == "https://example.org/article"
    assert not entry.get("content_url")
    assert not entry.get("enclosures") or entry["enclosures"][0]["length"] is None


def _v2ex_topic_fixture(replies: int = 1) -> dict[str, object]:
    """构造包含作者和原始回复计数的固定主题，不依赖真实 V2EX 用户。"""

    return {
        "id": 7,
        "title": "CLI discussion",
        "url": "https://www.v2ex.com/t/7",
        "content": "Topic body",
        "replies": replies,
        "member": {
            "id": 10,
            "username": "topic-author",
            "url": "https://www.v2ex.com/member/topic-author",
        },
    }


def _v2ex_reply_fixture(reply_id: int = 8) -> dict[str, object]:
    """构造有正文、作者和时间的回复回执，供主题下钻与数量上限测试使用。"""

    return {
        "id": reply_id,
        "content": "Reply body",
        "created": 123456,
        "member": {"id": 20, "username": "reply-author"},
    }


def test_v2ex_topic_reads_reply_bodies_and_preserves_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主题成功后追加一次固定公开回复 API，原 replies 计数和 member 字段仍兼容。"""

    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> bytes:
        """按主题及回复端点提供独立回执，防止把主题对象重复当成回复。"""

        calls.append(url)
        payload = [_v2ex_reply_fixture()] if "/replies/" in url else [_v2ex_topic_fixture()]
        return json.dumps(payload).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "topic", "topic_id": 7},
        settings=_settings(),
    )
    item = result["items"][0]
    assert calls == [
        "https://www.v2ex.com/api/topics/show.json?id=7",
        "https://www.v2ex.com/api/replies/show.json?topic_id=7&page=1",
    ]
    assert item["member"] == _v2ex_topic_fixture()["member"]
    assert item["replies"] == 1
    assert item["author"] == "topic-author"
    assert item["reply_items"][0]["content"] == "Reply body"
    assert item["reply_items"][0]["author"] == "reply-author"
    assert item["reply_items"][0]["created"] == 123456
    assert item["reply_items_count"] == 1
    assert item["reply_items_result_state"] == "success_with_data"
    assert item["replies_url"] == calls[1]
    assert result["partial"] is False


@pytest.mark.parametrize("failure", ["http", "invalid-json", "invalid-shape", "invalid-item"])
def test_v2ex_reply_failure_keeps_topic_and_reports_partial_with_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """回复接口失败不能抹掉已读主题，也不能被包装为成功读取零条回复。"""

    def get(url: str, **_kwargs: object) -> bytes:
        """仅让子资源失败，验证错误分类和主资源保留行为。"""

        if "/replies/" not in url:
            return json.dumps([_v2ex_topic_fixture()]).encode()
        if failure == "http":
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
                "HTTP 503",
                status_code=503,
            )
        return {
            "invalid-json": b"not-json",
            "invalid-shape": b'{"error":"unavailable"}',
            "invalid-item": b'[{"unexpected":"reply"}]',
        }[failure]

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "topic", "topic_id": 7},
        settings=_settings(),
    )
    assert result["partial"] is True
    assert result["items"][0]["content"] == "Topic body"
    assert result["items"][0]["reply_items_result_state"] == "error"
    assert result["subresource_errors"][0]["resource"] == "v2ex.replies"
    assert result["subresource_errors"][0]["url"].endswith("topic_id=7&page=1")
    assert result["subresource_errors"][0]["code"] == (
        "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"
        if failure == "http"
        else "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"
    )


@pytest.mark.parametrize("reported_count", [0, 2])
def test_v2ex_reply_empty_result_distinguishes_zero_from_missing_reported_replies(
    monkeypatch: pytest.MonkeyPatch,
    reported_count: int,
) -> None:
    """空回复列表如实返回 no_results，若与主题计数矛盾则不能声称完整取得。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda url, **_kwargs: (
            b"[]"
            if "/replies/" in url
            else json.dumps([_v2ex_topic_fixture(reported_count)]).encode()
        ),
    )
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "topic", "topic_id": 7},
        settings=_settings(),
    )
    assert result["items"][0]["reply_items"] == []
    assert result["items"][0]["reply_items_result_state"] == "no_results"
    assert result["partial"] is (reported_count > 0)


def test_v2ex_reply_limit_is_bounded_and_reported_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沿用既有 limit 限制回复数量，同时保留总回复数和截断事实。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda url, **_kwargs: (
            json.dumps([_v2ex_reply_fixture(8), _v2ex_reply_fixture(9)]).encode()
            if "/replies/" in url
            else json.dumps([_v2ex_topic_fixture(2)]).encode()
        ),
    )
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "topic", "topic_id": 7, "limit": 1},
        settings=_settings(),
    )
    assert result["items"][0]["replies"] == 2
    assert result["items"][0]["reply_items_count"] == 1
    assert result["items"][0]["reply_items_truncated"] is True
    assert result["partial"] is True


def test_v2ex_reply_cancellation_is_not_converted_to_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子资源请求被取消时继续抛出取消信号，不产生迟到的部分成功。"""

    def get(url: str, **_kwargs: object) -> bytes:
        """取得主题后模拟底层回复读取观察到取消。"""

        if "/replies/" in url:
            raise TurnCancellationRequested("cancelled fixture")
        return json.dumps([_v2ex_topic_fixture()]).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    with pytest.raises(TurnCancellationRequested):
        execute_vertical_adapter(
            "v2ex.public_api",
            {"mode": "topic", "topic_id": 7},
            settings=_settings(),
        )


@pytest.mark.parametrize("mode", ["hot", "node"])
def test_v2ex_collection_modes_do_not_fetch_unrequested_reply_resources(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """热门和节点列表不逐条展开回复，保持原操作成本与 member 数据保真。"""

    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> bytes:
        """仅接受一次列表读取，任何额外回复请求都会进入可观测调用记录。"""

        calls.append(url)
        return json.dumps([_v2ex_topic_fixture()]).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": mode, "node_name": "python"},
        settings=_settings(),
    )
    assert len(calls) == 1
    assert result["items"][0]["member"]["username"] == "topic-author"
    assert "reply_items" not in result["items"][0]


def _github_readme_fixture(content: bytes = b"# CLI\n\nRead the guide.\n") -> dict[str, object]:
    """构造 GitHub JSON README 回执，保留编码、来源与文件大小供生产校验。"""

    return {
        "type": "file",
        "encoding": "base64",
        "size": len(content),
        "name": "README.md",
        "path": "README.md",
        "sha": "a" * 40,
        "content": base64.encodebytes(content).decode("ascii"),
        "html_url": "https://github.com/acme/demo/blob/main/README.md",
        "download_url": "https://not-a-source.example/unused-file",
    }


def _mock_github_readme(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> list[tuple[str, dict[str, object]]]:
    """只替换 HTTP 获取，分别返回仓库、Issue 和 README，并记录来源及请求头。"""

    calls: list[tuple[str, dict[str, object]]] = []

    def get(url: str, **kwargs: object) -> bytes:
        """不跟随 README 提供的下载地址，也不读取真实 GitHub 或登录凭据。"""

        calls.append((url, kwargs))
        if url == "https://api.github.com/repos/acme/demo":
            value = {
                "full_name": "acme/demo",
                "html_url": "https://github.com/acme/demo",
                "description": "CLI repository",
                "stargazers_count": 0,
            }
        elif url.startswith("https://api.github.com/repos/acme/demo/issues?"):
            value = [
                {
                    "number": 1,
                    "html_url": "https://github.com/acme/demo/issues/1",
                    "title": "Completion",
                    "body": "Completion issue body",
                }
            ]
        else:
            assert url == "https://api.github.com/repos/acme/demo/readme"
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, bytes):
                return payload
            value = payload
        return json.dumps(value).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    return calls


def test_github_repo_preserves_readme_raw_text_and_validated_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仓库 REST 成功时补取 README 原文并保留 Markdown、来源和时间，不只返回 description。"""

    content = "# 工程指南\n\n```bash\ncli --completion bash\n```\n"
    calls = _mock_github_readme(monkeypatch, _github_readme_fixture(content.encode()))
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
    )
    repository = result["repository"]
    assert len(calls) == 3
    assert calls[2][1]["headers"]["Accept"] == "application/vnd.github+json"
    assert calls[2][1]["allowed_hosts"] == {"api.github.com"}
    assert "Authorization" not in calls[2][1]["headers"]
    assert repository["description"] == "CLI repository"
    assert repository["stars"] == 0
    assert repository["content"] == content
    assert repository["content_url"] == "https://github.com/acme/demo/blob/main/README.md"
    assert repository["content_api_url"] == calls[2][0]
    assert repository["content_provider"] == "GitHub public REST API"
    assert repository["content_retrieved_at"]
    assert repository["content_truncated"] is False
    assert repository["readme_path"] == "README.md"
    assert repository["readme_sha"] == "a" * 40
    assert repository["readme_result_state"] == "success_with_data"
    assert result["partial"] is False
    assert result["issues"][0]["body"] == "Completion issue body"
    assert "not-a-source.example" not in json.dumps(result)


def test_github_readme_404_is_explicit_absence_not_total_repository_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """README 404 保留已经成功的仓库和 Issue，明确不存在，不制造读取异常或正文。"""

    _mock_github_readme(
        monkeypatch,
        VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            "HTTP 404",
            status_code=404,
        ),
    )
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
    )
    assert result["repository"]["url"] == "https://github.com/acme/demo"
    assert result["repository"]["content"] is None
    assert result["repository"]["readme_missing"] is True
    assert result["repository"]["readme_status_code"] == 404
    assert result["repository"]["readme_result_state"] == "no_results"
    assert result["issues_count"] == 1
    assert result["partial"] is False
    assert result["subresource_errors"] == []


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_github_readme_http_failure_is_partial_with_original_error(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """README 权限、限流或服务失败不能伪装不存在，也不能改走页面降级掩盖错误。"""

    calls = _mock_github_readme(
        monkeypatch,
        VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            f"HTTP {status}",
            status_code=status,
        ),
    )
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
    )
    assert len(calls) == 3
    assert result["partial"] is True
    assert result["repository"]["description"] == "CLI repository"
    assert result["repository"]["content"] is None
    assert result["repository"]["readme_result_state"] == "error"
    assert result["issues_count"] == 1
    error = result["subresource_errors"][0]
    assert error["resource"] == "github.readme"
    assert error["url"] == "https://api.github.com/repos/acme/demo/readme"
    assert error["code"] == "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"
    assert error["status_code"] == status


@pytest.mark.parametrize(
    "failure",
    [
        "invalid-json",
        "empty-shape",
        "directory",
        "encoding",
        "base64",
        "binary",
        "size-mismatch",
        "other-repo",
        "other-file",
        "credential-url",
        "unsafe-url",
    ],
)
def test_github_readme_invalid_payload_never_claims_valid_repository_body(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """编码、文件大小和仓库来源任一不符都显式部分失败，不能将错误文档当 README。"""

    payload = _github_readme_fixture()
    overrides = {
        "directory": {"type": "dir"},
        "encoding": {"encoding": "none"},
        "base64": {"content": "!!!"},
        "binary": {"content": "/w==", "size": 1},
        "size-mismatch": {"size": 100000},
        "other-repo": {"html_url": "https://github.com/other/demo/blob/main/README.md"},
        "other-file": {"html_url": "https://github.com/acme/demo/blob/main/OTHER.md"},
        "credential-url": {
            "html_url": "https://user:fixture-secret@github.com/acme/demo/blob/main/README.md"
        },
        "unsafe-url": {"html_url": "javascript:alert(1)"},
    }
    payload.update(overrides.get(failure, {}))
    raw = b"not-json" if failure == "invalid-json" else {} if failure == "empty-shape" else payload
    _mock_github_readme(monkeypatch, raw)
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
    )
    assert result["partial"] is True
    assert result["repository"]["readme_result_state"] == "error"
    assert result["repository"]["content"] is None
    assert result["subresource_errors"][0]["code"] == "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.parametrize("content", ["", "详" * 81000], ids=["empty-file", "bounded-body"])
def test_github_readme_empty_and_truncated_body_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    """合法空文件不伪造非空正文，大文件保留既有八万字符上限并披露 partial。"""

    _mock_github_readme(monkeypatch, _github_readme_fixture(content.encode()))
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
    )
    assert result["repository"]["content"] == content[:80000]
    assert result["repository"]["content_truncated"] is bool(content)
    assert result["repository"]["readme_result_state"] == (
        "success_with_data" if content else "no_results"
    )
    assert result["repository"]["readme_missing"] is False
    assert result["partial"] is bool(content)


def test_github_readme_cancellation_propagates_without_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """README 读取取消必须保留取消语义，不返回用户已取消后的仓库成功结果。"""

    _mock_github_readme(monkeypatch, TurnCancellationRequested("cancelled fixture"))
    with pytest.raises(TurnCancellationRequested):
        execute_vertical_adapter(
            "github.public_repo",
            {"repository": "acme/demo"},
            settings=_settings(),
        )


def test_v2ex_partial_error_survives_existing_model_output_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既有 schema 的 items 是开放数组，子资源错误必须在其中可见而非仅放顶层侧带。"""

    def get(url: str, **_kwargs: object) -> bytes:
        """模拟主题成功但回复失败，验证实际模型投影仍能看到稳定错误码。"""

        if "/replies/" in url:
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
                "HTTP 503",
                status_code=503,
            )
        return json.dumps([_v2ex_topic_fixture()]).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "topic", "topic_id": 7},
        settings=_settings(),
    )
    visible = project_result_for_model(result, _V2EX_OUTPUT_SCHEMA)
    assert visible["result_state"] == "success_with_data"
    assert visible["items"][0]["partial"] is True
    assert visible["items"][0]["subresource_errors"][0]["code"] == (
        "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"
    )
    assert visible["items"][0]["content"] == "Topic body"


def test_github_partial_error_survives_existing_model_output_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """README 子资源错误落入已有 repository 开放对象，不依赖修改冻结的输出合同。"""

    _mock_github_readme(
        monkeypatch,
        VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            "HTTP 503",
            status_code=503,
        ),
    )
    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo"},
        settings=_settings(),
    )
    visible = project_result_for_model(result, _GITHUB_REPOSITORY_OUTPUT_SCHEMA)
    assert visible["repository"]["partial"] is True
    assert visible["repository"]["subresource_errors"][0]["code"] == (
        "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"
    )
    assert visible["repository"]["content"] is None
    assert visible["issues"][0]["body"] == "Completion issue body"
