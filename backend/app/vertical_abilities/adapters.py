"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : adapters.py
@CallChain  : VerticalAbilityToolExecutor → Agent-Reach project-native adapter → public data
@Description: 实现只读情报、字幕与公开关联材料保真，拒绝原始对象兜底和无效指标，披露失败及截断。
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
from html import unescape
import json
import math
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

import httpx
import yaml

from app.cancellation import raise_if_cancelled, run_cancellable
from app.config import Settings, get_settings
from app.security.dns_resolver import AdaptiveDNSResolver, default_adaptive_resolver
from app.security.outbound import allowed_hosts_from_settings, prepare_outbound_request
from app.tools.mcp_client import MCPClientError, execute_mcp_tool
from app.vertical_abilities.youtube_runtime import (
    YouTubeRuntimeError,
    normalize_subtitle_segments,
    parse_vtt_subtitle,
    read_youtube_video,
    search_youtube_videos,
)
from app.vertical_abilities.transcription_runtime import (
    TranscriptionRuntimeError,
    WHISPER_PROVIDERS,
    transcribe_public_media,
)
from app.vertical_abilities.platform_runtime import (
    PlatformRuntimeError,
    run_opencli,
    run_twitter_cli,
)
from app.vertical_abilities.xiaoyuzhou_runtime import transcribe_xiaoyuzhou_episode


_JINA_BASE_URL = "https://r.jina.ai/"
_V2EX_BASE_URL = "https://www.v2ex.com"
_GITHUB_API_BASE_URL = "https://api.github.com"
_GITHUB_API_HOST = "api.github.com"
_GITHUB_REPOSITORY_HOST = "github.com"
_GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "gongge-xuban/vertical-ability",
}
_BILIBILI_API_BASE_URL = "https://api.bilibili.com"
_BILIBILI_API_HOST = "api.bilibili.com"
_BILIBILI_HOSTS = frozenset({"bilibili.com", "www.bilibili.com", "m.bilibili.com"})
_BILIBILI_API_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.bilibili.com/",
    "User-Agent": "Mozilla/5.0 (compatible; gongge-xuban/1.0)",
}
_BILIBILI_BVID_PATTERN = re.compile(r"^BV[0-9A-Za-z]{10}$", re.IGNORECASE)
_BILIBILI_AID_PATTERN = re.compile(r"^(?:av)?([0-9]{1,20})$", re.IGNORECASE)
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
_YOUTUBE_DEFAULT_SUBTITLE_LANGUAGES = ("zh-Hans", "zh", "en")
_MAX_HTTP_BYTES = 5 * 1024 * 1024
_MAX_RESULT_CHARS = 80_000
_MAX_ITEM_CHARS = 4_000
_MAX_PLATFORM_RELATED_ITEMS = 50
_MAX_PLATFORM_RELATED_DEPTH = 3
_MAX_PLATFORM_RELATED_TOTAL = 200
_PLATFORM_ID_FIELDS = (
    "id", "id_str", "note_id", "post_id", "tweet_id", "comment_id", "parent_id",
    "parent_comment_id", "reply_to_comment_id", "in_reply_to_status_id",
    "in_reply_to_status_id_str", "root_id", "conversation_id", "user_id", "author_id",
)
_PLATFORM_METRIC_FIELDS = (
    "likes", "liked_count", "like_count", "collected_count", "collect_count",
    "comment_count", "comments_count", "num_comments", "reply_count", "replies_count",
    "sub_comment_count", "share_count", "shares", "retweet_count", "repost_count",
    "quote_count", "view_count", "views", "score", "upvotes", "downvotes", "favorite_count",
)
_PLATFORM_RELATED_FIELDS = ("comments", "replies", "sub_comments", "sub_comment_list")
_DEFAULT_TIMEOUT_SECONDS = 30.0
_REDDIT_HOSTS = frozenset({"www.reddit.com", "old.reddit.com", "reddit.com"})
_XUEQIU_HOSTS = frozenset({"xueqiu.com", "www.xueqiu.com", "stock.xueqiu.com"})
_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_TWITTER_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_FACEBOOK_HOSTS = frozenset({"facebook.com", "www.facebook.com", "fb.com", "fb.watch"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "instagr.am"})
_XIAOHONGSHU_HOSTS = frozenset({"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"})
_SOCIAL_HOSTS = {
    "facebook": _FACEBOOK_HOSTS,
    "instagram": _INSTAGRAM_HOSTS,
    "xiaohongshu": _XIAOHONGSHU_HOSTS,
}


class VerticalAbilityAdapterError(RuntimeError):
    """表示垂直能力外部数据源失败或返回了不符合契约的内容。"""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        """保存稳定错误码、可选上游状态码和不含凭据的用户可见摘要。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _collection_result(
    *,
    collection_key: str,
    items: list[dict[str, object]],
    common: Mapping[str, object],
) -> dict[str, object]:
    """为列表型外部回执补充数量与明确的有数据/无结果状态。"""

    count = len(items)
    return {
        **dict(common),
        collection_key: items,
        "count": count,
        "result_state": "success_with_data" if count > 0 else "no_results",
    }


def execute_vertical_adapter(
    adapter: str,
    arguments: Mapping[str, Any],
    *,
    settings: Settings | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    resolver: AdaptiveDNSResolver | None = None,
    jina_api_key: str | None = None,
    platform_credentials: Mapping[str, str] | None = None,
    platform_connections: Mapping[str, Mapping[str, str]] | None = None,
    relay_executor: Callable[..., bytes] | None = None,
) -> dict[str, object]:
    """按能力包冻结的适配器名称执行一次有界只读请求。"""

    raise_if_cancelled(is_cancelled)
    effective_settings = settings or get_settings()
    effective_resolver = resolver or default_adaptive_resolver()
    normalized = dict(arguments or {})
    if adapter == "exa.web_search_exa":
        return _exa_search(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "jina.reader":
        return _jina_read(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            jina_api_key=jina_api_key,
        )
    if adapter == "rss.atom":
        return _rss_read(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "v2ex.public_api":
        return _v2ex_topics(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "v2ex.public_user":
        return _v2ex_user(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "github.public_repo":
        return _github_public_repo(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            github_credentials=(platform_connections or {}).get("github"),
            jina_api_key=jina_api_key,
        )
    if adapter == "github.issue":
        return _github_issue(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            github_credentials=(platform_connections or {}).get("github"),
        )
    if adapter == "github.pr":
        return _github_pull_request(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            github_credentials=(platform_connections or {}).get("github"),
        )
    if adapter == "github.releases":
        return _github_releases(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            github_credentials=(platform_connections or {}).get("github"),
        )
    if adapter == "github.public_search":
        return _github_public_search(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            github_credentials=(platform_connections or {}).get("github"),
            jina_api_key=jina_api_key,
        )
    if adapter == "bilibili.public_search":
        return _bilibili_search(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "bilibili.public_video":
        return _bilibili_video(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "bilibili.opencli_subtitle":
        return _bilibili_subtitle(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            credentials=(platform_connections or {}).get("bilibili"),
            relay_executor=relay_executor,
        )
    if adapter == "twitter.read":
        return _twitter_read(
            normalized,
            effective_settings,
            is_cancelled,
            credentials=(platform_connections or {}).get("twitter"),
            relay_executor=relay_executor,
        )
    if adapter == "twitter.search":
        return _twitter_search(
            normalized,
            effective_settings,
            is_cancelled,
            credentials=(platform_connections or {}).get("twitter"),
            relay_executor=relay_executor,
        )
    if adapter == "reddit.read":
        return _reddit_read(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            credentials=(platform_connections or {}).get("reddit"),
            relay_executor=relay_executor,
        )
    if adapter == "reddit.search":
        return _reddit_search(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            credentials=(platform_connections or {}).get("reddit"),
            relay_executor=relay_executor,
        )
    if adapter == "facebook.opencli":
        return _social_opencli(
            "facebook",
            normalized,
            effective_settings,
            is_cancelled,
            credentials=(platform_connections or {}).get("facebook"),
            relay_executor=relay_executor,
        )
    if adapter == "instagram.opencli":
        return _social_opencli(
            "instagram",
            normalized,
            effective_settings,
            is_cancelled,
            credentials=(platform_connections or {}).get("instagram"),
            relay_executor=relay_executor,
        )
    if adapter == "xiaohongshu.opencli":
        return _social_opencli(
            "xiaohongshu",
            normalized,
            effective_settings,
            is_cancelled,
            credentials=(platform_connections or {}).get("xiaohongshu"),
            relay_executor=relay_executor,
        )
    if adapter == "linkedin.public_read":
        return _linkedin_read(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            jina_api_key=jina_api_key,
        )
    if adapter == "linkedin.public_search":
        return _linkedin_search(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "xueqiu.public_api":
        return _xueqiu_api(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            credentials=(platform_connections or {}).get("xueqiu"),
            relay_executor=relay_executor,
        )
    if adapter == "xiaoyuzhou.whisper_transcribe":
        return _xiaoyuzhou_transcribe(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            platform_credentials=platform_credentials or {},
        )
    if adapter == "youtube.ytdlp_video":
        return _youtube_video(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "youtube.ytdlp_search":
        return _youtube_search(normalized, effective_settings, is_cancelled, effective_resolver)
    if adapter == "media.whisper_transcribe":
        return _media_whisper_transcribe(
            normalized,
            effective_settings,
            is_cancelled,
            effective_resolver,
            platform_credentials=platform_credentials or {},
        )
    raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ADAPTER_NOT_FOUND", "能力包适配器不存在。")


def execute_vertical_provider_probe(
    provider: str,
    api_key: str,
    *,
    settings: Settings | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    resolver: AdaptiveDNSResolver | None = None,
) -> dict[str, object]:
    """调用固定 Whisper Provider 的模型目录接口，验证密钥和出网链路而不上传用户音频。"""

    normalized_provider = str(provider or "").strip().casefold()
    provider_info = WHISPER_PROVIDERS.get(normalized_provider)
    if provider_info is None:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_NOT_FOUND",
            "能力包 Provider 配置不存在。",
        )
    value = str(api_key or "").strip()
    if not value:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
            f"请先配置 {provider_info.display_name} API Key。",
        )
    effective_settings = settings or get_settings()
    effective_resolver = resolver or default_adaptive_resolver()
    probe_url = provider_info.endpoint.rsplit("/audio/transcriptions", 1)[0] + "/models"
    body = _http_get_bytes(
        probe_url,
        settings=effective_settings,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {value}",
            "User-Agent": "gongge-xuban/vertical-ability",
        },
        is_cancelled=is_cancelled,
        resolver=effective_resolver,
        allowed_hosts={provider_info.host},
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            f"{provider_info.display_name} 模型目录无法解析。",
        ) from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            f"{provider_info.display_name} 模型目录返回了无法识别的内容。",
        )
    return {
        "provider": provider_info.display_name,
        "provider_id": normalized_provider,
        "models_count": len(payload["data"]),
    }


def _media_whisper_transcribe(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    platform_credentials: Mapping[str, str],
) -> dict[str, object]:
    """把公开 YouTube 音频交给平台 Whisper Runtime，并收敛为统一转写回执。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    provider = str(arguments.get("provider") or "auto").strip().casefold() or "auto"
    allow_provider_fallback = arguments.get("allow_provider_fallback", False)
    if not isinstance(allow_provider_fallback, bool):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "allow_provider_fallback 必须是布尔值。",
        )
    try:
        return transcribe_public_media(
            source_url,
            provider=provider,
            provider_credentials=platform_credentials,
            settings=settings,
            resolver=resolver,
            is_cancelled=is_cancelled,
            allow_provider_fallback=allow_provider_fallback,
        )
    except TranscriptionRuntimeError as exc:
        raise VerticalAbilityAdapterError(exc.code, exc.message) from exc


def _bilibili_subtitle(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """读取已有 OpenCLI 字幕，在原文之外保留可用时间轴，披露无效时间和截断。"""

    _require_platform_connection(credentials, "B站字幕")
    reference = _required_string(arguments, "video", max_length=2048)
    bvid, aid, source_url = _parse_bilibili_video_reference(reference, settings, resolver)
    command_reference = bvid or f"av{aid}"
    raw = _run_opencli(
        "bilibili",
        "subtitle",
        [command_reference],
        credentials=credentials,
        settings=settings,
        is_cancelled=is_cancelled,
        relay_executor=relay_executor,
    )
    subtitle = _bilibili_subtitle_payload(raw)
    return {
        "url": source_url,
        "subtitle": {"language": "", **subtitle},
        "subtitle_result_state": "success_with_data" if subtitle["content"] else "no_results",
        "provider": "OpenCLI Bilibili",
        "retrieved_at": _retrieved_at(),
    }


def _bilibili_subtitle_payload(raw: object) -> dict[str, object]:
    """兼容 B站文本、VTT、JSON 与非 TTY 默认 YAML，不给无时间的文本补造时间轴。"""

    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith(("{", "[")):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, (Mapping, list)):
                raw = decoded
        elif re.match(r"(?:-\s+(?:index|from|start|content|text):|(?:segments|body|items|data):)", text):
            try:
                decoded = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise VerticalAbilityAdapterError(
                    "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "B站字幕结构化回执无法解析。",
                ) from exc
            if isinstance(decoded, (Mapping, list)):
                raw = decoded
        if isinstance(raw, str) and (
            text.lstrip("\ufeff").startswith("WEBVTT") or " --> " in text
        ):
            return parse_vtt_subtitle(text)

    return normalize_subtitle_segments(_bilibili_subtitle_segments(raw))


def _bilibili_subtitle_segments(raw: object, depth: int = 0) -> Iterable[Mapping[str, object]]:
    """仅展开已知字幕包装，保留 start/end 或 from/to，限制异常递归层数。"""

    if depth > 8:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "B站字幕回执嵌套层级异常。",
        )
    if isinstance(raw, Mapping):
        for key in ("segments", "body", "items", "data", "subtitle", "transcript"):
            value = raw.get(key)
            if isinstance(value, (Mapping, list, tuple)):
                yield from _bilibili_subtitle_segments(value, depth + 1)
                return
        text = _platform_text(raw)
        if text:
            segment: dict[str, object] = {"text": text}
            for target, aliases in (("start", ("start", "from")), ("end", ("end", "to"))):
                for key in aliases:
                    if key in raw:
                        segment[target] = raw[key]
                        break
            yield segment
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            yield from _bilibili_subtitle_segments(item, depth + 1)
    elif isinstance(raw, str) and raw.strip():
        yield {"text": raw.strip()}


def _twitter_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """按登录浏览器或显式 Cookie 调用 Twitter/X 搜索，并统一为来源卡片。"""

    query = _required_string(arguments, "query", max_length=500)
    limit = _bounded_int(arguments.get("num_results", 10), default=10, minimum=1, maximum=30)
    raw = _twitter_command(
        "search",
        [query],
        credentials=credentials,
        limit=limit,
        settings=settings,
        is_cancelled=is_cancelled,
        relay_executor=relay_executor,
    )
    return _platform_collection_result(
        "results",
        raw,
        limit=limit,
        provider="Twitter/X",
        common={"query": query, "retrieved_at": _retrieved_at()},
    )


def _twitter_read(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """读取单条 Twitter/X 推文或文章，保留原始来源 URL。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    _validate_platform_url(source_url, _TWITTER_HOSTS, "Twitter/X")
    action = "article" if "/i/articles/" in source_url or "/articles/" in source_url else "tweet"
    raw = _twitter_command(
        action,
        [source_url],
        credentials=credentials,
        limit=None,
        settings=settings,
        is_cancelled=is_cancelled,
        relay_executor=relay_executor,
    )
    items = _normalize_platform_items(raw, 1)
    return {
        "url": source_url,
        "items": items,
        "count": len(items),
        "result_state": "success_with_data" if items else "no_results",
        "provider": "Twitter/X",
        "retrieved_at": _retrieved_at(),
    }


def _twitter_command(
    action: str,
    values: list[str],
    *,
    credentials: Mapping[str, str] | None,
    limit: int | None,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    relay_executor: Callable[..., bytes] | None,
) -> object:
    """选择 Twitter/X 的 OpenCLI 或显式 Cookie CLI 路径，不共享用户环境状态。"""

    safe_credentials = credentials or {}
    _require_platform_connection(safe_credentials, "Twitter/X")
    if _uses_opencli(safe_credentials):
        try:
            return run_opencli(
                "twitter",
                action,
                values,
                credentials=safe_credentials,
                limit=limit,
                settings=settings,
                is_cancelled=is_cancelled,
                relay_executor=relay_executor,
            )
        except PlatformRuntimeError as exc:
            raise VerticalAbilityAdapterError(exc.code, exc.message) from exc
    cookie_credentials = _twitter_cookie_credentials(safe_credentials)
    try:
        return run_twitter_cli(
            action,
            values,
            credentials=cookie_credentials,
            limit=limit,
            settings=settings,
            is_cancelled=is_cancelled,
        )
    except PlatformRuntimeError as exc:
        raise VerticalAbilityAdapterError(exc.code, exc.message) from exc


def _reddit_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """按 OpenCLI 或用户 Cookie 搜索 Reddit 帖子，不把匿名 403 伪装成零结果。"""

    _require_platform_connection(credentials, "Reddit")
    query = _required_string(arguments, "query", max_length=500)
    limit = _bounded_int(arguments.get("num_results", 10), default=10, minimum=1, maximum=30)
    if _uses_opencli(credentials):
        raw = _run_opencli(
            "reddit",
            "search",
            [query],
            credentials=credentials,
            limit=limit,
            settings=settings,
            is_cancelled=is_cancelled,
            relay_executor=relay_executor,
        )
    else:
        raw = _reddit_http_json(
            "/search.json",
            query_params={"q": query, "limit": limit, "raw_json": 1},
            credentials=credentials,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
        )
    return _platform_collection_result(
        "results",
        raw,
        limit=limit,
        provider="Reddit",
        common={"query": query, "retrieved_at": _retrieved_at()},
        relative_base_url="https://www.reddit.com",
    )


def _reddit_read(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """读取 Reddit 帖子及有限评论，并保留用户给出的帖子来源。"""

    _require_platform_connection(credentials, "Reddit")
    reference = _required_string(arguments, "post", max_length=2048)
    post_id, source_url = _reddit_reference(reference)
    limit = _bounded_int(arguments.get("limit", 20), default=20, minimum=1, maximum=50)
    if _uses_opencli(credentials):
        raw = _run_opencli(
            "reddit",
            "read",
            [post_id],
            credentials=credentials,
            limit=limit,
            settings=settings,
            is_cancelled=is_cancelled,
            relay_executor=relay_executor,
        )
    else:
        raw = _reddit_http_json(
            f"/comments/{quote(post_id, safe='')}.json",
            query_params={"raw_json": 1, "limit": limit},
            credentials=credentials,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
        )
    items = _normalize_platform_items(
        raw,
        limit,
        relative_base_url="https://www.reddit.com",
    )
    return {
        "post": post_id,
        "url": source_url,
        "items": items,
        "count": len(items),
        "result_state": "success_with_data" if items else "no_results",
        "provider": "Reddit",
        "retrieved_at": _retrieved_at(),
    }


def _social_opencli(
    platform_id: str,
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """执行 Facebook、Instagram 或小红书的固定 OpenCLI 操作并统一列表契约。"""

    _require_platform_connection(credentials, platform_id)
    operation = _bounded_string(arguments.get("operation", "search"), "operation", 32).casefold()
    allowed = {
        "facebook": {"search", "profile", "feed", "groups"},
        "instagram": {"search", "profile", "user", "explore", "saved"},
        "xiaohongshu": {"search", "note", "comments", "feed", "user"},
    }[platform_id]
    if operation not in allowed:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "网站操作不在能力包允许范围内。")
    value = str(arguments.get("value") or arguments.get("query") or arguments.get("username") or "").strip()
    if operation in {"search", "profile", "user", "note", "comments"} and not value:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "该网站操作需要填写 query、username 或 value。")
    if value and "://" in value:
        _validate_platform_url(value, _SOCIAL_HOSTS[platform_id], platform_id)
    limit = _bounded_int(arguments.get("limit", 20), default=20, minimum=1, maximum=50)
    positional = [value] if value else []
    raw = _run_opencli(
        platform_id,
        operation,
        positional,
        credentials=credentials,
        limit=limit,
        settings=settings,
        is_cancelled=is_cancelled,
        relay_executor=relay_executor,
    )
    return _platform_collection_result(
        "items",
        raw,
        limit=limit,
        provider=f"OpenCLI {platform_id}",
        common={"operation": operation, "retrieved_at": _retrieved_at()},
    )


def _linkedin_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """使用平台 Exa 搜索 LinkedIn 公开页面，避免把本机 mcporter 全局登录态带入服务。"""

    query = _required_string(arguments, "query", max_length=500)
    limit = _bounded_int(arguments.get("num_results", 5), default=5, minimum=1, maximum=20)
    result = _exa_search(
        {"query": query, "num_results": limit, "include_domains": ["linkedin.com"]},
        settings,
        is_cancelled,
        resolver,
    )
    return {**result, "provider": "Exa LinkedIn public search"}


def _linkedin_read(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    jina_api_key: str | None,
) -> dict[str, object]:
    """限制 Jina Reader 只读取 LinkedIn 公开页面，避免能力名掩盖跨站抓取。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    _validate_platform_url(source_url, _LINKEDIN_HOSTS, "LinkedIn")
    return _jina_read(
        arguments,
        settings,
        is_cancelled,
        resolver,
        jina_api_key=jina_api_key,
    )


def _xueqiu_api(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    credentials: Mapping[str, str] | None,
    relay_executor: Callable[..., bytes] | None,
) -> dict[str, object]:
    """调用 Agent-Reach 约定的雪球固定公开接口，Cookie 只随当前请求发送。"""

    operation = _bounded_string(arguments.get("operation", "stock"), "operation", 32).casefold()
    if operation not in {"stock", "search", "hot", "hot_stock"}:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "雪球操作不在能力包允许范围内。")
    limit = _bounded_int(arguments.get("limit", 10), default=10, minimum=1, maximum=50)
    if _uses_opencli(credentials):
        values: list[str] = []
        if operation == "stock":
            values.append(_required_string(arguments, "symbol", max_length=64))
        elif operation == "search":
            values.append(_required_string(arguments, "query", max_length=200))
        raw = _run_opencli(
            "xueqiu",
            operation.replace("_", "-"),
            values,
            credentials=credentials,
            limit=limit,
            settings=settings,
            is_cancelled=is_cancelled,
            relay_executor=relay_executor,
        )
    else:
        raw = _xueqiu_http_json(
            operation,
            arguments,
            limit=limit,
            credentials=credentials,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
        )
    return _platform_collection_result(
        "items",
        raw,
        limit=limit,
        provider="Xueqiu public API",
        common={"operation": operation, "retrieved_at": _retrieved_at()},
    )


def _xiaoyuzhou_transcribe(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    platform_credentials: Mapping[str, str],
) -> dict[str, object]:
    """读取小宇宙公开播客并交给与 YouTube 共用的 Whisper Provider。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    provider = str(arguments.get("provider") or "auto").strip().casefold() or "auto"
    allow_provider_fallback = arguments.get("allow_provider_fallback", False)
    if not isinstance(allow_provider_fallback, bool):
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "allow_provider_fallback 必须是布尔值。")
    try:
        return transcribe_xiaoyuzhou_episode(
            source_url,
            provider=provider,
            provider_credentials=platform_credentials,
            settings=settings,
            resolver=resolver,
            is_cancelled=is_cancelled,
            allow_provider_fallback=allow_provider_fallback,
        )
    except TranscriptionRuntimeError as exc:
        raise VerticalAbilityAdapterError(exc.code, exc.message) from exc


def _run_opencli(
    platform_id: str,
    action: str,
    values: list[str],
    *,
    credentials: Mapping[str, str] | None,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    limit: int | None = None,
    relay_executor: Callable[..., bytes] | None = None,
) -> object:
    """把 OpenCLI 运行时错误转换为能力包稳定错误，并拒绝未支持的 Cookie 旁路。"""

    safe_credentials = credentials or {}
    if not _uses_opencli(safe_credentials):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_CONNECTION_RUNTIME_PENDING",
            f"{platform_id} 当前的 Cookie 连接需要专用的隔离适配器；请连接已登录浏览器会话。",
        )
    try:
        return run_opencli(
            platform_id,
            action,
            values,
            credentials=safe_credentials,
            limit=limit,
            settings=settings,
            is_cancelled=is_cancelled,
            relay_executor=relay_executor,
        )
    except PlatformRuntimeError as exc:
        raise VerticalAbilityAdapterError(exc.code, exc.message) from exc


def _uses_opencli(credentials: Mapping[str, str] | None) -> bool:
    """判断当前用户连接是否明确引用 OpenCLI 浏览器会话。"""

    reference = str((credentials or {}).get("session_reference") or "")
    return reference.startswith("opencli:") or reference.startswith("relay:")


def _require_platform_connection(
    credentials: Mapping[str, str] | None,
    platform_name: str,
) -> None:
    """在确实需要登录态的平台调用前给出可操作的连接中心提示。"""

    if credentials:
        return
    raise VerticalAbilityAdapterError(
        "VERTICAL_ABILITY_CONNECTION_REQUIRED",
        f"{platform_name} 需要先在对话框中打开“网站连接中心”并完成平台连接。",
    )


def _twitter_cookie_credentials(credentials: Mapping[str, str]) -> dict[str, str]:
    """从连接密文中提取 Twitter CLI 所需的两个 Cookie 字段。"""

    parsed: dict[str, str] = {
        "auth_token": str(credentials.get("auth_token") or "").strip(),
        "ct0": str(credentials.get("ct0") or "").strip(),
    }
    cookie = str(credentials.get("cookie") or "")
    for pair in cookie.split(";"):
        key, separator, value = pair.strip().partition("=")
        if separator and key.strip() in {"auth_token", "ct0"} and not parsed[key.strip()]:
            parsed[key.strip()] = value.strip()
    return parsed


def _platform_collection_result(
    collection_key: str,
    raw: object,
    *,
    limit: int,
    provider: str,
    common: Mapping[str, object],
    relative_base_url: str | None = None,
) -> dict[str, object]:
    """把各平台 CLI/API 回执归一化为带 count/result_state 的集合结果。"""

    items = _normalize_platform_items(raw, limit, relative_base_url=relative_base_url)
    return _collection_result(
        collection_key=collection_key,
        items=items,
        common={**dict(common), "provider": provider},
    )


def _normalize_platform_items(
    raw: object,
    limit: int,
    *,
    relative_base_url: str | None = None,
) -> list[dict[str, object]]:
    """保留来源卡片和公开关联材料，限制评论树总量且在字段内披露截断。"""

    candidates = _platform_candidates(raw)
    result: list[dict[str, object]] = []
    remaining_related = [_MAX_PLATFORM_RELATED_TOTAL]
    for item in candidates[:limit]:
        normalized = _normalize_platform_item(
            item, relative_base_url=relative_base_url, depth=0, remaining=remaining_related,
        )
        if normalized:
            result.append(normalized)
    return result


def _platform_bounded_text(
    target: dict[str, object], key: str, text: str, maximum: int = _MAX_ITEM_CHARS,
) -> None:
    """保留原有字符串字段，并在超过该字段上限时显式标记而非静默裁剪。"""

    target[key] = text[:maximum]
    if len(text) > maximum:
        target[f"{key}_truncated"] = True


def _platform_public_metadata(item: Mapping[str, object]) -> dict[str, object]:
    """仅复制公开元数据，保留零值与未知值，拒绝布尔伪计数、非有限浮点及连接凭据。"""

    result: dict[str, object] = {}
    for key in (*_PLATFORM_ID_FIELDS, *_PLATFORM_METRIC_FIELDS):
        if key in item and (item[key] is None or isinstance(item[key], (str, int, float))):
            value = item[key]
            if isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
                continue
            if isinstance(value, str):
                _platform_bounded_text(result, key, value, 256)
            else:
                result[key] = value
    for key in ("interact_info", "note_interact_info", "public_metrics"):
        raw = item.get(key)
        if isinstance(raw, Mapping):
            metrics = _platform_public_metadata({
                name: raw[name] for name in _PLATFORM_METRIC_FIELDS if name in raw
            })
            if metrics:
                result[key] = metrics
                for name, value in metrics.items():
                    result.setdefault(name, value)
    for key in ("user", "user_info", "author", "member"):
        raw = item.get(key)
        if not isinstance(raw, Mapping):
            continue
        user: dict[str, object] = {}
        for name in (
            "id", "user_id", "username", "login", "name", "nickname", "nick_name",
            "display_name", "url", "profile_url",
        ):
            value = raw.get(name)
            if isinstance(value, str):
                _platform_bounded_text(user, name, value, 2048 if "url" in name else 256)
            elif isinstance(value, int) and not isinstance(value, bool):
                user[name] = value
        if user:
            result["author_details" if key == "author" else key] = user
    for key in ("has_more", "has_more_comments", "has_more_replies"):
        if isinstance(item.get(key), bool):
            result[key] = item[key]
    return result


def _platform_first_text(
    item: Mapping[str, object], keys: tuple[str, ...], *, allow_numbers: bool = False,
) -> str:
    """只从已知字段选择真实文本；时间字段可接收有限数值，绝不字符串化嵌套对象。"""

    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if allow_numbers and not isinstance(value, bool):
            if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
                return str(value)
    return ""


def _reject_platform_error_envelope(item: Mapping[str, object]) -> None:
    """在展开包装和生成来源前拒绝显式错误，错误说明不回显 Provider 原始载荷。"""

    if item.get("isError") is True or item.get("success") is False or item.get("error"):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "平台返回了错误回执，未获得有效来源。",
        )


def _platform_has_public_content(item: Mapping[str, object]) -> bool:
    """要求存在公开身份、作者、正文或有效关联材料，不把分页标记和空元数据当来源。"""

    if any(item.get(key) not in (None, "") for key in (
        "title", "url", "summary", "content", "author", *_PLATFORM_ID_FIELDS,
    )):
        return True
    for key in ("user", "user_info", "author_details", "member"):
        user = item.get(key)
        if isinstance(user, Mapping) and any(value not in (None, "") for value in user.values()):
            return True
    return any(isinstance(item.get(key), list) and item[key] for key in _PLATFORM_RELATED_FIELDS)


def _normalize_platform_item(
    item: object,
    *,
    relative_base_url: str | None,
    depth: int,
    remaining: list[int],
) -> dict[str, object] | None:
    """仅用已知公开字段生成帖子或评论；拒绝错误包装，未知对象不制造摘要或来源。"""

    normalized: dict[str, object] = {"title": "", "url": "", "published_at": "", "summary": ""}
    if isinstance(item, str):
        if not item.strip():
            return None
        _platform_bounded_text(normalized, "summary", item.strip())
        return normalized
    if not isinstance(item, Mapping):
        return None
    _reject_platform_error_envelope(item)
    for key in ("note_card", "note"):
        if isinstance(item.get(key), Mapping):
            item = {**{name: value for name, value in item.items() if name != key}, **item[key]}
            break
    _reject_platform_error_envelope(item)
    normalized.update(_platform_public_metadata(item))
    url = _platform_first_text(item, ("url", "link", "html_url", "permalink", "target"))
    if url.startswith("/") and relative_base_url:
        url = relative_base_url.rstrip("/") + url
    title = _platform_first_text(item, ("title", "display_title", "name", "full_name"))
    author = ""
    for key in ("author", "username", "user", "user_info", "member"):
        raw_author = item.get(key)
        if isinstance(raw_author, Mapping):
            raw_author = next((raw_author[name] for name in (
                "name", "username", "login", "nickname", "nick_name", "display_name",
            ) if isinstance(raw_author.get(name), str) and raw_author[name].strip()), "")
        if isinstance(raw_author, str) and raw_author.strip():
            author = raw_author.strip()
            break
    published = _platform_first_text(
        item, ("published_at", "published", "created_at", "timestamp", "time"), allow_numbers=True,
    )
    summary = _platform_first_text(item, (
        "summary", "text", "content", "body", "selftext", "desc", "description", "snippet",
    ))
    for key, text, maximum in (
        ("title", title, _MAX_ITEM_CHARS), ("url", url, 2048),
        ("published_at", published, 128), ("summary", summary, _MAX_ITEM_CHARS),
    ):
        _platform_bounded_text(normalized, key, text, maximum)
    if author:
        _platform_bounded_text(normalized, "author", author, 256)
    for key in ("content", "body", "selftext", "desc", "text"):
        if isinstance(item.get(key), str):
            _platform_bounded_text(normalized, "content", item[key].strip())
            break
    _platform_related_comments(
        item, normalized, relative_base_url=relative_base_url, depth=depth, remaining=remaining,
    )
    return normalized if _platform_has_public_content(normalized) else None


def _platform_related_comments(
    item: Mapping[str, object],
    normalized: dict[str, object],
    *,
    relative_base_url: str | None,
    depth: int,
    remaining: list[int],
) -> None:
    """保留嵌套评论所属字段，共享总预算并披露数量、深度及来源未读完的边界。"""

    for key in _PLATFORM_RELATED_FIELDS:
        raw = item.get(key)
        if not isinstance(raw, (Mapping, list, tuple)):
            continue
        candidates = _platform_candidates(raw)
        comments: list[dict[str, object]] = []
        truncated = len(candidates) > _MAX_PLATFORM_RELATED_ITEMS
        for child in candidates[:_MAX_PLATFORM_RELATED_ITEMS]:
            if depth >= _MAX_PLATFORM_RELATED_DEPTH or remaining[0] <= 0:
                truncated = True
                break
            remaining[0] -= 1
            comment = _normalize_platform_item(
                child, relative_base_url=relative_base_url, depth=depth + 1, remaining=remaining,
            )
            if comment:
                comments.append(comment)
                truncated = truncated or any(
                    name.endswith("_truncated") and value is True for name, value in comment.items()
                )
        normalized[key] = comments
        source_has_more = isinstance(raw, Mapping) and raw.get("has_more") is True
        if truncated or source_has_more or item.get(f"has_more_{key}") is True:
            normalized[f"{key}_truncated"] = True


def _platform_candidates(raw: object) -> list[object]:
    """拒绝错误回执后展开集合包装，评论列表与单条帖子保持不同结构。"""

    if isinstance(raw, Mapping):
        _reject_platform_error_envelope(raw)
        for key in ("results", "items", "posts", "tweets", "entries", "stocks", "list", "children", "quote", "stock"):
            if key in raw and isinstance(raw[key], (Mapping, list, tuple)):
                return _platform_candidates(raw[key])
        data = raw.get("data")
        if isinstance(data, (Mapping, list, tuple)):
            return _platform_candidates(data)
        if not any(key in raw for key in (
            *_PLATFORM_ID_FIELDS, "title", "content", "body", "text", "desc", "note_card", "note",
        )):
            for key in (*_PLATFORM_RELATED_FIELDS, "notes"):
                if isinstance(raw.get(key), (Mapping, list, tuple)):
                    return _platform_candidates(raw[key])
        return [raw]
    if isinstance(raw, (list, tuple)):
        result: list[object] = []
        for item in raw:
            if isinstance(item, Mapping) and isinstance(item.get("data"), Mapping):
                result.extend(_platform_candidates(item["data"]))
            else:
                result.append(item)
        return result
    if raw is None:
        return []
    return [raw]


def _platform_text(raw: object) -> str:
    """从字幕/正文 CLI 回执中提取有限纯文本，不把对象 repr 当作正文。"""

    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, Mapping):
        for key in ("text", "content", "transcript", "subtitle", "body"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("segments", "items", "data"):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                return "\n".join(part for part in (_platform_text(item) for item in value) if part).strip()
        return ""
    if isinstance(raw, (list, tuple)):
        return "\n".join(part for part in (_platform_text(item) for item in raw) if part).strip()
    return ""


def _reddit_reference(reference: str) -> tuple[str, str]:
    """把 Reddit 帖子 ID 或公开 URL 归一化为 OpenCLI/API 可接受的 ID 和来源。"""

    raw = reference.strip()
    if "://" not in raw:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{2,128}", raw):
            raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "post 必须是 Reddit 帖子 ID 或公开 URL。")
        return raw, f"https://www.reddit.com/comments/{raw}"
    _validate_platform_url(raw, _REDDIT_HOSTS, "Reddit")
    parts = [part for part in urlsplit(raw).path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() != "comments":
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", "Reddit URL 必须包含 /comments/{post_id}。")
    post_id = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_\-]{2,128}", post_id):
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", "Reddit URL 中没有有效帖子 ID。")
    return post_id, raw


def _reddit_http_json(
    path: str,
    *,
    query_params: Mapping[str, object],
    credentials: Mapping[str, str] | None,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> object:
    """用当前用户 Cookie 请求 Reddit 固定 JSON 接口，匿名 403 直接保留为错误。"""

    headers = {"Accept": "application/json", "User-Agent": "gongge-xuban/vertical-ability"}
    cookie = _cookie_header(credentials, "reddit_session")
    if cookie:
        headers["Cookie"] = cookie
    body = _http_get_bytes(
        "https://www.reddit.com" + path + ("&" if "?" in path else "?") + urlencode(query_params),
        settings=settings,
        headers=headers,
        is_cancelled=is_cancelled,
        resolver=resolver,
        allowed_hosts=_REDDIT_HOSTS,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "Reddit 返回内容无法解析。") from exc


def _cookie_header(credentials: Mapping[str, str] | None, single_key: str | None = None) -> str:
    """把当前用户连接中的 Cookie 字段拼成单次请求头，不写入进程级 Cookie Jar。"""

    safe = credentials or {}
    cookie = str(safe.get("cookie") or "").strip()
    if cookie:
        return cookie
    if single_key:
        value = str(safe.get(single_key) or "").strip()
        if value:
            return f"{single_key}={value}"
    return ""


def _xueqiu_http_json(
    operation: str,
    arguments: Mapping[str, Any],
    *,
    limit: int,
    credentials: Mapping[str, str] | None,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> object:
    """调用雪球固定公共端点，并按用户连接逐请求注入 Cookie。"""

    if operation == "stock":
        symbol = _required_string(arguments, "symbol", max_length=64)
        url = "https://stock.xueqiu.com/v5/stock/quote.json?" + urlencode({"symbol": symbol, "extend": "detail"})
    elif operation == "search":
        query = _required_string(arguments, "query", max_length=200)
        url = "https://xueqiu.com/stock/search.json?" + urlencode({"code": query, "size": limit})
    elif operation == "hot":
        url = "https://xueqiu.com/v4/statuses/public_timeline_by_category.json?" + urlencode(
            {"since_id": -1, "max_id": -1, "count": limit, "category": -1}
        )
    else:
        url = "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?" + urlencode({"size": limit, "type": "10"})
    headers = {
        "Accept": "application/json",
        "Referer": "https://xueqiu.com/",
        "User-Agent": "Mozilla/5.0 (compatible; gongge-xuban/1.0)",
    }
    cookie = _cookie_header(credentials)
    if cookie:
        headers["Cookie"] = cookie
    body = _http_get_bytes(
        url,
        settings=settings,
        headers=headers,
        is_cancelled=is_cancelled,
        resolver=resolver,
        allowed_hosts=_XUEQIU_HOSTS,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "雪球返回内容无法解析。") from exc


def _validate_platform_url(url: str, allowed_hosts: Iterable[str], platform_name: str) -> None:
    """校验平台来源 URL 的协议、主机、端口和凭据边界。"""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", f"{platform_name} URL 格式无效。") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host not in {str(item).casefold().rstrip(".") for item in allowed_hosts}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port not in {80, 443})
    ):
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", f"只允许公开 {platform_name} URL。")


def _exa_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """经真实 MCP 调用保留 Exa 混合载荷，并区分可用来源、空结果和协议失败。"""

    query = _required_string(arguments, "query", max_length=2000)
    limit = _bounded_int(arguments.get("num_results", 5), default=5, minimum=1, maximum=20)
    mcp_arguments: dict[str, Any] = {"query": query, "numResults": limit}
    include_domains = arguments.get("include_domains")
    if include_domains is not None:
        if not isinstance(include_domains, list) or not all(
            isinstance(item, str) and item.strip() for item in include_domains
        ):
            raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "include_domains 参数无效。")
        mcp_arguments["includeDomains"] = [str(item).strip() for item in include_domains[:20]]
    start_date = arguments.get("start_published_date")
    if start_date is not None:
        mcp_arguments["startPublishedDate"] = _bounded_string(start_date, "start_published_date", 64)
    try:
        raw = execute_mcp_tool(
            {
                "transport": "streamable_http",
                "url": settings.vertical_ability_exa_mcp_url,
            },
            mcp_arguments,
            timeout_seconds=_provider_timeout_seconds(settings),
            tool_name="web_search_exa",
            allowed_hosts=allowed_hosts_from_settings(settings),
            is_cancelled=is_cancelled,
            resolver=resolver,
            preserve_mixed_content=True,
        )
    except (MCPClientError, OSError, ValueError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
            f"Exa 搜索暂不可用：{str(exc)[:300]}",
        ) from exc
    results = _normalize_search_results(raw, limit)
    return _collection_result(
        collection_key="results",
        items=results,
        common={
            "query": query,
            "provider": "Exa via MCP",
            "retrieved_at": _retrieved_at(),
        },
    )


def _jina_read(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    jina_api_key: str | None,
) -> dict[str, object]:
    """校验公开 URL 和 Reader 原文，拒绝空正文、目标 HTTP 失败及反爬页面。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    _validate_public_url(source_url, settings, resolver)
    jina_url = _JINA_BASE_URL + quote(source_url, safe=":/%")
    headers = {"Accept": "text/plain", "User-Agent": "gongge-xuban/vertical-ability"}
    effective_jina_api_key = (
        str(jina_api_key).strip()
        if jina_api_key is not None
        else str(getattr(settings, "vertical_ability_jina_api_key", "") or "").strip()
    )
    if effective_jina_api_key:
        headers["Authorization"] = f"Bearer {effective_jina_api_key}"
    body = _http_get_bytes(
        jina_url,
        settings=settings,
        headers=headers,
        is_cancelled=is_cancelled,
        resolver=resolver,
    )
    text = body.decode("utf-8", errors="replace")
    marker = re.search(r"(?im)^Markdown Content:[ \t]*", text)
    metadata = text[:marker.start()] if marker else text
    content = text[marker.end():] if marker else text
    target_error = re.search(
        r"(?im)^Warning:[ \t]*Target URL returned error[ \t]+([45]\d{2})\b",
        metadata[:4096],
    )
    if target_error:
        status = int(target_error.group(1))
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            f"目标网页返回 HTTP {status}，未获取到可用正文。",
            status_code=status,
        )
    if not content.strip().lstrip("\ufeff").strip() or (
        marker is None and re.search(r"(?im)^URL Source:[ \t]*https?://", metadata)
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "Jina Reader 未返回非空正文，不能作为读取成功的证据。",
        )
    sample = metadata[:4096].casefold()
    if (
        ("warning:" in sample and "requiring captcha" in sample)
        or "title: just a moment..." in sample
        or "title: attention required! | cloudflare" in sample
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_ANTIBOT_PAGE",
            "网页返回了反爬验证页，未获取到可用正文。",
        )
    return {
        "url": source_url,
        "content": text[:_MAX_RESULT_CHARS],
        "provider": "Jina Reader",
        "retrieved_at": _retrieved_at(),
        "truncated": len(text) > _MAX_RESULT_CHARS,
    }


def _rss_read(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """读取公开 RSS/Atom，并支持调用方用游标和已见标识做增量比较。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    _validate_public_url(source_url, settings, resolver)
    limit = _bounded_int(arguments.get("limit", 20), default=20, minimum=1, maximum=50)
    known_ids = _bounded_string_list(arguments.get("known_ids"), "known_ids", max_items=100)
    cursor = str(arguments.get("cursor") or "").strip()
    if len(cursor) > 2048:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "cursor 参数无效。")
    body = _http_get_bytes(
        source_url,
        settings=settings,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml", "User-Agent": "gongge-xuban/vertical-ability"},
        is_cancelled=is_cancelled,
        resolver=resolver,
    )
    lowered = body[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_FEED_INVALID", "RSS/Atom 内容包含不受支持的实体声明。")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_FEED_INVALID", "RSS/Atom 内容无法解析。") from exc
    entries: list[dict[str, object]] = []
    candidates = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    for node in candidates[:limit]:
        title = _xml_child_text(node, "title")
        link, links, enclosures = _rss_entry_links(node, source_url)
        published = (
            _xml_child_text(node, "pubDate")
            or _xml_child_text(node, "published")
            or _xml_child_text(node, "updated")
        )
        summary = _xml_child_text(node, "description") or _xml_child_text(node, "summary") or _xml_child_text(node, "content")
        item = {
            "title": title[:_MAX_ITEM_CHARS],
            "url": link[:2048],
            "published_at": published[:128],
            "summary": summary[:_MAX_ITEM_CHARS],
        }
        entry_id = _xml_child_text(node, "id") or _xml_child_text(node, "guid")
        if entry_id:
            item["id"] = entry_id[:2048]
        author = _rss_entry_author(node)
        if author:
            item["author"] = author[:_MAX_ITEM_CHARS]
        if len(links) > 1 or any(value["rel"] != "alternate" for value in links):
            item["links"] = links[:20]
            if len(links) > 20:
                item["links_truncated"] = True
        if enclosures:
            item["enclosures"] = enclosures[:20]
            if len(enclosures) > 20:
                item["enclosures_truncated"] = True
        item.update(_rss_entry_content(node, source_url))
        if len(summary) > _MAX_ITEM_CHARS:
            item["summary_truncated"] = True
        if any(str(value or "").strip() for value in item.values()):
            entries.append(item)
    known_set = set(known_ids)
    cursor_found = not cursor
    new_entries: list[dict[str, object]] = []
    deduped_count = 0
    for entry in entries:
        entry_key = _rss_entry_key(entry)
        if cursor and entry_key == cursor:
            cursor_found = True
            break
        if entry_key in known_set:
            deduped_count += 1
            continue
        new_entries.append(entry)
    result = _collection_result(
        collection_key="entries",
        items=entries,
        common={
            "url": source_url,
            "provider": "RSS/Atom",
            "retrieved_at": _retrieved_at(),
        },
    )
    result.update(
        {
            "new_entries": new_entries,
            "new_count": len(new_entries),
            "deduped_count": deduped_count,
            "cursor": cursor,
            "cursor_found": cursor_found,
            "next_cursor": _rss_entry_key(entries[0]) if entries else cursor,
            "cursor_kind": "entry_key",
        }
    )
    if cursor and not cursor_found:
        result["cursor_miss"] = True
    return result


def _rss_entry_author(node: ElementTree.Element) -> str:
    """读取 Atom author/name、RSS author 或 Dublin Core creator，不把 URI 拼成作者名。"""

    for child in node:
        if _local_name(child.tag) == "author":
            return _xml_child_text(child, "name") or " ".join("".join(child.itertext()).split())
    return _xml_child_text(node, "creator")


def _rss_entry_links(
    node: ElementTree.Element, source_url: str,
) -> tuple[str, list[dict[str, str]], list[dict[str, object]]]:
    """解析文章、self 与附件链接，优先 alternate，并拒绝有凭据或非 HTTP 的地址。"""

    links: list[dict[str, str]] = []
    enclosures: list[dict[str, object]] = []
    base = _rss_link_url(
        source_url, node.get("{http://www.w3.org/XML/1998/namespace}base", ""),
    )
    for child in node:
        kind = _local_name(child.tag)
        if kind not in {"link", "enclosure"}:
            continue
        raw_url = child.get("href") or child.get("url") or (child.text or "").strip()
        if not raw_url:
            continue
        url = _rss_link_url(base, raw_url)
        if not url:
            continue
        rel = "enclosure" if kind == "enclosure" else str(child.get("rel") or "alternate")
        links.append({"rel": rel[:128], "url": url})
        if rel == "enclosure":
            length = str(child.get("length") or "")
            enclosures.append({
                "url": url,
                "type": str(child.get("type") or "")[:200],
                "length": (
                    int(length) if length.isascii() and length.isdigit() and len(length) <= 18
                    else None
                ),
            })
    article = next((item["url"] for item in links if item["rel"] == "alternate"), "")
    return article, links, enclosures


def _rss_link_url(base: str, value: str) -> str:
    """解析可选 RSS/Atom 关联地址；畸形 URL 仅丢弃该地址，不让有效条目整体失败。"""

    try:
        return _search_source_url(urljoin(base, value))
    except ValueError:
        return ""


def _rss_entry_content(node: ElementTree.Element, source_url: str) -> dict[str, object]:
    """保留 RSS encoded 或 Atom content 的原始段落，记录类型、外部引用与截断状态。"""

    candidates = [child for child in node if _local_name(child.tag) in {"encoded", "content"}]
    if not candidates:
        return {}
    element = next(
        (child for child in candidates if _local_name(child.tag) == "encoded"), candidates[0],
    )
    content = (
        (element.text or "")
        + "".join(ElementTree.tostring(child, encoding="unicode") for child in element)
    ).strip()
    result: dict[str, object] = {
        "content": content[:_MAX_RESULT_CHARS],
        "content_type": str(element.get("type") or (
            "text/html" if _local_name(element.tag) == "encoded" else "text"
        ))[:200],
        "content_truncated": len(content) > _MAX_RESULT_CHARS,
    }
    if element.get("src"):
        result["content_url"] = _rss_link_url(source_url, element.get("src", ""))
        result["content_result_state"] = "success_with_data" if content else "not_read"
    return result


def _rss_entry_key(item: Mapping[str, object]) -> str:
    """为 RSS 条目生成跨轮稳定标识，优先使用源标识或文章 URL。"""

    for key in ("id", "url"):
        value = str(item.get(key) or "").strip()
        if value:
            return value[:2048]
    identity = "\x1f".join(
        str(item.get(key) or "").strip()
        for key in ("title", "published_at", "summary")
    )
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _v2ex_topics(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """读取公开主题并在 topic 模式追加有界多页回复，明确披露子资源失败和截断。"""

    mode = _bounded_string(arguments.get("mode"), "mode", 16)
    if mode not in {"hot", "node", "topic"}:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "V2EX mode 必须是 hot、node 或 topic。")
    limit = _bounded_int(arguments.get("limit", 10), default=10, minimum=1, maximum=30)
    page_count = _bounded_int(arguments.get("page_count", 1), default=1, minimum=1, maximum=5)
    if mode == "hot":
        paths = ["/api/topics/hot.json"]
    elif mode == "node":
        node_name = _required_string(arguments, "node_name", max_length=64)
        paths = [
            f"/api/topics/show.json?{urlencode({'node_name': node_name, 'page': page})}"
            for page in range(1, page_count + 1)
        ]
    else:
        topic_id = _bounded_int(arguments.get("topic_id"), default=0, minimum=1, maximum=2_147_483_647)
        paths = [f"/api/topics/show.json?{urlencode({'id': topic_id})}"]
    raw_items: list[object] = []
    pages_fetched = 0
    for path in paths:
        body = _http_get_bytes(
            _V2EX_BASE_URL + path,
            settings=settings,
            headers={"Accept": "application/json", "User-Agent": "agent-reach/1.0"},
            is_cancelled=is_cancelled,
            resolver=resolver,
        )
        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "V2EX 返回内容无法解析。",
            ) from exc
        page_items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
        raw_items.extend(page_items)
        pages_fetched += 1
        if mode == "topic" or len(raw_items) >= limit or len(page_items) == 0:
            break
    items = []
    seen_ids: set[int] = set()
    for raw_item in raw_items[:limit]:
        if not isinstance(raw_item, dict):
            continue
        item = _normalize_v2ex_item(raw_item)
        item_id = item.get("id")
        if isinstance(item_id, int) and item_id in seen_ids:
            continue
        if isinstance(item_id, int):
            seen_ids.add(item_id)
        if any(
            item.get(key) not in (None, "")
            for key in ("id", "title", "url", "node_name", "node_title", "content")
        ):
            items.append(item)
    result = _collection_result(
        collection_key="items",
        items=items,
        common={
            "mode": mode,
            "provider": "V2EX public API",
            "retrieved_at": _retrieved_at(),
            "pages_requested": 1 if mode in {"hot", "topic"} else page_count,
            "pages_fetched": pages_fetched,
        },
    )
    if mode == "topic" and items:
        result["partial"] = False
        result["subresource_errors"] = []
        for item in items:
            if item["id"] != topic_id:
                raise VerticalAbilityAdapterError(
                    "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                    "V2EX 返回主题标识与请求不一致，不能关联回复。",
                )
            replies_base_url = _V2EX_BASE_URL + "/api/replies/show.json"
            replies_url = replies_base_url + (
                f"?{urlencode({'topic_id': topic_id, 'page': 1})}"
            )
            item.update({
                "replies_url": replies_url,
                "replies_page": 1,
                "replies_pages_requested": page_count,
                "replies_pages_fetched": 0,
                "reply_items": [],
                "reply_items_count": 0,
                "reply_items_result_state": "error",
                "partial": bool(item.get("content_truncated")),
                "subresource_errors": [],
            })
            reply_items: list[dict[str, object]] = []
            reply_ids: set[int] = set()
            truncated = False
            for reply_page in range(1, page_count + 1):
                current_url = replies_base_url + (
                    f"?{urlencode({'topic_id': topic_id, 'page': reply_page})}"
                )
                remaining = max(1, limit - len(reply_items))
                try:
                    page_items, page_truncated = _v2ex_topic_replies(
                        current_url,
                        limit=remaining,
                        # 主题总回复数要在所有已请求页合并后比较；将总数传给单页
                        # helper 会把“本页尚未覆盖总数”误报成单页截断。
                        reported_count=None,
                        settings=settings,
                        is_cancelled=is_cancelled,
                        resolver=resolver,
                    )
                except VerticalAbilityAdapterError as exc:
                    error = _subresource_error("v2ex.replies", current_url, exc)
                    item["partial"] = True
                    item["subresource_errors"].append(error)
                    result["partial"] = True
                    result["subresource_errors"].append(error)
                    break
                item["replies_pages_fetched"] = int(item["replies_pages_fetched"]) + 1
                truncated = truncated or page_truncated
                for reply in page_items:
                    reply_id = reply.get("id")
                    if isinstance(reply_id, int) and reply_id in reply_ids:
                        continue
                    if isinstance(reply_id, int):
                        reply_ids.add(reply_id)
                    reply_items.append(reply)
                if len(reply_items) >= limit or not page_items:
                    break
            reported_count = item.get("replies")
            truncated = truncated or (
                isinstance(reported_count, int) and reported_count > len(reply_items)
            )
            item.update({
                "reply_items": reply_items,
                "reply_items_count": len(reply_items),
                "reply_items_result_state": "success_with_data" if reply_items else (
                    "error" if item["subresource_errors"] else "no_results"
                ),
                "reply_items_truncated": truncated,
            })
            item["partial"] = bool(item["partial"] or truncated)
            result["partial"] = bool(result["partial"] or item["partial"])
    return result


def _v2ex_user(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """读取 V2EX 公开用户资料，作为主题作者的独立关联证据。"""

    username = _required_string(arguments, "username", max_length=64)
    url = _V2EX_BASE_URL + f"/api/members/show.json?{urlencode({'username': username})}"
    body = _http_get_bytes(
        url,
        settings=settings,
        headers={"Accept": "application/json", "User-Agent": "agent-reach/1.0"},
        is_cancelled=is_cancelled,
        resolver=resolver,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "V2EX 用户接口返回内容无法解析。",
        ) from exc
    if not isinstance(payload, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "V2EX 用户接口没有返回对象。",
        )
    user = _normalize_v2ex_member(payload)
    if not user.get("username"):
        return {
            "username": username,
            "user": {},
            "result_state": "no_results",
            "provider": "V2EX public API",
            "retrieved_at": _retrieved_at(),
        }
    return {
        "username": username,
        "user": user,
        "result_state": "success_with_data",
        "provider": "V2EX public API",
        "retrieved_at": _retrieved_at(),
    }


def _v2ex_topic_replies(
    url: str,
    *,
    limit: int,
    reported_count: object,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> tuple[list[dict[str, object]], bool]:
    """读取固定主题回复第一页，校验条目并保留正文、作者及数量或正文截断事实。"""

    raise_if_cancelled(is_cancelled)
    body = _http_get_bytes(
        url,
        settings=settings,
        headers={"Accept": "application/json", "User-Agent": "agent-reach/1.0"},
        is_cancelled=is_cancelled,
        resolver=resolver,
    )
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "V2EX 回复接口返回内容无法解析。",
        ) from exc
    if not isinstance(raw, list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "V2EX 回复接口没有返回条目列表。",
        )
    replies: list[dict[str, object]] = []
    truncated = len(raw) > limit
    for value in raw[:limit]:
        if (
            not isinstance(value, Mapping)
            or type(value.get("id")) is not int
            or value["id"] <= 0
            or not isinstance(value.get("content"), str)
        ):
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "V2EX 回复条目缺少有效标识或正文。",
            )
        content = value["content"]
        member = _normalize_v2ex_member(value.get("member"))
        reply = {
            "id": value["id"],
            "content": content[:_MAX_ITEM_CHARS],
            "content_truncated": len(content) > _MAX_ITEM_CHARS,
            "author": member.get("username", ""),
            "member": member,
            "created": value.get("created"),
        }
        truncated = truncated or bool(reply["content_truncated"])
        replies.append(reply)
    if type(reported_count) is int and reported_count > len(replies):
        truncated = True
    return replies, truncated


def _subresource_error(
    resource: str, url: str, error: VerticalAbilityAdapterError,
) -> dict[str, object]:
    """记录已知子资源地址和稳定错误分类，避免把失败包装为空集合成功。"""

    return {
        "resource": resource,
        "url": url,
        "code": error.code,
        "message": error.message[:_MAX_ITEM_CHARS],
        "status_code": error.status_code,
        "retrieved_at": _retrieved_at(),
    }


def _github_public_repo(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    github_credentials: Mapping[str, str] | None = None,
    jina_api_key: str | None = None,
) -> dict[str, object]:
    """读取仓库、Issue 与 README；保留旧字段并单独披露 README 缺失或部分失败。"""

    owner, repository, repository_url = _parse_github_repository(arguments.get("repository"))
    issue_limit = _bounded_int(
        arguments.get("issue_limit", 10),
        default=10,
        minimum=1,
        maximum=30,
    )
    repository_path = f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    try:
        repository_payload = _github_api_json(
            repository_path,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            credentials=github_credentials,
        )
    except VerticalAbilityAdapterError as exc:
        if _github_public_fallback_allowed(github_credentials, exc):
            return _github_public_repo_via_jina(
                owner=owner,
                repository=repository,
                repository_url=repository_url,
                issue_limit=issue_limit,
                settings=settings,
                is_cancelled=is_cancelled,
                resolver=resolver,
                jina_api_key=jina_api_key,
            )
        raise
    if not isinstance(repository_payload, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub 仓库接口返回了无法识别的内容。",
        )
    try:
        issues_payload = _github_api_json(
            f"{repository_path}/issues?{urlencode({'state': 'all', 'per_page': min(issue_limit * 2, 50), 'page': 1})}",
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            credentials=github_credentials,
        )
    except VerticalAbilityAdapterError as exc:
        if _github_public_fallback_allowed(github_credentials, exc):
            return _github_public_repo_via_jina(
                owner=owner,
                repository=repository,
                repository_url=repository_url,
                issue_limit=issue_limit,
                settings=settings,
                is_cancelled=is_cancelled,
                resolver=resolver,
                jina_api_key=jina_api_key,
            )
        raise
    if not isinstance(issues_payload, list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub Issue 接口返回了无法识别的内容。",
        )
    issues: list[dict[str, object]] = []
    for item in issues_payload:
        if not isinstance(item, Mapping) or item.get("pull_request"):
            continue
        normalized = _normalize_github_issue(item)
        if str(normalized.get("url") or "").strip():
            issues.append(normalized)
        if len(issues) >= issue_limit:
            break
    provider = "GitHub REST API (authenticated)" if github_credentials else "GitHub public REST API"
    readme, readme_error = _github_repository_readme(
        repository_path,
        repository_url=repository_url,
        provider=provider,
        settings=settings,
        is_cancelled=is_cancelled,
        resolver=resolver,
        credentials=github_credentials,
    )
    repository_result = _normalize_github_repository(repository_payload, repository_url)
    repository_result.update(readme)
    partial = readme_error is not None or bool(readme.get("content_truncated"))
    subresource_errors = [readme_error] if readme_error is not None else []
    repository_result.update({"partial": partial, "subresource_errors": subresource_errors})
    return {
        "repository": repository_result,
        "issues": issues,
        "issues_count": len(issues),
        "issues_result_state": "success_with_data" if issues else "no_results",
        "provider": provider,
        "retrieved_at": _retrieved_at(),
        "partial": partial,
        "subresource_errors": subresource_errors,
    }


def _github_repository_readme(
    repository_path: str,
    *,
    repository_url: str,
    provider: str,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    credentials: Mapping[str, str] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """从固定 README API 取有界原文，404 表示未取得文件，其余错误保留结构化子资源证据。"""

    path = f"{repository_path}/readme"
    url = _GITHUB_API_BASE_URL + path
    result: dict[str, object] = {
        "content": None,
        "content_url": "",
        "content_api_url": url,
        "content_provider": provider,
        "content_truncated": False,
        "readme_result_state": "error",
        "readme_missing": None,
    }
    raise_if_cancelled(is_cancelled)
    try:
        payload = _github_api_json(
            path, settings=settings, is_cancelled=is_cancelled,
            resolver=resolver, credentials=credentials,
        )
        result.update(_normalize_github_readme(payload, repository_url))
    except VerticalAbilityAdapterError as exc:
        result["content_retrieved_at"] = _retrieved_at()
        result["readme_status_code"] = exc.status_code
        if exc.code == "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR" and exc.status_code == 404:
            result["readme_result_state"] = "no_results"
            result["readme_missing"] = True
            return result, None
        return result, _subresource_error("github.readme", url, exc)
    result["content_retrieved_at"] = _retrieved_at()
    result["readme_status_code"] = 200
    return result, None


def _github_issue(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    github_credentials: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """读取单个 GitHub Issue 的正文、评论和作者公开资料。"""

    owner, repository, repository_url = _parse_github_repository(arguments.get("repository"))
    issue_number = _bounded_int(
        arguments.get("issue_number"), default=0, minimum=1, maximum=2_147_483_647,
    )
    comments_limit = _bounded_int(
        arguments.get("comments_limit", 20), default=20, minimum=1, maximum=50,
    )
    comments_page_count = _bounded_int(
        arguments.get("comments_page_count", 1), default=1, minimum=1, maximum=5,
    )
    include_author_profile = _argument_bool(
        arguments.get("include_author_profile"), "include_author_profile", default=True,
    )
    repository_path = f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    detail_url = f"{repository_url}/issues/{issue_number}"
    payload = _github_api_json(
        f"{repository_path}/issues/{issue_number}",
        settings=settings,
        is_cancelled=is_cancelled,
        resolver=resolver,
        credentials=github_credentials,
    )
    if not isinstance(payload, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub Issue 详情不是对象。",
        )
    if payload.get("pull_request"):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_RESOURCE_KIND_MISMATCH",
            "请求的 GitHub 编号是 Pull Request，请使用 github.pr 能力。",
        )
    issue = _normalize_github_issue(payload)
    issue["detail_url"] = detail_url
    issue["comments_page_count_requested"] = comments_page_count
    issue["comments_items"] = []
    issue["comments_result_state"] = "error"
    issue["comments_pages_fetched"] = 0
    issue["comments_has_more"] = False
    issue["author_profile_result_state"] = "not_requested"
    errors: list[dict[str, object]] = []
    try:
        raw_comments, pagination = _github_paginated(
            f"{repository_path}/issues/{issue_number}/comments",
            limit=comments_limit,
            page_count=comments_page_count,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            credentials=github_credentials,
        )
        comments = [_normalize_github_comment(item) for item in raw_comments]
        issue.update(
            {
                "comments_items": comments,
                "comments_count_fetched": len(comments),
                "comments_result_state": "success_with_data" if comments else "no_results",
                "comments_pages_fetched": pagination["pages_fetched"],
                "comments_has_more": pagination["has_more"],
                "comments_truncated": bool(pagination["has_more"]),
            }
        )
    except VerticalAbilityAdapterError as exc:
        errors.append(_subresource_error("github.issue.comments", detail_url, exc))
    reported_comments = _github_optional_count(payload.get("comments"))
    comments_fetched = int(issue.get("comments_count_fetched") or 0)
    comments_incomplete = (
        reported_comments is not None and reported_comments > comments_fetched
    ) or bool(issue.get("comments_truncated"))
    issue["comments_incomplete"] = comments_incomplete
    if include_author_profile:
        login = str(issue.get("author") or "").strip()
        if login:
            try:
                profile = _github_api_json(
                    f"/users/{quote(login, safe='')}",
                    settings=settings,
                    is_cancelled=is_cancelled,
                    resolver=resolver,
                    credentials=github_credentials,
                )
                if not isinstance(profile, Mapping):
                    raise VerticalAbilityAdapterError(
                        "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                        "GitHub 作者资料不是对象。",
                    )
                issue["author_profile"] = _normalize_github_user(profile)
                issue["author_profile_result_state"] = "success_with_data"
            except VerticalAbilityAdapterError as exc:
                issue["author_profile_result_state"] = "error"
                errors.append(_subresource_error("github.issue.author_profile", detail_url, exc))
        else:
            issue["author_profile_result_state"] = "no_results"
    partial = bool(errors or comments_incomplete)
    provider = (
        "GitHub REST API (authenticated)" if github_credentials else "GitHub public REST API"
    )
    return {
        "repository": _github_repository_identity(owner, repository, repository_url),
        "issue": issue,
        "provider": provider,
        "retrieved_at": _retrieved_at(),
        "partial": partial,
        "subresource_errors": errors,
    }


def _github_pull_request(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    github_credentials: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """读取单个 GitHub Pull Request 的正文、讨论、Review、文件和作者资料。"""

    owner, repository, repository_url = _parse_github_repository(arguments.get("repository"))
    pr_number = _bounded_int(
        arguments.get("pr_number"), default=0, minimum=1, maximum=2_147_483_647,
    )
    comments_limit = _bounded_int(
        arguments.get("comments_limit", 20), default=20, minimum=1, maximum=50,
    )
    reviews_limit = _bounded_int(
        arguments.get("reviews_limit", 20), default=20, minimum=1, maximum=50,
    )
    files_limit = _bounded_int(
        arguments.get("files_limit", 50), default=50, minimum=1, maximum=100,
    )
    page_count = _bounded_int(
        arguments.get("page_count", 1), default=1, minimum=1, maximum=5,
    )
    include_author_profile = _argument_bool(
        arguments.get("include_author_profile"), "include_author_profile", default=True,
    )
    repository_path = f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    detail_url = f"{repository_url}/pull/{pr_number}"
    payload = _github_api_json(
        f"{repository_path}/pulls/{pr_number}",
        settings=settings,
        is_cancelled=is_cancelled,
        resolver=resolver,
        credentials=github_credentials,
    )
    if not isinstance(payload, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub Pull Request 详情不是对象。",
        )
    pull_request = _normalize_github_pull_request(payload)
    errors: list[dict[str, object]] = []
    collections: tuple[tuple[str, str, int, Callable[[Mapping[str, object]], dict[str, object]]], ...] = (
        (
            "comment_items",
            f"{repository_path}/issues/{pr_number}/comments",
            comments_limit,
            _normalize_github_comment,
        ),
        (
            "review_items",
            f"{repository_path}/pulls/{pr_number}/reviews",
            reviews_limit,
            _normalize_github_review,
        ),
        (
            "review_comment_items",
            f"{repository_path}/pulls/{pr_number}/comments",
            comments_limit,
            _normalize_github_comment,
        ),
        (
            "file_items",
            f"{repository_path}/pulls/{pr_number}/files",
            files_limit,
            _normalize_github_file,
        ),
    )
    for field, path, limit, normalizer in collections:
        try:
            raw_items, pagination = _github_paginated(
                path,
                limit=limit,
                page_count=page_count,
                settings=settings,
                is_cancelled=is_cancelled,
                resolver=resolver,
                credentials=github_credentials,
            )
        except VerticalAbilityAdapterError as exc:
            pull_request[f"{field}_result_state"] = "error"
            errors.append(_subresource_error(f"github.pr.{field}", detail_url, exc))
            continue
        items = [normalizer(item) for item in raw_items]
        pull_request[field] = items
        pull_request[f"{field}_count"] = len(items)
        pull_request[f"{field}_result_state"] = "success_with_data" if items else "no_results"
        pull_request[f"{field}_pages_fetched"] = pagination["pages_fetched"]
        pull_request[f"{field}_has_more"] = pagination["has_more"]
        pull_request[f"{field}_truncated"] = bool(pagination["has_more"])
    author_profile_state = "not_requested"
    if include_author_profile:
        login = str(pull_request.get("author") or "").strip()
        if login:
            try:
                profile = _github_api_json(
                    f"/users/{quote(login, safe='')}",
                    settings=settings,
                    is_cancelled=is_cancelled,
                    resolver=resolver,
                    credentials=github_credentials,
                )
                if not isinstance(profile, Mapping):
                    raise VerticalAbilityAdapterError(
                        "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                        "GitHub Pull Request 作者资料不是对象。",
                    )
                pull_request["author_profile"] = _normalize_github_user(profile)
                author_profile_state = "success_with_data"
            except VerticalAbilityAdapterError as exc:
                author_profile_state = "error"
                errors.append(_subresource_error("github.pr.author_profile", detail_url, exc))
        else:
            author_profile_state = "no_results"
    pull_request["author_profile_result_state"] = author_profile_state
    partial = bool(
        errors
        or any(
            bool(pull_request.get(f"{field}_truncated"))
            for field, _path, _limit, _normalizer in collections
        )
    )
    provider = (
        "GitHub REST API (authenticated)" if github_credentials else "GitHub public REST API"
    )
    return {
        "repository": _github_repository_identity(owner, repository, repository_url),
        "pull_request": pull_request,
        "provider": provider,
        "retrieved_at": _retrieved_at(),
        "partial": partial,
        "subresource_errors": errors,
    }


def _github_releases(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    github_credentials: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """读取 GitHub Release 列表及其资产下载统计，明确公开分页边界。"""

    owner, repository, repository_url = _parse_github_repository(arguments.get("repository"))
    release_limit = _bounded_int(
        arguments.get("release_limit", 10), default=10, minimum=1, maximum=20,
    )
    page_count = _bounded_int(
        arguments.get("page_count", 1), default=1, minimum=1, maximum=5,
    )
    repository_path = f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    errors: list[dict[str, object]] = []
    try:
        raw_releases, pagination = _github_paginated(
            f"{repository_path}/releases",
            limit=release_limit,
            page_count=page_count,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            credentials=github_credentials,
        )
    except VerticalAbilityAdapterError as exc:
        errors.append(_subresource_error("github.releases", repository_url, exc))
        raw_releases = []
        pagination = {"pages_requested": page_count, "pages_fetched": 0, "has_more": False}
    releases = [_normalize_github_release(item) for item in raw_releases]
    provider = (
        "GitHub REST API (authenticated)" if github_credentials else "GitHub public REST API"
    )
    partial = bool(errors or pagination["has_more"])
    return {
        "repository": _github_repository_identity(owner, repository, repository_url),
        "releases": releases,
        "count": len(releases),
        "result_state": "success_with_data" if releases else "no_results",
        "pages_requested": pagination["pages_requested"],
        "pages_fetched": pagination["pages_fetched"],
        "has_more": pagination["has_more"],
        "provider": provider,
        "retrieved_at": _retrieved_at(),
        "partial": partial,
        "subresource_errors": errors,
    }


def _github_paginated(
    path: str,
    *,
    limit: int,
    page_count: int,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    credentials: Mapping[str, str] | None,
) -> tuple[list[Mapping[str, object]], dict[str, object]]:
    """在固定页数和条数内读取 GitHub 列表，并把未读完状态显式返回。"""

    per_page = min(max(limit, 1), 100)
    items: list[Mapping[str, object]] = []
    pages_fetched = 0
    has_more = False
    for page in range(1, page_count + 1):
        separator = "&" if "?" in path else "?"
        payload = _github_api_json(
            f"{path}{separator}{urlencode({'per_page': per_page, 'page': page})}",
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            credentials=credentials,
        )
        if not isinstance(payload, list):
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub 列表接口没有返回数组。",
            )
        pages_fetched += 1
        valid_page = [item for item in payload if isinstance(item, Mapping)]
        remaining = max(0, limit - len(items))
        items.extend(valid_page[:remaining])
        if len(payload) >= per_page:
            has_more = True
        if len(items) >= limit:
            break
        if len(payload) < per_page:
            has_more = False
            break
    return items[:limit], {
        "pages_requested": page_count,
        "pages_fetched": pages_fetched,
        "has_more": has_more,
    }


def _github_repository_identity(owner: str, repository: str, repository_url: str) -> dict[str, object]:
    """构造详情能力共用的仓库身份，不把用户输入扩大为外部地址。"""

    return {
        "owner": owner,
        "name": repository,
        "full_name": f"{owner}/{repository}",
        "url": repository_url,
    }


def _github_optional_string(item: Mapping[str, object], key: str, *, limit: int = _MAX_ITEM_CHARS) -> str | None:
    """读取 GitHub 可选字符串并把空值保持为缺失语义。"""

    value = item.get(key)
    if not isinstance(value, str):
        return None
    return value[:limit]


def _normalize_github_user(item: Mapping[str, object]) -> dict[str, object]:
    """保留 GitHub 作者的公开资料字段，不回传邮箱、Token 或任意嵌套对象。"""

    result: dict[str, object] = {
        "login": str(item.get("login") or "")[:100],
        "name": _github_optional_string(item, "name", limit=200),
        "url": str(item.get("html_url") or "")[:2048],
        "avatar_url": str(item.get("avatar_url") or "")[:2048],
        "bio": _github_optional_string(item, "bio"),
        "company": _github_optional_string(item, "company", limit=200),
        "location": _github_optional_string(item, "location", limit=200),
        "blog": _github_optional_string(item, "blog", limit=2048),
        "public_repos": _github_optional_count(item.get("public_repos")),
        "followers": _github_optional_count(item.get("followers")),
        "following": _github_optional_count(item.get("following")),
        "created_at": str(item.get("created_at") or "")[:64],
        "updated_at": str(item.get("updated_at") or "")[:64],
    }
    return result


def _normalize_github_comment(item: Mapping[str, object]) -> dict[str, object]:
    """归一化 GitHub Issue/Review 评论正文、作者、时间和来源 URL。"""

    user = item.get("user") if isinstance(item.get("user"), Mapping) else {}
    body = item.get("body") if isinstance(item.get("body"), str) else None
    return {
        "id": _github_optional_count(item.get("id")),
        "url": str(item.get("html_url") or item.get("url") or "")[:2048],
        "author": str(user.get("login") or "")[:100],
        "body": body[:_MAX_ITEM_CHARS] if body is not None else None,
        "body_truncated": body is not None and len(body) > _MAX_ITEM_CHARS,
        "created_at": str(item.get("created_at") or "")[:64],
        "updated_at": str(item.get("updated_at") or "")[:64],
        "author_association": str(item.get("author_association") or "")[:64],
    }


def _normalize_github_review(item: Mapping[str, object]) -> dict[str, object]:
    """归一化 GitHub Pull Request Review 的状态、正文、作者和提交身份。"""

    result = _normalize_github_comment(item)
    result.update(
        {
            "state": str(item.get("state") or "")[:32],
            "submitted_at": str(item.get("submitted_at") or "")[:64],
            "commit_id": str(item.get("commit_id") or "")[:128],
        }
    )
    return result


def _normalize_github_file(item: Mapping[str, object]) -> dict[str, object]:
    """归一化 Pull Request 文件变更统计和同仓库 blob 来源。"""

    return {
        "filename": str(item.get("filename") or "")[:2048],
        "status": str(item.get("status") or "")[:32],
        "additions": _github_optional_count(item.get("additions")),
        "deletions": _github_optional_count(item.get("deletions")),
        "changes": _github_optional_count(item.get("changes")),
        "url": str(item.get("blob_url") or "")[:2048],
        "patch": _github_optional_string(item, "patch"),
    }


def _normalize_github_pull_request(item: Mapping[str, object]) -> dict[str, object]:
    """在 Issue 通用正文基础上保留 Pull Request 分支、合并和变更事实。"""

    result = _normalize_github_issue(item)
    head = item.get("head") if isinstance(item.get("head"), Mapping) else {}
    base = item.get("base") if isinstance(item.get("base"), Mapping) else {}
    result.update(
        {
            "draft": item.get("draft") if isinstance(item.get("draft"), bool) else None,
            "merged": item.get("merged") if isinstance(item.get("merged"), bool) else None,
            "mergeable": item.get("mergeable") if isinstance(item.get("mergeable"), bool) else None,
            "merged_at": str(item.get("merged_at") or "")[:64],
            "closed_at": str(item.get("closed_at") or "")[:64],
            "head": {
                "ref": str(head.get("ref") or "")[:256],
                "sha": str(head.get("sha") or "")[:128],
                "repo": str((head.get("repo") or {}).get("full_name") or "")[:300]
                if isinstance(head.get("repo"), Mapping)
                else "",
            },
            "base": {
                "ref": str(base.get("ref") or "")[:256],
                "sha": str(base.get("sha") or "")[:128],
                "repo": str((base.get("repo") or {}).get("full_name") or "")[:300]
                if isinstance(base.get("repo"), Mapping)
                else "",
            },
            "commits": _github_optional_count(item.get("commits")),
            "changed_files": _github_optional_count(item.get("changed_files")),
            "additions": _github_optional_count(item.get("additions")),
            "deletions": _github_optional_count(item.get("deletions")),
            "review_comments": _github_optional_count(item.get("review_comments")),
        }
    )
    return result


def _normalize_github_release(item: Mapping[str, object]) -> dict[str, object]:
    """归一化 Release 版本说明和有界资产下载统计。"""

    author = item.get("author") if isinstance(item.get("author"), Mapping) else {}
    body = item.get("body") if isinstance(item.get("body"), str) else None
    raw_assets = item.get("assets") if isinstance(item.get("assets"), list) else []
    assets = [
        {
            "name": str(asset.get("name") or "")[:512],
            "url": str(asset.get("browser_download_url") or "")[:2048],
            "content_type": str(asset.get("content_type") or "")[:200],
            "size": _github_optional_count(asset.get("size")),
            "download_count": _github_optional_count(asset.get("download_count")),
            "created_at": str(asset.get("created_at") or "")[:64],
            "updated_at": str(asset.get("updated_at") or "")[:64],
        }
        for asset in raw_assets[:100]
        if isinstance(asset, Mapping)
    ]
    return {
        "id": _github_optional_count(item.get("id")),
        "tag_name": str(item.get("tag_name") or "")[:256],
        "name": str(item.get("name") or "")[:_MAX_ITEM_CHARS],
        "url": str(item.get("html_url") or "")[:2048],
        "author": str(author.get("login") or "")[:100],
        "body": body[:_MAX_RESULT_CHARS] if body is not None else None,
        "body_truncated": body is not None and len(body) > _MAX_RESULT_CHARS,
        "draft": item.get("draft") if isinstance(item.get("draft"), bool) else None,
        "prerelease": item.get("prerelease") if isinstance(item.get("prerelease"), bool) else None,
        "created_at": str(item.get("created_at") or "")[:64],
        "published_at": str(item.get("published_at") or "")[:64],
        "assets": assets,
        "assets_count": len(assets),
        "assets_truncated": len(raw_assets) > len(assets),
    }


def _normalize_github_readme(payload: object, repository_url: str) -> dict[str, object]:
    """校验 GitHub JSON README 的文件类型、Base64、UTF-8、大小及同仓库来源后返回原文。"""

    if (
        not isinstance(payload, Mapping)
        or payload.get("type") != "file"
        or payload.get("encoding") != "base64"
        or not isinstance(payload.get("content"), str)
        or type(payload.get("size")) is not int
        or payload["size"] < 0
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub README 未返回有效的编码文件内容。",
        )
    if payload["size"] > _MAX_HTTP_BYTES or len(payload["content"]) > _MAX_HTTP_BYTES:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_RESPONSE_TOO_LARGE", "GitHub README 超过安全大小上限。",
        )
    source_url = _github_readme_source_url(payload, repository_url)
    try:
        decoded = base64.b64decode(re.sub(r"\s+", "", payload["content"]), validate=True)
        content = decoded.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub README 编码或正文字符无效。",
        ) from exc
    if len(decoded) != payload["size"]:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub README 实际正文长度与文件大小不一致。",
        )
    return {
        "content": content[:_MAX_RESULT_CHARS],
        "content_url": source_url,
        "content_truncated": len(content) > _MAX_RESULT_CHARS,
        "readme_result_state": "success_with_data" if content.strip() else "no_results",
        "readme_missing": False,
        "readme_path": payload["path"],
        "readme_sha": str(payload.get("sha") or "")[:128],
    }


def _github_readme_source_url(payload: Mapping[str, object], repository_url: str) -> str:
    """只认可同仓库且与 README 路径匹配的 GitHub blob 来源，不跟随任意 download_url。"""

    path = payload.get("path")
    source_url = _search_source_url(payload.get("html_url"))
    valid = isinstance(path, str) and 0 < len(path) <= 2048
    if valid:
        valid = "\\" not in path and not any(
            part in {"", ".", ".."} for part in path.split("/")
        )
    try:
        parsed = urlsplit(source_url)
        prefix = urlsplit(repository_url).path + "/blob/"
        decoded_path = unquote(parsed.path)
        valid = bool(
            valid
            and parsed.scheme == "https"
            and parsed.hostname == _GITHUB_REPOSITORY_HOST
            and parsed.port in (None, 443)
            and not parsed.query
            and not parsed.fragment
            and decoded_path[:len(prefix)].casefold() == prefix.casefold()
            and decoded_path.endswith("/" + path)
            and len(decoded_path) > len(prefix) + len(path) + 1
            and not any(part in {"", ".", ".."} for part in decoded_path[1:].split("/"))
        )
    except ValueError:
        valid = False
    if not valid:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "GitHub README 来源与请求仓库或文件路径不一致。",
        )
    return source_url


def _github_public_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    *,
    github_credentials: Mapping[str, str] | None = None,
    jina_api_key: str | None = None,
) -> dict[str, object]:
    """通过 GitHub 仓库搜索接口返回有界来源，存在用户连接时支持授权范围。"""

    query = _required_string(arguments, "query", max_length=256)
    limit = _bounded_int(arguments.get("num_results", 5), default=5, minimum=1, maximum=20)
    try:
        payload = _github_api_json(
            f"/search/repositories?{urlencode({'q': query, 'per_page': limit, 'page': 1})}",
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            credentials=github_credentials,
        )
    except VerticalAbilityAdapterError as exc:
        if _github_public_fallback_allowed(github_credentials, exc):
            return _github_public_search_via_jina(
                query=query,
                limit=limit,
                settings=settings,
                is_cancelled=is_cancelled,
                resolver=resolver,
                jina_api_key=jina_api_key,
            )
        raise
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub 搜索接口返回了无法识别的内容。",
        )
    results: list[dict[str, object]] = []
    for item in payload["items"][:limit]:
        if not isinstance(item, Mapping):
            continue
        normalized = _normalize_github_search_result(item)
        if str(normalized.get("url") or "").strip():
            results.append(normalized)
    return _collection_result(
        collection_key="results",
        items=results,
        common={
            "query": query,
            "provider": "GitHub REST API (authenticated)" if github_credentials else "GitHub public REST API",
            "retrieved_at": _retrieved_at(),
        },
    )


def _youtube_video(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """读取公开视频、字幕时间轴和按需评论，透传真实有界截断标记。"""

    source_url = _required_string(arguments, "url", max_length=2048)
    _validate_youtube_url(source_url, settings, resolver)
    subtitle_languages = _youtube_subtitle_languages(arguments.get("subtitle_languages"))
    include_comments = _argument_bool(
        arguments.get("include_comments"), "include_comments", default=False,
    )
    comments_limit = _bounded_int(
        arguments.get("comments_limit", 20), default=20, minimum=1, maximum=100,
    )
    try:
        capture = read_youtube_video(
            source_url,
            subtitle_languages=subtitle_languages,
            include_comments=include_comments,
            comments_limit=comments_limit,
            timeout_seconds=settings.tool_timeout_seconds,
            is_cancelled=is_cancelled,
        )
    except YouTubeRuntimeError as exc:
        raise VerticalAbilityAdapterError(exc.code, exc.message) from exc
    info = capture.get("info")
    if not isinstance(info, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "YouTube 返回了无法识别的视频信息。",
        )
    subtitle_content = str(capture.get("subtitle_content") or "")[:_MAX_RESULT_CHARS]
    subtitle_language = str(capture.get("subtitle_language") or "")[:32]
    subtitle_result_state = str(capture.get("subtitle_result_state") or "no_results")
    if subtitle_result_state not in {"success_with_data", "no_results"}:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "YouTube 字幕结果状态无法识别。",
        )
    subtitle: dict[str, object] = {
        "language": subtitle_language,
        "content": subtitle_content,
    }
    for key in (
        "timeline", "content_truncated", "timeline_truncated",
        "timeline_invalid_count", "timeline_untimed_count",
    ):
        if f"subtitle_{key}" in capture:
            subtitle[key] = capture[f"subtitle_{key}"]
    if len(str(capture.get("subtitle_content") or "")) > _MAX_RESULT_CHARS:
        subtitle["content_truncated"] = True
    comments = _normalize_youtube_comments(info, source_url, comments_limit)
    if not include_comments:
        comments = {
            "items": [],
            "count": 0,
            "result_state": "not_requested",
            "truncated": False,
        }
    return {
        "url": source_url,
        "video": _normalize_youtube_video(info, source_url),
        "subtitle": subtitle,
        "subtitle_result_state": subtitle_result_state,
        "comments": comments["items"],
        "comments_count": comments["count"],
        "comments_result_state": comments["result_state"],
        "comments_truncated": comments["truncated"],
        "provider": "yt-dlp YouTube Runtime",
        "retrieved_at": _retrieved_at(),
    }


def _bilibili_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """通过 B站公开搜索 API 返回视频来源卡片，不依赖登录态或本机 CLI。"""

    query = _required_string(arguments, "query", max_length=256)
    limit = _bounded_int(arguments.get("num_results", 5), default=5, minimum=1, maximum=20)
    page = _bounded_int(arguments.get("page", 1), default=1, minimum=1, maximum=50)
    payload = _bilibili_api_json(
        f"/x/web-interface/search/all/v2?{urlencode({'keyword': query, 'page': page})}",
        settings=settings,
        is_cancelled=is_cancelled,
        resolver=resolver,
    )
    data = _bilibili_success_data(payload, "B站搜索")
    groups = data.get("result")
    if not isinstance(groups, list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "B站搜索接口没有返回结果分组。",
        )
    results: list[dict[str, object]] = []
    for group in groups:
        if not isinstance(group, Mapping) or str(group.get("result_type") or "") != "video":
            continue
        raw_items = group.get("data")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            normalized = _normalize_bilibili_search_result(item)
            if str(normalized.get("url") or "").strip():
                results.append(normalized)
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    return _collection_result(
        collection_key="videos",
        items=results[:limit],
        common={
            "query": query,
            "page": page,
            "provider": "Bilibili public search API",
            "retrieved_at": _retrieved_at(),
        },
    )


def _bilibili_video(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """读取 B站公开视频详情，并把 BV 号、av 号或完整 URL 规范为来源。"""

    reference = _required_string(arguments, "video", max_length=2048)
    bvid, aid, source_url = _parse_bilibili_video_reference(reference, settings, resolver)
    query = urlencode({"bvid": bvid} if bvid else {"aid": aid})
    payload = _bilibili_api_json(
        f"/x/web-interface/view?{query}",
        settings=settings,
        is_cancelled=is_cancelled,
        resolver=resolver,
    )
    data = _bilibili_success_data(payload, "B站视频详情")
    if not data:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "B站视频详情接口返回了空对象。",
        )
    canonical_bvid = str(data.get("bvid") or bvid or "").strip()
    canonical_aid = data.get("aid", aid)
    canonical_url = (
        f"https://www.bilibili.com/video/{canonical_bvid}"
        if _BILIBILI_BVID_PATTERN.fullmatch(canonical_bvid)
        else source_url
    )
    return {
        "url": source_url,
        "video": _normalize_bilibili_video(data, canonical_url, canonical_aid),
        "provider": "Bilibili public API",
        "retrieved_at": _retrieved_at(),
    }


def _bilibili_api_json(
    path: str,
    *,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
) -> Mapping[str, object]:
    """请求固定 B站公共 API 主机并解析有限 JSON 回执。"""

    api_url = _BILIBILI_API_BASE_URL + path
    try:
        body = _http_get_bytes(
            api_url,
            settings=settings,
            headers=_BILIBILI_API_HEADERS,
            is_cancelled=is_cancelled,
            resolver=resolver,
            allowed_hosts={_BILIBILI_API_HOST},
        )
    except VerticalAbilityAdapterError as exc:
        if exc.code != "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR" or "412" not in exc.message:
            raise
        # Agent-Reach 的 B站公共路径在部分数据中心会对 DoH 直连返回 412，但系统 DNS
        # 提供的 198.18/15 路由实际连接到透明出口。这里只对固定公共 API 主机复用该
        # 已校验的系统路由，不接受用户自定义地址，也不改变其他 Provider 的 DNS 策略。
        body = _http_get_bytes(
            api_url,
            settings=settings,
            headers=_BILIBILI_API_HEADERS,
            is_cancelled=is_cancelled,
            resolver=socket.getaddrinfo,
            allowed_hosts={_BILIBILI_API_HOST},
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "B站公共 API 返回内容无法解析。",
        ) from exc
    if not isinstance(payload, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "B站公共 API 返回了无法识别的对象。",
        )
    return payload


def _bilibili_success_data(payload: Mapping[str, object], operation: str) -> dict[str, object]:
    """校验 B站 code/message/data 三段回执并收敛业务错误。"""

    code = payload.get("code")
    if code != 0:
        raw_message = str(payload.get("message") or "接口未返回成功状态").strip()
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_ERROR",
            f"{operation}暂不可用：{raw_message[:200]}。",
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            f"{operation}接口没有返回对象数据。",
        )
    return dict(data)


def _parse_bilibili_video_reference(
    value: str,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
) -> tuple[str, str, str]:
    """解析 BV 号、av 号或 B站公开视频 URL，并拒绝外部跳转目标。"""

    raw = value.strip()
    if "//" not in raw:
        bvid_match = _BILIBILI_BVID_PATTERN.fullmatch(raw)
        if bvid_match:
            bvid = "BV" + raw[2:]
            return bvid, "", f"https://www.bilibili.com/video/{bvid}"
        aid_match = _BILIBILI_AID_PATTERN.fullmatch(raw)
        if aid_match:
            aid = aid_match.group(1)
            return "", aid, f"https://www.bilibili.com/video/av{aid}"
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "video 必须是 B站 BV 号、av 号或公开视频 HTTPS URL。",
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_URL_INVALID",
            "B站视频 URL 格式无效。",
        ) from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host not in _BILIBILI_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port != 443)
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_URL_INVALID",
            "只允许公开 B站 HTTPS 视频 URL。",
        )
    try:
        prepare_outbound_request(
            raw,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | set(_BILIBILI_HOSTS),
            resolver=resolver,
        )
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() != "video":
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_URL_INVALID",
            "B站视频 URL 必须包含 /video/BV... 或 /video/av...。",
        )
    reference = parts[1]
    bvid_match = _BILIBILI_BVID_PATTERN.fullmatch(reference)
    if bvid_match:
        bvid = "BV" + reference[2:]
        return bvid, "", f"https://www.bilibili.com/video/{bvid}"
    aid_match = _BILIBILI_AID_PATTERN.fullmatch(reference)
    if aid_match:
        aid = aid_match.group(1)
        return "", aid, f"https://www.bilibili.com/video/av{aid}"
    raise VerticalAbilityAdapterError(
        "VERTICAL_ABILITY_ARGUMENTS_INVALID",
        "B站视频 URL 中没有有效的 BV 号或 av 号。",
    )


def _normalize_bilibili_search_result(item: Mapping[str, object]) -> dict[str, object]:
    """将 B站搜索结果压缩成带标题、作者和来源 URL 的卡片。"""

    bvid = str(item.get("bvid") or "").strip()
    aid = str(item.get("aid") or "").strip()
    if _BILIBILI_BVID_PATTERN.fullmatch(bvid):
        url = f"https://www.bilibili.com/video/{bvid}"
    elif aid.isdigit():
        url = f"https://www.bilibili.com/video/av{aid}"
    else:
        url = ""
    return {
        "id": bvid or aid,
        "bvid": bvid,
        "aid": item.get("aid"),
        "title": _clean_bilibili_text(item.get("title"), _MAX_ITEM_CHARS),
        "url": url,
        "author": str(item.get("author") or "")[:200],
        "description": _clean_bilibili_text(item.get("description"), _MAX_ITEM_CHARS),
        "duration": str(item.get("duration") or "")[:32],
        "published_at": _unix_timestamp_text(item.get("pubdate")),
        "view_count": item.get("play"),
        "like_count": item.get("like"),
        "comment_count": item.get("review"),
        "cover": _normalize_bilibili_cover(item.get("pic")),
    }


def _normalize_bilibili_video(
    item: Mapping[str, object],
    fallback_url: str,
    fallback_aid: object,
) -> dict[str, object]:
    """将 B站详情对象压缩成报告需要的公开字段并保留分页摘要。"""

    owner = item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
    stats = item.get("stat") if isinstance(item.get("stat"), Mapping) else {}
    pages = item.get("pages") if isinstance(item.get("pages"), list) else []
    page_items = [
        {
            "page": page.get("page"),
            "part": str(page.get("part") or "")[:_MAX_ITEM_CHARS],
            "duration": page.get("duration"),
            "cid": page.get("cid"),
        }
        for page in pages[:50]
        if isinstance(page, Mapping)
    ]
    bvid = str(item.get("bvid") or "")[:32]
    aid = item.get("aid", fallback_aid)
    return {
        "id": bvid or str(aid or "")[:32],
        "bvid": bvid,
        "aid": aid,
        "title": _clean_bilibili_text(item.get("title"), _MAX_ITEM_CHARS),
        "url": fallback_url,
        "description": _clean_bilibili_text(item.get("desc"), _MAX_ITEM_CHARS),
        "author": str(owner.get("name") or "")[:200],
        "author_id": owner.get("mid"),
        "duration_seconds": item.get("duration"),
        "published_at": _unix_timestamp_text(item.get("pubdate")),
        "cover": _normalize_bilibili_cover(item.get("pic")),
        "view_count": stats.get("view"),
        "like_count": stats.get("like"),
        "comment_count": stats.get("reply"),
        "danmaku_count": stats.get("danmaku"),
        "favorite_count": stats.get("favorite"),
        "pages": page_items,
        "pages_count": len(page_items),
        "subtitle_available": bool(item.get("subtitle")),
    }


def _clean_bilibili_text(value: object, max_length: int) -> str:
    """去除 B站搜索标题中的 HTML 标记并限制外部文本长度。"""

    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", "", text)
    return " ".join(text.split())[:max_length]


def _normalize_bilibili_cover(value: object) -> str:
    """把 B站协议相对封面地址规范成 HTTPS，其他值不进入来源字段。"""

    cover = str(value or "").strip()
    if cover.startswith("//"):
        cover = "https:" + cover
    if cover.startswith("https://"):
        return cover[:2048]
    return ""


def _unix_timestamp_text(value: object) -> str:
    """把 B站 Unix 秒时间戳转为 UTC ISO 文本，异常值返回空。"""

    if value in (None, ""):
        return ""
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return ""
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _youtube_search(
    arguments: dict[str, Any],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    _resolver: AdaptiveDNSResolver,
) -> dict[str, object]:
    """通过项目 YouTube Runtime 搜索公开视频并保留可访问来源 URL。"""

    query = _required_string(arguments, "query", max_length=256)
    limit = _bounded_int(arguments.get("num_results", 5), default=5, minimum=1, maximum=20)
    try:
        raw_results = search_youtube_videos(
            query,
            limit=limit,
            timeout_seconds=settings.tool_timeout_seconds,
            is_cancelled=is_cancelled,
        )
    except YouTubeRuntimeError as exc:
        raise VerticalAbilityAdapterError(exc.code, exc.message) from exc
    results: list[dict[str, object]] = []
    for item in raw_results[:limit]:
        if not isinstance(item, Mapping):
            continue
        normalized = _normalize_youtube_search_result(item)
        if str(normalized.get("url") or "").strip():
            results.append(normalized)
    return _collection_result(
        collection_key="videos",
        items=results,
        common={
            "query": query,
            "provider": "yt-dlp YouTube Runtime",
            "retrieved_at": _retrieved_at(),
        },
    )


def _github_api_json(
    path: str,
    *,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    credentials: Mapping[str, str] | None = None,
) -> object:
    """请求固定 GitHub API 主机并解析有限 JSON，认证信息只来自已验证用户连接。"""

    headers = dict(_GITHUB_API_HEADERS)
    token = str((credentials or {}).get("access_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = _http_get_bytes(
        _GITHUB_API_BASE_URL + path,
        settings=settings,
        headers=headers,
        is_cancelled=is_cancelled,
        resolver=resolver,
        allowed_hosts={_GITHUB_API_HOST},
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub 公共 API 返回内容无法解析。",
        ) from exc


def _github_public_fallback_allowed(
    credentials: Mapping[str, str] | None,
    error: VerticalAbilityAdapterError,
) -> bool:
    """仅在未携带 GitHub 凭据且公共 API 明确返回 403 时启用公开降级。"""

    token = str((credentials or {}).get("access_token") or "").strip()
    return not token and error.code == "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR" and error.status_code == 403


def _effective_jina_api_key(jina_api_key: str | None, settings: Settings) -> str:
    """解析当前调用可用的 Jina Key，兼容数据库注入和环境变量回退。"""

    if jina_api_key is not None:
        return str(jina_api_key).strip()
    return str(getattr(settings, "vertical_ability_jina_api_key", "") or "").strip()


def _github_public_repo_via_jina(
    *,
    owner: str,
    repository: str,
    repository_url: str,
    issue_limit: int,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    jina_api_key: str | None,
) -> dict[str, object]:
    """在 GitHub 公共 REST 匿名配额耗尽时，经 Jina 读取公开仓库和 Issues 页面。"""

    key = _effective_jina_api_key(jina_api_key, settings)
    if not key:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RATE_LIMITED",
            "GitHub 公共 API 匿名配额已耗尽，请配置 GitHub Token 或 Jina API Key。",
            status_code=403,
        )
    try:
        repository_payload = _github_repository_page_payload(
            repository_url=repository_url,
            owner=owner,
            repository=repository,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            jina_api_key=key,
        )
        issues_payload = _github_issues_from_page(
            repository_url=repository_url,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            jina_api_key=key,
        )
    except VerticalAbilityAdapterError as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RATE_LIMITED",
            "GitHub 公共 API 匿名配额已耗尽，Jina Reader 降级读取失败。",
            status_code=exc.status_code,
        ) from exc
    if not isinstance(repository_payload, Mapping) or not str(repository_payload.get("full_name") or "").strip():
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub Jina 降级仓库回执无法识别。",
        )
    if not isinstance(issues_payload, list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub Jina 降级 Issue 回执无法识别。",
        )
    issues: list[dict[str, object]] = []
    for item in issues_payload:
        if not isinstance(item, Mapping) or item.get("pull_request"):
            continue
        normalized = _normalize_github_issue(item)
        if str(normalized.get("url") or "").strip():
            issues.append(normalized)
        if len(issues) >= issue_limit:
            break
    return {
        "repository": _normalize_github_repository(repository_payload, repository_url),
        "issues": issues,
        "issues_count": len(issues),
        "issues_result_state": "success_with_data" if issues else "no_results",
        "provider": "GitHub public pages via Jina Reader (rate-limit fallback)",
        "retrieved_at": _retrieved_at(),
    }


def _github_public_search_via_jina(
    *,
    query: str,
    limit: int,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    jina_api_key: str | None,
) -> dict[str, object]:
    """在 GitHub 公共搜索配额受限时，经 Jina 读取同一搜索 API JSON。"""

    key = _effective_jina_api_key(jina_api_key, settings)
    if not key:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RATE_LIMITED",
            "GitHub 公共 API 匿名配额已耗尽，请配置 GitHub Token 或 Jina API Key。",
            status_code=403,
        )
    api_url = f"{_GITHUB_API_BASE_URL}/search/repositories?{urlencode({'q': query, 'per_page': limit, 'page': 1})}"
    try:
        payload = _github_json_via_jina(
            api_url,
            settings=settings,
            is_cancelled=is_cancelled,
            resolver=resolver,
            jina_api_key=key,
        )
    except VerticalAbilityAdapterError as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RATE_LIMITED",
            "GitHub 公共搜索匿名配额已耗尽，Jina Reader 降级读取失败。",
            status_code=exc.status_code,
        ) from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "GitHub Jina 降级搜索回执无法识别。",
        )
    results: list[dict[str, object]] = []
    for item in payload["items"][:limit]:
        if not isinstance(item, Mapping):
            continue
        normalized = _normalize_github_search_result(item)
        if str(normalized.get("url") or "").strip():
            results.append(normalized)
    return _collection_result(
        collection_key="results",
        items=results,
        common={
            "query": query,
            "provider": "GitHub public search via Jina Reader (rate-limit fallback)",
            "retrieved_at": _retrieved_at(),
        },
    )


def _github_repository_page_payload(
    *,
    repository_url: str,
    owner: str,
    repository: str,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    jina_api_key: str,
) -> dict[str, object]:
    """保留限流降级取得的有界仓库原文和来源元数据，不从页面猜测统计值。"""

    result = _jina_read(
        {"url": repository_url},
        settings,
        is_cancelled,
        resolver,
        jina_api_key=jina_api_key,
    )
    content = str(result.get("content") or "")
    return {
        "name": repository,
        "full_name": f"{owner}/{repository}",
        "html_url": repository_url,
        "owner": {"login": owner},
        "content": content[:_MAX_RESULT_CHARS],
        "content_truncated": bool(result.get("truncated")) or len(content) > _MAX_RESULT_CHARS,
        "content_retrieved_at": str(result.get("retrieved_at") or "")[:64],
        "content_provider": str(result.get("provider") or "Jina Reader")[:128],
    }


def _github_issues_from_page(
    *,
    repository_url: str,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    jina_api_key: str,
) -> list[dict[str, object]]:
    """从公开 Issues 页面提取列表；页面未给出的正文和评论数维持缺失语义。"""

    page_url = f"{repository_url}/issues?q=is%3Aissue+state%3Aopen"
    result = _jina_read(
        {"url": page_url},
        settings,
        is_cancelled,
        resolver,
        jina_api_key=jina_api_key,
    )
    content = str(result.get("content") or "")
    issues: list[dict[str, object]] = []
    for line in content.splitlines():
        match = re.search(
            r"\]\((https://github\.com/[^)\s]+/issues/\d+)\)\s+Status:\s+([A-Za-z]+)\.\s+#(\d+)",
            line,
        )
        if match is None:
            continue
        title_match = re.search(r"### \[(.*?)\]\(", line)
        opened_match = re.search(r"opened on\s+([^\s]+\s+\d{1,2},\s+\d{4})", line)
        opened_at = str(opened_match.group(1)) if opened_match else ""
        issues.append(
            {
                "number": int(match.group(3)),
                "title": (title_match.group(1) if title_match else "")[:_MAX_ITEM_CHARS],
                "url": match.group(1)[:2048],
                "state": match.group(2).casefold()[:32],
                "author": "",
                "comments": None,
                "labels": [],
                "created_at": opened_at[:64],
                "updated_at": "",
            }
        )
    return issues


def _github_json_via_jina(
    source_url: str,
    *,
    settings: Settings,
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    jina_api_key: str,
) -> object:
    """用 Jina Reader 获取固定 GitHub API URL，并从 Markdown Content 提取 JSON。"""

    result = _jina_read(
        {"url": source_url},
        settings,
        is_cancelled,
        resolver,
        jina_api_key=jina_api_key,
    )
    content = str(result.get("content") or "").strip()
    marker = "Markdown Content:"
    if marker in content:
        content = content.split(marker, 1)[1].strip()
    decoder = json.JSONDecoder()
    for offset, character in enumerate(content):
        if character not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(content[offset:])
        except json.JSONDecodeError:
            continue
        return payload
    raise VerticalAbilityAdapterError(
        "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
        "Jina Reader 返回的 GitHub 内容不是有效 JSON。",
    )


def _parse_github_repository(value: object) -> tuple[str, str, str]:
    """把 owner/repo 或公开 GitHub 仓库 URL 规范为安全的仓库标识。"""

    raw = str(value or "").strip()
    if not raw or len(raw) > 256:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "repository 参数无效。")
    if "://" in raw:
        try:
            parsed = urlsplit(raw)
        except ValueError as exc:
            raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "GitHub 仓库 URL 无效。") from exc
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold().rstrip(".") != _GITHUB_REPOSITORY_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "只允许公开 GitHub HTTPS 仓库 URL。")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in raw.split("/") if part]
    if len(parts) != 2:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "repository 必须是 owner/repo 或 GitHub 仓库 URL。")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository or not all(
        _GITHUB_REPOSITORY_PATTERN.fullmatch(item) for item in (owner, repository)
    ):
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "GitHub 仓库标识无效。")
    return owner, repository, f"https://github.com/{owner}/{repository}"


def _validate_youtube_url(url: str, settings: Settings, resolver: AdaptiveDNSResolver) -> None:
    """只允许公开 YouTube HTTPS 地址，并在交给 yt-dlp 前完成出网目标校验。"""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", "YouTube URL 格式无效。") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host not in _YOUTUBE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port != 443)
        or not parsed.path.strip("/")
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_URL_INVALID",
            "只允许公开 YouTube HTTPS 视频 URL。",
        )
    try:
        prepare_outbound_request(
            url,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | set(_YOUTUBE_HOSTS),
            resolver=resolver,
        )
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc


def _youtube_subtitle_languages(value: object) -> tuple[str, ...]:
    """校验字幕语言优先级，避免把任意命令参数或过大集合传给 Runtime。"""

    if value is None:
        return _YOUTUBE_DEFAULT_SUBTITLE_LANGUAGES
    if not isinstance(value, list) or not value or len(value) > 8:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "subtitle_languages 参数无效。",
        )
    languages = tuple(str(item).strip() for item in value)
    if any(
        not item
        or len(item) > 32
        or not re.fullmatch(r"[A-Za-z0-9_-]+", item)
        for item in languages
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "subtitle_languages 参数无效。",
        )
    return languages


def _normalize_youtube_video(info: Mapping[str, object], fallback_url: str) -> dict[str, object]:
    """将 yt-dlp 视频信息压缩为带来源 URL 的公开字段。"""

    channel = str(info.get("channel") or info.get("uploader") or "")[:200]
    return {
        "id": str(info.get("id") or "")[:128],
        "title": str(info.get("title") or "")[:_MAX_ITEM_CHARS],
        "url": fallback_url,
        "canonical_url": str(info.get("webpage_url") or fallback_url)[:2048],
        "channel": channel,
        "description": str(info.get("description") or "")[:_MAX_ITEM_CHARS],
        "duration_seconds": info.get("duration"),
        "published_at": str(info.get("upload_date") or "")[:32],
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
    }


def _normalize_youtube_comments(
    info: Mapping[str, object], source_url: str, limit: int,
) -> dict[str, object]:
    """归一化 yt-dlp 内存评论，保留作者、正文、时间和真实评论标识。"""

    raw_comments = info.get("comments")
    if not isinstance(raw_comments, list):
        return {"items": [], "count": 0, "result_state": "no_results", "truncated": False}
    items: list[dict[str, object]] = []
    for raw in raw_comments[:limit]:
        if not isinstance(raw, Mapping):
            continue
        text = raw.get("text") if isinstance(raw.get("text"), str) else raw.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        item: dict[str, object] = {
            "id": str(raw.get("id") or "")[:256],
            "url": str(raw.get("url") or "")[:2048],
            "source_url": source_url,
            "author": str(raw.get("author") or "")[:200],
            "author_id": str(raw.get("author_id") or "")[:256],
            "text": text[:_MAX_ITEM_CHARS],
            "text_truncated": len(text) > _MAX_ITEM_CHARS,
            "timestamp": raw.get("timestamp"),
            "like_count": raw.get("like_count"),
            "parent": str(raw.get("parent") or "")[:256],
        }
        items.append(item)
    reported_count = info.get("comment_count")
    truncated = len(raw_comments) > limit or (
        isinstance(reported_count, int) and not isinstance(reported_count, bool)
        and reported_count > len(items)
    )
    return {
        "items": items,
        "count": len(items),
        "result_state": "success_with_data" if items else "no_results",
        "truncated": truncated,
    }


def _normalize_youtube_search_result(item: Mapping[str, object]) -> dict[str, object]:
    """将 yt-dlp 搜索条目转换成通用来源卡片。"""

    url = str(item.get("webpage_url") or item.get("original_url") or item.get("url") or "")[:2048]
    if not _is_youtube_url(url):
        url = ""
    return {
        "title": str(item.get("title") or "")[:_MAX_ITEM_CHARS],
        "url": url,
        "published_at": str(item.get("upload_date") or "")[:128],
        "summary": str(item.get("description") or item.get("channel") or "")[:_MAX_ITEM_CHARS],
    }


def _is_youtube_url(url: str) -> bool:
    """判断 Provider 返回的来源是否仍是允许的 YouTube HTTPS 地址。"""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme.casefold() == "https"
        and host in _YOUTUBE_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and (port is None or port == 443)
        and bool(parsed.path.strip("/"))
    )


def _normalize_github_repository(item: Mapping[str, object], fallback_url: str) -> dict[str, object]:
    """归一化仓库字段，保留已读取原文，并将未知计数与确切零值分开。"""

    owner = item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
    license_info = item.get("license") if isinstance(item.get("license"), Mapping) else {}
    topics = item.get("topics") if isinstance(item.get("topics"), list) else []
    result = {
        "name": str(item.get("name") or "")[:200],
        "full_name": str(item.get("full_name") or "")[:300],
        "url": str(item.get("html_url") or fallback_url)[:2048],
        "description": str(item.get("description") or "")[:_MAX_ITEM_CHARS],
        "owner": str(owner.get("login") or "")[:100],
        "language": str(item.get("language") or "")[:100],
        "default_branch": str(item.get("default_branch") or "")[:200],
        "stars": _github_optional_count(item.get("stargazers_count")),
        "forks": _github_optional_count(item.get("forks_count")),
        "open_issues": _github_optional_count(item.get("open_issues_count")),
        "license": str(license_info.get("spdx_id") or license_info.get("name") or "")[:100],
        "topics": [str(topic)[:100] for topic in topics[:20] if str(topic).strip()],
        "created_at": str(item.get("created_at") or "")[:64],
        "updated_at": str(item.get("updated_at") or "")[:64],
    }
    if isinstance(item.get("content"), str):
        content = item["content"]
        result.update(
            {
                "content": content[:_MAX_RESULT_CHARS],
                "content_truncated": (
                    bool(item.get("content_truncated")) or len(content) > _MAX_RESULT_CHARS
                ),
                "content_retrieved_at": str(item.get("content_retrieved_at") or "")[:64],
                "content_provider": str(item.get("content_provider") or "")[:128],
            }
        )
    return result


def _github_optional_count(value: object) -> int | None:
    """只接受来源给出的非负整数计数，缺失、布尔值和未知文本不转成零。"""

    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _normalize_github_issue(item: Mapping[str, object]) -> dict[str, object]:
    """保留带来源 URL 的 Issue 元数据及有界正文，明确标注截断与缺失。"""

    user = item.get("user") if isinstance(item.get("user"), Mapping) else {}
    labels = item.get("labels") if isinstance(item.get("labels"), list) else []
    body = item.get("body") if isinstance(item.get("body"), str) else None
    return {
        "number": item.get("number"),
        "title": str(item.get("title") or "")[:_MAX_ITEM_CHARS],
        "url": str(item.get("html_url") or item.get("url") or "")[:2048],
        "state": str(item.get("state") or "")[:32],
        "author": str(user.get("login") or "")[:100],
        "comments": _github_optional_count(item.get("comments")),
        "body": body[:_MAX_ITEM_CHARS] if body is not None else None,
        "body_truncated": body is not None and len(body) > _MAX_ITEM_CHARS,
        "labels": [
            str(label.get("name") or "")[:100]
            for label in labels[:20]
            if isinstance(label, Mapping) and str(label.get("name") or "").strip()
        ],
        "created_at": str(item.get("created_at") or "")[:64],
        "updated_at": str(item.get("updated_at") or "")[:64],
    }


def _normalize_github_search_result(item: Mapping[str, object]) -> dict[str, object]:
    """将 GitHub 仓库搜索对象转换为通用来源卡片。"""

    return {
        "title": str(item.get("full_name") or item.get("name") or "")[:_MAX_ITEM_CHARS],
        "url": str(item.get("html_url") or "")[:2048],
        "published_at": str(item.get("created_at") or "")[:128],
        "summary": str(item.get("description") or "")[:_MAX_ITEM_CHARS],
    }


def _http_get_bytes(
    url: str,
    *,
    settings: Settings,
    headers: Mapping[str, str],
    is_cancelled: Callable[[], bool] | None,
    resolver: AdaptiveDNSResolver,
    allowed_hosts: Iterable[str] | None = None,
) -> bytes:
    """通过平台固定目标的出网请求读取有限大小响应。"""

    try:
        trusted_hosts = set(allowed_hosts_from_settings(settings))
        if allowed_hosts is not None:
            trusted_hosts.update(str(host).strip() for host in allowed_hosts if str(host).strip())
        target = prepare_outbound_request(
            url,
            allowed_hosts=trusted_hosts,
            resolver=resolver,
        )
        with httpx.Client(
            timeout=_provider_timeout_seconds(settings),
            follow_redirects=False,
        ) as client:
            response = run_cancellable(
                lambda: client.get(
                    target.request_url,
                    headers={**headers, **target.headers},
                    **({"extensions": target.extensions} if target.extensions else {}),
                ),
                is_cancelled,
                on_cancel=client.close,
            )
            response.raise_for_status()
            body = response.content
    except httpx.TimeoutException as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "互联网数据源请求超时。") from exc
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)[:300]) from exc
    except httpx.HTTPStatusError as exc:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            f"互联网数据源返回异常状态码：{exc.response.status_code}。",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", "互联网数据源连接失败。") from exc
    if len(body) > _MAX_HTTP_BYTES:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_RESPONSE_TOO_LARGE", "互联网数据源响应超过安全上限。")
    return body


def _provider_timeout_seconds(settings: object) -> float:
    """返回 Agent-Reach 公共 Provider 的独立请求窗口，不受通用工具短超时误伤。"""

    raw = getattr(settings, "vertical_ability_provider_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw or _DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        value = _DEFAULT_TIMEOUT_SECONDS
    return max(5.0, min(value, 120.0))


def _validate_public_url(url: str, settings: Settings, resolver: AdaptiveDNSResolver) -> None:
    """校验能力包接收的 URL，拒绝凭据、fragment 和受限网络地址。"""

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", "URL 格式无效。") from exc
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_INVALID", "URL 不得携带凭据或 fragment。")
    try:
        prepare_outbound_request(
            url,
            allowed_hosts=allowed_hosts_from_settings(settings),
            resolver=resolver,
        )
    except ValueError as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_URL_BLOCKED", str(exc)) from exc


def _required_string(arguments: Mapping[str, Any], key: str, *, max_length: int) -> str:
    """读取必填短字符串并执行长度边界校验。"""

    value = str(arguments.get(key) or "").strip()
    if not value or len(value) > max_length:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", f"{key} 参数无效。")
    return value


def _bounded_string(value: object, key: str, max_length: int) -> str:
    """读取可选字符串并执行长度边界校验。"""

    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", f"{key} 参数无效。")
    return text


def _argument_bool(value: object, key: str, *, default: bool) -> bool:
    """读取布尔参数并拒绝字符串化真值，避免模型悄悄改变深度读取范围。"""

    if value is None or value == "":
        return default
    if type(value) is not bool:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", f"{key} 参数无效。")
    return value


def _bounded_string_list(value: object, key: str, *, max_items: int) -> list[str]:
    """读取有界字符串列表，供 RSS 增量状态只传递不透明条目标识。"""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", f"{key} 参数无效。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 2048:
            raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", f"{key} 参数无效。")
        result.append(item.strip())
    return list(dict.fromkeys(result))


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    """把外部整数参数收敛到适配器声明的范围。"""

    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "整数参数无效。")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "整数参数无效。") from exc
    if not minimum <= result <= maximum:
        raise VerticalAbilityAdapterError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "整数参数超出范围。")
    return result


def _normalize_search_results(raw: object, limit: int) -> list[dict[str, object]]:
    """优先选用含合法来源的 Exa 载荷；空结果、服务错误和未知格式分别处理。"""

    # 某些 MCP 网关会同时返回 structuredContent 和 content：前者保留了协议字段，
    # 但 results 可能是空数组；后者仍携带真实的文本结果。不能因为优先读取结构化
    # 字段就丢掉同一响应中的可用来源，否则上层只能得到“0 条”而没有 Provider 错误。
    errors: list[VerticalAbilityAdapterError] = []
    variants = _search_payload_variants(raw)
    for candidates in variants:
        try:
            normalized = _normalize_search_candidate(candidates, limit)
        except VerticalAbilityAdapterError as exc:
            errors.append(exc)
            continue
        if normalized:
            return normalized
    if errors:
        raise errors[0]
    if not variants:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "Exa 搜索回执未包含可识别的结果。"
        )
    return []


def _normalize_search_candidate(candidates: object, limit: int) -> list[dict[str, object]]:
    """只把有合法 HTTP 来源的候选转成卡片，拒绝用错误摘要填充虚构结果。"""

    if isinstance(candidates, str):
        stripped = candidates.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                return _normalize_search_results(parsed, limit)
        return _normalize_text_search_results(candidates, limit)
    if isinstance(candidates, Mapping):
        candidates = [candidates]
    if not isinstance(candidates, list):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "Exa 搜索回执类型无法识别。"
        )
    result: list[dict[str, object]] = []
    unrecognized_entries = False
    for item in candidates:
        if isinstance(item, Mapping):
            url = _search_source_url(item.get("url") or item.get("link"))
            if not url:
                unrecognized_entries = True
                continue
            normalized = {
                "title": str(item.get("title") or item.get("name") or "")[:_MAX_ITEM_CHARS],
                "url": url,
                "published_at": str(item.get("publishedDate") or item.get("published_at") or "")[:128],
                "summary": str(item.get("text") or item.get("snippet") or item.get("summary") or "")[:_MAX_ITEM_CHARS],
            }
            result.append(normalized)
        elif isinstance(item, str):
            result.extend(_normalize_text_search_results(item, limit - len(result)))
        else:
            unrecognized_entries = True
        if len(result) >= limit:
            break
    if unrecognized_entries and not result:
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "Exa 返回的非空列表未包含合法来源 URL，不能作为搜索成功的证据。",
        )
    return result


def _search_source_url(value: object) -> str:
    """仅验证搜索卡片 URL 的 HTTP 语法；实际读取仍使用既有公网与 DNS 校验。"""

    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2048 or any(char.isspace() or ord(char) < 32 for char in url):
        return ""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme.casefold() not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port == 0
        ):
            return ""
    except ValueError:
        return ""
    return url


def _search_payload_variants(raw: object) -> list[object]:
    """有限展开结构化与文本候选，不把协议包装对象、错误对象本身当作搜索结果。"""

    variants: list[object] = []
    seen: set[int] = set()

    def visit(value: object, depth: int = 0) -> None:
        """按优先级展开最多十二层包装，拒绝服务错误并防止循环引用。"""

        if depth > 12:
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID", "Exa 搜索回执嵌套超过安全上限。"
            )
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(value, Mapping):
            status = value.get("status_code", value.get("code", value.get("status")))
            if (
                value.get("isError")
                or value.get("error")
                or value.get("errors")
                or value.get("success") is False
                or str(value.get("status") or "").casefold() in {"error", "failed", "failure"}
                or (isinstance(status, int) and 400 <= status <= 599)
            ):
                raise VerticalAbilityAdapterError(
                    "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", "Exa 搜索返回了服务错误。"
                )
            keys = (
                "structuredContent", "structured_content", "results", "data", "items", "content",
            )
            nested = [key for key in keys if key in value and value[key] is not None]
            if (value.get("url") or value.get("link")) or not nested:
                variants.append(value)
            else:
                for key in nested:
                    visit(value[key], depth + 1)
            return
        if isinstance(value, list):
            text_blocks = [
                str(item.get("text") or "")
                for item in value
                if isinstance(item, Mapping) and item.get("type") == "text"
            ]
            if text_blocks:
                variants.append("\n".join(item for item in text_blocks if item))
            else:
                variants.append(value)
            return
        variants.append(value)

    visit(raw)
    return variants


def _unwrap_search_payload(raw: object) -> object:
    """兼容旧调用方读取 Exa/MCP 的首个结构化载荷。"""

    variants = _search_payload_variants(raw)
    return variants[0] if variants else raw


def _normalize_text_search_results(text: str, limit: int) -> list[dict[str, object]]:
    """解析有来源 URL 的 Exa 文本块，区分明确空结果、服务错误及未知文本。"""

    normalized_text = " ".join(text.casefold().split())
    if normalized_text in {"results: []", "results:[]", "data: []", "items: []"} or re.fullmatch(
        r"(?:no(?: search)? results?(?: found)?|0 (?:search )?results?)[.!]?", normalized_text
    ):
        return []
    if re.match(
        r"^(?:error\b|service (?:temporarily )?unavailable\b|rate limit\b|too many requests\b)",
        normalized_text,
    ):
        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", "Exa 搜索返回了服务错误，未取得可用来源。"
        )

    chunks = [
        chunk.strip()
        for chunk in re.split(r"(?m)(?=^Title:\s*)", text.replace("\r\n", "\n"))
        if chunk.strip()
    ]
    results: list[dict[str, object]] = []
    for chunk in chunks:
        title = _text_result_field(chunk, "Title")
        url = _search_source_url(_text_result_field(chunk, "URL"))
        if not url:
            continue
        published_at = _text_result_field(chunk, "Published")
        if published_at.casefold() in {"n/a", "na", "none"}:
            published_at = ""
        highlights_match = re.search(r"(?ms)^Highlights:\s*\n?(.*)$", chunk)
        summary = ""
        if highlights_match:
            summary = " ".join(highlights_match.group(1).split())
            summary = summary.replace("--- Title:", "Title:").strip(" -")
        results.append(
            {
                "title": title[:_MAX_ITEM_CHARS],
                "url": url[:2048],
                "published_at": published_at[:128],
                "summary": summary[:_MAX_ITEM_CHARS],
            }
        )
        if len(results) >= limit:
            break
    if results:
        return results
    raise VerticalAbilityAdapterError(
        "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
        "Exa 文本回执既不是明确空结果，也没有可识别的来源 URL。",
    )


def _text_result_field(text: str, field: str) -> str:
    """读取 Exa 纯文本结果块中的单行元数据字段。"""

    match = re.search(rf"(?m)^{re.escape(field)}:\s*(.*)$", text)
    return match.group(1).strip() if match else ""


def _normalize_v2ex_item(item: Mapping[str, Any]) -> dict[str, object]:
    """保留主题原有字段和公开 member 信息，正文有界且截断可见。"""

    node = item.get("node") if isinstance(item.get("node"), Mapping) else {}
    content = str(item.get("content") or "")
    result = {
        "id": item.get("id"),
        "title": str(item.get("title") or "")[:_MAX_ITEM_CHARS],
        "url": str(item.get("url") or "")[:2048],
        "replies": item.get("replies", 0),
        "node_name": str(node.get("name") or "")[:128],
        "node_title": str(node.get("title") or "")[:_MAX_ITEM_CHARS],
        "content": content[:_MAX_ITEM_CHARS],
        "created": item.get("created"),
    }
    if isinstance(item.get("member"), Mapping):
        member = _normalize_v2ex_member(item["member"])
        result["member"] = member
        result["author"] = member.get("username", "")
    if len(content) > _MAX_ITEM_CHARS:
        result["content_truncated"] = True
    return result


def _normalize_v2ex_member(value: object) -> dict[str, object]:
    """有界保留 V2EX 公开作者字段，不引入新用户操作或回传任意嵌套数据。"""

    if not isinstance(value, Mapping):
        return {}
    public_fields = {
        "id", "username", "url", "avatar_mini", "avatar_normal", "avatar_large",
        "website", "twitter", "psn", "github", "btc", "location", "bio", "created", "tagline",
    }
    return {
        key: item[:_MAX_ITEM_CHARS] if isinstance(item, str) else item
        for key, item in value.items()
        if key in public_fields and (item is None or isinstance(item, (str, int, float, bool)))
    }


def _local_name(tag: object) -> str:
    """返回 XML 标签的本地名，兼容 RSS/Atom 命名空间。"""

    return str(tag).rsplit("}", 1)[-1]


def _xml_child_text(node: ElementTree.Element, name: str) -> str:
    """读取 XML 子元素纯文本并折叠空白。"""

    for child in list(node):
        if _local_name(child.tag) == name:
            return " ".join("".join(child.itertext()).split())
    return ""


def _xml_child_attribute(node: ElementTree.Element, name: str, attribute: str) -> str:
    """读取 XML 子元素属性，供 Atom link href 兼容使用。"""

    for child in list(node):
        if _local_name(child.tag) == name:
            return str(child.attrib.get(attribute) or "").strip()
    return ""


def _retrieved_at() -> str:
    """返回 UTC 读取时间，作为外部情报的时间证据字段。"""

    return datetime.now(timezone.utc).isoformat()
