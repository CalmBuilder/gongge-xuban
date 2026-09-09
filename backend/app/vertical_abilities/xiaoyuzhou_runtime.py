"""
@Time       : 2026/09/03 19:25
@Author     : zhanglp8181
@File       : xiaoyuzhou_runtime.py
@CallChain  : 全网情报员 → 小宇宙页面/媒体地址 → ffmpeg/Whisper Provider
@Description: 在项目出网和媒体大小边界内读取小宇宙公开播客并复用统一音频转写运行时。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import html
import json
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import httpx

from app.cancellation import run_cancellable
from app.config import Settings
from app.security.dns_resolver import AdaptiveDNSResolver
from app.security.outbound import allowed_hosts_from_settings, prepare_outbound_request
from app.vertical_abilities.transcription_runtime import (
    MAX_SOURCE_BYTES,
    TranscriptionRuntimeError,
    transcribe_downloaded_audio,
)


_PODCAST_HOSTS = frozenset({"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"})
_MEDIA_HOST = "media.xyzcdn.net"
_MAX_PAGE_BYTES = 5 * 1024 * 1024
_MAX_TRANSCRIPTION_TIMEOUT_SECONDS = 30 * 60
_MEDIA_PATTERN = re.compile(
    r"https://media\.xyzcdn\.net/[^\s\"'<>]+?\.(?:m4a|mp3)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_TITLE_PATTERN = re.compile(r'"title"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.IGNORECASE)


def transcribe_xiaoyuzhou_episode(
    source_url: str,
    *,
    provider: str,
    provider_credentials: Mapping[str, str],
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None = None,
    allow_provider_fallback: bool = False,
) -> dict[str, object]:
    """读取小宇宙公开单集页面并返回带标题和来源的统一转写回执。"""

    normalized_url = _validate_episode_url(source_url, settings, resolver)
    page = _get_bounded_bytes(
        normalized_url,
        max_bytes=_MAX_PAGE_BYTES,
        settings=settings,
        resolver=resolver,
        is_cancelled=is_cancelled,
        allowed_hosts=_PODCAST_HOSTS,
    )
    audio_url = _extract_audio_url(page, settings, resolver)
    title = _extract_title(page)
    with tempfile.TemporaryDirectory(prefix="gongge-xiaoyuzhou-") as temporary:
        audio_path = Path(temporary) / "episode.m4a"
        _download_to_file(
            audio_url,
            audio_path,
            settings=settings,
            resolver=resolver,
            is_cancelled=is_cancelled,
        )
        result = transcribe_downloaded_audio(
            audio_path,
            normalized_url,
            provider=provider,
            provider_credentials=provider_credentials,
            settings=settings,
            resolver=resolver,
            is_cancelled=is_cancelled,
            allow_provider_fallback=allow_provider_fallback,
        )
    result["title"] = title
    result["media_url"] = audio_url
    result["provider_scope"] = "小宇宙公开单集"
    return result


def _validate_episode_url(
    source_url: str,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
) -> str:
    """只接受小宇宙公开页面 URL，并在请求前完成固定 DNS 出网校验。"""

    normalized = str(source_url or "").strip()
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_URL_INVALID", "小宇宙来源 URL 格式无效。") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host not in _PODCAST_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port not in {80, 443})
    ):
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_URL_INVALID",
            "小宇宙转写来源必须是 xiaoyuzhoufm.com 的 HTTP(S) 页面。",
        )
    try:
        prepare_outbound_request(
            normalized,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | set(_PODCAST_HOSTS),
            resolver=resolver,
        )
    except ValueError as exc:
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc
    return normalized


def _extract_audio_url(page: bytes, settings: Settings, resolver: AdaptiveDNSResolver) -> str:
    """从公开页面中提取固定媒体 CDN 的 m4a/mp3 地址，并再次校验目标主机。"""

    text = html.unescape(page.decode("utf-8", errors="replace"))
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    match = _MEDIA_PATTERN.search(text)
    if match is None:
        raise TranscriptionRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "小宇宙页面没有找到可转写的公开音频地址。",
        )
    audio_url = match.group(0).rstrip("\\")
    try:
        parsed = urlsplit(audio_url)
        if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold().rstrip(".") != _MEDIA_HOST:
            raise ValueError("媒体地址不是受信任的 CDN 主机。")
        prepare_outbound_request(
            audio_url,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | {_MEDIA_HOST},
            resolver=resolver,
        )
    except ValueError as exc:
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_URL_BLOCKED", "小宇宙媒体地址未通过出网校验。") from exc
    return audio_url


def _extract_title(page: bytes) -> str:
    """从页面 JSON-LD/状态数据提取有限标题，提取失败不影响音频转写。"""

    text = page.decode("utf-8", errors="replace")
    match = _TITLE_PATTERN.search(text)
    if match is None:
        return ""
    try:
        title = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        title = match.group(1)
    return html.unescape(str(title))[:400]


def _get_bounded_bytes(
    url: str,
    *,
    max_bytes: int,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None,
    allowed_hosts: set[str],
) -> bytes:
    """通过固定目标读取有限页面响应，不跟随重定向。"""

    target = _target(url, settings, resolver, allowed_hosts)
    timeout = _http_timeout(settings)
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        def operation() -> bytes:
            """读取公开页面并在流式过程中执行字节上限。"""

            with client.stream(
                "GET",
                target.request_url,
                headers={"Accept": "text/html,application/xhtml+xml", **target.headers},
                **({"extensions": target.extensions} if target.extensions else {}),
            ) as response:
                response.raise_for_status()
                return _read_response_stream(response, max_bytes)

        try:
            return run_cancellable(operation, is_cancelled, on_cancel=client.close)
        except httpx.TimeoutException as exc:
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "小宇宙页面请求超时。") from exc
        except httpx.HTTPStatusError as exc:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
                f"小宇宙页面返回异常状态码：{exc.response.status_code}。",
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", "小宇宙页面连接失败。") from exc
        except ValueError as exc:
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_RESPONSE_TOO_LARGE", "小宇宙页面超过大小上限。") from exc
    finally:
        client.close()


def _download_to_file(
    url: str,
    destination: Path,
    *,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    """流式下载固定媒体 CDN 音频并在写盘过程中执行 512 MiB 上限。"""

    target = _target(url, settings, resolver, {_MEDIA_HOST})
    timeout = _http_timeout(settings, media=True)
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        def operation() -> None:
            """执行一次带 Content-Length 和流式大小检查的媒体下载。"""

            with client.stream(
                "GET",
                target.request_url,
                headers={"Accept": "audio/mpeg, audio/mp4", **target.headers},
                **({"extensions": target.extensions} if target.extensions else {}),
            ) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > MAX_SOURCE_BYTES:
                    raise ValueError("媒体文件超过大小上限。")
                total = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise ValueError("媒体文件超过大小上限。")
                        handle.write(chunk)
                if total == 0:
                    raise ValueError("媒体文件为空。")

        try:
            run_cancellable(operation, is_cancelled, on_cancel=client.close)
        except httpx.TimeoutException as exc:
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "小宇宙音频下载超时。") from exc
        except httpx.HTTPStatusError as exc:
            raise TranscriptionRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
                f"小宇宙音频返回异常状态码：{exc.response.status_code}。",
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", "小宇宙音频下载失败。") from exc
        except (ValueError, OSError) as exc:
            raise TranscriptionRuntimeError("VERTICAL_ABILITY_RESPONSE_INVALID", "小宇宙音频无法安全处理。") from exc
    finally:
        client.close()


def _read_response_stream(response: httpx.Response, max_bytes: int) -> bytes:
    """读取 HTTP 流并在返回给页面解析前校验总字节数。"""

    content_length = response.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise ValueError("响应超过大小上限。")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("响应超过大小上限。")
        chunks.append(chunk)
    return b"".join(chunks)


def _target(
    url: str,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
    allowed_hosts: set[str],
):
    """构造并固定小宇宙页面或媒体 CDN 的受控出网目标。"""

    try:
        return prepare_outbound_request(
            url,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | allowed_hosts,
            resolver=resolver,
        )
    except ValueError as exc:
        raise TranscriptionRuntimeError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc


def _http_timeout(settings: Settings, *, media: bool = False) -> float:
    """按运行类型选择有限 HTTP 超时，避免沿用无限等待。"""

    raw = getattr(settings, "vertical_ability_media_timeout_seconds", None) if media else None
    if raw is None:
        raw = getattr(settings, "tool_timeout_seconds", 30.0)
    try:
        value = float(raw or 30.0)
    except (TypeError, ValueError):
        value = 30.0
    return max(1.0, min(value, _MAX_TRANSCRIPTION_TIMEOUT_SECONDS))
