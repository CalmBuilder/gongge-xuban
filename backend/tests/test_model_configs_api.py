"""
@Time       : 2026/07/27 16:05
@Author     : zhanglp8181
@File       : test_model_configs_api.py
@CallChain  : pytest → model_configs API helpers → SQLite/SQLModel
@Description: 回归默认模型唯一约束、无瞬时冲突切换及同租户并发串行化。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.model_configs import (
    preflight_dynamic_model_config,
    set_default_model_config,
    test_model_config as run_model_config_test,
    update_model_config,
)
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
        """模拟DeepSeek对当前API Key返回不可用余额。"""

        status_code = 200
        content = b"{}"

        @staticmethod
        def json() -> dict[str, object]:
            """返回不包含账户身份的余额事实。"""

            return {
                "is_available": False,
                "balance_infos": [{"currency": "CNY", "total_balance": "-0.13"}],
            }

    monkeypatch.setattr("app.api.model_configs.httpx.get", lambda *args, **kwargs: BalanceResponse())
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
