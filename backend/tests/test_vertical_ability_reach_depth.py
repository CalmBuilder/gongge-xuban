"""
@Time       : 2026/09/08
@Author     : zhanglp8181
@File       : test_vertical_ability_reach_depth.py
@CallChain  : pytest → RSS/V2EX 垂直能力适配器 → 增量、分页、作者关联和结果状态
@Description: 验证 Agent-Reach 公共 RSS/V2EX 深度读取不退化为一次列表请求或空数组包装。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.vertical_abilities.adapters import execute_vertical_adapter


def _settings() -> SimpleNamespace:
    """构造不触达真实网络的 RSS/V2EX 适配器设置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def test_rss_reports_incremental_entries_without_changing_legacy_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 RSS 保持 entries 全量回执，同时按已见标识返回 new_entries 和游标。"""

    monkeypatch.setattr("app.vertical_abilities.adapters._validate_public_url", lambda *_args: None)
    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda *_args, **_kwargs: (
            b"<rss><channel>"
            b"<item><guid>new-1</guid><title>New</title>"
            b"<link>https://example.org/new</link></item>"
            b"<item><guid>old-1</guid><title>Old</title>"
            b"<link>https://example.org/old</link></item>"
            b"</channel></rss>"
        ),
    )

    result = execute_vertical_adapter(
        "rss.atom",
        {
            "url": "https://example.org/feed.xml",
            "limit": 10,
            "known_ids": ["old-1"],
        },
        settings=_settings(),
    )

    assert [item["id"] for item in result["entries"]] == ["new-1", "old-1"]
    assert [item["id"] for item in result["new_entries"]] == ["new-1"]
    assert result["new_count"] == 1
    assert result["deduped_count"] == 1
    assert result["next_cursor"] == "new-1"
    assert result["cursor_kind"] == "entry_key"


def test_v2ex_topic_reads_multiple_reply_pages_and_keeps_author_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 V2EX 主题在 page_count 下读取连续回复页，并保留页数与作者证据。"""

    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> bytes:
        """按主题详情和回复页返回可区分的公开回执。"""

        calls.append(url)
        if "/replies/" not in url:
            return json.dumps(
                [
                    {
                        "id": 7,
                        "title": "CLI discussion",
                        "url": "https://www.v2ex.com/t/7",
                        "content": "Topic body",
                        "replies": 2,
                        "member": {"id": 10, "username": "topic-author"},
                    }
                ]
            ).encode()
        page = "page=2" in url
        reply_id = 9 if page else 8
        return json.dumps(
            [{
                "id": reply_id,
                "content": f"Reply page {2 if page else 1}",
                "created": 123456 + reply_id,
                "member": {"id": 20 + reply_id, "username": f"reply-{reply_id}"},
            }]
        ).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", get)
    result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "topic", "topic_id": 7, "limit": 10, "page_count": 2},
        settings=_settings(),
    )

    item = result["items"][0]
    assert calls == [
        "https://www.v2ex.com/api/topics/show.json?id=7",
        "https://www.v2ex.com/api/replies/show.json?topic_id=7&page=1",
        "https://www.v2ex.com/api/replies/show.json?topic_id=7&page=2",
    ]
    assert result["pages_fetched"] == 1
    assert item["replies_pages_requested"] == 2
    assert item["replies_pages_fetched"] == 2
    assert [reply["id"] for reply in item["reply_items"]] == [8, 9]
    assert item["reply_items_truncated"] is False
    assert item["partial"] is False


def test_v2ex_user_is_an_independent_public_author_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证作者资料有独立能力和明确的 no_results/有数据状态。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda url, **_kwargs: json.dumps(
            {
                "id": 10,
                "username": "topic-author",
                "url": "https://www.v2ex.com/member/topic-author",
                "bio": "CLI builder",
                "created": 123,
            }
        ).encode(),
    )

    result = execute_vertical_adapter(
        "v2ex.public_user",
        {"username": "topic-author"},
        settings=_settings(),
    )

    assert result["result_state"] == "success_with_data"
    assert result["user"]["username"] == "topic-author"
    assert result["user"]["bio"] == "CLI builder"
