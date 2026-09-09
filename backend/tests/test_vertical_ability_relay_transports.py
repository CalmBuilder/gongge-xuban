"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_relay_transports.py
@CallChain  : pytest → FastAPI/ProxyHeadersMiddleware → 配对 DTO → 用户设备队列授权
@Description: 验证本机与远端模式如实持久化、旧请求兼容和真实请求来源校验。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.browser_relay import relay_router, user_router
from app.api.vertical_ability_connections import router as connection_router
from app.config import get_settings
from app.db import get_session
from app.db.models import (
    User,
    VerticalAbilityBrowserRelayCommand,
    VerticalAbilityBrowserRelaySession,
    VerticalAbilityConnection,
    utc_now,
)
from app.security.auth import get_current_user
from app.vertical_abilities.connection_service import resolve_user_connection_credentials
from app.vertical_abilities.relay_service import BrowserRelayError, _require_connector_peer


PAIRING_PATH = "/api/chat/vertical-ability-connections/browser-relay/pairings"
LOCAL_PEER = "127.0.0.1"
REMOTE_PEER = "203.0.113.21"


@pytest.fixture
def relay_application(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """建立无真实账号与出网行为的数据库/API，连接器请求仍通过完整服务端授权链。"""

    monkeypatch.setenv("APP_SECRET", "relay-transport-fixture-secret")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    user = User(
        id="transport-owner", tenant_id="transport-tenant", username="transport-owner",
        password_hash="unused", role="member",
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add(user)
        db.commit()
    app = FastAPI()
    app.include_router(user_router)
    app.include_router(relay_router)
    app.include_router(connection_router)
    app.dependency_overrides[get_current_user] = lambda: user

    def session_override() -> Iterator[Session]:
        """每个 HTTP 请求使用独立会话，避免内存对象伪造认证和提交结果。"""

        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    app.state.test_engine = engine
    app.state.test_owner = user
    try:
        yield app
    finally:
        engine.dispose()
        get_settings.cache_clear()


def _create_pairing(client: TestClient, transport: str | None) -> dict[str, object]:
    """从用户接口创建配对，省略字段时验证兼容旧版前端请求。"""

    payload = {"platform_id": "reddit", "account_label": "自己的浏览器", "expected_revision": 0}
    if transport is not None:
        payload["transport"] = transport
    response = client.post(PAIRING_PATH, json=payload)
    assert response.status_code == 201
    return response.json()


def _auth(pairing: dict[str, object]) -> dict[str, str]:
    """把测试令牌只放入当前请求认证头，不写入数据库明文或日志。"""

    return {"Authorization": f"Bearer {pairing['pairing_token']}"}


@pytest.mark.parametrize("transport", [None, "browser_relay", "local_loopback"])
def test_pairing_response_and_connection_persist_the_actual_transport(
    relay_application: FastAPI, transport: str | None,
) -> None:
    """缺省保持远端兼容，本机与远端的响应、会话、连接及运行时解析必须一致。"""

    mode = transport or "browser_relay"
    user_client = TestClient(relay_application, client=(REMOTE_PEER, 40000))
    pairing = _create_pairing(user_client, transport)
    assert pairing["transport"] == mode
    with Session(relay_application.state.test_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pairing["session_id"])
        assert session.transport == mode
        assert session.status == "pending"
        assert session.connection_id is None
    peer = LOCAL_PEER if mode == "local_loopback" else REMOTE_PEER
    connector = TestClient(relay_application, client=(peer, 41000))
    assert connector.post("/api/browser-relay/heartbeat", headers=_auth(pairing)).status_code == 200
    with Session(relay_application.state.test_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pairing["session_id"])
        connection = db.get(VerticalAbilityConnection, session.connection_id)
        assert session.transport == connection.transport == mode
        owner = relay_application.state.test_owner
        assert resolve_user_connection_credentials(
            db, tenant_id=owner.tenant_id, user_id=owner.id,
        ) == {"reddit": {"session_reference": f"relay:{session.id}"}}
    status = user_client.get(f"{PAIRING_PATH}/{pairing['session_id']}")
    assert status.json()["transport"] == mode
    catalog = user_client.get("/api/chat/vertical-ability-connections").json()
    assert catalog["connections"][0]["transport"] == mode
    assert catalog["connections"][0]["status"] == "connected"
    connection_id = catalog["connections"][0]["id"]
    verified = user_client.post(f"/api/chat/vertical-ability-connections/{connection_id}/verify")
    assert verified.status_code == 200
    assert verified.json()["transport"] == mode
    assert verified.json()["verification_success"] is True
    reused = user_client.post(
        "/api/chat/vertical-ability-connections/browser-session",
        json={"platform_id": "reddit", "account_label": "自己的浏览器", "expected_revision": 1},
    )
    assert reused.status_code == 200
    assert reused.json()["id"] == connection_id and reused.json()["transport"] == mode


def test_local_pairing_rejects_remote_claim_result_and_heartbeat_before_mutation(
    relay_application: FastAPI,
) -> None:
    """远端即使拿到本用户本机令牌，也不能伪造转发头领取、回传或刷新本机设备。"""

    remote = TestClient(relay_application, client=(REMOTE_PEER, 40000))
    local = TestClient(relay_application, client=(LOCAL_PEER, 41000))
    pairing = _create_pairing(remote, "local_loopback")
    spoofed = {
        **_auth(pairing), "X-Forwarded-For": LOCAL_PEER, "X-Real-IP": LOCAL_PEER,
        "Forwarded": f"for={LOCAL_PEER}",
    }
    denied = remote.post("/api/browser-relay/heartbeat", headers=spoofed)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "VERTICAL_ABILITY_LOCAL_CONNECTOR_REQUIRED"
    with Session(relay_application.state.test_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pairing["session_id"])
        assert session.status == "pending" and session.connection_id is None
        assert session.last_seen_at is None
        assert db.exec(select(VerticalAbilityConnection)).all() == []
    assert local.post("/api/browser-relay/heartbeat", headers=_auth(pairing)).status_code == 200
    with Session(relay_application.state.test_engine, expire_on_commit=False) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pairing["session_id"])
        last_seen = session.last_seen_at
        command = VerticalAbilityBrowserRelayCommand(
            relay_session_id=session.id, tenant_id=session.tenant_id, user_id=session.user_id,
            platform_id="reddit", action="search", arguments_json='["fixture"]',
            deadline_at=utc_now() + timedelta(minutes=1),
        )
        db.add(command)
        db.commit()
        command_id = command.id
    assert remote.get(
        "/api/browser-relay/commands/next?wait_seconds=0", headers=spoofed,
    ).status_code == 403
    with Session(relay_application.state.test_engine) as db:
        assert db.get(VerticalAbilityBrowserRelayCommand, command_id).status == "queued"
        assert db.get(VerticalAbilityBrowserRelaySession, pairing["session_id"]).last_seen_at == last_seen
    leased = local.get(
        "/api/browser-relay/commands/next?wait_seconds=0", headers=_auth(pairing),
    )
    assert leased.status_code == 200
    payload = {"lease_token": leased.json()["lease_token"], "success": True, "output": "fixture"}
    assert remote.post(
        f"/api/browser-relay/commands/{command_id}/result", json=payload, headers=spoofed,
    ).status_code == 403
    with Session(relay_application.state.test_engine) as db:
        command = db.get(VerticalAbilityBrowserRelayCommand, command_id)
        assert command.status == "leased" and command.result_json is None
    completed = local.post(
        f"/api/browser-relay/commands/{command_id}/result", json=payload, headers=_auth(pairing),
    )
    assert completed.status_code == 200 and completed.json()["status"] == "succeeded"


@pytest.mark.parametrize("peer,headers,allowed", [
    (REMOTE_PEER, {"X-Forwarded-For": LOCAL_PEER}, False),
    (LOCAL_PEER, {"X-Forwarded-For": REMOTE_PEER}, False),
    (LOCAL_PEER, {"X-Forwarded-For": f"{LOCAL_PEER}, {REMOTE_PEER}"}, False),
    (LOCAL_PEER, {}, True),
])
def test_uvicorn_default_trust_cannot_promote_remote_requests_to_local(
    relay_application: FastAPI, peer: str, headers: dict[str, str], allowed: bool,
) -> None:
    """默认只信本地代理；直连远端伪造 XFF 无效，正确传递真实来源的本地代理仍被拒绝。"""

    user_client = TestClient(relay_application, client=(REMOTE_PEER, 40000))
    pairing = _create_pairing(user_client, "local_loopback")
    application = ProxyHeadersMiddleware(relay_application, trusted_hosts="127.0.0.1")
    connector = TestClient(application, client=(peer, 41000))
    response = connector.post("/api/browser-relay/heartbeat", headers={**_auth(pairing), **headers})
    assert response.status_code == (200 if allowed else 403)


def test_pairing_rejects_unknown_transport(relay_application: FastAPI) -> None:
    """未知模式在 DTO 层拒绝，不能降级成另一个模式或新增可用性开关。"""

    client = TestClient(relay_application)
    response = client.post(PAIRING_PATH, json={"platform_id": "reddit", "transport": "auto"})
    assert response.status_code == 422


@pytest.mark.parametrize("peer,allowed", [
    ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
    (None, False), ("localhost", False), ("127.0.0.1:5137", False),
    ("192.168.0.1", False), ("::ffff:192.168.0.1", False),
    ("127.0.0.1.example.invalid", False),
])
def test_local_peer_requires_a_literal_loopback_address(peer: str | None, allowed: bool) -> None:
    """来源必须是服务器解析的字面 loopback IP，不把缺失地址、域名或私网误作同机。"""

    session = VerticalAbilityBrowserRelaySession(
        tenant_id="fixture-tenant", user_id="fixture-user", platform_id="reddit",
        transport="local_loopback", token_hash="unused",
    )
    if allowed:
        _require_connector_peer(session, peer)
    else:
        with pytest.raises(BrowserRelayError) as rejected:
            _require_connector_peer(session, peer)
        assert rejected.value.code == "VERTICAL_ABILITY_LOCAL_CONNECTOR_REQUIRED"
