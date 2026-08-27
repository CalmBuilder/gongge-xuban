"""
@Time       : 2026/08/27 22:30
@Author     : zhanglp8181
@File       : test_provider_file_adapters.py
@CallChain  : pytest → provider capability profile → Files API adapter → 有限对账结果
@Description: 验证 Ark、DeepSeek、SiliconFlow 的文件端点、输入形态、错误收敛和凭据隔离。
"""

from __future__ import annotations

import httpx
import pytest

from app.db.models import ModelConfig
from app.security.encryption import encrypt_secret
from app.session.provider_file_adapters import (
    ProviderFileApiAdapter,
    canonical_provider_name,
    is_supported_chat_provider,
    profile_for_model_config,
    provider_file_profile_payload,
)
from app.session.provider_input_reconciliation_worker import (
    adapter_health_payload,
    build_provider_exposure_adapter,
)


def _model(*, provider: str, base_url: str) -> ModelConfig:
    """构造不落库的模型配置，测试只验证 URL/协议，不使用真实凭据。"""

    return ModelConfig(
        id=f"model-{provider}",
        tenant_id="tenant-test",
        name=provider,
        provider=provider,
        base_url=base_url,
        api_key_encrypted=encrypt_secret("test-secret"),
        model="test-model",
    )


def _client(handler):  # noqa: ANN001
    """用 MockTransport 记录请求并禁止测试越过本地协议边界访问公网。"""

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_provider_aliases_and_official_file_base_paths() -> None:
    """三家别名统一后，Files API 路径必须与 Chat Base URL 的版本差异保持明确。"""

    assert canonical_provider_name("volcengine") == "ark"
    assert canonical_provider_name("ark-agent-plan-cn") == "ark"
    assert canonical_provider_name("byteplus_ark") == "ark"
    assert canonical_provider_name("硅基流动") == "siliconflow"
    assert canonical_provider_name("deepseek") == "deepseek"
    assert is_supported_chat_provider("ark", "https://ark.cn-beijing.volces.com/api/plan/v3")
    assert is_supported_chat_provider("ark", "https://ark.ap-southeast.bytepluses.com/api/v3")
    assert is_supported_chat_provider("deepseek", "https://api.deepseek.com")
    assert is_supported_chat_provider("siliconflow", "https://api.siliconflow.cn/v1")
    assert canonical_provider_name("openai_compatible", "https://api.deepseek.com") == "deepseek"
    assert not is_supported_chat_provider("unknown", "https://example.test/v1")

    ark = profile_for_model_config(
        _model(provider="ark", base_url="https://ark.cn-beijing.volces.com/api/plan/v3")
    )
    deepseek = profile_for_model_config(
        _model(provider="deepseek", base_url="https://api.deepseek.com")
    )
    deepseek_v1 = profile_for_model_config(
        _model(provider="openai_compatible", base_url="https://api.deepseek.com/v1")
    )
    siliconflow = profile_for_model_config(
        _model(provider="siliconflow", base_url="https://api.siliconflow.cn/v1")
    )
    assert ark.files_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert deepseek.files_base_url == "https://api.deepseek.com"
    assert deepseek_v1.canonical_provider == "deepseek"
    assert deepseek_v1.files_base_url == "https://api.deepseek.com"
    assert siliconflow.files_base_url == "https://api.siliconflow.cn/v1"
    assert ark.upload_mode == "any" and ark.delete_supported is True
    assert deepseek.upload_mode == "image_only" and deepseek.delete_supported is True
    assert siliconflow.upload_mode == "batch_only" and siliconflow.delete_supported is False
    assert ark.list_limit_max == 100
    assert deepseek.list_limit_max == 1000

    regional_ark = profile_for_model_config(
        _model(provider="ark", base_url="https://ark.cn-shanghai.volces.com/api/v3")
    )
    assert regional_ark.files_base_url == "https://ark.cn-shanghai.volces.com/api/v3"


def test_provider_profile_payload_is_safe_and_complete() -> None:
    """能力快照包含三家 Files 差异，但不暴露配置密钥或可变 HTTP 对象。"""

    profile = profile_for_model_config(
        _model(provider="siliconflow", base_url="https://api.siliconflow.cn/v1")
    )
    payload = provider_file_profile_payload(profile)
    assert payload == {
        "canonical_provider": "siliconflow",
        "display_name": "SiliconFlow",
        "chat_protocol": "openai_compatible",
        "chat_supported": True,
        "files_configured": True,
        "files_supported": True,
        "upload_supported": True,
        "retrieve_supported": False,
        "list_supported": True,
        "delete_supported": False,
        "upload_purpose": "batch",
        "upload_mode": "batch_only",
        "upload_limit_bytes": None,
        "list_limit_max": 1000,
        "notes": ["/files 是 Batch 文件接口；在线附件继续使用受控 image_url 内联，不伪造删除能力。"],
    }


def test_worker_factory_registers_provider_profile_without_exposing_secret() -> None:
    """worker 工厂使用真实模型配置构造适配器，健康面只投影能力而不回显密钥。"""

    config = _model(provider="ark", base_url="https://ark.cn-beijing.volces.com/api/plan/v3")
    adapter = build_provider_exposure_adapter(config)
    health = adapter_health_payload(adapter)
    assert isinstance(adapter, ProviderFileApiAdapter)
    assert health["adapter_type"] == "ProviderFileApiAdapter"
    assert health["provider_file_api"]["canonical_provider"] == "ark"
    assert "test-secret" not in str(health)


def test_ark_upload_retrieve_delete_and_chat_file_part() -> None:
    """Ark 应按 user_data 上传，检索身份后可生成 Chat image_url.file_id 并删除。"""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """按官方三步端点返回最小合法响应，并记录请求头与 multipart。"""

        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/api/v3/files"
            body = request.content
            assert b"name=\"purpose\"" in body and b"user_data" in body
            assert b"name=\"file\"" in body and b"demo.png" in body
            return httpx.Response(200, json={"object": "file", "id": "file-ark-1", "status": "processing"})
        if request.method == "GET":
            assert request.url.path == "/api/v3/files/file-ark-1"
            return httpx.Response(200, json={"object": "file", "id": "file-ark-1", "status": "active"})
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/files/file-ark-1"
        return httpx.Response(200, json={"object": "file", "id": "file-ark-1", "deleted": True})

    adapter = ProviderFileApiAdapter(
        _model(provider="ark", base_url="https://ark.cn-beijing.volces.com/api/plan/v3"),
        client=_client(handler),
    )
    uploaded = adapter.upload_file(
        tenant_id="tenant-test",
        filename="demo.png",
        content=b"PNG-data",
        content_type="image/png",
    )
    assert uploaded.status == "uploaded" and uploaded.provider_file_id == "file-ark-1"
    reconciled = adapter.reconcile_exposure(
        tenant_id="tenant-test",
        provider_request_id="file-ark-1",
        dispatch_token="dispatch-token",
    )
    assert reconciled.kind == "found" and reconciled.provider_file_id == "file-ark-1"
    assert adapter.chat_file_part(provider_file_id="file-ark-1", content_type="image/png") == {
        "type": "image_url",
        "image_url": {"file_id": "file-ark-1"},
    }
    assert adapter.chat_file_part(provider_file_id="file-ark-1", content_type="application/pdf") == {
        "type": "file",
        "file": {"file_id": "file-ark-1"},
    }
    deleted = adapter.delete_file(tenant_id="tenant-test", provider_file_id="file-ark-1")
    assert deleted.kind == "deleted"
    assert len(requests) == 3
    assert all(request.headers["authorization"] == "Bearer test-secret" for request in requests)


def test_deepseek_upload_list_retrieve_delete_and_image_only_contract() -> None:
    """DeepSeek Files API 只允许四种图片，file-api ID 贯穿查询、Chat 和删除。"""

    paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回 DeepSeek 规范文件对象或列表对象。"""

        paths.append((request.method, request.url.path))
        if request.method == "POST":
            assert b"user_data" in request.content
            return httpx.Response(
                200,
                json={
                    "id": "file-api-deep-1",
                    "object": "file",
                    "filename": "demo.webp",
                    "purpose": "user_data",
                },
            )
        if request.method == "GET" and request.url.path == "/files":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "file-api-deep-1", "object": "file", "purpose": "user_data"}],
                    "has_more": False,
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": "file-api-deep-1", "object": "file", "purpose": "user_data"},
            )
        return httpx.Response(200, json={"id": "file-api-deep-1", "object": "file", "deleted": True})

    adapter = ProviderFileApiAdapter(
        _model(provider="deepseek", base_url="https://api.deepseek.com"),
        client=_client(handler),
    )
    uploaded = adapter.upload_file(
        tenant_id="tenant-test",
        filename="demo.webp",
        content=b"WEBP-data",
        content_type="image/webp",
    )
    assert uploaded.status == "uploaded"
    assert adapter.chat_file_part(provider_file_id="file-api-deep-1", content_type="image/webp") == {
        "type": "file",
        "file_id": "file-api-deep-1",
    }
    listed = adapter.list_files()
    assert listed.status == "listed" and listed.files[0]["id"] == "file-api-deep-1"
    found = adapter.reconcile_exposure(
        tenant_id="tenant-test",
        provider_request_id="file-api-deep-1",
        dispatch_token="dispatch-token",
    )
    assert found.kind == "found"
    assert adapter.delete_file(tenant_id="tenant-test", provider_file_id="file-api-deep-1").kind == "deleted"
    with pytest.raises(ValueError, match="IMAGE_ONLY"):
        adapter.chat_file_part(provider_file_id="file-api-deep-1", content_type="application/pdf")
    assert adapter.reconcile_exposure(
        tenant_id="tenant-test",
        provider_request_id="file-not-deepseek-prefix",
        dispatch_token="dispatch-token",
    ).kind == "unsupported"
    assert paths == [
        ("POST", "/files"),
        ("GET", "/files"),
        ("GET", "/files/file-api-deep-1"),
        ("DELETE", "/files/file-api-deep-1"),
    ]


def test_ark_empty_file_list_null_is_valid_but_invalid_json_is_not() -> None:
    """兼容 Ark 空列表的 JSON null，同时继续拒绝 200 非 JSON 响应。"""

    responses = iter((httpx.Response(200, content=b"null"), httpx.Response(200, text="upstream html")))

    def handler(_request: httpx.Request) -> httpx.Response:
        """按顺序返回合法空列表和非法正文。"""

        return next(responses)

    adapter = ProviderFileApiAdapter(
        _model(provider="ark", base_url="https://ark.cn-beijing.volces.com/api/v3"),
        client=_client(handler),
    )
    listed = adapter.list_files()
    assert listed.status == "listed" and listed.files == ()
    invalid = adapter.list_files()
    assert invalid.status == "failed"
    assert invalid.detail["code"] == "PROVIDER_FILE_LIST_RESPONSE_INVALID"


def test_siliconflow_is_batch_only_and_never_fakes_online_delete() -> None:
    """SiliconFlow 的 /files 只上传 Batch JSONL；删除和在线 Chat file-id 必须显式不支持。"""

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回 SiliconFlow 的嵌套 data 文件响应。"""

        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 20000, "status": True, "data": {"id": "file-sf-1", "purpose": "batch"}},
            )
        return httpx.Response(
            200,
            json={
                "code": 20000,
                "status": True,
                "data": {"object": "file", "data": [{"id": "file-sf-1", "purpose": "batch"}]},
            },
        )

    adapter = ProviderFileApiAdapter(
        _model(provider="siliconflow", base_url="https://api.siliconflow.cn/v1"),
        client=_client(handler),
    )
    rejected = adapter.upload_file(
        tenant_id="tenant-test",
        filename="demo.png",
        content=b"image",
        content_type="image/png",
    )
    assert rejected.status == "unsupported"
    uploaded = adapter.upload_file(
        tenant_id="tenant-test",
        filename="requests.jsonl",
        content=b'{"custom_id":"1"}\n',
        content_type="application/jsonl",
    )
    assert uploaded.status == "uploaded" and uploaded.provider_file_id == "file-sf-1"
    assert adapter.list_files().files[0]["id"] == "file-sf-1"
    assert adapter.reconcile_exposure(
        tenant_id="tenant-test",
        provider_request_id="file-sf-1",
        dispatch_token="dispatch-token",
    ).kind == "found"
    assert adapter.delete_file(tenant_id="tenant-test", provider_file_id="file-sf-1").kind == "unsupported"
    with pytest.raises(ValueError, match="CHAT_PART_UNSUPPORTED"):
        adapter.chat_file_part(provider_file_id="file-sf-1", content_type="image/png")
    assert calls == [("POST", "/v1/files"), ("GET", "/v1/files"), ("GET", "/v1/files")]


def test_provider_file_adapter_fail_closed_on_missing_identity_mismatch_and_network() -> None:
    """无 file-id、响应身份漂移和网络未知均不产生可删除成功。"""

    def mismatch(request: httpx.Request) -> httpx.Response:
        """返回不同 file-id，模拟代理或供应商响应错配。"""

        return httpx.Response(200, json={"id": "file-other", "object": "file"})

    adapter = ProviderFileApiAdapter(
        _model(provider="deepseek", base_url="https://api.deepseek.com"),
        client=_client(mismatch),
    )
    assert adapter.reconcile_exposure(
        tenant_id="tenant-test", provider_request_id=None, dispatch_token="dispatch-token"
    ).kind == "unsupported"
    assert adapter.reconcile_exposure(
        tenant_id="tenant-test", provider_request_id="file-api-deep-1", dispatch_token="dispatch-token"
    ).kind == "unknown"

    def network(_request: httpx.Request) -> httpx.Response:
        """让 MockTransport 显式抛出网络异常，验证 unknown 收敛。"""

        raise httpx.ReadTimeout("simulated timeout")

    network_adapter = ProviderFileApiAdapter(
        _model(provider="ark", base_url="https://ark.cn-beijing.volces.com/api/v3"),
        client=_client(network),
    )
    upload = network_adapter.upload_file(
        tenant_id="tenant-test", filename="demo.pdf", content=b"pdf", content_type="application/pdf"
    )
    assert upload.status == "unknown"
    assert "test-secret" not in str(upload.detail)


def test_file_api_override_cannot_cross_provider_host() -> None:
    """管理端的显式 Files URL 只能同源官方 host，防止借 provider 配置形成 SSRF。"""

    config = _model(provider="deepseek", base_url="https://api.deepseek.com")
    config.extra_body_json = {"provider_file_api": {"base_url": "https://evil.example/files"}}
    with pytest.raises(ValueError, match="HOST_INVALID"):
        profile_for_model_config(config)


@pytest.mark.parametrize(
    "override",
    [
        "http://api.deepseek.com/files",
        "https://user:pass@api.deepseek.com/files",
        "https://api.deepseek.com:8443/files",
        "https://api.deepseek.com/v1/files",
    ],
)
def test_file_api_override_rejects_unsafe_scheme_authority_port_and_path(override: str) -> None:
    """Files 覆盖地址不能通过协议、userinfo、非标准端口或错误版本路径越界。"""

    config = _model(provider="deepseek", base_url="https://api.deepseek.com")
    config.extra_body_json = {"provider_file_api": {"base_url": override}}
    with pytest.raises(ValueError):
        profile_for_model_config(config)


def test_official_origin_drops_chat_url_userinfo_before_file_request() -> None:
    """即使 Chat 地址误含 userinfo，Files URL 也不能把它复制到外发地址。"""

    config = _model(provider="deepseek", base_url="https://user:pass@api.deepseek.com")
    profile = profile_for_model_config(config)
    assert profile.files_base_url == ""
    assert profile.files_supported is False


def test_ark_unknown_host_does_not_create_file_api_target() -> None:
    """显式 Ark provider 也不能把任意自定义 host 当作官方 Files 端点。"""

    config = _model(provider="ark", base_url="https://ark.internal.example/api/v3")
    profile = profile_for_model_config(config)
    assert profile.files_base_url == ""
    assert profile.files_supported is False
