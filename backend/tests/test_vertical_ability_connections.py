"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_connections.py
@CallChain  : pytest → 网站连接 API → connection_service → 加密密文/租户用户隔离
@Description: 验证网站连接中心的平台专属字段、用户级隔离、CAS、浏览器桥接和撤销语义。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.vertical_ability_connections import router
from app.config import get_settings
from app.db import get_session
from app.db.models import User, VerticalAbilityConnection
from app.security.auth import get_current_user
from app.security.encryption import decrypt_secret, encrypt_secret
from app.vertical_abilities.browser_runtime import BrowserBridgeStatus
from app.vertical_abilities.connection_service import (
    VerticalAbilityConnectionError,
    decrypt_connection_credentials,
    resolve_user_connection_credentials,
    save_user_connection,
    verify_user_connection,
)
from app.vertical_abilities.relay_service import authenticate_relay_token, create_relay_pairing


@pytest.fixture
def connection_test_settings(monkeypatch: pytest.MonkeyPatch):
    """隔离连接服务加密密钥和环境 Provider，避免测试读取本机秘密。"""

    monkeypatch.setenv("APP_SECRET", "vertical-ability-connections-test-secret")
    monkeypatch.setenv("VERTICAL_ABILITY_JINA_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _engine():
    """创建包含连接表的进程内 SQLite 测试数据库。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _client_for_user(db: Session, user: User) -> TestClient:
    """创建将认证和数据库依赖固定到当前测试用户的客户端。"""

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user

    def session_override():
        """为请求返回同一个隔离数据库会话。"""

        yield db

    app.dependency_overrides[get_session] = session_override
    return TestClient(app)


def _user(user_id: str, *, tenant_id: str = "tenant_connections") -> User:
    """构造具备最小租户身份的测试用户。"""

    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role="member",
        password_hash="unused",
    )


def test_catalog_exposes_platform_specific_connection_methods_without_secrets(
    connection_test_settings,
) -> None:
    """连接目录应披露平台专属方式，但绝不返回凭据值或通用密码字段。"""

    engine = _engine()
    current_user = _user("connection-catalog-user")
    with Session(engine) as db:
        response = _client_for_user(db, current_user).get(
            "/api/chat/vertical-ability-connections"
        )

    assert response.status_code == 200
    body = response.json()
    platforms = {item["platform_id"]: item for item in body["platforms"]}
    assert set(platforms) == {
        "github",
        "twitter",
        "reddit",
        "bilibili",
        "facebook",
        "instagram",
        "xiaohongshu",
        "linkedin",
        "xueqiu",
    }
    assert platforms["github"]["auth_methods"] == ["api_token"]
    assert platforms["twitter"]["auth_methods"] == ["cookie", "browser_session"]
    assert all(
        field["key"] not in {"username", "password"}
        for platform in platforms.values()
        for fields in platform["fields_by_method"].values()
        for field in fields
    )
    assert "fixture-github-token" not in response.text
    engine.dispose()


def test_user_connection_is_encrypted_and_isolated_by_tenant_and_user(
    connection_test_settings,
) -> None:
    """保存 GitHub Token 后只能由当前用户解密读取，响应和数据库不出现明文。"""

    engine = _engine()
    first_user = _user("connection-owner")
    other_user = _user("connection-other")
    raw_token = "fixture-github-token-never-echoed"
    with Session(engine) as db:
        client = _client_for_user(db, first_user)
        saved = client.post(
            "/api/chat/vertical-ability-connections",
            json={
                "platform_id": "github",
                "account_label": "工作账号",
                "auth_method": "api_token",
                "credentials": {"access_token": raw_token},
                "expected_revision": 0,
            },
        )
        assert saved.status_code == 201
        assert raw_token not in saved.text
        row = db.exec(select(VerticalAbilityConnection)).one()
        assert raw_token not in row.encrypted_credentials_json
        assert decrypt_secret(row.encrypted_credentials_json) == '{"access_token":"fixture-github-token-never-echoed"}'

        other = _client_for_user(db, other_user).get(
            "/api/chat/vertical-ability-connections"
        )
        assert other.status_code == 200
        assert other.json()["connections"] == []

        assert decrypt_connection_credentials(row) == {"access_token": raw_token}
        assert resolve_user_connection_credentials(
            db,
            tenant_id=first_user.tenant_id,
            user_id=first_user.id,
        ) == {}
    engine.dispose()


def test_browser_session_reuses_only_explicit_user_device_pairing(
    connection_test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """浏览器在线不等于用户授权；已由当前用户设备完成的配对才可复用为连接。"""

    engine = _engine()
    current_user = _user("browser-owner")
    monkeypatch.setattr(
        "app.vertical_abilities.browser_runtime.require_opencli_browser_bridge",
        lambda _platform_id: BrowserBridgeStatus(
            daemon_running=True,
            extension_connected=True,
            version="fixture",
        ),
    )
    with Session(engine) as db:
        db.add(current_user)
        db.commit()
        pairing, token = create_relay_pairing(
            db, current_user=current_user, platform_id="reddit",
            account_label="浏览器账号", expected_revision=0,
        )
        authenticate_relay_token(db, token)
        session_id = pairing.id
        response = _client_for_user(db, current_user).post(
            "/api/chat/vertical-ability-connections/browser-session",
            json={
                "platform_id": "reddit",
                "account_label": "浏览器账号",
                "expected_revision": 1,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "connected"
        assert body["health_status"] == "healthy"
        assert body["transport"] == "browser_relay"
        assert "opencli:reddit" not in response.text

        row = db.exec(select(VerticalAbilityConnection)).one()
        assert decrypt_connection_credentials(row) == {"session_reference": f"relay:{session_id}"}
        assert resolve_user_connection_credentials(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
        ) == {"reddit": {"session_reference": f"relay:{session_id}"}}
    engine.dispose()


def test_online_server_browser_does_not_authorize_any_platform_user(
    connection_test_settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同租户与跨租户用户都不能仅凭服务端全局浏览器在线创建自己的账号连接。"""

    monkeypatch.setattr(
        "app.vertical_abilities.browser_runtime.require_opencli_browser_bridge",
        lambda _platform: BrowserBridgeStatus(True, True, "fixture"),
    )
    engine = _engine()
    with Session(engine) as db:
        for user in (
            _user("local-owner"), _user("local-other"),
            _user("other-tenant-user", tenant_id="another-tenant"),
        ):
            response = _client_for_user(db, user).post(
                "/api/chat/vertical-ability-connections/browser-session",
                json={"platform_id": "reddit", "account_label": "浏览器账号"},
            )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == (
                "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED"
            )
        assert db.exec(select(VerticalAbilityConnection)).all() == []
    engine.dispose()


def test_manual_reference_cannot_bypass_device_pairing(
    connection_test_settings,
) -> None:
    """客户端手写全局 OpenCLI 或 Relay 引用不能伪造已确认的设备授权。"""

    engine = _engine()
    with Session(engine) as db:
        client = _client_for_user(db, _user("injected-reference-user"))
        for reference in ("opencli:reddit", "relay:somebody-elses-pairing"):
            response = client.post(
                "/api/chat/vertical-ability-connections",
                json={
                    "platform_id": "reddit", "auth_method": "browser_session",
                    "credentials": {"session_reference": reference},
                },
            )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == (
                "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED"
            )
        assert db.exec(select(VerticalAbilityConnection)).all() == []
    engine.dispose()


def test_legacy_unowned_browser_connection_cannot_resolve_runtime_credentials(
    connection_test_settings,
) -> None:
    """历史标成健康的全局浏览器行没有设备授权，运行时不能继续借用且不影响其他凭据。"""

    engine = _engine()
    user = _user("legacy-browser-user")
    with Session(engine) as db:
        db.add(VerticalAbilityConnection(
            tenant_id=user.tenant_id, user_id=user.id, platform_id="reddit",
            account_label="旧浏览器", auth_method="browser_session", transport="local_loopback",
            encrypted_credentials_json=encrypt_secret('{"session_reference":"opencli:reddit"}'),
            status="connected", health_status="healthy",
        ))
        db.add(VerticalAbilityConnection(
            tenant_id=user.tenant_id, user_id=user.id, platform_id="github",
            account_label="独立Token", auth_method="api_token",
            encrypted_credentials_json=encrypt_secret('{"access_token":"fixture-token"}'),
            status="connected", health_status="healthy",
        ))
        db.commit()
        credentials = resolve_user_connection_credentials(db, tenant_id=user.tenant_id, user_id=user.id)
        assert credentials == {"github": {"access_token": "fixture-token"}}
        catalog = _client_for_user(db, user).get("/api/chat/vertical-ability-connections").json()
        reddit = next(item for item in catalog["platforms"] if item["platform_id"] == "reddit")
        legacy = next(item for item in catalog["connections"] if item["platform_id"] == "reddit")
        assert reddit["status"] == "needs_reauth"
        assert legacy["status"] == "needs_reauth"
        assert legacy["health_error_code"] == "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED"
        assert "配对" in legacy["message"]
        assert db.exec(select(VerticalAbilityConnection).where(
            VerticalAbilityConnection.platform_id == "reddit",
        )).one().status == "connected", "安全投影不能暗中改写历史连接行"
    engine.dispose()


def test_two_users_pair_independent_devices_and_revocation_is_not_global(
    connection_test_settings,
) -> None:
    """每个用户完成自己的设备配对后均可使用，撤销一个账号不能影响另一个账号。"""

    from app.vertical_abilities.connection_service import revoke_user_connection

    engine = _engine()
    users = (_user("paired-first"), _user("paired-second", tenant_id="second-tenant"))
    with Session(engine, expire_on_commit=False) as db:
        for user in users:
            db.add(user)
        db.commit()
        references = []
        connections = []
        for user in users:
            session, token = create_relay_pairing(
                db, current_user=user, platform_id="reddit",
                account_label="自己的浏览器", expected_revision=0,
            )
            authenticate_relay_token(db, token)
            response = _client_for_user(db, user).post(
                "/api/chat/vertical-ability-connections/browser-session",
                json={"platform_id": "reddit", "account_label": "自己的浏览器", "expected_revision": 1},
            )
            assert response.status_code == 200
            connections.append(response.json()["id"])
            references.append(f"relay:{session.id}")
            assert resolve_user_connection_credentials(
                db, tenant_id=user.tenant_id, user_id=user.id,
            ) == {"reddit": {"session_reference": references[-1]}}
        assert len(set(references)) == 2
        revoke_user_connection(db, current_user=users[0], connection_id=connections[0])
        assert resolve_user_connection_credentials(
            db, tenant_id=users[0].tenant_id, user_id=users[0].id,
        ) == {}
        assert resolve_user_connection_credentials(
            db, tenant_id=users[1].tenant_id, user_id=users[1].id,
        ) == {"reddit": {"session_reference": references[1]}}
    engine.dispose()


def test_connection_revision_conflict_and_revoke_clear_runtime_secret(
    connection_test_settings,
) -> None:
    """旧修订不能覆盖新连接，撤销后运行时解析结果必须为空。"""

    engine = _engine()
    current_user = _user("cas-owner")
    with Session(engine) as db:
        first = save_user_connection(
            db,
            current_user=current_user,
            platform_id="github",
            account_label="默认账号",
            auth_method="api_token",
            credentials={"access_token": "first-token"},
            expected_revision=0,
        )
        with pytest.raises(VerticalAbilityConnectionError) as conflict:
            save_user_connection(
                db,
                current_user=current_user,
                platform_id="github",
                account_label="默认账号",
                auth_method="api_token",
                credentials={"access_token": "stale-token"},
                expected_revision=0,
            )
        assert conflict.value.code == "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT"

        client = _client_for_user(db, current_user)
        revoked = client.delete(f"/api/chat/vertical-ability-connections/{first.id}")
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        assert resolve_user_connection_credentials(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
        ) == {}
        assert decrypt_connection_credentials(db.get(VerticalAbilityConnection, first.id)) == {}
    engine.dispose()


def test_connection_rejects_unknown_fields_and_incomplete_platform_cookie(
    connection_test_settings,
) -> None:
    """连接服务拒绝客户端注入未声明字段以及不完整的 Twitter Cookie。"""

    engine = _engine()
    current_user = _user("validation-owner")
    with Session(engine) as db:
        client = _client_for_user(db, current_user)
        unknown = client.post(
            "/api/chat/vertical-ability-connections",
            json={
                "platform_id": "github",
                "auth_method": "api_token",
                "credentials": {"access_token": "token", "command": "rm -rf"},
            },
        )
        assert unknown.status_code == 422
        assert unknown.json()["detail"]["code"] == "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_INVALID"

        incomplete = client.post(
            "/api/chat/vertical-ability-connections",
            json={
                "platform_id": "twitter",
                "auth_method": "cookie",
                "credentials": {"auth_token": "only-one-half"},
            },
        )
        assert incomplete.status_code == 422
        assert incomplete.json()["detail"]["code"] == "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_REQUIRED"
    engine.dispose()


def test_cookie_connection_verification_uses_fixed_endpoint_and_marks_health(
    connection_test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Twitter/Reddit Cookie 只随固定请求发送，并能把真实探测结果落为健康。"""

    class FakeTarget:
        """提供出网边界返回的最小目标对象。"""

        def __init__(self, request_url: str) -> None:
            """保存不含 Cookie 的固定请求地址。"""

            self.request_url = request_url
            self.headers: dict[str, str] = {}
            self.extensions: dict[str, object] = {}

    class FakeResponse:
        """模拟平台成功响应，分别覆盖 HTML 和 Reddit JSON 身份回执。"""

        def __init__(self, url: str) -> None:
            """根据固定目标地址准备最小响应体。"""

            self.status_code = 200
            self.content = b'{"name":"reddit-demo"}' if "reddit.com" in url else b"<html>ok</html>"

        def json(self) -> dict[str, str]:
            """返回 Reddit 身份字段。"""

            return {"name": "reddit-demo"}

    class FakeClient:
        """捕获当前请求头并阻断真实网络访问。"""

        calls: list[dict[str, object]] = []

        def __init__(self, **_kwargs: object) -> None:
            """初始化无状态假客户端。"""

            pass

        def __enter__(self) -> "FakeClient":
            """进入客户端上下文。"""

            return self

        def __exit__(self, *_args: object) -> None:
            """退出客户端上下文。"""

            return None

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            """记录固定 URL 和 Cookie 头并返回对应平台响应。"""

            self.calls.append({"url": url, "headers": kwargs["headers"]})
            return FakeResponse(url)

    monkeypatch.setattr(
        "app.vertical_abilities.connection_service.prepare_outbound_request",
        lambda url, **_kwargs: FakeTarget(url),
    )
    monkeypatch.setattr("app.vertical_abilities.connection_service.httpx.Client", FakeClient)

    engine = _engine()
    current_user = _user("cookie-owner")
    with Session(engine) as db:
        twitter = save_user_connection(
            db,
            current_user=current_user,
            platform_id="twitter",
            account_label="Twitter Cookie",
            auth_method="cookie",
            credentials={"cookie": "auth_token=fixture-auth; ct0=fixture-ct0"},
        )
        verified_twitter = verify_user_connection(
            db,
            current_user=current_user,
            connection_id=twitter.id,
            settings=get_settings(),
            resolver=object(),  # type: ignore[arg-type]
        )
        assert verified_twitter.status == "connected"
        assert verified_twitter.health_status == "healthy"

        reddit = save_user_connection(
            db,
            current_user=current_user,
            platform_id="reddit",
            account_label="Reddit Cookie",
            auth_method="cookie",
            credentials={"reddit_session": "fixture-reddit-session"},
        )
        verified_reddit = verify_user_connection(
            db,
            current_user=current_user,
            connection_id=reddit.id,
            settings=get_settings(),
            resolver=object(),  # type: ignore[arg-type]
        )
        assert verified_reddit.account_identity == "@reddit-demo"

    assert FakeClient.calls == [
        {
            "url": "https://x.com/home",
            "headers": {
                "Accept": "text/html, application/json",
                "Cookie": "auth_token=fixture-auth; ct0=fixture-ct0",
                "User-Agent": "gongge-xuban/vertical-ability",
            },
        },
        {
            "url": "https://www.reddit.com/api/me.json?raw_json=1",
            "headers": {
                "Accept": "text/html, application/json",
                "Cookie": "reddit_session=fixture-reddit-session",
                "User-Agent": "gongge-xuban/vertical-ability",
            },
        },
    ]
    engine.dispose()
