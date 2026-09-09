"""
@Time       : 2026/09/03 20:20
@Author     : zhanglp8181
@File       : test_vertical_ability_xiaoyuzhou.py
@CallChain  : pytest → XiaoyuzhouRuntime → 页面/CDN 安全边界 → Whisper 转写契约
@Description: 验证小宇宙页面媒体地址提取、固定主机校验和统一转写调用。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.vertical_abilities.xiaoyuzhou_runtime import transcribe_xiaoyuzhou_episode
from app.vertical_abilities.transcription_runtime import TranscriptionRuntimeError


def _settings() -> SimpleNamespace:
    """构造小宇宙运行时测试配置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        vertical_ability_media_timeout_seconds=30,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def test_xiaoyuzhou_runtime_extracts_escaped_cdn_and_reuses_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证页面 JSON 转义、音频下载和 Whisper 结果能够形成一个来源回执。"""

    page = (
        b'{"title":"\xe7\xa4\xba\xe4\xbe\x8b\xe5\x8d\x95\xe9\x9b\x86",'
        b'"audio":"https:\\/\\/media.xyzcdn.net\\/demo\\/episode.m4a?token=public"}'
    )
    monkeypatch.setattr(
        "app.vertical_abilities.xiaoyuzhou_runtime._get_bounded_bytes",
        lambda *_args, **_kwargs: page,
    )
    monkeypatch.setattr(
        "app.vertical_abilities.xiaoyuzhou_runtime._download_to_file",
        lambda _url, destination, **_kwargs: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        "app.vertical_abilities.xiaoyuzhou_runtime.transcribe_downloaded_audio",
        lambda audio_path, source_url, **_kwargs: {
            "source_url": source_url,
            "transcript": f"audio={audio_path.stat().st_size}",
            "provider": "Groq Whisper",
            "provider_id": "groq",
            "chunks_count": 1,
            "result_state": "success_with_data",
            "truncated": False,
            "retrieved_at": "2026-09-03T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "app.vertical_abilities.xiaoyuzhou_runtime.prepare_outbound_request",
        lambda *_args, **_kwargs: object(),
    )

    result = transcribe_xiaoyuzhou_episode(
        "https://www.xiaoyuzhoufm.com/episode/demo",
        provider="groq",
        provider_credentials={"groq": "secret"},
        settings=_settings(),
        resolver=object(),
    )

    assert result["title"] == "示例单集"
    assert result["media_url"].startswith("https://media.xyzcdn.net/demo/episode.m4a")
    assert result["transcript"] == "audio=5"


def test_xiaoyuzhou_runtime_rejects_non_platform_url() -> None:
    """验证播客转写不能被页面参数带到任意外部主机。"""

    with pytest.raises(TranscriptionRuntimeError) as error:
        transcribe_xiaoyuzhou_episode(
            "https://evil.example/episode/demo",
            provider="groq",
            provider_credentials={"groq": "secret"},
            settings=_settings(),
            resolver=object(),
        )

    assert error.value.code == "VERTICAL_ABILITY_URL_INVALID"
