"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : test_model_configs_api.py
@CallChain  : pytest → model_configs API helpers → SQLite/SQLModel
@Description: 回归默认模型唯一约束、并发切换，以及目录未列别名时以实际生成验证配置。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.model_configs import (
    preflight_dynamic_model_config,
    set_default_model_config,
    test_model_config as run_model_config_test,
    update_model_config,
)
from app.api import model_configs as model_config_api
from app.db.models import ModelConfig, Tenant
from app.security.encryption import encrypt_secret
from app.llm.schemas import ModelConfigUpdateRequest


def _engine(tmp_path):
    """创建支持多线程会话和忙等待的文件型 SQLite 测试引擎。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'model-configs.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_models(engine) -> None:
    """写入一个租户、一个旧默认模型和两个可切换模型。"""

    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        db.add_all(
            [
                ModelConfig(
                    id="z_previous",
                    tenant_id="tenant_a",
                    name="Previous",
                    api_key_encrypted=encrypt_secret("secret"),
                    model="model-previous",
                    is_default=True,
                ),
                ModelConfig(
                    id="a_next",
                    tenant_id="tenant_a",
                    name="Next",
                    api_key_encrypted=encrypt_secret("secret"),
                    model="model-next",
                ),
                ModelConfig(
                    id="b_next",
                    tenant_id="tenant_a",
                    name="Another",
                    api_key_encrypted=encrypt_secret("secret"),
                    model="model-another",
                ),
            ]
        )
        db.commit()


def test_model_schema_rejects_two_defaults_for_one_tenant(tmp_path) -> None:
    """验证实际 SQLModel 建表结果拒绝同租户的两个默认模型。"""

    engine = _engine(tmp_path)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        db.add_all(
            [
                ModelConfig(
                    id="model_a",
                    tenant_id="tenant_a",
                    name="A",
                    api_key_encrypted="",
                    model="a",
                    is_default=True,
                ),
                ModelConfig(
                    id="model_b",
                    tenant_id="tenant_a",
                    name="B",
                    api_key_encrypted="",
                    model="b",
                    is_default=True,
                ),
            ]
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("同租户重复默认模型未被数据库唯一索引拒绝")


def test_switching_default_clears_existing_row_before_setting_new(tmp_path) -> None:
    """验证 ID 排序相反时仍先清旧默认，避免 ORM flush 的瞬时唯一冲突。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    with Session(engine) as db:
        result = set_default_model_config("a_next", tenant_id="tenant_a", db=db)
        previous = db.get(ModelConfig, "z_previous")

        assert previous is not None and previous.is_default is False
        assert result.id == "a_next"
        assert result.is_default is True


def test_concurrent_default_switches_are_serialized_per_tenant(tmp_path) -> None:
    """验证同租户并发切换均能完成，最终数据库仍恰有一个默认模型。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    barrier = threading.Barrier(2)

    def switch(model_id: str) -> str:
        """在独立会话同步起跑一次默认模型切换。"""

        barrier.wait()
        with Session(engine) as db:
            return set_default_model_config(model_id, tenant_id="tenant_a", db=db).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        switched = set(executor.map(switch, ("a_next", "b_next")))

    with Session(engine) as db:
        defaults = db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == "tenant_a",
                ModelConfig.is_default == True,  # noqa: E712 - SQLModel expression.
            )
        ).all()

    assert switched == {"a_next", "b_next"}
    assert len(defaults) == 1


def test_dynamic_preflight_persists_capabilities_and_config_change_invalidates(
    tmp_path, monkeypatch
) -> None:
    """验证预检成功事实落库，且切换模型后必须重新验证。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.preflight_dynamic_capabilities",
        lambda self: {
            "protocol_version": "dynamic-v1",
            "sdk_available": True,
            "credentials_verified": True,
            "structured_output": True,
            "tool_calling": True,
        },
    )
    with Session(engine) as db:
        response = preflight_dynamic_model_config("a_next", tenant_id="tenant_a", db=db)
        persisted = db.get(ModelConfig, "a_next")
        assert response.success is True
        assert persisted is not None and persisted.preflight_status == "ready"
        assert persisted.capability_checksum == response.checksum
        previous_generation = persisted.memory_capture_generation

        update_model_config(
            "a_next",
            ModelConfigUpdateRequest(tenant_id="tenant_a", model="replacement-model"),
            db,
            _admin_user_for_model_test(),
        )
        db.refresh(persisted)
        assert persisted.preflight_status == "unverified"
        assert persisted.capability_snapshot_json == {}
        assert persisted.capability_checksum is None
        assert persisted.memory_capture_generation == previous_generation + 1


def test_connection_test_reports_auth_catalog_and_generation_success(
    tmp_path, monkeypatch
) -> None:
    """普通兼容端点应展示模型目录与最小生成两个独立成功阶段。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.probe_model_catalog",
        lambda self: ["model-next"],
    )
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.probe_text_connection",
        lambda self: "连接成功",
    )
    with Session(engine) as db:
        response = run_model_config_test("a_next", tenant_id="tenant_a", db=db)

    assert response.success is True
    assert response.output == "连接成功"
    assert [(item.name, item.status) for item in response.checks] == [
        ("配置", "passed"),
        ("模型目录", "passed"),
        ("账户状态", "skipped"),
        ("最小生成", "passed"),
    ]


def test_connection_test_uses_generation_when_catalog_omits_working_model_alias(
    tmp_path, monkeypatch
) -> None:
    """目录未列出兼容别名时，必须以配置模型的实际生成结果判定可用性。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    generation_calls: list[str] = []
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.probe_model_catalog",
        lambda self: ["provider-family-v2", "provider-family-fast"],
    )

    def generate(self) -> str:
        """记录最小生成已实际尝试，并模拟供应商接受配置的兼容别名。"""

        generation_calls.append(self.model)
        return "连接成功"

    monkeypatch.setattr("app.api.model_configs.LLMClient.probe_text_connection", generate)
    with Session(engine) as db:
        response = run_model_config_test("a_next", tenant_id="tenant_a", db=db)

    assert response.success is True
    assert response.output == "连接成功"
    assert generation_calls == ["model-next"]
    assert [(item.name, item.status) for item in response.checks] == [
        ("配置", "passed"),
        ("模型目录", "skipped"),
        ("账户状态", "skipped"),
        ("最小生成", "passed"),
    ]


def test_connection_test_reports_generation_failure_after_catalog_alias_mismatch(
    tmp_path, monkeypatch
) -> None:
    """目录别名不匹配时仍须发出最小生成，并以真实生成失败给出最终结论。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    generation_calls: list[str] = []
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.probe_model_catalog",
        lambda self: ["provider-family-v2"],
    )

    def fail_generation(self) -> str:
        """记录生成确实被调用，再返回稳定的供应商传输失败。"""

        generation_calls.append(self.model)
        from app.llm.client import LLMError

        raise LLMError("provider_transport_error")

    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.probe_text_connection", fail_generation
    )
    with Session(engine) as db:
        response = run_model_config_test("a_next", tenant_id="tenant_a", db=db)

    assert response.success is False
    assert response.error_code == "MODEL_CONNECTION_FAILED"
    assert generation_calls == ["model-next"]
    assert [(item.name, item.status) for item in response.checks] == [
        ("配置", "passed"),
        ("模型目录", "skipped"),
        ("账户状态", "skipped"),
        ("最小生成", "failed"),
    ]


def test_connection_test_explains_deepseek_key_account_balance(
    tmp_path, monkeypatch
) -> None:
    """DeepSeek目录认证成功但Key账户不可用时必须明确归类计费而非误报网络故障。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    with Session(engine) as db:
        row = db.get(ModelConfig, "a_next")
        assert row is not None
        row.base_url = "https://api.deepseek.com"
        db.add(row)
        db.commit()
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.probe_model_catalog",
        lambda self: ["model-next"],
    )

    class BalanceResponse:
        """模拟DeepSeek对当前API Key返回不可用余额的identity响应流。"""

        status_code = 200
        headers = {"content-encoding": "identity"}

        def iter_raw(self, chunk_size: int):
            """返回有界合成余额正文，不包括任何账户身份。"""
            yield (
                b'{"is_available":false,"balance_infos":'
                b'[{"currency":"CNY","total_balance":"-0.13"}]}'
            )

    class BalanceStream:
        """提供与httpx同步流式响应相同的上下文边界。"""

        def __enter__(self) -> BalanceResponse:
            """开始合成请求并返回响应。"""
            return BalanceResponse()

        def __exit__(self, *_exc_info: object) -> None:
            """结束合成响应流。"""

    monkeypatch.setattr(
        "app.api.model_configs.httpx.stream",
        lambda *_args, **_kwargs: BalanceStream(),
    )
    with Session(engine) as db:
        response = run_model_config_test("a_next", tenant_id="tenant_a", db=db)

    assert response.success is False
    assert response.error_code == "BILLING_UNAVAILABLE"
    assert response.http_status == 402
    assert response.suggestion == "请确认充值的是这把 API Key 所属账户或项目，充值后重新测试。"
    assert response.checks[-2].name == "账户状态"
    assert response.checks[-2].status == "failed"
    assert "CNY余额 -0.13" in response.checks[-2].message
    assert response.checks[-1].name == "最小生成"
    assert response.checks[-1].status == "skipped"


@pytest.mark.parametrize("content_length", [64 * 1024 + 1, None])
def test_deepseek_balance_response_is_bounded_before_json_parse(
        tmp_path, monkeypatch, content_length: int | None) -> None:
    """DeepSeek余额响应超上限时应停止读取并继续标记账户状态不可用。"""
    engine = _engine(tmp_path)
    _seed_models(engine)
    with Session(engine) as db:
        row = db.get(ModelConfig, "a_next")
        assert row is not None
        row.base_url = "https://api.deepseek.com"
        db.add(row)
        db.commit()
        row = db.get(ModelConfig, "a_next")
        assert row is not None

        read_chunks: list[int] = []
        stream_calls: list[dict[str, object]] = []

        class OversizedResponse:
            """只暴露超限响应元数据，并记录单次有限流块。"""

            status_code = 200

            def __init__(self) -> None:
                """为合成响应设置可选长度和identity编码头。"""
                self.headers = {"content-encoding": "identity"}
                if content_length is not None:
                    self.headers["content-length"] = str(content_length)

            def iter_raw(self, chunk_size: int):
                """返回一块超过接受正文上限的合成流并记录最多读取字节。"""
                read_chunks.append(chunk_size)
                yield b"x" * chunk_size

        class StreamContext:
            """模拟httpx流式请求上下文而不建立网络连接。"""

            def __enter__(self) -> OversizedResponse:
                """返回合成流响应。"""
                return OversizedResponse()

            def __exit__(self, *_exc_info: object) -> None:
                """关闭合成请求上下文。"""

        def stream_response(method: str, url: str, *, headers: dict[str, str], timeout: int):
            """记录不含端点和认证头值的请求边界，再返回合成响应。"""
            stream_calls.append({
                "method": method,
                "balance_path": url.endswith("/user/balance"),
                "timeout": timeout,
                "accept_encoding": headers.get("Accept-Encoding"),
            })
            return StreamContext()

        monkeypatch.setattr(model_config_api.httpx, "stream", stream_response)
        monkeypatch.setattr(
            model_config_api.httpx,
            "get",
            lambda *_args, **_kwargs: type(
                "UnboundedResponse",
                (),
                {"status_code": 200, "content": b"{}", "json": lambda _self: {"is_available": True}},
            )(),
        )

        checks = []
        result = model_config_api._deepseek_balance_failure(row, checks)

    assert result is None
    assert checks[-1].name == "账户状态"
    assert checks[-1].status == "skipped"
    assert model_config_api.DEEPSEEK_BALANCE_MAX_RESPONSE_BYTES == 64 * 1024
    assert stream_calls == [{
        "method": "GET",
        "balance_path": True,
        "timeout": 15,
        "accept_encoding": "identity",
    }]
    assert sum(read_chunks) <= 64 * 1024 + 1


def test_dynamic_preflight_accepts_supported_provider_aliases(tmp_path, monkeypatch) -> None:
    """Ark、DeepSeek、SiliconFlow 均沿用统一 Chat 协议，不应再被旧 provider 门禁拒绝。"""

    engine = _engine(tmp_path)
    _seed_models(engine)
    monkeypatch.setattr(
        "app.api.model_configs.LLMClient.preflight_dynamic_capabilities",
        lambda self: {
            "protocol_version": "dynamic-v1",
            "sdk_available": True,
            "credentials_verified": True,
            "structured_output": True,
            "tool_calling": True,
        },
    )
    provider_urls = {
        "a_next": ("ark", "https://ark.cn-beijing.volces.com/api/plan/v3"),
        "b_next": ("deepseek", "https://api.deepseek.com"),
        "z_previous": ("siliconflow", "https://api.siliconflow.cn/v1"),
    }
    with Session(engine) as db:
        for config_id, (provider, base_url) in provider_urls.items():
            row = db.get(ModelConfig, config_id)
            assert row is not None
            row.provider = provider
            row.base_url = base_url
            db.add(row)
            db.commit()
            result = preflight_dynamic_model_config(config_id, tenant_id="tenant_a", db=db)
            assert result.success is True
            assert result.capabilities["provider"] == provider
            provider_file_api = result.capabilities["provider_file_api"]
            assert isinstance(provider_file_api, dict)
            assert provider_file_api["chat_supported"] is True
            assert provider_file_api["canonical_provider"] == {
                "ark": "ark",
                "deepseek": "deepseek",
                "siliconflow": "siliconflow",
            }[provider]


def _admin_user_for_model_test():  # noqa: ANN201
    """构造模型配置更新的租户管理员身份。"""

    from app.db.models import User

    return User(
        id="admin_a",
        tenant_id="tenant_a",
        username="admin",
        role="admin",
        password_hash="test",
    )
