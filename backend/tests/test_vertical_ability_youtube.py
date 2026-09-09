"""
@Time       : 2026/09/03 12:00
@Author     : zhanglp8181
@File       : test_vertical_ability_youtube.py
@CallChain  : pytest → YouTube 垂直能力适配器/Runtime → 结果契约和安全边界
@Description: 验证 YouTube 公开视频读取、字幕状态、搜索结果和 Runtime 错误收敛。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.vertical_abilities import adapters
from app.vertical_abilities.adapters import VerticalAbilityAdapterError, execute_vertical_adapter
from app.vertical_abilities import youtube_runtime
from app.vertical_abilities.youtube_runtime import YouTubeRuntimeError, _clean_vtt


def _settings() -> SimpleNamespace:
    """构造不触达真实网络的 YouTube 适配器设置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def test_youtube_video_normalizes_metadata_and_non_empty_subtitle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证公开视频回执保留来源 URL，并明确返回字幕语言与内容。"""

    monkeypatch.setattr(adapters, "_validate_youtube_url", lambda *_args: None)
    monkeypatch.setattr(
        adapters,
        "read_youtube_video",
        lambda *_args, **_kwargs: {
            "info": {
                "id": "video-1",
                "title": "公开教程",
                "webpage_url": "https://www.youtube.com/watch?v=video-1",
                "uploader": "公开频道",
                "duration": 120,
                "upload_date": "20260903",
            },
            "subtitle_content": "第一句\n第二句",
            "subtitle_language": "zh-Hans",
            "subtitle_result_state": "success_with_data",
        },
    )

    result = execute_vertical_adapter(
        "youtube.ytdlp_video",
        {"url": "https://www.youtube.com/watch?v=video-1"},
        settings=_settings(),
    )

    assert result["url"] == "https://www.youtube.com/watch?v=video-1"
    assert result["video"] == {
        "id": "video-1",
        "title": "公开教程",
        "url": "https://www.youtube.com/watch?v=video-1",
        "canonical_url": "https://www.youtube.com/watch?v=video-1",
        "channel": "公开频道",
        "description": "",
        "duration_seconds": 120,
        "published_at": "20260903",
        "view_count": None,
        "like_count": None,
    }
    assert result["subtitle_result_state"] == "success_with_data"
    assert result["subtitle"] == {"language": "zh-Hans", "content": "第一句\n第二句"}


def test_youtube_search_filters_non_youtube_urls_and_exposes_no_result_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证搜索只接受 YouTube 来源，并把空集合编码为合法无结果。"""

    monkeypatch.setattr(
        adapters,
        "search_youtube_videos",
        lambda *_args, **_kwargs: [
            {
                "title": "公开视频",
                "webpage_url": "https://youtu.be/video-1",
                "upload_date": "20260903",
                "description": "摘要",
            },
            {"title": "伪造来源", "webpage_url": "https://example.com/video"},
        ],
    )

    result = execute_vertical_adapter(
        "youtube.ytdlp_search",
        {"query": "公开教程", "num_results": 5},
        settings=_settings(),
    )

    assert result["count"] == 1
    assert result["result_state"] == "success_with_data"
    assert result["videos"] == [
        {
            "title": "公开视频",
            "url": "https://youtu.be/video-1",
            "published_at": "20260903",
            "summary": "摘要",
        }
    ]

    monkeypatch.setattr(adapters, "search_youtube_videos", lambda *_args, **_kwargs: [])
    empty = execute_vertical_adapter(
        "youtube.ytdlp_search",
        {"query": "不存在的主题", "num_results": 5},
        settings=_settings(),
    )
    assert empty["videos"] == []
    assert empty["count"] == 0
    assert empty["result_state"] == "no_results"


def test_youtube_rejects_non_youtube_url_before_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 YouTube 视频输入不会把任意 URL 交给媒体 Runtime。"""

    called = False

    def _unexpected_runtime(*_args: object, **_kwargs: object) -> dict[str, object]:
        """记录不应发生的 Runtime 调用。"""

        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(adapters, "read_youtube_video", _unexpected_runtime)
    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "youtube.ytdlp_video",
            {"url": "https://example.com/video"},
            settings=_settings(),
        )

    assert error.value.code == "VERTICAL_ABILITY_URL_INVALID"
    assert called is False


def test_youtube_runtime_failure_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 yt-dlp/JS Runtime 缺失只形成 Provider 失败，不影响适配器目录。"""

    monkeypatch.setattr(adapters, "_validate_youtube_url", lambda *_args: None)
    monkeypatch.setattr(
        adapters,
        "read_youtube_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            YouTubeRuntimeError("YOUTUBE_JS_RUNTIME_MISSING", "YouTube 需要 JS Runtime。")
        ),
    )

    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "youtube.ytdlp_video",
            {"url": "https://www.youtube.com/watch?v=video-1"},
            settings=_settings(),
        )

    assert error.value.code == "YOUTUBE_JS_RUNTIME_MISSING"
    assert error.value.message == "YouTube 需要 JS Runtime。"


def test_youtube_video_can_request_bounded_comments_and_preserves_comment_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证用户明确询问评论时，Runtime 开启 getcomments 且适配器不把评论丢掉。"""

    captured: dict[str, object] = {}

    def read(*_args: object, **kwargs: object) -> dict[str, object]:
        """记录评论参数并返回带评论的公开视频回执。"""

        captured.update(kwargs)
        return {
            "info": {
                "id": "video-1",
                "title": "公开教程",
                "webpage_url": "https://www.youtube.com/watch?v=video-1",
                "comment_count": 2,
                "comments": [
                    {"id": "c1", "author": "viewer", "text": "很有帮助", "timestamp": 10},
                    {"id": "c2", "author": "maintainer", "text": "谢谢反馈", "timestamp": 20},
                ],
            },
            "subtitle_content": "",
            "subtitle_language": "",
            "subtitle_result_state": "no_results",
        }

    monkeypatch.setattr(adapters, "_validate_youtube_url", lambda *_args: None)
    monkeypatch.setattr(adapters, "read_youtube_video", read)
    result = execute_vertical_adapter(
        "youtube.ytdlp_video",
        {
            "url": "https://www.youtube.com/watch?v=video-1",
            "include_comments": True,
            "comments_limit": 2,
        },
        settings=_settings(),
    )

    assert captured == {
        "subtitle_languages": ("zh-Hans", "zh", "en"),
        "include_comments": True,
        "comments_limit": 2,
        "timeout_seconds": 8,
        "is_cancelled": None,
    }
    assert result["comments_result_state"] == "success_with_data"
    assert result["comments_count"] == 2
    assert result["comments"][0]["text"] == "很有帮助"
    assert result["comments"][0]["source_url"] == "https://www.youtube.com/watch?v=video-1"
    assert result["comments_truncated"] is False


def test_youtube_runtime_uses_getcomments_only_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 yt-dlp 评论抓取沿用 Agent-Reach 的 getcomments 与有界 max_comments 参数。"""

    captured: dict[str, object] = {}

    class FakeDownloader:
        """提供最小 yt-dlp 上下文，验证选项而不访问 YouTube。"""

        def __init__(self, options: dict[str, object]) -> None:
            """保存 Runtime 实际传入的 yt-dlp 选项。"""

            captured.update(options)

        def __enter__(self) -> "FakeDownloader":
            """返回假的下载器上下文。"""

            return self

        def __exit__(self, *_args: object) -> None:
            """结束假的下载器上下文。"""

        def close(self) -> None:
            """提供取消路径所需的关闭方法。"""

        def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
            """返回没有字幕但带评论的公开视频信息。"""

            assert download is True
            return {"id": "video-1", "title": "教程", "comments": [{"id": "c1", "text": "好"}]}

    monkeypatch.setattr(youtube_runtime, "_load_ytdlp", lambda: (FakeDownloader, RuntimeError))
    monkeypatch.setattr(youtube_runtime, "_js_runtime_options", lambda: {})
    result = youtube_runtime.read_youtube_video(
        "https://www.youtube.com/watch?v=video-1",
        subtitle_languages=("zh",),
        include_comments=True,
        comments_limit=7,
        timeout_seconds=3,
    )

    assert captured["getcomments"] is True
    assert captured["extractor_args"] == {
        "youtube": {"player_client": ["android"], "max_comments": ["7"]}
    }
    assert result["info"]["comments"][0]["id"] == "c1"


def test_clean_vtt_removes_timing_tags_and_consecutive_duplicate_lines() -> None:
    """验证字幕清洗不会把 VTT 控制字段当作正文返回。"""

    text = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c>你好</c>

00:00:02.000 --> 00:00:04.000
你好

00:00:04.000 --> 00:00:06.000
世界
"""

    assert _clean_vtt(text) == "你好\n世界"


def test_youtube_runtime_prefers_bundled_node_and_passes_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """验证冻结包内置 Node.js 会被发现并显式传给 yt-dlp。"""

    runtime_root = tmp_path / "runtime" / "youtube-js"
    runtime_root.mkdir(parents=True)
    node = runtime_root / "node"
    node.write_bytes(b"node-fixture")
    node.chmod(0o755)
    monkeypatch.setattr(youtube_runtime.sys, "executable", str(tmp_path / "gongge-xuban"))
    monkeypatch.setattr(youtube_runtime.sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(youtube_runtime.shutil, "which", lambda _name: None)

    assert youtube_runtime._find_js_runtime_path() == ("Node.js", str(node))
    assert youtube_runtime._js_runtime_options() == {
        "js_runtimes": {"node": {"path": str(node)}}
    }
