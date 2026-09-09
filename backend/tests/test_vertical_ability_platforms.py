"""
@Time       : 2026/09/03 20:00
@Author     : zhanglp8181
@File       : test_vertical_ability_platforms.py
@CallChain  : pytest → 全网情报员平台适配器 → OpenCLI/Cookie/公开 API 结果契约
@Description: 验证 Agent-Reach README 平台扩展的路由、连接边界和结构化非空/无结果语义。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
)


def _settings() -> SimpleNamespace:
    """构造不依赖网络和真实凭据的适配器配置。"""

    return SimpleNamespace(
        vertical_ability_exa_mcp_url="https://mcp.exa.ai/mcp",
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def _connection(platform_id: str, **values: str) -> dict[str, str]:
    """构造当前用户已经通过连接中心验证的最小连接快照。"""

    return {"session_reference": f"opencli:{platform_id}", **values}


def test_exa_accepts_structured_content_and_preserves_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 MCP 同时返回 structuredContent/content 时优先使用结构化结果。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters.execute_mcp_tool",
        lambda *_args, **_kwargs: {
            "structuredContent": {
                "results": [
                    {
                        "title": "LLM Runtime",
                        "url": "https://example.com/runtime",
                        "text": "A runtime article",
                    }
                ]
            },
            "content": [{"type": "text", "text": "results: []"}],
        },
    )

    result = execute_vertical_adapter(
        "exa.web_search_exa",
        {"query": "LLM runtime", "num_results": 5},
        settings=_settings(),
    )

    assert result["count"] == 1
    assert result["result_state"] == "success_with_data"
    assert result["results"][0]["url"] == "https://example.com/runtime"


def test_twitter_cookie_search_uses_child_runtime_without_exposing_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Twitter Cookie 路径使用显式凭据并返回来源卡片，而不把 Cookie 放入结果。"""

    captured: dict[str, object] = {}

    def fake_twitter(*args: object, **kwargs: object) -> object:
        """捕获固定 CLI 调用参数并返回 YAML 等价对象。"""

        captured["args"] = args
        captured["credentials"] = kwargs["credentials"]
        return {"tweets": [{"text": "公开观点", "url": "https://x.com/acme/status/1", "author": "acme"}]}

    monkeypatch.setattr("app.vertical_abilities.adapters.run_twitter_cli", fake_twitter)
    result = execute_vertical_adapter(
        "twitter.search",
        {"query": "agent runtime", "num_results": 3},
        settings=_settings(),
        platform_connections={
            "twitter": {
                "auth_token": "token-secret",
                "ct0": "ct0-secret",
            }
        },
    )

    assert result["count"] == 1
    assert result["results"][0]["summary"] == "公开观点"
    assert "token-secret" not in json.dumps(result, ensure_ascii=False)
    assert captured["credentials"] == {"auth_token": "token-secret", "ct0": "ct0-secret"}


def test_login_platform_without_connection_returns_actionable_error() -> None:
    """验证登录态平台未连接时返回连接中心提示，而不是空成功。"""

    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "twitter.search",
            {"query": "agent runtime"},
            settings=_settings(),
        )

    assert error.value.code == "VERTICAL_ABILITY_CONNECTION_REQUIRED"
    assert "网站连接中心" in error.value.message


def test_reddit_cookie_search_uses_fixed_json_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Reddit Cookie 只随项目固定请求发送，并将 children 归一化为结果。"""

    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> bytes:
        """返回 Reddit listing fixture 并捕获请求头。"""

        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return json.dumps(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "A Reddit issue",
                                "permalink": "/r/python/comments/abc123/issue",
                                "selftext": "Details",
                            }
                        }
                    ]
                }
            }
        ).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", fake_get)
    result = execute_vertical_adapter(
        "reddit.search",
        {"query": "python", "num_results": 5},
        settings=_settings(),
        platform_connections={"reddit": {"reddit_session": "session-secret"}},
    )

    assert result["count"] == 1
    assert result["results"][0]["title"] == "A Reddit issue"
    assert captured["url"].startswith("https://www.reddit.com/search.json?")
    assert captured["headers"]["Cookie"] == "reddit_session=session-secret"


def test_bilibili_subtitle_uses_opencli_and_distinguishes_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 B站视频详情和字幕读取是两个独立结果状态。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters.run_opencli",
        lambda *_args, **_kwargs: "第一句\n第二句",
    )
    result = execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": _connection("bilibili")},
    )

    assert result["subtitle_result_state"] == "success_with_data"
    assert result["subtitle"]["content"] == "第一句\n第二句"

    monkeypatch.setattr("app.vertical_abilities.adapters.run_opencli", lambda *_args, **_kwargs: "")
    empty = execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": _connection("bilibili")},
    )
    assert empty["subtitle_result_state"] == "no_results"


def test_opencli_social_platforms_share_fixed_operation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Facebook、Instagram、小红书共用平台运行时但不能传入任意命令。"""

    captured: list[tuple[object, ...]] = []

    def fake_opencli(*args: object, **_kwargs: object) -> object:
        """返回固定帖子对象并记录平台和操作。"""

        captured.append(args)
        return {"items": [{"name": "public profile", "url": "https://example.com/p/1"}]}

    monkeypatch.setattr("app.vertical_abilities.adapters.run_opencli", fake_opencli)
    for platform_id, adapter in (
        ("facebook", "facebook.opencli"),
        ("instagram", "instagram.opencli"),
        ("xiaohongshu", "xiaohongshu.opencli"),
    ):
        result = execute_vertical_adapter(
            adapter,
            {"operation": "search", "query": "agent", "limit": 5},
            settings=_settings(),
            platform_connections={platform_id: _connection(platform_id)},
        )
        assert result["count"] == 1
        assert result["result_state"] == "success_with_data"
    assert [item[:2] for item in captured] == [
        ("facebook", "search"),
        ("instagram", "search"),
        ("xiaohongshu", "search"),
    ]


def test_xueqiu_public_quote_normalizes_quote_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证雪球固定行情接口结果不会因为 data.quote 包装而变成空数组。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda *_args, **_kwargs: json.dumps(
            {"data": {"quote": {"symbol": "SH600519", "name": "贵州茅台", "current": 1500}}}
        ).encode(),
    )
    result = execute_vertical_adapter(
        "xueqiu.public_api",
        {"operation": "stock", "symbol": "SH600519"},
        settings=_settings(),
    )

    assert result["count"] == 1
    assert result["items"][0]["title"] == "贵州茅台"
    assert result["result_state"] == "success_with_data"


def test_xiaoyuzhou_adapter_reuses_whisper_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证小宇宙适配器调用统一 Whisper 运行时并保留来源字段。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters.transcribe_xiaoyuzhou_episode",
        lambda *_args, **_kwargs: {
            "source_url": "https://www.xiaoyuzhoufm.com/episode/demo",
            "transcript": "播客正文",
            "provider": "Groq Whisper",
            "provider_id": "groq",
            "chunks_count": 1,
            "result_state": "success_with_data",
            "truncated": False,
            "retrieved_at": "2026-09-03T00:00:00+00:00",
            "title": "示例单集",
        },
    )
    result = execute_vertical_adapter(
        "xiaoyuzhou.whisper_transcribe",
        {"url": "https://www.xiaoyuzhoufm.com/episode/demo", "provider": "groq"},
        settings=_settings(),
        platform_credentials={"groq": "groq-secret"},
    )

    assert result["transcript"] == "播客正文"
    assert result["title"] == "示例单集"
