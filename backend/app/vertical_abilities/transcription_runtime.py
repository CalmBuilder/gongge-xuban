"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : transcription_runtime.py
@CallChain  : media.whisper_transcribe → yt-dlp/ffmpeg → Groq/OpenAI Whisper API
@Description: 以有界响应流执行公开音频转写，保证读取或关闭失败时取消语义优先于 Provider 错误。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from app.cancellation import TurnCancellationRequested, raise_if_cancelled, run_cancellable
from app.config import Settings
from app.security.dns_resolver import AdaptiveDNSResolver
from app.security.outbound import allowed_hosts_from_settings, prepare_outbound_request


MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 96 * 1024 * 1024
MAX_AUDIO_SECONDS = 4 * 60 * 60
CHUNK_SECONDS = 10 * 60
MAX_CHUNKS = 24
MAX_TRANSCRIPT_CHARS = 120_000
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MEDIA_TIMEOUT_SECONDS = 30 * 60
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 120.0
_FFMPEG_TIMEOUT_SECONDS = 10 * 60
_PUBLIC_MEDIA_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
_AUDIO_EXTENSIONS = frozenset({"aac", "m4a", "mp3", "mp4", "ogg", "opus", "wav", "webm"})


@dataclass(frozen=True, slots=True)
class WhisperProvider:
    """描述一个 OpenAI-compatible Whisper 服务端点，不包含运行时密钥。"""

    provider_id: str
    display_name: str
    endpoint: str
    host: str
    model: str


WHISPER_PROVIDERS: dict[str, WhisperProvider] = {
    "groq": WhisperProvider(
        provider_id="groq",
        display_name="Groq Whisper",
        endpoint="https://api.groq.com/openai/v1/audio/transcriptions",
        host="api.groq.com",
        model="whisper-large-v3",
    ),
    "openai": WhisperProvider(
        provider_id="openai",
        display_name="OpenAI Whisper",
        endpoint="https://api.openai.com/v1/audio/transcriptions",
        host="api.openai.com",
        model="whisper-1",
    ),
}


class TranscriptionRuntimeError(RuntimeError):
    """表示媒体运行时、Whisper Provider 或输入边界失败。"""

    def __init__(self, code: str, message: str) -> None:
        """保存不包含密钥和外部正文的稳定运行时错误。"""

        super().__init__(message)
        self.code = code
        self.message = message


def transcribe_public_media(
    source_url: str,
    *,
    provider: str,
    provider_credentials: Mapping[str, str],
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None = None,
    allow_provider_fallback: bool = False,
) -> dict[str, object]:
    """下载公开 YouTube 音频并通过已配置的 Whisper Provider 返回结构化转写。"""

    raise_if_cancelled(is_cancelled)
    normalized_url = _validate_source_url(source_url, settings, resolver)
    provider_order = _provider_order(provider, allow_provider_fallback)
    configured = [
        provider_id
        for provider_id in provider_order
        if str(provider_credentials.get(provider_id) or "").strip()
    ]
    if not configured:
        labels = "、".join(WHISPER_PROVIDERS[item].display_name for item in provider_order)
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
            f"YouTube 视频转写需要先在能力包配置中配置 {labels} API Key。",
        )
    order = configured if allow_provider_fallback else configured[:1]

    with tempfile.TemporaryDirectory(prefix="gongge-transcription-") as temporary:
        work_dir = Path(temporary)
        audio, duration_seconds = _download_audio(
            normalized_url,
            work_dir,
            settings=settings,
            is_cancelled=is_cancelled,
        )
        if duration_seconds is not None and duration_seconds > MAX_AUDIO_SECONDS:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_MEDIA_TOO_LONG",
                "公开视频音频超过平台单次转写时长上限。",
            )
        chunks = _prepare_audio_chunks(
            audio,
            work_dir,
            duration_seconds=duration_seconds,
            is_cancelled=is_cancelled,
        )
        total_bytes = sum(_checked_size(chunk, MAX_UPLOAD_BYTES, "音频分片") for chunk in chunks)
        if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_MEDIA_TOO_LARGE",
                "公开视频音频分片总大小超过平台转写上限。",
            )

        transcript_parts: list[str] = []
        actual_provider: str | None = None
        for chunk in chunks:
            chunk_text, chunk_provider = _transcribe_chunk_with_fallback(
                chunk,
                order,
                provider_credentials,
                settings=settings,
                resolver=resolver,
                is_cancelled=is_cancelled,
            )
            transcript_parts.append(chunk_text.strip())
            actual_provider = actual_provider or chunk_provider
        transcript = "\n".join(part for part in transcript_parts if part).strip()
        if not transcript:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "Whisper Provider 返回了空转写结果。",
            )
        truncated = len(transcript) > MAX_TRANSCRIPT_CHARS
        return {
            "source_url": normalized_url,
            "transcript": transcript[:MAX_TRANSCRIPT_CHARS],
            "provider": WHISPER_PROVIDERS[actual_provider or order[0]].display_name,
            "provider_id": actual_provider or order[0],
            "chunks_count": len(chunks),
            "result_state": "success_with_data",
            "truncated": truncated,
            "retrieved_at": _retrieved_at(),
        }


def transcribe_downloaded_audio(
    audio_path: Path,
    source_url: str,
    *,
    provider: str,
    provider_credentials: Mapping[str, str],
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None = None,
    allow_provider_fallback: bool = False,
) -> dict[str, object]:
    """把已由项目适配器下载的音频交给同一套 Whisper 分片和结果契约。"""

    raise_if_cancelled(is_cancelled)
    provider_order = _provider_order(provider, allow_provider_fallback)
    configured = [
        provider_id
        for provider_id in provider_order
        if str(provider_credentials.get(provider_id) or "").strip()
    ]
    if not configured:
        labels = "、".join(WHISPER_PROVIDERS[item].display_name for item in provider_order)
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
            f"音频转写需要先在能力包配置中配置 {labels} API Key。",
        )
    order = configured if allow_provider_fallback else configured[:1]
    source = Path(audio_path)
    if not source.is_file():
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "音频文件不存在。",
        )
    with tempfile.TemporaryDirectory(prefix="gongge-transcription-") as temporary:
        work_dir = Path(temporary)
        copied = work_dir / f"source{source.suffix.casefold() or '.audio'}"
        try:
            shutil.copyfile(source, copied)
        except OSError as exc:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "音频文件无法读取。",
            ) from exc
        _checked_size(copied, MAX_SOURCE_BYTES, "下载音频")
        chunks = _prepare_audio_chunks(
            copied,
            work_dir,
            duration_seconds=None,
            is_cancelled=is_cancelled,
        )
        total_bytes = sum(_checked_size(chunk, MAX_UPLOAD_BYTES, "音频分片") for chunk in chunks)
        if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_MEDIA_TOO_LARGE",
                "音频分片总大小超过平台转写上限。",
            )
        transcript_parts: list[str] = []
        actual_provider: str | None = None
        for chunk in chunks:
            chunk_text, chunk_provider = _transcribe_chunk_with_fallback(
                chunk,
                order,
                provider_credentials,
                settings=settings,
                resolver=resolver,
                is_cancelled=is_cancelled,
            )
            transcript_parts.append(chunk_text.strip())
            actual_provider = actual_provider or chunk_provider
        transcript = "\n".join(part for part in transcript_parts if part).strip()
        if not transcript:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "Whisper Provider 返回了空转写结果。",
            )
        truncated = len(transcript) > MAX_TRANSCRIPT_CHARS
        return {
            "source_url": str(source_url).strip(),
            "transcript": transcript[:MAX_TRANSCRIPT_CHARS],
            "provider": WHISPER_PROVIDERS[actual_provider or order[0]].display_name,
            "provider_id": actual_provider or order[0],
            "chunks_count": len(chunks),
            "result_state": "success_with_data",
            "truncated": truncated,
            "retrieved_at": _retrieved_at(),
        }


def _provider_order(provider: str, allow_provider_fallback: bool) -> tuple[str, ...]:
    """校验转写 Provider 选择，并只在 auto 模式允许显式 fallback。"""

    normalized = str(provider or "auto").strip().casefold() or "auto"
    if normalized == "auto":
        return ("groq", "openai")
    if normalized not in WHISPER_PROVIDERS:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "provider 必须是 auto、groq 或 openai。",
        )
    if allow_provider_fallback:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "只有 provider=auto 时才能启用 Provider fallback。",
        )
    return (normalized,)


def _validate_source_url(
    source_url: str,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
) -> str:
    """只允许公开 YouTube HTTPS/HTTP 视频来源并执行一次出网解析校验。"""

    normalized = str(source_url or "").strip()
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_URL_INVALID",
            "转写来源 URL 格式无效。",
        ) from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host not in _PUBLIC_MEDIA_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port not in {80, 443})
    ):
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_URL_INVALID",
            "转写来源必须是公开 YouTube HTTP(S) 视频 URL。",
        )
    try:
        prepare_outbound_request(
            normalized,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | set(_PUBLIC_MEDIA_HOSTS),
            resolver=resolver,
        )
    except ValueError as exc:
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc
    return normalized


def _download_audio(
    source_url: str,
    work_dir: Path,
    *,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
) -> tuple[Path, float | None]:
    """使用项目已安装的 yt-dlp 下载单个 YouTube 公开视频音频，不执行上游 shell 命令。"""

    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except (ImportError, ModuleNotFoundError) as exc:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_RUNTIME_MISSING",
            "公开视频转写需要平台部署 yt-dlp Python Runtime。",
        ) from exc

    output_holder: dict[str, object] = {}
    options: dict[str, object] = {
        "format": "bestaudio[abr<=96]/bestaudio/best",
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietYtdlpLogger(),
        "socket_timeout": 30,
        "retries": 1,
        "fragment_retries": 1,
        "max_filesize": MAX_SOURCE_BYTES,
        "overwrites": False,
    }
    if (urlsplit(source_url).hostname or "").casefold().rstrip(".") in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }:
        try:
            from app.vertical_abilities.youtube_runtime import _js_runtime_options

            options.update(_js_runtime_options())
        except Exception as exc:
            raise TranscriptionRuntimeError(
                "YOUTUBE_JS_RUNTIME_MISSING",
                "YouTube 音频转写需要平台部署 JS Runtime。",
            ) from exc
        options["extractor_args"] = {"youtube": {"player_client": ["android"]}}

    def operation() -> tuple[Path, float | None]:
        """执行一次视频音频下载并返回下载文件和 yt-dlp 提供的时长。"""

        try:
            with YoutubeDL(options) as downloader:
                output_holder["close"] = downloader.close
                info = downloader.extract_info(source_url, download=True)
            if not isinstance(info, Mapping):
                raise TranscriptionRuntimeError(
                    "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                    "媒体 Provider 没有返回可识别的视频信息。",
                )
            files = sorted(
                path
                for path in work_dir.glob("source.*")
                if path.is_file() and path.suffix.casefold().lstrip(".") in _AUDIO_EXTENSIONS
            )
            if not files:
                raise TranscriptionRuntimeError(
                    "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                    "媒体 Provider 没有生成可转写的音频文件。",
                )
            audio = files[0]
            _checked_size(audio, MAX_SOURCE_BYTES, "下载音频")
            duration = _positive_duration(info.get("duration"))
            return audio, duration
        except TranscriptionRuntimeError:
            raise
        except DownloadError as exc:
            del exc
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
                "公开视频音频下载失败。",
            ) from None
        except (OSError, TypeError, ValueError) as exc:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "公开视频音频无法处理。",
            ) from exc

    timeout_seconds = float(
        getattr(settings, "vertical_ability_media_timeout_seconds", DEFAULT_MEDIA_TIMEOUT_SECONDS)
        or DEFAULT_MEDIA_TIMEOUT_SECONDS
    )
    return _run_controlled(
        operation,
        timeout_seconds=max(timeout_seconds, 1.0),
        is_cancelled=is_cancelled,
        close_holder=output_holder,
    )


def _prepare_audio_chunks(
    audio: Path,
    work_dir: Path,
    *,
    duration_seconds: float | None,
    is_cancelled: Callable[[], bool] | None,
) -> list[Path]:
    """优先直接使用小音频，超出单次上传大小时用平台 ffmpeg 压缩并分段。"""

    raise_if_cancelled(is_cancelled)
    if audio.stat().st_size <= MAX_UPLOAD_BYTES:
        return [audio]
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_RUNTIME_MISSING",
            "音频超过单次上传大小，需要平台部署 ffmpeg 进行压缩分段。",
        )
    compressed = work_dir / "compressed.m4a"
    duration_args = (
        ["-t", str(min(duration_seconds, MAX_AUDIO_SECONDS))]
        if duration_seconds is not None
        else []
    )
    command = [
        ffmpeg,
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(audio),
        *duration_args,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        str(compressed),
    ]
    _run_process(command, timeout_seconds=_FFMPEG_TIMEOUT_SECONDS, is_cancelled=is_cancelled)
    if not compressed.is_file():
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "ffmpeg 没有生成压缩音频。",
        )
    if compressed.stat().st_size <= MAX_UPLOAD_BYTES:
        return [compressed]

    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunks_dir / "chunk_%03d.m4a"
    _run_process(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(compressed),
            "-f",
            "segment",
            "-segment_time",
            str(CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(pattern),
        ],
        timeout_seconds=_FFMPEG_TIMEOUT_SECONDS,
        is_cancelled=is_cancelled,
    )
    chunks = sorted(path for path in chunks_dir.glob("chunk_*.m4a") if path.is_file())
    if not chunks or len(chunks) > MAX_CHUNKS:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_MEDIA_TOO_LARGE",
            "音频分段数量超过平台转写上限。",
        )
    return chunks


def _transcribe_chunk_with_fallback(
    chunk: Path,
    provider_order: tuple[str, ...],
    provider_credentials: Mapping[str, str],
    *,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None,
) -> tuple[str, str]:
    """按明确的 Provider 顺序处理一个音频分片并返回文本与实际 Provider。"""

    last_error: TranscriptionRuntimeError | None = None
    for provider_id in provider_order:
        key = str(provider_credentials.get(provider_id) or "").strip()
        if not key:
            continue
        try:
            return (
                _post_transcription(
                    chunk,
                    WHISPER_PROVIDERS[provider_id],
                    key,
                    settings=settings,
                    resolver=resolver,
                    is_cancelled=is_cancelled,
                ),
                provider_id,
            )
        except TranscriptionRuntimeError as exc:
            last_error = exc
    raise last_error or TranscriptionRuntimeError(
        "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
        "没有可用的 Whisper Provider API Key。",
    )


def _post_transcription(
    chunk: Path,
    provider: WhisperProvider,
    api_key: str,
    *,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None,
) -> str:
    """向固定 Whisper API 有界读取转写，读取或关闭异常不能覆盖已经发生的取消。"""

    raise_if_cancelled(is_cancelled)
    try:
        target = prepare_outbound_request(
            provider.endpoint,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | {provider.host},
            resolver=resolver,
        )
    except ValueError as exc:
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc
    content_type = _content_type_for_path(chunk)
    timeout_seconds = float(
        getattr(settings, "tool_timeout_seconds", DEFAULT_PROVIDER_TIMEOUT_SECONDS)
        or DEFAULT_PROVIDER_TIMEOUT_SECONDS
    )
    try:
        with chunk.open("rb") as handle:
            with httpx.Client(
                timeout=max(min(timeout_seconds, DEFAULT_PROVIDER_TIMEOUT_SECONDS), 1.0),
                follow_redirects=False,
            ) as client:
                def read_response() -> bytes:
                    """在可取消阶段内接收响应头和有界正文，所有退出路径均关闭响应。"""

                    with client.stream(
                        "POST",
                        target.request_url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Accept": "text/plain, application/json",
                            **target.headers,
                        },
                        files={"file": (chunk.name, handle, content_type)},
                        data={"model": provider.model, "response_format": "text"},
                        **({"extensions": target.extensions} if target.extensions else {}),
                    ) as response:
                        raise_if_cancelled(is_cancelled)
                        if response.status_code >= 400:
                            if response.status_code in {401, 403}:
                                raise TranscriptionRuntimeError(
                                    "VERTICAL_ABILITY_PROVIDER_AUTH_ERROR",
                                    f"{provider.display_name} API Key 无效或无权使用转写服务。",
                                )
                            raise TranscriptionRuntimeError(
                                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
                                f"{provider.display_name} 返回异常状态码：{response.status_code}。",
                            )
                        return _read_whisper_response(response, is_cancelled)

                body = run_cancellable(
                    read_response,
                    is_cancelled,
                    on_cancel=client.close,
                )
    except TurnCancellationRequested:
        raise
    except httpx.TimeoutException as exc:
        raise_if_cancelled(is_cancelled)
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "Whisper Provider 请求超时。") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise_if_cancelled(is_cancelled)
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", "Whisper Provider 连接失败。") from exc
    raise_if_cancelled(is_cancelled)
    text = _parse_transcription_response(body)
    if not text:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            f"{provider.display_name} 返回了空转写结果。",
        )
    raise_if_cancelled(is_cancelled)
    return text


def _read_whisper_response(
    response: httpx.Response,
    is_cancelled: Callable[[], bool] | None,
) -> bytes:
    """逐块累计解码后字节，声明或实际长度超限立即失败，不消费剩余正文。"""

    try:
        declared_length = int(response.headers.get("Content-Length", ""))
    except ValueError:
        declared_length = 0
    if declared_length > MAX_PROVIDER_RESPONSE_BYTES:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_RESPONSE_TOO_LARGE",
            "Whisper Provider 返回内容超过安全上限。",
        )
    body = bytearray()
    for part in response.iter_bytes():
        raise_if_cancelled(is_cancelled)
        if len(body) + len(part) > MAX_PROVIDER_RESPONSE_BYTES:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_RESPONSE_TOO_LARGE",
                "Whisper Provider 返回内容超过安全上限。",
            )
        body.extend(part)
    raise_if_cancelled(is_cancelled)
    return bytes(body)


def _parse_transcription_response(body: bytes) -> str:
    """兼容纯文本和 OpenAI-compatible JSON 响应，只取转写正文。"""

    decoded = body.decode("utf-8", errors="replace").strip()
    if not decoded:
        return ""
    if decoded.startswith("{"):
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "Whisper Provider JSON 响应无法解析。",
            ) from exc
        if not isinstance(payload, Mapping):
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "Whisper Provider 返回了无法识别的响应。",
            )
        return str(payload.get("text") or "").strip()
    return decoded


def _content_type_for_path(path: Path) -> str:
    """把音频扩展名映射为 Whisper 上传需要的媒体类型。"""

    return {
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _checked_size(path: Path, limit: int, label: str) -> int:
    """读取普通文件大小并在任何 Provider 调用前执行上限校验。"""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            f"{label}无法读取。",
        ) from exc
    if size > limit:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_MEDIA_TOO_LARGE",
            f"{label}超过平台大小上限。",
        )
    return size


def _positive_duration(value: object) -> float | None:
    """把 yt-dlp 时长归一化为正数，未知时长保留空值并由大小边界兜底。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def _find_ffmpeg() -> str | None:
    """优先发现随桌面包提供的 ffmpeg，再兼容服务器 PATH。"""

    configured = os.environ.get("GONGGE_XUBAN_FFMPEG_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.append(configured_path)
        if configured_path.is_dir():
            candidates.extend((configured_path / "ffmpeg", configured_path / "ffmpeg.exe"))
    executable = Path(sys.executable).resolve()
    roots = [executable.parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(str(meipass)))
    for root in roots:
        candidates.extend(
            (
                root / "runtime" / "media" / "ffmpeg",
                root / "runtime" / "media" / "ffmpeg.exe",
            )
        )
    system_path = shutil.which("ffmpeg")
    if system_path:
        candidates.append(Path(system_path))
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_process(
    command: list[str],
    *,
    timeout_seconds: float,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    """运行 ffmpeg，轮询取消和超时并在收敛后清理子进程。"""

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_RUNTIME_MISSING",
            "平台 ffmpeg 无法启动。",
        ) from exc
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    try:
        while process.poll() is None:
            raise_if_cancelled(is_cancelled)
            if time.monotonic() >= deadline:
                _terminate_process(process)
                raise TranscriptionRuntimeError(
                    "VERTICAL_ABILITY_PROVIDER_TIMEOUT",
                    "音频处理超过平台超时。",
                )
            time.sleep(0.05)
        stderr = process.stderr.read() if process.stderr is not None else ""
        if process.returncode != 0:
            del stderr
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "平台 ffmpeg 无法处理公开视频音频。",
            )
    except TurnCancellationRequested:
        _terminate_process(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_process(process)
        else:
            process.wait()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """跨平台终止 ffmpeg 子进程并回收管道。"""

    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


class _QuietYtdlpLogger:
    """阻止 yt-dlp 将外部媒体正文、命令细节和 URL 凭据写入普通日志。"""

    def debug(self, _message: str) -> None:
        """忽略 yt-dlp 调试输出。"""

    def warning(self, _message: str) -> None:
        """忽略 yt-dlp 警告输出。"""

    def error(self, _message: str) -> None:
        """忽略 yt-dlp 错误正文。"""


def _run_controlled(
    operation: Callable[[], tuple[Path, float | None]],
    *,
    timeout_seconds: float,
    is_cancelled: Callable[[], bool] | None,
    close_holder: Mapping[str, object],
) -> tuple[Path, float | None]:
    """在线程边界运行 yt-dlp，统一处理取消、总超时和资源关闭。"""

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        """执行同步 yt-dlp 调用并把结果或异常送回控制线程。"""

        try:
            result_queue.put_nowait((True, operation()))
        except BaseException as exc:
            try:
                result_queue.put_nowait((False, exc))
            except queue.Full:
                pass

    threading.Thread(target=worker, name="vertical-transcription-runtime", daemon=True).start()
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    while True:
        if is_cancelled is not None and is_cancelled():
            _close_ytdlp(close_holder)
            raise TurnCancellationRequested("当前 Turn 已取消。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _close_ytdlp(close_holder)
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "媒体下载超过平台超时。")
        try:
            succeeded, value = result_queue.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if succeeded:
            return value  # type: ignore[return-value]
        if isinstance(value, BaseException):
            raise value
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_EXECUTION_ERROR",
            "媒体 Runtime 返回了未知结果。",
        )


def _close_ytdlp(holder: Mapping[str, object]) -> None:
    """取消或超时时尝试关闭 yt-dlp 会话，不等待其后台线程。"""

    closer = holder.get("close")
    if callable(closer):
        try:
            closer()
        except BaseException:
            pass


def _retrieved_at() -> str:
    """生成带 UTC 时区的转写获取时间。"""

    return datetime.now(timezone.utc).isoformat()
