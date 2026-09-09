"""
@Time       : 2026/09/02 14:50
@Author     : zhanglp8181
@File       : test_vertical_ability_settings.py
@CallChain  : pytest → vertical ability settings API → encrypted PlatformSecret/runtime credential
@Description: 验证能力包平台密钥的管理员写入、密文存储、健康验证和普通用户边界。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.vertical_abilities import router as vertical_abilities_router
from app.api.vertical_ability_settings import router
from app.config import get_settings
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.db import get_session
from app.db.models import ManagementAuditLog, PlatformSecret, SopInstance, User
from app.security.auth import get_current_user
from app.security.encryption import decrypt_secret
from app.tools.tool_schema import ToolCall
from app.vertical_abilities.executor import VerticalAbilityToolExecutor
from app.vertical_abilities.credentials import resolve_jina_credential, save_jina_credential
from app.vertical_abilities.resolver import VerticalAbilityResolver


@pytest.fixture
def settings_without_jina(monkeypatch: pytest.MonkeyPatch):
    """隔离真实环境变量，确保 API 测试明确覆盖数据库来源。"""

    monkeypatch.setenv("APP_SECRET", "vertical-ability-settings-test-secret")
    monkeypatch.setenv("VERTICAL_ABILITY_JINA_API_KEY", "")
    monkeypatch.setenv("VERTICAL_ABILITY_GROQ_API_KEY", "")
    monkeypatch.setenv("VERTICAL_ABILITY_OPENAI_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client_for_user(
    db: Session,
    current_user: User,
    *,
    include_vertical_abilities: bool = False,
) -> TestClient:
    """创建带可选公共能力目录路由、且使用隔离数据库会话的测试客户端。"""

    app = FastAPI()
    app.include_router(router)
    if include_vertical_abilities:
        app.include_router(vertical_abilities_router)
    app.dependency_overrides[get_current_user] = lambda: current_user

    def session_override():
        """为测试请求提供同一个可回收 SQLModel 会话。"""

        yield db

    app.dependency_overrides[get_session] = session_override
    return TestClient(app)


def test_logged_in_member_can_check_jina_readiness_without_access_to_admin_settings(
    settings_without_jina,
) -> None:
    """普通登录用户可检查选中能力后的 Jina 状态，但响应不包含任何密钥信息。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    member = User(
        id="user_vertical_readiness_member",
        tenant_id="tenant_vertical",
        username="readiness-member",
        role="member",
        password_hash="unused",
    )
    with Session(engine) as db:
        client = _client_for_user(db, member, include_vertical_abilities=True)
        response = client.get("/api/chat/abilities/web-intelligence/readiness")

        assert response.status_code == 200
        assert response.json() == {
            "ability_id": "web-intelligence",
            "configured": False,
            "provider": "jina",
            "provider_name": "Jina Reader",
            "credential_label": "Jina API Key",
            "message": "Jina API Key 尚未配置，请联系管理员前往“能力包配置”页面配置。",
        }
        assert "encrypted" not in response.text
    engine.dispose()


def test_settings_catalog_returns_provider_cards_for_the_configuration_center(
    settings_without_jina,
) -> None:
    """配置中心应返回数据驱动的 Provider 卡片目录，而不是绑定单个能力包页面。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user_vertical_catalog_admin",
        tenant_id="tenant_vertical",
        username="catalog-admin",
        role="admin",
        password_hash="unused",
    )
    with Session(engine) as db:
        client = _client_for_user(db, admin)
        response = client.get("/api/enterprise/vertical-ability-settings")

        assert response.status_code == 200
        body = response.json()
        assert body["abilities"] == [
            {
                "id": "web-intelligence",
                "name": "全网情报员",
                "category": "互联网情报",
                "icon_key": "globe",
                    "description": "把全网搜索、网页原文、RSS、V2EX、GitHub、视频、社交平台、职业与财经公开信息组织成可追溯的情报结果，并支持公开音频转写。",
                    "platform_tags": ["网页", "Exa 搜索", "RSS/Atom", "V2EX", "GitHub", "Twitter/X", "Reddit", "B站", "YouTube", "小红书", "LinkedIn", "雪球", "小宇宙"],
                    "version": "1.7.4",
                "checksum": body["abilities"][0]["checksum"],
                "configuration_status": "partial",
                "configuration_message": "部分平台依赖尚未配置；公共渠道仍可用，网站登录连接由用户按需完成。",
                        "resource_count": 24,
                        "ready_resource_count": 7,
                    "provider_ids": [
                        "jina",
                        "exa",
                        "github",
                        "twitter",
                        "reddit",
                        "bilibili",
                        "facebook",
                        "instagram",
                        "xiaohongshu",
                        "linkedin",
                        "xueqiu",
                        "groq",
                        "openai",
                    ],
                }
            ]
        assert body["providers"] == [
            {
                "provider": "jina",
                "ability_ids": ["web-intelligence"],
                "ability_names": ["全网情报员"],
                "provider_name": "Jina Reader",
                "description": "读取公开网页正文，为全网情报员提供可引用的原文内容。",
                "credential_label": "Jina API Key",
                "credential_placeholder": "粘贴 Jina API Key",
                "credential_setup_text": "打开 Jina 官方控制台，登录并创建 API Key：",
                "credential_setup_url": "https://jina.ai/api-dashboard",
                "credential_setup_link_label": "Jina API Dashboard",
                "supports_verification": True,
                "configured": False,
                "source": "none",
                "credential_masked": None,
                "revision": 0,
                "health_status": "unverified",
                "health_error_code": None,
                "last_checked_at": None,
                "last_healthy_at": None,
                "updated_at": None,
                "verification_success": None,
                "message": None,
            },
            {
                "provider": "groq",
                "ability_ids": ["web-intelligence"],
                "ability_names": ["全网情报员"],
                "provider_name": "Groq Whisper",
                "description": "在 YouTube 公开视频没有可用字幕时，通过 Whisper 将视频音频转换为可引用文本。",
                "credential_label": "Groq API Key",
                "credential_placeholder": "粘贴 Groq API Key",
                "credential_setup_text": "打开 Groq 官方控制台，登录并创建 API Key：",
                "credential_setup_url": "https://console.groq.com/keys",
                "credential_setup_link_label": "Groq API Keys",
                "supports_verification": True,
                "configured": False,
                "source": "none",
                "credential_masked": None,
                "revision": 0,
                "health_status": "unverified",
                "health_error_code": None,
                "last_checked_at": None,
                "last_healthy_at": None,
                "updated_at": None,
                "verification_success": None,
                "message": None,
            },
            {
                "provider": "openai",
                "ability_ids": ["web-intelligence"],
                "ability_names": ["全网情报员"],
                "provider_name": "OpenAI Whisper",
                "description": "在 YouTube 公开视频没有可用字幕时，通过 OpenAI Whisper 将视频音频转换为可引用文本。",
                "credential_label": "OpenAI API Key",
                "credential_placeholder": "粘贴 OpenAI API Key",
                "credential_setup_text": "打开 OpenAI 官方控制台，登录并创建 API Key：",
                "credential_setup_url": "https://platform.openai.com/api-keys",
                "credential_setup_link_label": "OpenAI API Keys",
                "supports_verification": True,
                "configured": False,
                "source": "none",
                "credential_masked": None,
                "revision": 0,
                "health_status": "unverified",
                "health_error_code": None,
                "last_checked_at": None,
                "last_healthy_at": None,
                "updated_at": None,
                "verification_success": None,
                "message": None,
            },
        ]
    engine.dispose()


def test_settings_detail_returns_package_resources_and_related_providers(
    settings_without_jina,
) -> None:
    """验证能力包详情按资源类型返回公共渠道和对应 Provider，不再绑定单个设置首页。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user_vertical_detail_admin",
        tenant_id="tenant_vertical",
        username="detail-admin",
        role="admin",
        password_hash="unused",
    )
    with Session(engine) as db:
        client = _client_for_user(db, admin)
        response = client.get(
            "/api/enterprise/vertical-ability-settings/abilities/web-intelligence"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ability"]["id"] == "web-intelligence"
        assert body["configuration_status"] == "partial"
        assert {item["resource_id"] for item in body["resources"]} == {
            "jina-reader",
            "exa-search",
            "rss-atom",
            "v2ex-public",
            "github-public",
            "github-connection",
            "twitter-connection",
            "reddit-connection",
            "bilibili-session",
            "facebook-connection",
            "instagram-connection",
            "xiaohongshu-connection",
            "linkedin-connection",
            "xueqiu-connection",
            "bilibili-public",
            "opencli-browser-runtime",
            "twitter-cli-runtime",
            "linkedin-public",
            "xueqiu-public",
            "youtube-runtime",
            "groq-whisper",
            "openai-whisper",
            "media-transcription-runtime",
            "xiaoyuzhou-transcription-runtime",
        }
        jina_resource = next(
            item for item in body["resources"] if item["resource_id"] == "jina-reader"
        )
        assert jina_resource["status"] == "needs_configuration"
        assert next(
            item for item in body["resources"] if item["resource_id"] == "exa-search"
        )["status"] == "ready"
        assert [item["provider"] for item in body["providers"]] == ["jina", "groq", "openai"]

        missing = client.get(
            "/api/enterprise/vertical-ability-settings/abilities/not-found"
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "VERTICAL_ABILITY_NOT_FOUND"
    engine.dispose()


def test_admin_can_save_encrypted_jina_key_and_verify_without_echoing_secret(
    settings_without_jina,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理员保存后应立即可解析，响应和审计均不得出现完整 API Key。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user_vertical_admin",
        tenant_id="tenant_vertical",
        username="admin",
        role="admin",
        password_hash="unused",
    )
    raw_key = "jina-test-key-never-echoed"
    with Session(engine) as db:
        client = _client_for_user(db, admin)
        initial = client.get("/api/enterprise/vertical-ability-settings/jina")
        assert initial.status_code == 200
        assert initial.json()["configured"] is False

        saved = client.put(
            "/api/enterprise/vertical-ability-settings/jina",
            json={"credential": raw_key, "expected_revision": 0},
        )
        assert saved.status_code == 200
        saved_body = saved.json()
        assert saved_body["configured"] is True
        assert saved_body["source"] == "database"
        assert saved_body["revision"] == 1
        assert raw_key not in saved.text

        row = db.exec(select(PlatformSecret)).one()
        assert row.encrypted_value != raw_key
        assert decrypt_secret(row.encrypted_value) == raw_key
        assert resolve_jina_credential(db).value == raw_key

        monkeypatch.setattr(
            "app.api.vertical_ability_settings.execute_vertical_adapter",
            lambda *_args, **_kwargs: {"content": "ok"},
        )
        verified = client.post("/api/enterprise/vertical-ability-settings/jina/verify")
        assert verified.status_code == 200
        assert verified.json()["verification_success"] is True
        assert verified.json()["health_status"] == "healthy"

        audit_rows = db.exec(select(ManagementAuditLog)).all()
        assert len(audit_rows) == 1
        assert raw_key not in str(audit_rows[0].model_dump())
    engine.dispose()


def test_admin_can_save_and_verify_groq_provider_without_echoing_secret(
    settings_without_jina,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证新增 Provider 复用同一配置 API、密文表和健康状态，而不需要复制页面。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user_vertical_groq_admin",
        tenant_id="tenant_vertical",
        username="groq-admin",
        role="admin",
        password_hash="unused",
    )
    raw_key = "fixture-provider-value-never-echoed"
    with Session(engine) as db:
        client = _client_for_user(db, admin)
        saved = client.put(
            "/api/enterprise/vertical-ability-settings/groq",
            json={"credential": raw_key, "expected_revision": 0},
        )
        assert saved.status_code == 200
        assert saved.json()["provider"] == "groq"
        assert saved.json()["revision"] == 1
        assert raw_key not in saved.text

        rows = db.exec(select(PlatformSecret)).all()
        assert len(rows) == 1
        assert rows[0].provider == "groq"
        assert rows[0].encrypted_value != raw_key
        assert raw_key not in str(rows[0].model_dump())

        monkeypatch.setattr(
            "app.api.vertical_ability_settings.execute_vertical_provider_probe",
            lambda *_args, **_kwargs: {"models_count": 1},
        )
        verified = client.post("/api/enterprise/vertical-ability-settings/groq/verify")
        assert verified.status_code == 200
        assert verified.json()["verification_success"] is True
        assert verified.json()["health_status"] == "healthy"

        catalog = client.get("/api/enterprise/vertical-ability-settings")
        groq = next(item for item in catalog.json()["providers"] if item["provider"] == "groq")
        assert groq["configured"] is True
        assert groq["credential_masked"]
        assert groq["health_status"] == "healthy"
        assert raw_key not in catalog.text
    engine.dispose()


def test_stale_revision_is_rejected_and_member_cannot_manage_platform_key(
    settings_without_jina,
) -> None:
    """并发旧修订必须冲突，普通成员不能写入平台凭据。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user_vertical_admin_two",
        tenant_id="tenant_vertical",
        username="admin-two",
        role="admin",
        password_hash="unused",
    )
    member = User(
        id="user_vertical_member",
        tenant_id="tenant_vertical",
        username="member",
        role="member",
        password_hash="unused",
    )
    with Session(engine) as db:
        admin_client = _client_for_user(db, admin)
        first = admin_client.put(
            "/api/enterprise/vertical-ability-settings/jina",
            json={"credential": "first-key", "expected_revision": 0},
        )
        assert first.status_code == 200

        stale = admin_client.put(
            "/api/enterprise/vertical-ability-settings/jina",
            json={"credential": "stale-key", "expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "VERTICAL_ABILITY_CREDENTIAL_REVISION_CONFLICT"

        member_client = _client_for_user(db, member)
        denied = member_client.put(
            "/api/enterprise/vertical-ability-settings/jina",
            json={"credential": "member-key", "expected_revision": 1},
        )
        assert denied.status_code == 403
    engine.dispose()


def test_environment_key_remains_a_compatible_runtime_fallback() -> None:
    """没有数据库记录时仍兼容服务器环境变量，但标记来源为 environment。"""

    credential = resolve_jina_credential(
        None,
        settings=SimpleNamespace(vertical_ability_jina_api_key="environment-key"),
    )

    assert credential.value == "environment-key"
    assert credential.source == "environment"
    assert credential.revision == 0


def test_runtime_executor_injects_database_key_without_restarting_service(
    settings_without_jina,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证动态 Execution 每次调用读取最新数据库密钥，而不是启动时固定环境值。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user_vertical_runtime_admin",
        tenant_id="tenant_vertical",
        username="runtime-admin",
        role="admin",
        password_hash="unused",
    )
    captured: dict[str, object] = {}
    with Session(engine) as db:
        save_jina_credential(
            db,
            api_key="database-runtime-key",
            current_user=admin,
            expected_revision=0,
        )
        resolution = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_vertical",
            agent_id="agent_vertical",
        )
        instance = SopInstance(
            id="sopinst_vertical_runtime_key",
            tenant_id="tenant_vertical",
            session_id="session_vertical",
            kind="dynamic_task",
            initiator_user_id=admin.id,
            agent_id="agent_vertical",
            goal_snapshot_json={"goal": "读取网页"},
            current_plan_revision_id="plan_vertical",
            current_plan_checksum="a" * 64,
            capability_snapshot_json={
                **resolution.snapshot,
                "tools": [item.model_dump(mode="json") for item in resolution.capabilities],
            },
            status="running",
            active_slot_key="dynamic:vertical",
        )
        instance.capability_checksum = capability_checksum(instance.capability_snapshot_json)
        db.add(instance)
        db.commit()

        def fake_adapter(*_args: object, **kwargs: object) -> dict[str, object]:
            """捕获运行时传入的数据库凭据，不执行公网请求。"""

            captured.update(kwargs)
            return {"content": "ok"}

        monkeypatch.setattr(
            "app.vertical_abilities.executor.execute_vertical_adapter",
            fake_adapter,
        )
        result = VerticalAbilityToolExecutor(db).execute(
            "tenant_vertical",
            ToolCall(name="intelligence.web_read", arguments={"url": "https://example.com/"}),
            agent_id="agent_vertical",
            actor_user_id=admin.id,
            execution_id=instance.id,
        )

        assert result.success is True
        assert captured["jina_api_key"] == "database-runtime-key"
        assert captured["settings"] is not None
        assert captured["resolver"] is not None
    engine.dispose()
