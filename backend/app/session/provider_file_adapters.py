"""
@Time       : 2026/08/27 22:15
@Author     : zhanglp8181
@File       : provider_file_adapters.py
@CallChain  : ModelConfig → provider capability profile → Files API → ProviderExposureAdapter
@Description: 统一封装 Ark、DeepSeek 与 SiliconFlow 的文件能力差异，保持外发对账的有限状态契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.db.models import ModelConfig
from app.security.encryption import decrypt_secret
from app.session.provider_input_reconciliation import (
    ProviderExposureOutcome,
)


ProviderCanonicalName = Literal["ark", "deepseek", "siliconflow", "openai_compatible", "unsupported"]
ProviderFileUploadStatus = Literal["uploaded", "unsupported", "unknown", "failed"]
ProviderFileListStatus = Literal["listed", "unsupported", "unknown", "failed"]

_KNOWN_PROVIDER_NAMES = frozenset({"ark", "deepseek", "siliconflow", "openai_compatible"})
_ARK_HOSTS = frozenset({"ark.cn-beijing.volces.com", "ark.ap-southeast.bytepluses.com"})
_ARK_HOST_SUFFIXES = (".volces.com", ".bytepluses.com")
_DEEPSEEK_HOSTS = frozenset({"api.deepseek.com"})
_SILICONFLOW_HOSTS = frozenset({"api.siliconflow.cn"})
_SAFE_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
_SILICONFLOW_BATCH_SUFFIXES = frozenset({".jsonl", ".ndjson"})
_SILICONFLOW_BATCH_MIME_TYPES = frozenset(
    {"application/jsonl", "application/x-ndjson", "application/json"}
)
_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_JSON_INVALID = object()


@dataclass(frozen=True, slots=True)
class ProviderFileProfile:
    """描述一个模型供应商的 Chat 与 Files 能力，禁止把差异藏在调用方分支中。"""

    canonical_provider: ProviderCanonicalName
    configured_provider: str
    display_name: str
    chat_base_url: str
    files_base_url: str | None
    upload_supported: bool
    retrieve_supported: bool
    list_supported: bool
    delete_supported: bool
    upload_purpose: str | None
    upload_mode: Literal["any", "image_only", "batch_only", "none"]
    upload_limit_bytes: int | None
    list_limit_max: int = 1000
    upload_mime_types: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()

    @property
    def files_supported(self) -> bool:
        """返回是否存在至少一项真实 Files API 能力。"""

        return bool(self.files_base_url) and (
            self.upload_supported
            or self.retrieve_supported
            or self.list_supported
            or self.delete_supported
        )


@dataclass(frozen=True, slots=True)
class ProviderFileUploadResult:
    """表达上传成功、未知、失败或明确不支持，不把未知结果当成已上传。"""

    status: ProviderFileUploadStatus
    provider_file_id: str | None = None
    provider_status: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderFileListResult:
    """承载受控文件列表响应，列表项只保留供应商公开元数据。"""

    status: ProviderFileListStatus
    files: tuple[dict[str, object], ...] = ()
    has_more: bool = False
    next_cursor: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


def canonical_provider_name(provider: object, base_url: str | None = None) -> ProviderCanonicalName:
    """规范化管理端 provider 别名；仅对官方 hostname 做安全的兼容推断。"""

    value = str(provider or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ark": "ark",
        "volcengine": "ark",
        "volcengine_ark": "ark",
        "ark_agent_plan_cn": "ark",
        "byteplus_ark": "ark",
        "doubao": "ark",
        "deepseek": "deepseek",
        "siliconflow": "siliconflow",
        "silicon_flow": "siliconflow",
        "硅基流动": "siliconflow",
        "openai": "openai_compatible",
        "openai_compatible": "openai_compatible",
    }
    direct = aliases.get(value)
    if direct is not None and direct != "openai_compatible":
        return direct  # type: ignore[return-value]
    if value:
        if direct != "openai_compatible":
            return "unsupported"
    host = _hostname(base_url)
    if host in _ARK_HOSTS:
        return "ark"
    if host in _DEEPSEEK_HOSTS:
        return "deepseek"
    if host in _SILICONFLOW_HOSTS:
        return "siliconflow"
    if direct == "openai_compatible":
        return "openai_compatible"
    return "unsupported"


def is_supported_chat_provider(provider: object, base_url: str | None = None) -> bool:
    """判断模型是否可走项目采用的 OpenAI-compatible Chat 契约。"""

    canonical = canonical_provider_name(provider, base_url)
    return canonical in _KNOWN_PROVIDER_NAMES


def profile_for_model_config(model_config: ModelConfig) -> ProviderFileProfile:
    """从租户模型配置生成不可变能力画像，并为 Files API 计算官方路径。"""

    configured_provider = str(model_config.provider or "").strip()
    canonical = canonical_provider_name(configured_provider, model_config.base_url)
    chat_base_url = str(model_config.base_url or "").strip().rstrip("/")
    extra_body = model_config.extra_body_json if isinstance(model_config.extra_body_json, dict) else {}
    file_options = extra_body.get("provider_file_api")
    if not isinstance(file_options, Mapping):
        file_options = {}
    override = file_options.get("base_url")
    override_url = str(override).strip().rstrip("/") if isinstance(override, str) else ""
    if override_url:
        _validate_file_api_override(canonical, model_config.base_url, override_url)

    if canonical == "ark":
        files_base_url = override_url or _official_origin(
            model_config.base_url,
            "/api/v3",
            canonical="ark",
        )
        return ProviderFileProfile(
            canonical_provider="ark",
            configured_provider=configured_provider,
            display_name="Ark",
            chat_base_url=chat_base_url,
            files_base_url=files_base_url,
            upload_supported=True,
            retrieve_supported=True,
            list_supported=True,
            delete_supported=True,
            upload_purpose="user_data",
            upload_mode="any",
            upload_limit_bytes=512 * 1024 * 1024,
            list_limit_max=100,
            # Ark user_data accepts general files; Chat modality validation below decides
            # whether a returned id can be used as image/video/PDF input.
            upload_mime_types=frozenset(),
            notes=("Files API 使用 /api/v3/files；Chat 图片 file_id 放入 image_url.file_id。",),
        )
    if canonical == "deepseek":
        files_base_url = override_url or _official_origin(
            model_config.base_url,
            "",
            canonical="deepseek",
        )
        return ProviderFileProfile(
            canonical_provider="deepseek",
            configured_provider=configured_provider,
            display_name="DeepSeek",
            chat_base_url=chat_base_url,
            files_base_url=files_base_url,
            upload_supported=True,
            retrieve_supported=True,
            list_supported=True,
            delete_supported=True,
            upload_purpose="user_data",
            upload_mode="image_only",
            upload_limit_bytes=64 * 1024 * 1024,
            list_limit_max=1000,
            upload_mime_types=_IMAGE_MIME_TYPES,
            notes=("Files API 仅接受 JPEG、PNG、GIF、WebP；Chat 使用 type=file/file_id。",),
        )
    if canonical == "siliconflow":
        files_base_url = override_url or _official_origin(
            model_config.base_url,
            "/v1",
            canonical="siliconflow",
        )
        return ProviderFileProfile(
            canonical_provider="siliconflow",
            configured_provider=configured_provider,
            display_name="SiliconFlow",
            chat_base_url=chat_base_url,
            files_base_url=files_base_url,
            upload_supported=True,
            retrieve_supported=False,
            list_supported=True,
            delete_supported=False,
            upload_purpose="batch",
            upload_mode="batch_only",
            upload_limit_bytes=None,
            list_limit_max=1000,
            upload_mime_types=_SILICONFLOW_BATCH_MIME_TYPES,
            notes=("/files 是 Batch 文件接口；在线附件继续使用受控 image_url 内联，不伪造删除能力。",),
        )
    return ProviderFileProfile(
        canonical_provider="openai_compatible" if canonical == "openai_compatible" else "unsupported",
        configured_provider=configured_provider,
        display_name="OpenAI-compatible" if canonical == "openai_compatible" else "Unknown",
        chat_base_url=chat_base_url,
        files_base_url=None,
        upload_supported=False,
        retrieve_supported=False,
        list_supported=False,
        delete_supported=False,
        upload_purpose=None,
        upload_mode="none",
        upload_limit_bytes=None,
        list_limit_max=1,
        notes=("未声明真实 Files API；只允许项目已有的本地解析与内联输入路径。",),
    )


def provider_file_profile_payload(profile: ProviderFileProfile) -> dict[str, object]:
    """将能力画像投影成不含凭据的快照，供预检、健康面和部署证据复用。"""

    configured = bool(profile.files_base_url)
    return {
        "canonical_provider": profile.canonical_provider,
        "display_name": profile.display_name,
        "chat_protocol": "openai_compatible",
        "chat_supported": profile.canonical_provider in _KNOWN_PROVIDER_NAMES,
        "files_configured": configured,
        "files_supported": configured and profile.files_supported,
        "upload_supported": configured and profile.upload_supported,
        "retrieve_supported": configured and profile.retrieve_supported,
        "list_supported": configured and profile.list_supported,
        "delete_supported": configured and profile.delete_supported,
        "upload_purpose": profile.upload_purpose,
        "upload_mode": profile.upload_mode,
        "upload_limit_bytes": profile.upload_limit_bytes,
        "list_limit_max": profile.list_limit_max,
        "notes": list(profile.notes),
    }


class ProviderFileApiAdapter:
    """以有限状态适配器访问三家官方 Files API，并隔离凭据与 HTTP 细节。"""

    def __init__(
        self,
        model_config: ModelConfig | None = None,
        *,
        profile: ProviderFileProfile | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        """绑定模型配置或测试画像；生产密钥只在进程内存中短暂使用。"""

        if profile is None:
            if model_config is None:
                raise ValueError("model_config or profile is required")
            profile = profile_for_model_config(model_config)
        self.profile = profile
        configured_key = decrypt_secret(model_config.api_key_encrypted) if model_config is not None else ""
        self._api_key = api_key if api_key is not None else configured_key
        self._client = client
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    def close(self) -> None:
        """关闭由调用方托管的 HTTP client；注入 client 的所有权不转移。"""

        return

    def upload_file(
        self,
        *,
        tenant_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> ProviderFileUploadResult:
        """按画像上传文件，先执行类型/大小边界检查，再返回可对账的 file-id。"""

        del tenant_id  # tenant 由上层账本绑定；供应商 API 本身不接收租户标识。
        profile = self.profile
        if not profile.upload_supported or not profile.files_base_url:
            return ProviderFileUploadResult(
                status="unsupported",
                detail={"code": "PROVIDER_FILE_UPLOAD_UNSUPPORTED"},
            )
        safe_name = str(filename or "").strip()
        mime = str(content_type or "application/octet-stream").strip().lower()
        if not _SAFE_FILENAME.fullmatch(safe_name):
            return ProviderFileUploadResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_FILENAME_INVALID"},
            )
        if not isinstance(content, bytes) or not content:
            return ProviderFileUploadResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_CONTENT_INVALID"},
            )
        if profile.upload_limit_bytes is not None and len(content) > profile.upload_limit_bytes:
            return ProviderFileUploadResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_SIZE_EXCEEDED"},
            )
        if profile.upload_mode == "batch_only":
            suffix = safe_name[safe_name.rfind(".") :].lower() if "." in safe_name else ""
            if suffix not in _SILICONFLOW_BATCH_SUFFIXES and mime not in _SILICONFLOW_BATCH_MIME_TYPES:
                return ProviderFileUploadResult(
                    status="unsupported",
                    detail={"code": "PROVIDER_FILE_BATCH_ONLY"},
                )
        elif profile.upload_mime_types and mime not in profile.upload_mime_types:
            return ProviderFileUploadResult(
                status="unsupported",
                detail={"code": "PROVIDER_FILE_MIME_UNSUPPORTED", "content_type": mime},
            )
        try:
            response = self._request(
                "POST",
                "/files",
                data={"purpose": profile.upload_purpose or ""},
                files={"file": (safe_name, content, mime)},
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return ProviderFileUploadResult(
                status="unknown",
                detail={"code": "PROVIDER_FILE_UPLOAD_NETWORK_UNKNOWN"},
            )
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            return ProviderFileUploadResult(
                status="unknown",
                detail=_response_detail(response, "upload", secret=self._api_key),
            )
        if response.status_code >= 400:
            return ProviderFileUploadResult(
                status="failed",
                detail=_response_detail(response, "upload", secret=self._api_key),
            )
        body = _json_object(response)
        payload = body.get("data") if self.profile.canonical_provider == "siliconflow" else body
        if not isinstance(payload, Mapping):
            return ProviderFileUploadResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_RESPONSE_INVALID"},
            )
        provider_file_id = self._provider_file_id(payload.get("id"))
        if not provider_file_id:
            return ProviderFileUploadResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_ID_MISSING"},
            )
        return ProviderFileUploadResult(
            status="uploaded",
            provider_file_id=provider_file_id,
            provider_status=_safe_text(payload.get("status"), 32),
            detail={
                "operation": "upload",
                "provider": self.profile.canonical_provider,
                "purpose": _safe_text(payload.get("purpose"), 32),
            },
        )

    def list_files(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
        purpose: str | None = None,
    ) -> ProviderFileListResult:
        """读取官方文件列表并统一分页字段，禁止把原始响应直接写入业务账本。"""

        if not self.profile.list_supported or not self.profile.files_base_url:
            return ProviderFileListResult(
                status="unsupported",
                detail={"code": "PROVIDER_FILE_LIST_UNSUPPORTED"},
            )
        profile = self.profile
        bounded_limit = max(1, min(int(limit), profile.list_limit_max))
        params: dict[str, str | int] = {"limit": bounded_limit}
        if after:
            params["after"] = after
        effective_purpose = purpose or self.profile.upload_purpose
        if effective_purpose:
            params["purpose"] = effective_purpose
        try:
            response = self._request("GET", "/files", params=params)
        except (httpx.TimeoutException, httpx.NetworkError):
            return ProviderFileListResult(
                status="unknown",
                detail={"code": "PROVIDER_FILE_LIST_NETWORK_UNKNOWN"},
            )
        if response.status_code == 404:
            return ProviderFileListResult(status="failed", detail={"code": "PROVIDER_FILE_LIST_NOT_FOUND"})
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            return ProviderFileListResult(
                status="unknown",
                detail=_response_detail(response, "list", secret=self._api_key),
            )
        if response.status_code >= 400:
            return ProviderFileListResult(
                status="failed",
                detail=_response_detail(response, "list", secret=self._api_key),
            )
        raw_body = _json_value(response)
        if raw_body is None and self.profile.canonical_provider == "ark":
            # Ark 在没有任何 user_data 文件时返回 JSON null，而不是 {"data": []}。
            return ProviderFileListResult(status="listed")
        if raw_body is _JSON_INVALID:
            return ProviderFileListResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_LIST_RESPONSE_INVALID"},
            )
        body = dict(raw_body) if isinstance(raw_body, Mapping) else {}
        if not body and raw_body is not None:
            return ProviderFileListResult(
                status="failed",
                detail={"code": "PROVIDER_FILE_LIST_RESPONSE_INVALID"},
            )
        payload = body.get("data")
        if self.profile.canonical_provider == "siliconflow" and isinstance(payload, Mapping):
            raw_files = payload.get("data")
            has_more = False
            next_cursor = None
        else:
            raw_files = body.get("data")
            has_more = body.get("has_more") is True
            next_cursor = _safe_text(body.get("last_id"), 256) if has_more else None
        if not isinstance(raw_files, list):
            return ProviderFileListResult(status="failed", detail={"code": "PROVIDER_FILE_LIST_RESPONSE_INVALID"})
        files = tuple(_project_file_metadata(item) for item in raw_files if isinstance(item, Mapping))
        return ProviderFileListResult(
            status="listed",
            files=files,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def reconcile_exposure(
        self,
        *,
        tenant_id: str,
        provider_request_id: str | None,
        dispatch_token: str,
    ) -> ProviderExposureOutcome:
        """仅用真实 provider file-id 对账；无可查询身份时明确进入 Attention。"""

        del tenant_id
        if not dispatch_token:
            return ProviderExposureOutcome(
                kind="unsupported",
                detail={"code": "PROVIDER_DISPATCH_TOKEN_MISSING"},
            )
        provider_file_id = self._provider_file_id(provider_request_id)
        if not provider_file_id:
            return ProviderExposureOutcome(
                kind="unsupported",
                detail={"code": "PROVIDER_FILE_ID_MISSING_FOR_RECONCILIATION"},
            )
        if self.profile.retrieve_supported:
            return self._retrieve_outcome(provider_file_id)
        if self.profile.list_supported:
            listed = self.list_files(limit=1000)
            if listed.status == "unsupported":
                return ProviderExposureOutcome(kind="unsupported", detail=listed.detail)
            if listed.status != "listed":
                return ProviderExposureOutcome(kind="unknown", detail=listed.detail)
            if any(item.get("id") == provider_file_id for item in listed.files):
                return ProviderExposureOutcome(
                    kind="found",
                    provider_file_id=provider_file_id,
                    detail={"operation": "list", "provider": self.profile.canonical_provider},
                )
            return ProviderExposureOutcome(
                kind="not_found",
                detail={"operation": "list", "provider": self.profile.canonical_provider},
            )
        return ProviderExposureOutcome(
            kind="unsupported",
            detail={"code": "PROVIDER_FILE_RETRIEVE_UNSUPPORTED"},
        )

    def delete_file(self, *, tenant_id: str, provider_file_id: str) -> ProviderExposureOutcome:
        """只对画像声明支持删除的供应商发起 DELETE，并保留 404 幂等语义。"""

        del tenant_id
        safe_id = self._provider_file_id(provider_file_id)
        if not safe_id:
            return ProviderExposureOutcome(
                kind="unsupported",
                detail={"code": "PROVIDER_FILE_ID_INVALID"},
            )
        if not self.profile.delete_supported or not self.profile.files_base_url:
            return ProviderExposureOutcome(
                kind="unsupported",
                detail={"code": "PROVIDER_FILE_DELETE_UNSUPPORTED"},
            )
        try:
            response = self._request("DELETE", f"/files/{safe_id}")
        except (httpx.TimeoutException, httpx.NetworkError):
            return ProviderExposureOutcome(
                kind="unknown",
                detail={"code": "PROVIDER_FILE_DELETE_NETWORK_UNKNOWN"},
            )
        if response.status_code == 404:
            return ProviderExposureOutcome(kind="not_found", detail={"operation": "delete"})
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            return ProviderExposureOutcome(
                kind="unknown",
                detail=_response_detail(response, "delete", secret=self._api_key),
            )
        if response.status_code >= 400:
            return ProviderExposureOutcome(
                kind="unknown",
                detail=_response_detail(response, "delete", secret=self._api_key),
            )
        body = _json_object(response)
        if body.get("deleted") is not True:
            return ProviderExposureOutcome(
                kind="unknown",
                detail={"code": "PROVIDER_FILE_DELETE_UNCONFIRMED"},
            )
        response_id = self._provider_file_id(body.get("id"))
        if response_id and response_id != safe_id:
            return ProviderExposureOutcome(
                kind="unknown",
                detail={"code": "PROVIDER_FILE_DELETE_ID_MISMATCH"},
            )
        return ProviderExposureOutcome(
            kind="deleted",
            provider_file_id=safe_id,
            detail={"operation": "delete", "provider": self.profile.canonical_provider},
        )

    def chat_file_part(self, *, provider_file_id: str, content_type: str) -> dict[str, object]:
        """生成供应商明确支持的 Chat 文件 part；不支持的形态必须回退或显式失败。"""

        safe_id = self._provider_file_id(provider_file_id)
        if not safe_id:
            raise ValueError("PROVIDER_FILE_ID_INVALID")
        mime = str(content_type or "").strip().lower()
        if self.profile.canonical_provider == "deepseek":
            if mime not in _IMAGE_MIME_TYPES:
                raise ValueError("PROVIDER_FILE_IMAGE_ONLY")
            return {"type": "file", "file_id": safe_id}
        if self.profile.canonical_provider == "ark":
            if mime.startswith("image/"):
                return {"type": "image_url", "image_url": {"file_id": safe_id}}
            if mime.startswith("video/"):
                return {"type": "video_url", "video_url": {"file_id": safe_id}}
            if mime == "application/pdf":
                return {"type": "file", "file": {"file_id": safe_id}}
            raise ValueError("PROVIDER_FILE_CHAT_PART_UNSUPPORTED")
        raise ValueError("PROVIDER_FILE_CHAT_PART_UNSUPPORTED")

    def _retrieve_outcome(self, provider_file_id: str) -> ProviderExposureOutcome:
        """检索一个已知 file-id，并校验响应身份没有被供应商或代理改写。"""

        try:
            response = self._request("GET", f"/files/{provider_file_id}")
        except (httpx.TimeoutException, httpx.NetworkError):
            return ProviderExposureOutcome(
                kind="unknown",
                detail={"code": "PROVIDER_FILE_RETRIEVE_NETWORK_UNKNOWN"},
            )
        if response.status_code == 404:
            return ProviderExposureOutcome(kind="not_found", detail={"operation": "retrieve"})
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            return ProviderExposureOutcome(
                kind="unknown",
                detail=_response_detail(response, "retrieve", secret=self._api_key),
            )
        if response.status_code >= 400:
            return ProviderExposureOutcome(
                kind="unknown",
                detail=_response_detail(response, "retrieve", secret=self._api_key),
            )
        body = _json_object(response)
        response_id = self._provider_file_id(body.get("id"))
        if response_id != provider_file_id:
            return ProviderExposureOutcome(
                kind="unknown",
                detail={"code": "PROVIDER_FILE_RETRIEVE_ID_MISMATCH"},
            )
        return ProviderExposureOutcome(
            kind="found",
            provider_file_id=provider_file_id,
            detail={
                "operation": "retrieve",
                "provider": self.profile.canonical_provider,
                "status": _safe_text(body.get("status"), 32),
            },
        )

    def _provider_file_id(self, value: object) -> str | None:
        """按供应商固定前缀校验 file-id，避免把任意字符串当成远端资源身份。"""

        prefixes = (
            ("file-api-",)
            if self.profile.canonical_provider == "deepseek"
            else ("file-",)
        )
        return _safe_provider_file_id(value, prefixes=prefixes)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """执行固定 provider URL 请求，避免把可配置任意 URL 带入文件外发边界。"""

        if not self.profile.files_base_url:
            raise ValueError("PROVIDER_FILE_API_UNSUPPORTED")
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        owned_client = self._client is None
        try:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers.setdefault("Accept", "application/json")
            return client.request(
                method,
                f"{self.profile.files_base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=headers,
                **kwargs,
            )
        finally:
            if owned_client:
                client.close()


def _hostname(value: str | None) -> str:
    """读取 URL hostname 并统一小写，解析失败时返回空值。"""

    try:
        return (urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def _official_origin(
    value: str | None,
    path: str,
    *,
    canonical: ProviderCanonicalName | None = None,
) -> str:
    """从已配置官方端点保留 scheme/host/port 并替换为 Files API 版本路径。"""

    try:
        parsed = urlsplit(str(value or ""))
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and port != 443)
        or (canonical is not None and not _provider_host_allowed(canonical, hostname.lower()))
    ):
        return ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, path.rstrip("/"), "", "")).rstrip("/")


def _validate_file_api_override(canonical: ProviderCanonicalName, chat_url: str | None, override: str) -> None:
    """限制 Files API 覆盖地址只能同源官方 host，阻断配置型 SSRF。"""

    try:
        parsed = urlsplit(override)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PROVIDER_FILE_API_BASE_URL_INVALID") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and port != 443)
    ):
        raise ValueError("PROVIDER_FILE_API_BASE_URL_INVALID")
    host = (parsed.hostname or "").lower()
    if not _provider_host_allowed(canonical, host):
        raise ValueError("PROVIDER_FILE_API_BASE_URL_HOST_INVALID")
    if _hostname(chat_url) and _hostname(chat_url) != host:
        raise ValueError("PROVIDER_FILE_API_BASE_URL_HOST_MISMATCH")
    expected_paths = {
        "ark": "/api/v3",
        "deepseek": "",
        "siliconflow": "/v1",
    }
    expected_path = expected_paths.get(canonical)
    if expected_path is not None and parsed.path.rstrip("/") != expected_path:
        raise ValueError("PROVIDER_FILE_API_BASE_URL_PATH_INVALID")


def _provider_host_allowed(canonical: ProviderCanonicalName, host: str) -> bool:
    """判断 Files 地址是否属于该供应商官方域名集合，阻断配置型 SSRF。"""

    if canonical == "ark":
        return host in _ARK_HOSTS or (
            host.startswith("ark.") and any(host.endswith(suffix) for suffix in _ARK_HOST_SUFFIXES)
        )
    if canonical == "deepseek":
        return host in _DEEPSEEK_HOSTS
    if canonical == "siliconflow":
        return host in _SILICONFLOW_HOSTS
    return False


def _safe_provider_file_id(
    value: object,
    *,
    prefixes: tuple[str, ...] | None = None,
) -> str | None:
    """白名单化供应商 file-id，拒绝路径分隔符、控制字符和过长值。"""

    text = str(value or "").strip()
    if prefixes and not text.startswith(prefixes):
        return None
    return text if _SAFE_FILE_ID.fullmatch(text) else None


def _safe_text(value: object, limit: int) -> str | None:
    """截断供应商元数据字段，避免把未界定正文写入审计细节。"""

    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """仅接受 JSON object，数组或非法 JSON 归一为空对象。"""

    try:
        body = response.json()
    except ValueError:
        return {}
    return dict(body) if isinstance(body, Mapping) else {}


def _json_value(response: httpx.Response) -> object:
    """读取原始 JSON 值，允许 Ark 空文件列表用 null 表示合法空集合。"""

    try:
        return response.json()
    except ValueError:
        return _JSON_INVALID


def _project_file_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """投影可用于对账的文件身份与生命周期字段，不保留 provider 任意扩展正文。"""

    projected: dict[str, object] = {}
    for key, limit in (
        ("id", 256),
        ("object", 32),
        ("filename", 255),
        ("purpose", 32),
        ("status", 32),
    ):
        value_text = _safe_text(value.get(key), limit)
        if value_text is not None:
            projected[key] = value_text
    for key in ("bytes", "created_at", "createdAt", "expires_at", "expire_at"):
        numeric = value.get(key)
        if isinstance(numeric, int) and not isinstance(numeric, bool):
            projected[key] = numeric
    return projected


def _response_detail(response: httpx.Response, operation: str, *, secret: str) -> dict[str, object]:
    """从供应商错误中提取稳定诊断，不回显密钥、请求体或文件正文。"""

    body = _json_object(response)
    error = body.get("error") if isinstance(body.get("error"), Mapping) else body
    code = _safe_text(error.get("code") if isinstance(error, Mapping) else None, 64)
    message = _safe_text(error.get("message") if isinstance(error, Mapping) else None, 240)
    if message and secret:
        message = message.replace(secret, "***")
    detail: dict[str, object] = {
        "operation": operation,
        "http_status": response.status_code,
    }
    if code:
        detail["provider_code"] = code
    if message:
        detail["provider_message"] = message
    return detail


__all__ = [
    "ProviderFileApiAdapter",
    "ProviderFileListResult",
    "ProviderFileProfile",
    "ProviderFileUploadResult",
    "canonical_provider_name",
    "is_supported_chat_provider",
    "profile_for_model_config",
    "provider_file_profile_payload",
]
