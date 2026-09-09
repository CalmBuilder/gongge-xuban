"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_transcription.py
@CallChain  : pytest → Whisper Provider/媒体 Runtime → 转写契约与错误边界
@Description: 验证转写兼容、流式上限与 HTTP 错误，以及响应关闭失败时取消语义不被覆盖。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from app.cancellation import TurnCancellationRequested
from app.vertical_abilities import adapters, transcription_runtime
from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
    execute_vertical_provider_probe,
)
from app.vertical_abilities.transcription_runtime import TranscriptionRuntimeError


def _settings() -> SimpleNamespace:
    """构造不读取环境文件的转写适配器测试设置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        vertical_ability_media_timeout_seconds=60,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def _patch_download_and_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """将媒体下载替换为有界临时音频，避免单元测试触达平台网络。"""

    audio = tmp_path / "fixture.m4a"
    audio.write_bytes(b"fixture-audio")
    monkeypatch.setattr(
        transcription_runtime,
        "_validate_source_url",
        lambda source_url, _settings, _resolver: source_url,
    )
    monkeypatch.setattr(
        transcription_runtime,
        "_download_audio",
        lambda _source_url, _work_dir, **_kwargs: (audio, 90.0),
    )
    return audio


def test_whisper_transcription_returns_source_provider_and_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证小音频直接上传并返回可引用来源、实际 Provider 和非空转写。"""

    _patch_download_and_validation(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_post(chunk, provider, api_key, **_kwargs: object) -> str:
        """捕获固定 Provider 参数并模拟纯文本 Whisper 回执。"""

        captured.update({"chunk": chunk, "provider": provider, "api_key": api_key})
        return "公开视频中的第一句。"

    monkeypatch.setattr(transcription_runtime, "_post_transcription", fake_post)
    result = execute_vertical_adapter(
        "media.whisper_transcribe",
        {
            "url": "https://www.youtube.com/watch?v=fixture-video",
            "provider": "auto",
        },
        settings=_settings(),
        platform_credentials={"groq": "fixture-provider-value"},
    )

    assert result["source_url"] == "https://www.youtube.com/watch?v=fixture-video"
    assert result["transcript"] == "公开视频中的第一句。"
    assert result["provider"] == "Groq Whisper"
    assert result["provider_id"] == "groq"
    assert result["chunks_count"] == 1
    assert result["result_state"] == "success_with_data"
    assert captured["api_key"] == "fixture-provider-value"
    assert captured["provider"].provider_id == "groq"


def test_whisper_transcription_explicit_fallback_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证只有 auto 明确允许 fallback 时才会把同一音频交给下一 Provider。"""

    _patch_download_and_validation(monkeypatch, tmp_path)
    calls: list[str] = []

    def fake_post(_chunk, provider, _api_key, **_kwargs: object) -> str:
        """模拟 Groq 失败后 OpenAI 成功的 Provider 顺序。"""

        calls.append(provider.provider_id)
        if provider.provider_id == "groq":
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_HTTP_ERROR", "Groq 暂不可用")
        return "OpenAI 转写结果"

    monkeypatch.setattr(transcription_runtime, "_post_transcription", fake_post)
    result = execute_vertical_adapter(
        "media.whisper_transcribe",
        {
            "url": "https://www.youtube.com/watch?v=fixture-video",
            "provider": "auto",
            "allow_provider_fallback": True,
        },
        settings=_settings(),
        platform_credentials={
            "groq": "fixture-provider-value",
            "openai": "fixture-fallback-value",
        },
    )

    assert calls == ["groq", "openai"]
    assert result["provider_id"] == "openai"
    assert result["transcript"] == "OpenAI 转写结果"


def test_whisper_transcription_requires_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证缺少平台级 API Key 时返回结构化配置提示，不开始下载媒体。"""

    _patch_download_and_validation(monkeypatch, tmp_path)
    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "media.whisper_transcribe",
            {"url": "https://www.youtube.com/watch?v=fixture-video"},
            settings=_settings(),
            platform_credentials={},
        )

    assert error.value.code == "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED"
    assert "Groq Whisper" in error.value.message
    assert "OpenAI Whisper" in error.value.message


def test_whisper_transcription_rejects_unknown_source_before_download() -> None:
    """验证转写 Runtime 不把任意外部地址交给 yt-dlp。"""

    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "media.whisper_transcribe",
            {"url": "https://evil.example/video.mp4"},
            settings=_settings(),
            platform_credentials={"groq": "fixture-provider-value"},
        )

    assert error.value.code == "VERTICAL_ABILITY_URL_INVALID"


def test_whisper_transcription_rejects_bilibili_until_dedicated_runtime() -> None:
    """验证 B站不误走 YouTube yt-dlp 转写路径，等待其专用字幕运行时接入。"""

    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "media.whisper_transcribe",
            {"url": "https://www.bilibili.com/video/BV1xx411c7mD"},
            settings=_settings(),
            platform_credentials={"groq": "fixture-provider-value"},
        )

    assert error.value.code == "VERTICAL_ABILITY_URL_INVALID"


def test_whisper_transcription_rejects_oversized_audio_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证超过单次上传大小时必须进入平台压缩边界，不静默上传超大文件。"""

    oversized = tmp_path / "oversized.m4a"
    with oversized.open("wb") as handle:
        handle.seek(transcription_runtime.MAX_UPLOAD_BYTES)
        handle.write(b"x")
    monkeypatch.setattr(
        transcription_runtime,
        "_validate_source_url",
        lambda source_url, _settings, _resolver: source_url,
    )
    monkeypatch.setattr(
        transcription_runtime,
        "_download_audio",
        lambda _source_url, _work_dir, **_kwargs: (oversized, 120.0),
    )
    monkeypatch.setattr(transcription_runtime, "_find_ffmpeg", lambda: None)

    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "media.whisper_transcribe",
            {"url": "https://www.youtube.com/watch?v=fixture-video"},
            settings=_settings(),
            platform_credentials={"groq": "fixture-provider-value"},
        )

    assert error.value.code == "VERTICAL_ABILITY_RUNTIME_MISSING"


def test_whisper_provider_probe_uses_fixed_models_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Groq 探活只访问固定 models 端点并校验结构化目录。"""

    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> bytes:
        """记录探活请求并返回合法 OpenAI-compatible 模型目录。"""

        captured.update({"url": url, **kwargs})
        return json.dumps({"data": [{"id": "whisper-large-v3"}]}).encode("utf-8")

    monkeypatch.setattr(adapters, "_http_get_bytes", fake_get)
    result = execute_vertical_provider_probe(
        "groq",
        "fixture-provider-value",
        settings=_settings(),
    )

    assert captured["url"] == "https://api.groq.com/openai/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer fixture-provider-value"
    assert captured["allowed_hosts"] == {"api.groq.com"}
    assert result == {
        "provider": "Groq Whisper",
        "provider_id": "groq",
        "models_count": 1,
    }


def test_whisper_provider_probe_rejects_malformed_models_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Provider 探活不会把缺少 data 数组的响应当成健康。"""

    monkeypatch.setattr(adapters, "_http_get_bytes", lambda *_args, **_kwargs: b"{}")
    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_provider_probe(
            "openai",
            "fixture-provider-value",
            settings=_settings(),
        )

    assert error.value.code == "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"


class _WhisperStream(httpx.SyncByteStream):
    """记录消费块数和关闭状态的纯内存响应，允许注入读取取消及关闭异常。"""

    def __init__(
        self, chunks: tuple[bytes, ...], before_chunk: Callable[[int], None] | None = None,
        *, close_error: Exception | None = None,
    ) -> None:
        """保存受控响应块、读取回调及可选关闭异常，不建立任何外部连接。"""

        self.chunks = chunks
        self.before_chunk = before_chunk
        self.read_chunks = 0
        self.closed = False
        self.close_error = close_error

    def __iter__(self) -> Iterator[bytes]:
        """逐块计数，测试可证明实现没有偷偷完整读取超限响应。"""

        for index, chunk in enumerate(self.chunks):
            if self.before_chunk is not None:
                self.before_chunk(index)
            self.read_chunks += 1
            yield chunk

    def close(self) -> None:
        """记录 HTTPX 已尝试释放响应，并按测试输入模拟关闭阶段故障。"""

        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _mock_whisper_stream(
    monkeypatch: pytest.MonkeyPatch,
    stream: _WhisperStream,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> tuple[list[httpx.Client], list[httpx.Request]]:
    """保留真实 HTTPX 流读取行为，仅替换传输和出网目标，不使用真实 DNS、代理或凭据。"""

    clients: list[httpx.Client] = []
    requests: list[httpx.Request] = []
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """返回懒读取响应并记录请求，不提前把响应缓存成完整正文。"""

        requests.append(request)
        return httpx.Response(status, headers=headers, stream=stream, request=request)

    def client_factory(**kwargs: object) -> httpx.Client:
        """创建完全离线的真实 Client，并保留其关闭状态供断言。"""

        client = original_client(
            transport=httpx.MockTransport(handle), trust_env=False, **kwargs,
        )
        clients.append(client)
        return client

    target = SimpleNamespace(
        request_url="https://example.invalid/transcribe", headers={}, extensions={},
    )
    monkeypatch.setattr(transcription_runtime, "prepare_outbound_request", lambda *_a, **_k: target)
    monkeypatch.setattr(transcription_runtime.httpx, "Client", client_factory)
    return clients, requests


def _fixture_whisper_call(
    tmp_path: Path, *, is_cancelled: Callable[[], bool] | None = None,
) -> str:
    """仅上传临时测试音频，调用固定 Provider 的真实流读取方法。"""

    audio = tmp_path / "fixture.m4a"
    audio.write_bytes(b"fixture-audio")
    return transcription_runtime._post_transcription(
        audio, transcription_runtime.WHISPER_PROVIDERS["groq"], "fixture-provider-value",
        settings=_settings(), resolver=None, is_cancelled=is_cancelled,
    )


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_whisper_stops_stream_as_soon_as_actual_response_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, headers: dict[str, str],
) -> None:
    """无长度或伪报小长度的响应一旦越界即停止，不能先读完整正文再检查。"""

    limit = transcription_runtime.MAX_PROVIDER_RESPONSE_BYTES
    stream = _WhisperStream((b"x" * limit, b"!", b"must-not-read"))
    clients, _requests = _mock_whisper_stream(monkeypatch, stream, headers=headers)

    with pytest.raises(TranscriptionRuntimeError) as error:
        _fixture_whisper_call(tmp_path)

    assert error.value.code == "VERTICAL_ABILITY_RESPONSE_TOO_LARGE"
    assert stream.read_chunks == 2
    assert stream.closed
    assert clients[0].is_closed


def test_whisper_rejects_oversized_content_length_before_reading_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """响应已声明超过上限时无需消费正文，仍应关闭响应和客户端。"""

    stream = _WhisperStream((b"must-not-read",))
    clients, _requests = _mock_whisper_stream(
        monkeypatch, stream,
        headers={"Content-Length": str(transcription_runtime.MAX_PROVIDER_RESPONSE_BYTES + 1)},
    )

    with pytest.raises(TranscriptionRuntimeError) as error:
        _fixture_whisper_call(tmp_path)

    assert error.value.code == "VERTICAL_ABILITY_RESPONSE_TOO_LARGE"
    assert stream.read_chunks == 0
    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.parametrize("body,expected", [
    ("2026 中文正文".encode(), "2026 中文正文"),
    (b'{"text":"2026"}', "2026"),
])
def test_whisper_stream_keeps_text_json_and_multipart_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: bytes, expected: str,
) -> None:
    """跨块 UTF-8 和 JSON 均完整解析，Provider 上传模型与纯文本请求合同不变。"""

    stream = _WhisperStream((body[:6], body[6:]))
    clients, requests = _mock_whisper_stream(monkeypatch, stream)

    assert _fixture_whisper_call(tmp_path) == expected
    assert requests[0].method == "POST"
    assert requests[0].headers["Authorization"] == "Bearer fixture-provider-value"
    assert b'name="response_format"\r\n\r\ntext' in requests[0].content
    assert stream.closed
    assert clients[0].is_closed


def test_whisper_accepts_body_exactly_at_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """恰好达到上限属于合法响应，不以大于等于判断误伤边界正文。"""

    monkeypatch.setattr(transcription_runtime, "MAX_PROVIDER_RESPONSE_BYTES", 4)
    stream = _WhisperStream((b"20", b"26"))
    _mock_whisper_stream(monkeypatch, stream)

    assert _fixture_whisper_call(tmp_path) == "2026"
    assert stream.closed


@pytest.mark.parametrize("status,code", [
    (401, "VERTICAL_ABILITY_PROVIDER_AUTH_ERROR"),
    (403, "VERTICAL_ABILITY_PROVIDER_AUTH_ERROR"),
    (400, "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"),
    (429, "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"),
    (500, "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"),
    (503, "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR"),
])
def test_whisper_http_error_does_not_read_or_echo_provider_error_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, code: str,
) -> None:
    """HTTP 错误按既有分类返回，不下载巨大错误页且不回显外部错误正文。"""

    stream = _WhisperStream((b"fixture-private-error",))
    clients, _requests = _mock_whisper_stream(
        monkeypatch, stream, status=status,
        headers={"Content-Length": str(transcription_runtime.MAX_PROVIDER_RESPONSE_BYTES + 1)},
    )

    with pytest.raises(TranscriptionRuntimeError) as error:
        _fixture_whisper_call(tmp_path)

    assert error.value.code == code
    assert "fixture-private-error" not in error.value.message
    assert stream.read_chunks == 0
    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.parametrize("body", [b"", b" \n", b'{"no_text":1}', b'{"text":'])
def test_whisper_empty_and_malformed_stream_still_return_structured_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: bytes,
) -> None:
    """空白与畸形 JSON 不因改为流式读取而变成成功转写。"""

    stream = _WhisperStream((body,))
    clients, _requests = _mock_whisper_stream(monkeypatch, stream)

    with pytest.raises(TranscriptionRuntimeError) as error:
        _fixture_whisper_call(tmp_path)

    assert error.value.code == "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"
    assert stream.closed
    assert clients[0].is_closed


def test_whisper_cancel_during_stream_closes_resources_without_consuming_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """流读取中取消要保留 Turn 异常，关闭响应并拒绝继续消费尾部或发布转写。"""

    cancelled = Event()

    def cancel_on_second_chunk(index: int) -> None:
        """在第二个实际响应块到达时触发取消，避免依赖墙钟等待。"""

        if index == 1:
            cancelled.set()

    stream = _WhisperStream((b"first", b"cancel", b"must-not-read"), cancel_on_second_chunk)
    clients, _requests = _mock_whisper_stream(monkeypatch, stream)

    with pytest.raises(TurnCancellationRequested):
        _fixture_whisper_call(tmp_path, is_cancelled=cancelled.is_set)

    assert stream.read_chunks <= 2
    assert stream.closed
    assert clients[0].is_closed


def test_whisper_cancel_before_call_never_dispatches_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """已取消调用不得提交 HTTP 请求，也不能被转换为普通 Provider 错误。"""

    stream = _WhisperStream((b"must-not-read",))
    clients, requests = _mock_whisper_stream(monkeypatch, stream)

    with pytest.raises(TurnCancellationRequested):
        _fixture_whisper_call(tmp_path, is_cancelled=lambda: True)

    assert requests == []
    assert stream.read_chunks == 0
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("error_type,code", [
    (httpx.ReadTimeout, "VERTICAL_ABILITY_PROVIDER_TIMEOUT"),
    (httpx.ReadError, "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"),
])
def test_whisper_read_failure_closes_stream_and_preserves_error_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: type[httpx.HTTPError], code: str,
) -> None:
    """读取阶段的超时与连接失败仍使用原错误分类，并释放部分读取的响应。"""

    def fail_on_second_chunk(index: int) -> None:
        """在部分正文已收到后模拟传输异常，不发起网络请求。"""

        if index == 1:
            raise error_type("fixture-read-failure")

    stream = _WhisperStream((b"first", b"must-not-read"), fail_on_second_chunk)
    clients, _requests = _mock_whisper_stream(monkeypatch, stream)

    with pytest.raises(TranscriptionRuntimeError) as error:
        _fixture_whisper_call(tmp_path)

    assert error.value.code == code
    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.parametrize("error_type", [OSError, httpx.ReadError, httpx.ReadTimeout])
def test_whisper_cancelled_stream_close_error_keeps_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: type[Exception],
) -> None:
    """精确复现取消已抛出后响应关闭又失败，关闭异常不得把 Turn 取消改成 Provider 故障。"""

    cancelled = Event()

    def cancel_on_second_chunk(index: int) -> None:
        """在第二个响应块到达时标记取消，使真实有界读取抛出 Turn 取消异常。"""

        if index == 1:
            cancelled.set()

    def run_inline(
        operation: Callable[[], bytes], _is_cancelled: Callable[[], bool] | None,
        *, on_cancel: Callable[[], None] | None = None,
    ) -> bytes:
        """仅固定调度为同步执行，避免轮询提前发现取消而掩盖真实流 finally 的异常覆盖。"""

        return operation()

    close_error = error_type("fixture-close-failure")
    stream = _WhisperStream(
        (b"first", b"cancel", b"must-not-read"), cancel_on_second_chunk,
        close_error=close_error,
    )
    clients, _requests = _mock_whisper_stream(monkeypatch, stream)
    monkeypatch.setattr(transcription_runtime, "run_cancellable", run_inline)

    with pytest.raises(TurnCancellationRequested):
        _fixture_whisper_call(tmp_path, is_cancelled=cancelled.is_set)

    assert isinstance(close_error.__context__, TurnCancellationRequested)
    assert stream.read_chunks == 2
    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.parametrize("error_type,code", [
    (OSError, "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"),
    (httpx.ReadError, "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"),
    (httpx.ReadTimeout, "VERTICAL_ABILITY_PROVIDER_TIMEOUT"),
])
def test_whisper_stream_close_error_without_cancellation_remains_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: type[Exception], code: str,
) -> None:
    """未取消时相同关闭异常仍属于真实故障，不能被吞掉或把已读正文发布为成功。"""

    close_error = error_type("fixture-close-failure")
    stream = _WhisperStream((b"fixture transcript",), close_error=close_error)
    clients, _requests = _mock_whisper_stream(monkeypatch, stream)

    with pytest.raises(TranscriptionRuntimeError) as error:
        _fixture_whisper_call(tmp_path, is_cancelled=lambda: False)

    assert error.value.code == code
    assert error.value.__cause__ is close_error
    assert "fixture-close-failure" not in error.value.message
    assert stream.closed
    assert clients[0].is_closed
