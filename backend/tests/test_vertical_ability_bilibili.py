"""
@Time       : 2026/09/03 15:00
@Author     : zhanglp8181
@File       : test_vertical_ability_bilibili.py
@CallChain  : pytest → B站公共适配器 → API 回执归一化与 DynamicTask 输出契约
@Description: 验证 B站公开搜索、视频详情、来源校验和结构化 Provider 失败语义。
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from app.vertical_abilities import adapters
from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
)


def _settings() -> SimpleNamespace:
    """构造不读取环境文件的 B站适配器测试设置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def _search_payload() -> dict[str, object]:
    """构造包含视频组和无关结果组的 B站搜索回执。"""

    return {
        "code": 0,
        "message": "0",
        "data": {
            "result": [
                {
                    "result_type": "activity",
                    "data": [{"title": "活动，不应进入视频结果"}],
                },
                {
                    "result_type": "video",
                    "data": [
                        {
                            "bvid": "BV1xx411c7mD",
                            "aid": 2,
                            "title": "<em class=\"keyword\">Python</em> 教程",
                            "author": "公开 UP 主",
                            "description": "公开视频摘要",
                            "duration": "10:20",
                            "pubdate": 1_704_067_200,
                            "play": 120,
                            "like": 12,
                            "review": 3,
                            "pic": "//i0.hdslb.com/bfs/archive/example.jpg",
                        },
                        {
                            "bvid": "BV1ab2345678",
                            "aid": 3,
                            "title": "第二个视频",
                            "author": "另一个 UP 主",
                        },
                    ],
                },
            ]
        },
    }


def test_bilibili_search_normalizes_video_results_and_empty_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证搜索只提取视频组、清理标题并生成数量/状态字段。"""

    captured: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> bytes:
        """记录固定公共 API URL 并返回搜索回执。"""

        captured.append(url)
        return json.dumps(_search_payload()).encode("utf-8")

    monkeypatch.setattr(adapters, "_http_get_bytes", fake_get)
    result = execute_vertical_adapter(
        "bilibili.public_search",
        {"query": "Python", "num_results": 2},
        settings=_settings(),
    )

    assert "/x/web-interface/search/all/v2?" in captured[0]
    assert "keyword=Python" in captured[0]
    assert result["count"] == 2
    assert result["result_state"] == "success_with_data"
    assert result["videos"][0] == {
        "id": "BV1xx411c7mD",
        "bvid": "BV1xx411c7mD",
        "aid": 2,
        "title": "Python 教程",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "author": "公开 UP 主",
        "description": "公开视频摘要",
        "duration": "10:20",
        "published_at": "2024-01-01T00:00:00+00:00",
        "view_count": 120,
        "like_count": 12,
        "comment_count": 3,
        "cover": "https://i0.hdslb.com/bfs/archive/example.jpg",
    }

    monkeypatch.setattr(
        adapters,
        "_http_get_bytes",
        lambda *_args, **_kwargs: json.dumps(
            {"code": 0, "message": "0", "data": {"result": []}}
        ).encode("utf-8"),
    )
    empty = execute_vertical_adapter(
        "bilibili.public_search",
        {"query": "不会有这个结果", "num_results": 5},
        settings=_settings(),
    )
    assert empty["videos"] == []
    assert empty["count"] == 0
    assert empty["result_state"] == "no_results"


def test_bilibili_video_normalizes_public_detail_and_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 BV 号输入只访问固定 B站详情 API，并保留可引用的公开字段。"""

    captured: list[str] = []
    payload = {
        "code": 0,
        "message": "0",
        "data": {
            "bvid": "BV1xx411c7mD",
            "aid": 2,
            "title": "字幕君交流场所",
            "desc": "公开视频描述",
            "duration": 2055,
            "pubdate": 1_252_458_549,
            "pic": "//i0.hdslb.com/bfs/archive/example.png",
            "owner": {"mid": 2, "name": "公开 UP 主"},
            "stat": {"view": 548, "like": 27, "reply": 8, "danmaku": 19, "favorite": 31},
            "pages": [
                {"page": 1, "part": "第一 P", "duration": 2055, "cid": 62131},
            ],
            "subtitle": {"allow_submit": False},
        },
    }

    def fake_get(url: str, **_kwargs: object) -> bytes:
        """记录固定详情 API URL 并返回详情回执。"""

        captured.append(url)
        return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(adapters, "_http_get_bytes", fake_get)
    result = execute_vertical_adapter(
        "bilibili.public_video",
        {"video": "BV1xx411c7mD"},
        settings=_settings(),
    )

    assert captured == ["https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD"]
    assert result["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert result["provider"] == "Bilibili public API"
    assert result["video"]["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert result["video"]["author"] == "公开 UP 主"
    assert result["video"]["pages_count"] == 1
    assert result["video"]["subtitle_available"] is True


def test_bilibili_video_accepts_public_url_and_old_aid(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证完整 B站 HTTPS URL 和 av 号都能规范为公共详情查询。"""

    urls: list[str] = []
    payload = {"code": 0, "message": "0", "data": {"aid": 2, "title": "公开视频"}}
    monkeypatch.setattr(
        adapters,
        "_http_get_bytes",
        lambda url, **_kwargs: urls.append(url) or json.dumps(payload).encode("utf-8"),
    )
    monkeypatch.setattr(
        adapters,
        "prepare_outbound_request",
        lambda url, **_kwargs: url,
    )

    result = execute_vertical_adapter(
        "bilibili.public_video",
        {"video": "https://www.bilibili.com/video/av2?p=1"},
        settings=_settings(),
    )

    assert urls == ["https://api.bilibili.com/x/web-interface/view?aid=2"]
    assert result["url"] == "https://www.bilibili.com/video/av2"
    assert result["video"]["url"] == "https://www.bilibili.com/video/av2"


def test_bilibili_412_retries_only_fixed_api_with_system_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 B站 412 只触发固定 API 的系统路由 fallback，并保留正常结果。"""

    calls: list[object] = []
    payload = {"code": 0, "message": "0", "data": {"result": []}}

    def fake_get(_url: str, **kwargs: object) -> bytes:
        """第一次模拟 DoH 直连 412，第二次返回合法无结果回执。"""

        calls.append(kwargs.get("resolver"))
        if len(calls) == 1:
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
                "互联网数据源返回异常状态码：412。",
            )
        return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(adapters, "_http_get_bytes", fake_get)
    result = execute_vertical_adapter(
        "bilibili.public_search",
        {"query": "反风控测试"},
        settings=_settings(),
    )

    assert result["result_state"] == "no_results"
    assert len(calls) == 2
    assert calls[0] is not socket.getaddrinfo
    assert calls[1] is socket.getaddrinfo


def test_bilibili_api_error_is_structured_and_invalid_reference_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 B站业务错误不会降级为空成功，外部 URL 也不能旁路固定 API。"""

    monkeypatch.setattr(
        adapters,
        "_http_get_bytes",
        lambda *_args, **_kwargs: json.dumps(
            {"code": -404, "message": "视频不存在", "data": None}
        ).encode("utf-8"),
    )
    with pytest.raises(VerticalAbilityAdapterError) as api_error:
        execute_vertical_adapter(
            "bilibili.public_video",
            {"video": "BV1xx411c7mD"},
            settings=_settings(),
        )
    assert api_error.value.code == "VERTICAL_ABILITY_PROVIDER_ERROR"
    assert "视频不存在" in api_error.value.message

    with pytest.raises(VerticalAbilityAdapterError) as invalid:
        execute_vertical_adapter(
            "bilibili.public_video",
            {"video": "https://evil.example/video/BV1xx411c7mD"},
            settings=_settings(),
        )
    assert invalid.value.code == "VERTICAL_ABILITY_URL_INVALID"
