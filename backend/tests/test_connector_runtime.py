"""
@Time       : 2026/08/10 23:05
@Author     : zhanglp8181
@File       : test_connector_runtime.py
@CallChain  : pytest → ConnectorRuntimeService → inbox/principal/route/thread/outbox
@Description: 回归 Connector 未授权停放、显式路由、线程租约及幂等出站闭环。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.connectors.runtime import ConnectorRuntimeError, ConnectorRuntimeService
from app.connectors.wecom import WeComCallResult
from app.connectors.worker import connector_runtime_schema_ready
from app.db.models import (
    AgentConnectionBinding,
    AgentProfile,
    ConnectionProfile,
    ConnectionSecret,
    ConnectorInboundEvent,
    ConnectorOutboundDelivery,
    ConnectorThreadBinding,
    Message,
    Tenant,
    User,
)
from app.security.encryption import encrypt_secret


CORP_ID = "ww-test-corp"
AGENT_ID = "1000002"
SENDER = "external-user-a"


@pytest.fixture
def db() -> Session:
    """创建包含企业微信档案、Agent 绑定和两个活动用户的隔离数据库。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        secret = ConnectionSecret(
            id="secret_1",
            tenant_id="tenant_a",
            provider="wecom",
            reference_id="secret_ref_1",
            encrypted_payload=encrypt_secret(
                json.dumps(
                    {
                        "corp_id": CORP_ID,
                        "agent_id": AGENT_ID,
                        "corp_secret": "test-secret",
                        "callback_token": "callback-token",
                        "callback_encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
                    }
                )
            ),
        )
        profile = ConnectionProfile(
            id="profile_a",
            tenant_id="tenant_a",
            provider="wecom",
            account_id="account_a",
            display_name="企业微信测试",
            secret_ref_id="secret_ref_1",
            callback_configured=True,
            required_scopes_json=["application:read"],
            granted_scopes_json=["application:read"],
            tool_allowlist_json=["wecom.application_info"],
            status="active",
            health_status="healthy",
            created_by_user_id="admin_a",
            updated_by_user_id="admin_a",
        )
        session.add_all(
            [
                Tenant(id="tenant_a", name="Tenant A"),
                User(
                    id="admin_a",
                    tenant_id="tenant_a",
                    username="admin-a",
                    role="admin",
                    password_hash="unused",
                ),
                User(
                    id="user_a",
                    tenant_id="tenant_a",
                    username="user-a",
                    password_hash="unused",
                ),
                AgentProfile(
                    id="agent_a",
                    tenant_id="tenant_a",
                    name="Agent A",
                    owner_user_id="user_a",
                ),
                secret,
                profile,
                AgentConnectionBinding(
                    id="agentconn_a",
                    tenant_id="tenant_a",
                    agent_id="agent_a",
                    profile_id="profile_a",
                    allowed_scopes_json=["application:read"],
                    created_by_user_id="admin_a",
                    updated_by_user_id="admin_a",
                ),
            ]
        )
        session.commit()
        yield session


def _event(db: Session, *, event_id: str = "event_a", external_id: str = "msg-a") -> ConnectorInboundEvent:
    """创建已验签阶段等价的加密文本 inbox 事实。"""

    plaintext = (
        "<xml><ToUserName>ww-test-corp</ToUserName>"
        "<FromUserName>external-user-a</FromUserName>"
        "<MsgType>text</MsgType><Content>请查询应用信息</Content>"
        "<AgentID>1000002</AgentID><MsgId>msg-a</MsgId></xml>"
    )
    event = ConnectorInboundEvent(
        id=event_id,
        tenant_id="tenant_a",
        provider="wecom",
        profile_id="profile_a",
        external_event_id=external_id,
        payload_checksum=hashlib.sha256(plaintext.encode()).hexdigest(),
        encrypted_payload=encrypt_secret(plaintext),
        event_type="text",
        sender_ref_hash=hashlib.sha256(f"{CORP_ID}\0{SENDER}".encode()).hexdigest(),
    )
    db.add(event)
    db.commit()
    return event


def test_unresolved_principal_is_parked_without_creating_session(db: Session) -> None:
    """未知发送者不能进入 Agent Loop，事件保留为可重新授权投递的 failed。"""

    _event(db)
    service = ConnectorRuntimeService(db)
    event = service.claim_due_event(worker_id="worker-a")
    assert event is not None

    with pytest.raises(ConnectorRuntimeError, match="CONNECTOR_PRINCIPAL_UNRESOLVED"):
        service.prepare_dispatch(event, worker_id="worker-a")
    service.park_event(
        event,
        worker_id="worker-a",
        error_code="CONNECTOR_PRINCIPAL_UNRESOLVED",
    )

    assert event.status == "failed"
    assert event.last_error_code == "CONNECTOR_PRINCIPAL_UNRESOLVED"
    assert db.exec(select(ConnectorThreadBinding)).all() == []


def test_authorized_event_creates_one_thread_and_idempotent_outbox(db: Session) -> None:
    """绑定主体及路由后只创建一个会话，并以 assistant message 幂等登记回发。"""

    event = _event(db)
    service = ConnectorRuntimeService(db)
    service.bind_principal_from_event(
        tenant_id="tenant_a",
        event_id=event.id,
        user_id="user_a",
        actor_user_id="admin_a",
    )
    service.configure_route(
        tenant_id="tenant_a",
        profile_id="profile_a",
        agent_id="agent_a",
        actor_user_id="admin_a",
    )
    db.commit()

    claimed = service.claim_due_event(worker_id="worker-a")
    assert claimed is not None
    dispatch = service.prepare_dispatch(claimed, worker_id="worker-a")
    assert dispatch.user_id == "user_a"
    assert dispatch.agent_id == "agent_a"
    assert "external-user-a" not in repr(dispatch)
    db.add(
        Message(
            id="message_user",
            tenant_id="tenant_a",
            session_id=dispatch.session_id,
            role="user",
            content="请查询应用信息",
        )
    )
    db.add(
        Message(
            id="message_assistant",
            tenant_id="tenant_a",
            session_id=dispatch.session_id,
            role="assistant",
            content="应用处于启用状态。",
        )
    )
    db.commit()

    delivery = service.complete_dispatch(dispatch, worker_id="worker-a")
    replay = service._enqueue_assistant_delivery(
        event=claimed,
        thread_binding_id=dispatch.thread_binding_id,
        assistant=db.get(Message, "message_assistant"),
    )

    assert claimed.status == "processed"
    assert delivery.id == replay.id
    assert delivery.status == "pending"
    assert db.exec(select(ConnectorThreadBinding)).one().lease_owner is None
    assert len(db.exec(select(ConnectorOutboundDelivery)).all()) == 1


def test_principal_cannot_dispatch_to_private_agent_without_chat_access(db: Session) -> None:
    """主体绑定和连接档案绑定均不能绕过目标私有 Agent 的聊天使用权限。"""

    event = _event(db)
    agent = db.get(AgentProfile, "agent_a")
    assert agent is not None
    agent.owner_user_id = "admin_a"
    db.add(agent)
    db.commit()
    service = ConnectorRuntimeService(db)
    service.bind_principal_from_event(
        tenant_id="tenant_a",
        event_id=event.id,
        user_id="user_a",
        actor_user_id="admin_a",
    )
    service.configure_route(
        tenant_id="tenant_a",
        profile_id="profile_a",
        agent_id="agent_a",
        actor_user_id="admin_a",
    )
    db.commit()

    claimed = service.claim_due_event(worker_id="worker-a")
    assert claimed is not None
    with pytest.raises(ConnectorRuntimeError, match="CONNECTOR_AGENT_ACCESS_DENIED"):
        service.prepare_dispatch(claimed, worker_id="worker-a")

    assert db.exec(select(ConnectorThreadBinding)).all() == []


class _DeliveryService:
    """模拟企业微信回发结果并记录不输出的原始目标与正文。"""

    def __init__(self, result: WeComCallResult) -> None:
        """保存预设上游回执。"""

        self.result = result
        self.calls = 0

    def send_wecom_reply(self, **kwargs) -> WeComCallResult:
        """验证目标只在 provider 边界解密并返回预设结果。"""

        assert kwargs["recipient_ref"] == SENDER
        assert kwargs["content"] == "应用处于启用状态。"
        self.calls += 1
        return self.result


def test_outbound_timeout_becomes_unknown_and_is_not_claimed_again(db: Session) -> None:
    """发送后超时进入 unknown，后续扫描不得再次对企业微信产生写效果。"""

    event = _event(db)
    service = ConnectorRuntimeService(db)
    service.bind_principal_from_event(
        tenant_id="tenant_a",
        event_id=event.id,
        user_id="user_a",
        actor_user_id="admin_a",
    )
    service.configure_route(
        tenant_id="tenant_a",
        profile_id="profile_a",
        agent_id="agent_a",
        actor_user_id="admin_a",
    )
    db.commit()
    claimed_event = service.claim_due_event(worker_id="inbound-worker")
    dispatch = service.prepare_dispatch(claimed_event, worker_id="inbound-worker")
    db.add_all(
        [
            Message(
                id="message_user",
                tenant_id="tenant_a",
                session_id=dispatch.session_id,
                role="user",
                content="请查询应用信息",
            ),
            Message(
                id="message_assistant",
                tenant_id="tenant_a",
                session_id=dispatch.session_id,
                role="assistant",
                content="应用处于启用状态。",
            ),
        ]
    )
    db.commit()
    service.complete_dispatch(dispatch, worker_id="inbound-worker")

    delivery = service.claim_due_delivery(worker_id="outbound-worker")
    assert delivery is not None
    adapter = _DeliveryService(
        WeComCallResult(False, {}, error_code="WECOM_DELIVERY_UNKNOWN")
    )
    settled = service.deliver_claimed(
        delivery,
        worker_id="outbound-worker",
        connection_service=adapter,
    )

    assert settled.status == "unknown"
    assert adapter.calls == 1
    assert service.claim_due_delivery(worker_id="other-worker") is None
    assert adapter.calls == 1


def test_worker_starts_only_after_connector_runtime_schema_is_complete() -> None:
    """升级窗口内缺少 0044 表列时 worker 保持停用，迁移完成后才允许消费。"""

    empty_engine = create_engine("sqlite://")
    assert connector_runtime_schema_ready(empty_engine) is False
    SQLModel.metadata.create_all(empty_engine)
    assert connector_runtime_schema_ready(empty_engine) is True


def test_deleted_agent_cannot_settle_in_flight_delivery(db: Session) -> None:
    """删除 Agent 与回发终态并发时，迟到 settled 只能收敛为 unknown。"""

    db.add(
        ConnectorThreadBinding(
            id="thread_retire",
            tenant_id="tenant_a",
            provider="wecom",
            profile_id="profile_a",
            sender_ref_hash="sender-retire",
            encrypted_recipient_ref=encrypt_secret(SENDER),
            user_id="user_a",
            agent_id="agent_a",
            session_id="session_retire",
        )
    )
    db.add(
        ConnectorOutboundDelivery(
            id="delivery_retire",
            tenant_id="tenant_a",
            provider="wecom",
            profile_id="profile_a",
            thread_binding_id="thread_retire",
            source_type="assistant_message",
            source_ref="message_missing_after_purge",
            payload_checksum="a" * 64,
        )
    )
    db.commit()

    service = ConnectorRuntimeService(db)
    delivery = service.claim_due_delivery(worker_id="outbound-worker")
    assert delivery is not None
    agent = db.get(AgentProfile, "agent_a")
    assert agent is not None
    agent.status = "archived"
    agent.metadata_json = {"agent_deletion": {"state": "deleted"}}
    db.add(agent)
    db.commit()

    finished = service.finish_delivery(
        delivery,
        worker_id="outbound-worker",
        status="settled",
        receipt={"provider_message_id": "must-not-settle"},
    )

    assert finished.status == "unknown"
    assert finished.settled_at is None
    assert finished.error_json == {"code": "AGENT_NOT_AVAILABLE"}
