"""
@Time       : 2026/08/10 15:35
@Author     : zhanglp8181
@File       : test_wecom_callback_api.py
@CallChain  : pytest/TestClient → WeCom callback API → encrypted connector inbox
@Description: 回归公开回调握手、持久化后确认、重试幂等、租户身份和报文上限。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import hashlib
import os
import struct
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.connectors.service import ConnectionService
from app.connectors.wecom import WeComCallResult
from app.db import get_session
from app.db.models import ConnectorInboundEvent, Tenant
from app.main import app


TOKEN = "callback-token"
AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
CORP_ID = "ww-test-corp"
AGENT_ID = "1000002"


class TrustedIpBlockedWeCom:
    """模拟凭据有效但 agent/get 被企业可信 IP 门禁拒绝的初始化状态。"""

    def application_info(self, **_credentials: str) -> WeComCallResult:
        """稳定返回企业微信 60020，不访问外部网络。"""

        return WeComCallResult(False, {}, error_code="WECOM_60020")


@pytest.fixture
def callback_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Session, str]]:
    """创建单租户内存库和含加密回调配置的待可信 IP 档案。"""

    monkeypatch.setenv("APP_SECRET", "wecom-callback-test-master-key-32-bytes")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_a", name="Tenant A"))
    db.flush()
    profile = ConnectionService(db, wecom=TrustedIpBlockedWeCom()).create_wecom_profile(
        tenant_id="tenant_a",
        display_name="企业微信回调测试",
        corp_id=CORP_ID,
        agent_id=AGENT_ID,
        corp_secret="corp-secret",
        callback_token=TOKEN,
        callback_encoding_aes_key=AES_KEY,
        actor_user_id="admin",
    )
    db.commit()

    def override_session() -> Iterator[Session]:
        """让公开回调与断言共享同一测试事务数据库。"""

        yield db

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app), db, profile.id
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


def _encrypted(plaintext: bytes, *, receive_id: str = CORP_ID) -> str:
    """独立生成企业微信 AES-CBC 密文，供 API 黑盒测试。"""

    key = base64.b64decode(AES_KEY + "=")
    packet = os.urandom(16) + struct.pack(">I", len(plaintext)) + plaintext + receive_id.encode()
    pad_size = 32 - len(packet) % 32
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    ciphertext = encryptor.update(packet + bytes([pad_size]) * pad_size) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode()


def _params(encrypted: str) -> dict[str, str]:
    """为给定密文构造独立计算的企业微信签名查询参数。"""

    timestamp = "1720000000"
    nonce = "callback-nonce"
    signature = hashlib.sha1(
        "".join(sorted([TOKEN, timestamp, nonce, encrypted])).encode()
    ).hexdigest()
    return {"msg_signature": signature, "timestamp": timestamp, "nonce": nonce}


def _outer_xml(encrypted: str) -> bytes:
    """构造只含 Encrypt 节点的企业微信外层 XML。"""

    root = ElementTree.Element("xml")
    ElementTree.SubElement(root, "Encrypt").text = encrypted
    return ElementTree.tostring(root, encoding="utf-8")


def _text_message(*, message_id: str = "msg-100", agent_id: str = AGENT_ID) -> bytes:
    """构造包含企业、应用、发送者和稳定 MsgId 的文本事件。"""

    return (
        "<xml>"
        f"<ToUserName>{CORP_ID}</ToUserName>"
        "<FromUserName>employee-1</FromUserName>"
        "<CreateTime>1720000000</CreateTime>"
        "<MsgType>text</MsgType><Content>启动动态任务</Content>"
        f"<MsgId>{message_id}</MsgId><AgentID>{agent_id}</AgentID>"
        "</xml>"
    ).encode()


def test_get_verification_returns_plain_echo_and_rejects_bad_signature(callback_api) -> None:
    """URL 保存握手返回明文 echo，错误签名只返回统一拒绝且不泄漏配置。"""

    client, _db, profile_id = callback_api
    encrypted = _encrypted(b"verified-echo")
    url = f"/api/connectors/wecom/{profile_id}/callback"

    accepted = client.get(url, params={**_params(encrypted), "echostr": encrypted})
    rejected = client.get(
        url,
        params={**_params(encrypted), "msg_signature": "0" * 40, "echostr": encrypted},
    )

    assert accepted.status_code == 200
    assert accepted.text == "verified-echo"
    assert accepted.headers["content-type"].startswith("text/plain")
    assert rejected.status_code == 403
    assert TOKEN not in rejected.text
    assert AES_KEY not in rejected.text


def test_post_acknowledges_only_after_encrypted_inbox_write_and_deduplicates(callback_api) -> None:
    """同一 MsgId 的企业微信重试只形成一条密文事实，但两次均安全确认。"""

    client, db, profile_id = callback_api
    plaintext = _text_message()
    encrypted = _encrypted(plaintext)
    url = f"/api/connectors/wecom/{profile_id}/callback"

    first = client.post(url, params=_params(encrypted), content=_outer_xml(encrypted))
    second = client.post(url, params=_params(encrypted), content=_outer_xml(encrypted))

    assert first.status_code == second.status_code == 200
    assert first.text == second.text == "success"
    rows = db.exec(select(ConnectorInboundEvent)).all()
    assert len(rows) == 1
    assert rows[0].external_event_id == "msg-100"
    assert rows[0].event_type == "text"
    assert rows[0].status == "pending"
    assert "启动动态任务" not in rows[0].encrypted_payload
    assert "employee-1" not in rows[0].encrypted_payload


def test_post_rejects_wrong_agent_xxe_and_oversized_body_without_persisting(callback_api) -> None:
    """应用身份错配、实体声明和超限报文均不得形成 inbox 事实。"""

    client, db, profile_id = callback_api
    url = f"/api/connectors/wecom/{profile_id}/callback"
    wrong_plaintext = _text_message(agent_id="9999999")
    wrong_encrypted = _encrypted(wrong_plaintext)

    wrong_agent = client.post(
        url,
        params=_params(wrong_encrypted),
        content=_outer_xml(wrong_encrypted),
    )
    xxe = client.post(
        url,
        params={"msg_signature": "0" * 40, "timestamp": "1", "nonce": "n"},
        content=b'<!DOCTYPE xml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><xml>&xxe;</xml>',
    )
    oversized = client.post(
        url,
        params={"msg_signature": "0" * 40, "timestamp": "1", "nonce": "n"},
        content=b"x" * (256 * 1024 + 1),
    )

    assert wrong_agent.status_code == 403
    assert xxe.status_code == 403
    assert oversized.status_code == 413
    assert db.exec(select(ConnectorInboundEvent)).all() == []


def test_post_hashes_oversized_external_id_without_losing_idempotency(callback_api) -> None:
    """超长上游 ID 使用稳定摘要适配 MySQL 索引限制，原始值仍只在加密载荷中。"""

    client, db, profile_id = callback_api
    long_id = "message-" + "x" * 300
    plaintext = _text_message(message_id=long_id)
    encrypted = _encrypted(plaintext)

    response = client.post(
        f"/api/connectors/wecom/{profile_id}/callback",
        params=_params(encrypted),
        content=_outer_xml(encrypted),
    )

    assert response.status_code == 200
    row = db.exec(select(ConnectorInboundEvent)).one()
    assert row.external_event_id.startswith("sha256-id:")
    assert len(row.external_event_id) < 255
    assert long_id not in row.encrypted_payload
