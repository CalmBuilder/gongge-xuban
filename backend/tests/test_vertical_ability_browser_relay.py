"""
@Time       : 2026/09/04 13:10
@Author     : zhanglp8181
@File       : test_vertical_ability_browser_relay.py
@CallChain  : pytest → browser relay API/service → SQLite 会话与命令队列 → 宿主机回执
@Description: 验证 browser_relay 的配对隔离、首次连接、命令租约、回执闭环和撤销语义。
"""

from __future__ import annotations

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.browser_relay import relay_router, user_router
from app.db import get_session
from app.db.models import User, VerticalAbilityBrowserRelaySession, VerticalAbilityConnection
from app.security.auth import get_current_user
from app.security.encryption import decrypt_secret
from app.vertical_abilities.connection_service import decrypt_connection_credentials
from app.vertical_abilities.relay_service import enqueue_and_wait_for_relay_command
from app.vertical_abilities import browser_relay_connector


def _user(user_id: str, *, tenant_id: str = "tenant_relay") -> User:
    """构造用于 Relay 归属校验的最小租户用户。"""

    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role="member",
        password_hash="unused",
    )


def _app(engine, current_user: User) -> FastAPI:
    """创建同时暴露用户配对 API 和宿主机 Relay API 的测试应用。"""

    app = FastAPI()
    app.include_router(user_router)
    app.include_router(relay_router)
    app.dependency_overrides[get_current_user] = lambda: current_user

    def session_override():
        """为每个请求创建独立会话，模拟 API 与执行器并发访问数据库。"""

        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    return app


def test_relay_pairing_command_and_result_are_scoped_and_reusable(monkeypatch) -> None:
    """验证宿主机首次轮询完成配对，固定命令可租约并将文本回执送回执行器。"""

    monkeypatch.setenv("APP_SECRET", "browser-relay-test-secret")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    owner = _user("relay-owner")
    other = _user("relay-other")
    with Session(engine, expire_on_commit=False) as db:
        db.add(owner)
        db.add(other)
        db.commit()

    client = TestClient(_app(engine, owner))
    created = client.post(
        "/api/chat/vertical-ability-connections/browser-relay/pairings",
        json={
            "platform_id": "xiaohongshu",
            "account_label": "宿主机小红书",
            "expected_revision": 0,
        },
    )
    assert created.status_code == 201
    pairing = created.json()
    token = pairing["pairing_token"]
    assert token
    assert "token_hash" not in created.text

    wrong_client = TestClient(_app(engine, other))
    assert wrong_client.get(
        f"/api/chat/vertical-ability-connections/browser-relay/pairings/{pairing['session_id']}"
    ).status_code == 404

    empty = client.get(
        "/api/browser-relay/commands/next?wait_seconds=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert empty.status_code == 204
    with Session(engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pairing["session_id"])
        assert session is not None
        assert session.status == "connected"
        connection = db.exec(select(VerticalAbilityConnection)).one()
        assert connection.transport == "browser_relay"
        assert decrypt_connection_credentials(connection) == {
            "session_reference": f"relay:{pairing['session_id']}"
        }
        assert token not in decrypt_secret(connection.encrypted_credentials_json)

    result_holder: dict[str, bytes] = {}

    def wait_for_command() -> None:
        """在独立数据库会话中模拟服务端执行器等待宿主机回执。"""

        with Session(engine) as db:
            result_holder["output"] = enqueue_and_wait_for_relay_command(
                db,
                tenant_id=owner.tenant_id,
                user_id=owner.id,
                session_id=pairing["session_id"],
                platform_id="xiaohongshu",
                action="search",
                positional_args=["relay test"],
                limit=5,
                timeout_seconds=3,
            )

    worker = threading.Thread(target=wait_for_command)
    worker.start()
    command = None
    for _ in range(20):
        response = client.get(
            "/api/browser-relay/commands/next?wait_seconds=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 200:
            command = response.json()
            break
        time.sleep(0.05)
    assert command is not None
    assert command["platform_id"] == "xiaohongshu"
    assert command["action"] == "search"
    assert command["positional_args"] == ["relay test"]
    assert "session_reference" not in response.text

    completed = client.post(
        f"/api/browser-relay/commands/{command['command_id']}/result",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "lease_token": command["lease_token"],
            "success": True,
            "output": "items:\n  - title: relay result\n",
        },
    )
    assert completed.status_code == 200
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert result_holder["output"] == b"items:\n  - title: relay result\n"

    revoked = client.delete(
        f"/api/chat/vertical-ability-connections/browser-relay/pairings/{pairing['session_id']}"
    )
    assert revoked.status_code == 200
    denied = client.get(
        "/api/browser-relay/commands/next?wait_seconds=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 401
    engine.dispose()


def test_host_connector_executes_only_the_server_fixed_opencli_action(monkeypatch) -> None:
    """宿主机连接器只透传固定平台动作，不把服务端回执变成任意命令或 Cookie 通道。"""

    captured: dict[str, object] = {}

    def fake_run_opencli_raw(*args, **kwargs):
        """记录连接器交给本机 OpenCLI 的稳定参数并返回测试 YAML。"""

        captured["args"] = args
        captured["kwargs"] = kwargs
        return b"items:\n  - title: connector result\n"

    monkeypatch.setattr(browser_relay_connector, "run_opencli_raw", fake_run_opencli_raw)
    connector = browser_relay_connector.BrowserRelayConnector(
        server_url="https://relay.example.test",
        pairing_token="one-time-token",
        opencli_path="C:/Program Files/Gongge-Xuban/runtime/opencli/opencli.exe",
        opencli_profile="9etrfpdg",
        once=True,
    )
    result = connector._execute_command(
        {
            "command_id": "varelaycmd-test",
            "lease_token": "lease-token",
            "platform_id": "xiaohongshu",
            "action": "search",
            "positional_args": ["python"],
            "limit": 5,
            "timeout_seconds": 30,
        }
    )

    assert result["success"] is True
    assert result["output"] == "items:\n  - title: connector result\n"
    assert captured["args"] == ("xiaohongshu", "search", ["python"])
    assert captured["kwargs"]["credentials"] == {"session_reference": "opencli:xiaohongshu"}
    assert captured["kwargs"]["executable_path"].endswith("opencli.exe")
    assert captured["kwargs"]["opencli_profile"] == "9etrfpdg"
