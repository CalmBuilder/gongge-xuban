"""
@Time       : 2026/08/10 22:45
@Author     : zhanglp8181
@File       : runtime.py
@CallChain  : Connector worker/admin API → persistent inbox → ChatSession/Agent Loop → outbox
@Description: 管理 Connector 入站授权、明确路由、线程串行租约及可恢复投递状态。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.connectors.wecom_inbound import WeComInboundError, WeComInboundService
from app.connectors.service import ConnectionService
from app.db.models import (
    AgentConnectionBinding,
    AgentProfile,
    ChatSession,
    ConnectionProfile,
    ConnectorInboundEvent,
    ConnectorInboundRoute,
    ConnectorOutboundDelivery,
    ConnectorPrincipalBinding,
    ConnectorThreadBinding,
    ExecutionPublication,
    ExecutionResult,
    Message,
    SopInstance,
    User,
    new_id,
    utc_now,
)
from app.security.encryption import decrypt_secret, encrypt_secret
from app.security.permissions import can_use_agent_in_chat


class ConnectorRuntimeError(ValueError):
    """以稳定代码表示可停放、重试或拒绝的 Connector 运行时状态。"""

    def __init__(self, code: str) -> None:
        """保存不会携带外部正文、用户标识或凭据的错误代码。"""

        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ConnectorInboundDispatch:
    """提供给 Agent Loop 的最小可信上下文，正文不参与 repr。"""

    event_id: str
    tenant_id: str
    profile_id: str
    thread_binding_id: str
    session_id: str
    user_id: str
    agent_id: str
    content: str = field(repr=False)


class ConnectorRuntimeService:
    """在数据库事务内维护入站消息从授权到外部回复的权威状态。"""

    def __init__(self, db: Session) -> None:
        """绑定当前事务，所有 claim 和终态迁移均由调用方显式提交。"""

        self.db = db

    def bind_principal_from_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        user_id: str,
        actor_user_id: str,
    ) -> ConnectorPrincipalBinding:
        """从已验签事件提取发送者摘要并绑定活动平台用户，不接收原始外部 ID。"""

        event = self.db.get(ConnectorInboundEvent, event_id)
        user = self.db.get(User, user_id)
        if (
            event is None
            or event.tenant_id != tenant_id
            or not event.sender_ref_hash
            or user is None
            or user.tenant_id != tenant_id
            or user.membership_status != "active"
        ):
            raise ConnectorRuntimeError("CONNECTOR_PRINCIPAL_BINDING_INVALID")
        existing = self.db.exec(
            select(ConnectorPrincipalBinding).where(
                ConnectorPrincipalBinding.tenant_id == tenant_id,
                ConnectorPrincipalBinding.provider == event.provider,
                ConnectorPrincipalBinding.profile_id == event.profile_id,
                ConnectorPrincipalBinding.sender_ref_hash == event.sender_ref_hash,
            )
        ).first()
        now = utc_now()
        if existing is not None:
            if existing.user_id != user_id:
                raise ConnectorRuntimeError("CONNECTOR_PRINCIPAL_ALREADY_BOUND")
            existing.enabled = True
            existing.revision += 1
            existing.updated_by_user_id = actor_user_id
            existing.updated_at = now
            row = existing
        else:
            row = ConnectorPrincipalBinding(
                tenant_id=tenant_id,
                provider=event.provider,
                profile_id=event.profile_id,
                sender_ref_hash=event.sender_ref_hash,
                user_id=user_id,
                created_by_user_id=actor_user_id,
                updated_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
        self.db.add(row)
        event.status = "pending"
        event.available_at = now
        event.last_error_code = None
        event.updated_at = now
        self.db.add(event)
        self.db.flush()
        return row

    def configure_route(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> ConnectorInboundRoute:
        """验证同一档案的有效 Agent 连接绑定后设置唯一入站路由。"""

        profile = self.db.get(ConnectionProfile, profile_id)
        agent = self.db.get(AgentProfile, agent_id)
        connection_binding = self.db.exec(
            select(AgentConnectionBinding).where(
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.profile_id == profile_id,
                AgentConnectionBinding.agent_id == agent_id,
                AgentConnectionBinding.enabled.is_(True),
            )
        ).first()
        if (
            profile is None
            or profile.tenant_id != tenant_id
            or profile.status != "active"
            or not profile.callback_configured
            or agent is None
            or agent.tenant_id != tenant_id
            or agent.status != "active"
            or connection_binding is None
        ):
            raise ConnectorRuntimeError("CONNECTOR_INBOUND_ROUTE_INVALID")
        existing = self.db.exec(
            select(ConnectorInboundRoute).where(
                ConnectorInboundRoute.tenant_id == tenant_id,
                ConnectorInboundRoute.provider == profile.provider,
                ConnectorInboundRoute.profile_id == profile_id,
            )
        ).first()
        now = utc_now()
        if existing is None:
            row = ConnectorInboundRoute(
                tenant_id=tenant_id,
                provider=profile.provider,
                profile_id=profile_id,
                agent_id=agent_id,
                created_by_user_id=actor_user_id,
                updated_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
        else:
            existing.agent_id = agent_id
            existing.enabled = True
            existing.revision += 1
            existing.updated_by_user_id = actor_user_id
            existing.updated_at = now
            row = existing
        self.db.add(row)
        self.db.flush()
        return row

    def claim_due_event(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 180,
    ) -> ConnectorInboundEvent | None:
        """以条件更新抢占最早到期事件，并允许回收过期 processing lease。"""

        now = utc_now()
        candidate = self.db.exec(
            select(ConnectorInboundEvent)
            .where(
                ConnectorInboundEvent.available_at <= now,
                or_(
                    ConnectorInboundEvent.status.in_(("pending", "failed")),
                    (
                        (ConnectorInboundEvent.status == "processing")
                        & (ConnectorInboundEvent.lease_until < now)
                    ),
                ),
            )
            .order_by(ConnectorInboundEvent.created_at, ConnectorInboundEvent.id)
        ).first()
        if candidate is None:
            return None
        previous_status = candidate.status
        previous_lease = candidate.lease_until
        if not self._lock_inbound_event_agent(candidate):
            conditions = [
                ConnectorInboundEvent.id == candidate.id,
                ConnectorInboundEvent.status == previous_status,
            ]
            if previous_status == "processing":
                conditions.append(ConnectorInboundEvent.lease_until == previous_lease)
            self.db.exec(
                update(ConnectorInboundEvent)
                .where(*conditions)
                .values(
                    status="dead_letter",
                    last_error_code="AGENT_NOT_AVAILABLE",
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            self.db.commit()
            return None
        conditions = [ConnectorInboundEvent.id == candidate.id]
        if previous_status == "processing":
            conditions.extend(
                (
                    ConnectorInboundEvent.status == "processing",
                    ConnectorInboundEvent.lease_until == previous_lease,
                )
            )
        else:
            conditions.append(ConnectorInboundEvent.status == previous_status)
        result = self.db.exec(
            update(ConnectorInboundEvent)
            .where(*conditions)
            .values(
                status="processing",
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=max(30, lease_seconds)),
                attempt_count=ConnectorInboundEvent.attempt_count + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        self.db.commit()
        claimed = self.db.get(ConnectorInboundEvent, candidate.id)
        if claimed is None or claimed.lease_owner != worker_id:
            return None
        return claimed

    def prepare_dispatch(
        self,
        event: ConnectorInboundEvent,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> ConnectorInboundDispatch:
        """在事件租约内解析正文、校验授权与路由，并串行取得外部线程租约。"""

        self._assert_event_owner(event, worker_id)
        if not event.sender_ref_hash:
            raise ConnectorRuntimeError("CONNECTOR_PRINCIPAL_UNRESOLVED")
        principal = self.db.exec(
            select(ConnectorPrincipalBinding).where(
                ConnectorPrincipalBinding.tenant_id == event.tenant_id,
                ConnectorPrincipalBinding.provider == event.provider,
                ConnectorPrincipalBinding.profile_id == event.profile_id,
                ConnectorPrincipalBinding.sender_ref_hash == event.sender_ref_hash,
                ConnectorPrincipalBinding.enabled.is_(True),
            )
        ).first()
        route = self.db.exec(
            select(ConnectorInboundRoute).where(
                ConnectorInboundRoute.tenant_id == event.tenant_id,
                ConnectorInboundRoute.provider == event.provider,
                ConnectorInboundRoute.profile_id == event.profile_id,
                ConnectorInboundRoute.enabled.is_(True),
            )
        ).first()
        if principal is None:
            raise ConnectorRuntimeError("CONNECTOR_PRINCIPAL_UNRESOLVED")
        user = self.db.get(User, principal.user_id)
        if user is None or user.tenant_id != event.tenant_id or user.membership_status != "active":
            raise ConnectorRuntimeError("CONNECTOR_PRINCIPAL_INACTIVE")
        if route is None or not self._route_binding_active(event, route):
            raise ConnectorRuntimeError("CONNECTOR_INBOUND_ROUTE_UNRESOLVED")
        if not self._lock_agent(event.tenant_id, route.agent_id):
            raise ConnectorRuntimeError("CONNECTOR_AGENT_ACCESS_DENIED")
        agent = self.db.get(AgentProfile, route.agent_id)
        if agent is None or not can_use_agent_in_chat(self.db, agent, user):
            raise ConnectorRuntimeError("CONNECTOR_AGENT_ACCESS_DENIED")
        try:
            envelope = WeComInboundService(self.db).text_envelope(event)
        except WeComInboundError as exc:
            raise ConnectorRuntimeError(exc.code) from exc
        thread = self._ensure_thread(
            event=event,
            principal=principal,
            route=route,
            recipient_ref=envelope.sender_ref,
        )
        self._claim_thread(thread, worker_id=worker_id, lease_seconds=lease_seconds)
        event.thread_binding_id = thread.id
        event.session_id = thread.session_id
        event.updated_at = utc_now()
        self.db.add(event)
        self.db.commit()
        return ConnectorInboundDispatch(
            event_id=event.id,
            tenant_id=event.tenant_id,
            profile_id=event.profile_id,
            thread_binding_id=thread.id,
            session_id=thread.session_id,
            user_id=principal.user_id,
            agent_id=route.agent_id,
            content=envelope.content,
        )

    def park_event(
        self,
        event: ConnectorInboundEvent,
        *,
        worker_id: str,
        error_code: str,
        retry_seconds: int = 300,
        terminal: bool = False,
    ) -> None:
        """把未授权/缺配置事件可恢复停放，或把确定坏报文送入 dead letter。"""

        self._assert_event_owner(event, worker_id)
        now = utc_now()
        event.status = "dead_letter" if terminal else "failed"
        event.last_error_code = error_code[:128]
        event.available_at = now + timedelta(seconds=max(1, retry_seconds))
        event.lease_owner = None
        event.lease_until = None
        event.updated_at = now
        self.db.add(event)
        self._release_thread(event.thread_binding_id, worker_id)
        self.db.commit()

    def complete_dispatch(
        self,
        dispatch: ConnectorInboundDispatch,
        *,
        worker_id: str,
    ) -> ConnectorOutboundDelivery:
        """关联本轮持久消息并幂等登记普通回答回发，然后完成 inbox 事件。"""

        event = self.db.get(ConnectorInboundEvent, dispatch.event_id)
        if event is None:
            raise ConnectorRuntimeError("CONNECTOR_INBOUND_EVENT_NOT_FOUND")
        self._assert_event_owner(event, worker_id)
        if not self._lock_agent(dispatch.tenant_id, dispatch.agent_id):
            raise ConnectorRuntimeError("CONNECTOR_AGENT_ACCESS_DENIED")
        messages = self.db.exec(
            select(Message)
            .where(
                Message.tenant_id == dispatch.tenant_id,
                Message.session_id == dispatch.session_id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
        ).all()
        assistant = next((item for item in messages if item.role == "assistant"), None)
        user_message = next((item for item in messages if item.role == "user"), None)
        if assistant is None or user_message is None:
            raise ConnectorRuntimeError("CONNECTOR_TURN_MESSAGES_MISSING")
        event.message_id = user_message.id
        execution_id = str((assistant.metadata_json or {}).get("execution_id") or "")
        event.execution_id = execution_id or None
        if execution_id:
            publication = self.db.exec(
                select(ExecutionPublication).where(
                    ExecutionPublication.tenant_id == event.tenant_id,
                    ExecutionPublication.execution_id == execution_id,
                    ExecutionPublication.target_type == "external_thread",
                    ExecutionPublication.target_ref == dispatch.thread_binding_id,
                )
            ).first()
            delivery = (
                self.db.get(ConnectorOutboundDelivery, publication.outbox_id)
                if publication is not None and publication.outbox_id
                else None
            )
            if delivery is None:
                raise ConnectorRuntimeError("CONNECTOR_EXECUTION_PUBLICATION_MISSING")
        else:
            delivery = self._enqueue_assistant_delivery(
                event=event,
                thread_binding_id=dispatch.thread_binding_id,
                assistant=assistant,
            )
        now = utc_now()
        event.status = "processed"
        event.processed_at = now
        event.last_error_code = None
        event.lease_owner = None
        event.lease_until = None
        event.updated_at = now
        self.db.add(event)
        self._release_thread(dispatch.thread_binding_id, worker_id)
        self.db.commit()
        return delivery

    def claim_due_delivery(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> ConnectorOutboundDelivery | None:
        """抢占一条 pending 回发；过期 delivering 只转 unknown，绝不自动重发。"""

        now = utc_now()
        self.db.exec(
            update(ConnectorOutboundDelivery)
            .where(
                ConnectorOutboundDelivery.status == "delivering",
                ConnectorOutboundDelivery.lease_until < now,
            )
            .values(
                status="unknown",
                lease_owner=None,
                lease_until=None,
                error_json={"code": "CONNECTOR_DELIVERY_LEASE_EXPIRED"},
                updated_at=now,
            )
        )
        candidate = self.db.exec(
            select(ConnectorOutboundDelivery)
            .where(
                ConnectorOutboundDelivery.status == "pending",
                ConnectorOutboundDelivery.available_at <= now,
            )
            .order_by(ConnectorOutboundDelivery.created_at, ConnectorOutboundDelivery.id)
        ).first()
        if candidate is None:
            self.db.commit()
            return None
        if not self._lock_delivery_agent(candidate):
            self.db.commit()
            return None
        result = self.db.exec(
            update(ConnectorOutboundDelivery)
            .where(
                ConnectorOutboundDelivery.id == candidate.id,
                ConnectorOutboundDelivery.status == "pending",
            )
            .values(
                status="delivering",
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=max(30, lease_seconds)),
                attempt_count=ConnectorOutboundDelivery.attempt_count + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        self.db.commit()
        return self.db.get(ConnectorOutboundDelivery, candidate.id)

    def enqueue_execution_publication(
        self,
        publication: ExecutionPublication,
        *,
        thread_binding_id: str,
        content: str,
    ) -> ConnectorOutboundDelivery:
        """在 Execution 写事务中把 required 外部 publication 绑定到唯一 Connector outbox。"""

        thread = self.db.get(ConnectorThreadBinding, thread_binding_id)
        if (
            thread is None
            or thread.tenant_id != publication.tenant_id
            or publication.target_ref != thread.id
            or publication.target_type != "external_thread"
        ):
            raise ConnectorRuntimeError("CONNECTOR_PUBLICATION_TARGET_INVALID")
        existing = self.db.exec(
            select(ConnectorOutboundDelivery).where(
                ConnectorOutboundDelivery.tenant_id == publication.tenant_id,
                ConnectorOutboundDelivery.source_type == "execution_publication",
                ConnectorOutboundDelivery.source_ref == publication.id,
            )
        ).first()
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if existing is not None:
            if existing.payload_checksum != checksum:
                raise ConnectorRuntimeError("CONNECTOR_OUTBOUND_SOURCE_CONFLICT")
            return existing
        delivery = ConnectorOutboundDelivery(
            tenant_id=publication.tenant_id,
            provider=thread.provider,
            profile_id=thread.profile_id,
            thread_binding_id=thread.id,
            source_type="execution_publication",
            source_ref=publication.id,
            payload_checksum=checksum,
        )
        self.db.add(delivery)
        self.db.flush()
        publication.outbox_id = delivery.id
        publication.updated_at = utc_now()
        self.db.add(publication)
        self.db.flush()
        return delivery

    def deliver_claimed(
        self,
        delivery: ConnectorOutboundDelivery,
        *,
        worker_id: str,
        connection_service: ConnectionService | None = None,
    ) -> ConnectorOutboundDelivery:
        """解析持久来源和加密目标后回发，并按明确成功、可重试失败或未知效果收敛。"""

        self._assert_delivery_owner(delivery, worker_id)
        thread = self.db.get(ConnectorThreadBinding, delivery.thread_binding_id)
        if (
            thread is None
            or thread.tenant_id != delivery.tenant_id
            or thread.profile_id != delivery.profile_id
            or thread.status != "active"
        ):
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="dead_letter",
                error_code="CONNECTOR_THREAD_UNAVAILABLE",
            )
        content = self._delivery_content(delivery)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != delivery.payload_checksum:
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="dead_letter",
                error_code="CONNECTOR_DELIVERY_PAYLOAD_CHANGED",
            )
        if len(content) > 4000:
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="dead_letter",
                error_code="CONNECTOR_DELIVERY_CONTENT_TOO_LARGE",
            )
        if not self._lock_delivery_agent(delivery):
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="unknown",
                error_code="AGENT_NOT_AVAILABLE",
            )
        try:
            recipient_ref = decrypt_secret(thread.encrypted_recipient_ref)
        except (TypeError, ValueError):
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="dead_letter",
                error_code="CONNECTOR_RECIPIENT_UNAVAILABLE",
            )
        service = connection_service or ConnectionService(self.db)
        result = service.send_wecom_reply(
            tenant_id=delivery.tenant_id,
            profile_id=delivery.profile_id,
            recipient_ref=recipient_ref,
            content=content,
        )
        if result.success:
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="settled",
                receipt={
                    "provider_message_id": str(result.data.get("message_id") or ""),
                    "delivered_content_checksum": hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    "content_truncated": False,
                },
            )
        if result.error_code == "WECOM_RATE_LIMITED":
            return self.retry_delivery(
                delivery,
                worker_id=worker_id,
                available_at=result.rate_limited_until or (utc_now() + timedelta(seconds=60)),
                error_code="WECOM_RATE_LIMITED",
            )
        if result.error_code == "WECOM_DELIVERY_UNKNOWN":
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="unknown",
                error_code="WECOM_DELIVERY_UNKNOWN",
            )
        return self.finish_delivery(
            delivery,
            worker_id=worker_id,
            status="dead_letter",
            error_code=str(result.error_code or "WECOM_DELIVERY_FAILED"),
        )

    def _ensure_thread(
        self,
        *,
        event: ConnectorInboundEvent,
        principal: ConnectorPrincipalBinding,
        route: ConnectorInboundRoute,
        recipient_ref: str,
    ) -> ConnectorThreadBinding:
        """复用同一发送者/Agent 会话；首次消息原子创建会话和加密回复目标。"""

        existing = self.db.exec(
            select(ConnectorThreadBinding).where(
                ConnectorThreadBinding.tenant_id == event.tenant_id,
                ConnectorThreadBinding.provider == event.provider,
                ConnectorThreadBinding.profile_id == event.profile_id,
                ConnectorThreadBinding.sender_ref_hash == event.sender_ref_hash,
                ConnectorThreadBinding.agent_id == route.agent_id,
            )
        ).first()
        if existing is not None:
            if existing.user_id != principal.user_id or existing.status != "active":
                raise ConnectorRuntimeError("CONNECTOR_THREAD_BINDING_CONFLICT")
            return existing
        now = utc_now()
        session_id = new_id("session")
        session = ChatSession(
            id=session_id,
            tenant_id=event.tenant_id,
            user_id=principal.user_id,
            agent_id=route.agent_id,
            origin="connector",
            title="企业微信会话",
            context_state_json={"connector_profile_id": event.profile_id},
            created_at=now,
            updated_at=now,
        )
        thread = ConnectorThreadBinding(
            tenant_id=event.tenant_id,
            provider=event.provider,
            profile_id=event.profile_id,
            sender_ref_hash=str(event.sender_ref_hash),
            encrypted_recipient_ref=encrypt_secret(recipient_ref),
            user_id=principal.user_id,
            agent_id=route.agent_id,
            session_id=session_id,
            created_at=now,
            updated_at=now,
        )
        self.db.add(session)
        self.db.add(thread)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectorRuntimeError("CONNECTOR_THREAD_CREATE_CONFLICT") from exc
        return thread

    def _claim_thread(
        self,
        thread: ConnectorThreadBinding,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        """以 CAS 串行化同一外部线程，防止多进程乱序写入 ChatSession。"""

        now = utc_now()
        result = self.db.exec(
            update(ConnectorThreadBinding)
            .where(
                ConnectorThreadBinding.id == thread.id,
                ConnectorThreadBinding.status == "active",
                or_(
                    ConnectorThreadBinding.lease_owner.is_(None),
                    ConnectorThreadBinding.lease_until < now,
                    ConnectorThreadBinding.lease_owner == worker_id,
                ),
            )
            .values(
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=max(30, lease_seconds)),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise ConnectorRuntimeError("CONNECTOR_THREAD_BUSY")

    def _release_thread(self, thread_id: str | None, worker_id: str) -> None:
        """仅由当前 owner 释放线程租约，过期 worker 不得覆盖新 owner。"""

        if not thread_id:
            return
        self.db.exec(
            update(ConnectorThreadBinding)
            .where(
                ConnectorThreadBinding.id == thread_id,
                ConnectorThreadBinding.lease_owner == worker_id,
            )
            .values(lease_owner=None, lease_until=None, updated_at=utc_now())
        )

    def _enqueue_assistant_delivery(
        self,
        *,
        event: ConnectorInboundEvent,
        thread_binding_id: str,
        assistant: Message,
    ) -> ConnectorOutboundDelivery:
        """按 assistant message 身份创建唯一 outbox，不复制正文到 outbox。"""

        existing = self.db.exec(
            select(ConnectorOutboundDelivery).where(
                ConnectorOutboundDelivery.tenant_id == event.tenant_id,
                ConnectorOutboundDelivery.source_type == "assistant_message",
                ConnectorOutboundDelivery.source_ref == assistant.id,
            )
        ).first()
        checksum = hashlib.sha256(assistant.content.encode("utf-8")).hexdigest()
        if existing is not None:
            if existing.payload_checksum != checksum:
                raise ConnectorRuntimeError("CONNECTOR_OUTBOUND_SOURCE_CONFLICT")
            return existing
        delivery = ConnectorOutboundDelivery(
            tenant_id=event.tenant_id,
            provider=event.provider,
            profile_id=event.profile_id,
            thread_binding_id=thread_binding_id,
            source_type="assistant_message",
            source_ref=assistant.id,
            payload_checksum=checksum,
        )
        self.db.add(delivery)
        self.db.flush()
        return delivery

    def _delivery_content(self, delivery: ConnectorOutboundDelivery) -> str:
        """从不可变权威来源加载正文，outbox 本身不复制业务内容。"""

        if delivery.source_type == "assistant_message":
            message = self.db.get(Message, delivery.source_ref)
            if (
                message is None
                or message.tenant_id != delivery.tenant_id
                or message.role != "assistant"
            ):
                raise ConnectorRuntimeError("CONNECTOR_DELIVERY_SOURCE_MISSING")
            return message.content
        publication = self.db.get(ExecutionPublication, delivery.source_ref)
        if (
            publication is None
            or publication.tenant_id != delivery.tenant_id
            or publication.target_type != "external_thread"
        ):
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_SOURCE_MISSING")
        result = self.db.get(ExecutionResult, publication.result_id)
        if result is None or result.tenant_id != delivery.tenant_id:
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_SOURCE_MISSING")
        content = str(result.result_json.get("markdown") or "")
        if not content:
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_SOURCE_MISSING")
        return content

    def finish_delivery(
        self,
        delivery: ConnectorOutboundDelivery,
        *,
        worker_id: str,
        status: str,
        error_code: str | None = None,
        receipt: dict[str, object] | None = None,
    ) -> ConnectorOutboundDelivery:
        """以当前 worker 的 owner/租约 CAS 持久化回发终态，拒绝迟到 worker 覆盖删除结果。"""

        now = utc_now()
        if status not in {"settled", "unknown", "dead_letter"}:
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_STATUS_INVALID")
        if not self._lock_delivery_agent(delivery):
            result = self.db.exec(
                update(ConnectorOutboundDelivery)
                .where(
                    ConnectorOutboundDelivery.id == delivery.id,
                    ConnectorOutboundDelivery.tenant_id == delivery.tenant_id,
                    ConnectorOutboundDelivery.status == "delivering",
                    ConnectorOutboundDelivery.lease_owner == worker_id,
                    ConnectorOutboundDelivery.lease_until > now,
                )
                .values(
                    status="unknown",
                    receipt_json={},
                    error_json={"code": "AGENT_NOT_AVAILABLE"},
                    settled_at=None,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", 0) != 1:
                self.db.rollback()
                raise ConnectorRuntimeError("CONNECTOR_DELIVERY_LEASE_LOST")
            self.db.commit()
            current = self.db.get(ConnectorOutboundDelivery, delivery.id)
            if current is None:
                raise ConnectorRuntimeError("CONNECTOR_DELIVERY_MISSING")
            if current.source_type == "execution_publication":
                self.sync_execution_delivery_status(
                    current,
                    worker_id="connector-publication-status",
                )
            return current
        result = self.db.exec(
            update(ConnectorOutboundDelivery)
            .where(
                ConnectorOutboundDelivery.id == delivery.id,
                ConnectorOutboundDelivery.tenant_id == delivery.tenant_id,
                ConnectorOutboundDelivery.status == "delivering",
                ConnectorOutboundDelivery.lease_owner == worker_id,
                ConnectorOutboundDelivery.lease_until > now,
            )
            .values(
                status=status,
                receipt_json=dict(receipt or {}),
                error_json={"code": error_code} if error_code else {},
                settled_at=now if status == "settled" else None,
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_LEASE_LOST")
        self.db.commit()
        current = self.db.get(ConnectorOutboundDelivery, delivery.id)
        if current is None:
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_MISSING")
        if status == "settled" and current.source_type == "execution_publication":
            self.settle_execution_delivery(current, worker_id="connector-publication-settler")
        elif (
            status in {"unknown", "dead_letter"}
            and current.source_type == "execution_publication"
        ):
            self.sync_execution_delivery_status(
                current,
                worker_id="connector-publication-status",
            )
        return current

    def _lock_delivery_agent(self, delivery: ConnectorOutboundDelivery) -> bool:
        """在外部发送或终态 CAS 前锁定线程所属 Agent，形成删除竞态的同一锁序。"""

        thread = self.db.get(ConnectorThreadBinding, delivery.thread_binding_id)
        if (
            thread is None
            or thread.tenant_id != delivery.tenant_id
            or not thread.agent_id
        ):
            return False
        return self._lock_agent(delivery.tenant_id, thread.agent_id)

    def _lock_inbound_event_agent(self, event: ConnectorInboundEvent) -> bool:
        """根据已绑定线程或活动路由锁定入站事件所属 Agent，拒绝墓碑事件继续认领。"""

        agent_id: str | None = None
        if event.thread_binding_id:
            thread = self.db.get(ConnectorThreadBinding, event.thread_binding_id)
            if thread is not None and thread.tenant_id == event.tenant_id:
                agent_id = thread.agent_id
        if agent_id is None:
            route = self.db.exec(
                select(ConnectorInboundRoute)
                .where(
                    ConnectorInboundRoute.tenant_id == event.tenant_id,
                    ConnectorInboundRoute.provider == event.provider,
                    ConnectorInboundRoute.profile_id == event.profile_id,
                    ConnectorInboundRoute.enabled.is_(True),
                )
            ).first()
            agent_id = route.agent_id if route is not None else None
        return agent_id is None or self._lock_agent(event.tenant_id, agent_id)

    def _lock_agent(self, tenant_id: str, agent_id: str) -> bool:
        """按 tenant 和主键锁定 Agent 最新生命周期，供 Connector 控制面共用。"""

        with self.db.no_autoflush:
            agent = self.db.exec(
                select(AgentProfile)
                .where(
                    AgentProfile.tenant_id == tenant_id,
                    AgentProfile.id == agent_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
        deletion = (agent.metadata_json or {}).get("agent_deletion") if agent else None
        deletion_state = deletion.get("state") if isinstance(deletion, dict) else None
        return bool(
            agent is not None
            and agent.status == "active"
            and deletion_state not in {"deleting", "deletion_pending", "deleted"}
        )

    def retry_delivery(
        self,
        delivery: ConnectorOutboundDelivery,
        *,
        worker_id: str,
        available_at: datetime,
        error_code: str,
    ) -> ConnectorOutboundDelivery:
        """以当前 worker 的 owner/租约 CAS 将可重试投递重新放回 pending。"""

        if not self._lock_delivery_agent(delivery):
            return self.finish_delivery(
                delivery,
                worker_id=worker_id,
                status="unknown",
                error_code="AGENT_NOT_AVAILABLE",
            )
        now = utc_now()
        result = self.db.exec(
            update(ConnectorOutboundDelivery)
            .where(
                ConnectorOutboundDelivery.id == delivery.id,
                ConnectorOutboundDelivery.tenant_id == delivery.tenant_id,
                ConnectorOutboundDelivery.status == "delivering",
                ConnectorOutboundDelivery.lease_owner == worker_id,
                ConnectorOutboundDelivery.lease_until > now,
            )
            .values(
                status="pending",
                available_at=available_at,
                error_json={"code": error_code},
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_LEASE_LOST")
        self.db.commit()
        current = self.db.get(ConnectorOutboundDelivery, delivery.id)
        if current is None:
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_MISSING")
        return current

    def sync_execution_delivery_status(
        self,
        delivery: ConnectorOutboundDelivery,
        *,
        worker_id: str,
    ) -> bool:
        """把 outbox 的 pending/delivering/unknown/dead-letter 状态同步到 required publication。"""

        if delivery.source_type != "execution_publication" or delivery.status == "settled":
            return False
        publication = self.db.get(ExecutionPublication, delivery.source_ref)
        if publication is None or publication.tenant_id != delivery.tenant_id:
            raise ConnectorRuntimeError("CONNECTOR_PUBLICATION_SOURCE_MISSING")
        if publication.status == delivery.status and publication.outbox_id == delivery.id:
            return True
        from app.sop_runtime.execution_control import ExecutionControlService
        from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore

        instance = self.db.get(SopInstance, publication.execution_id)
        if instance is None or instance.tenant_id != delivery.tenant_id:
            raise ConnectorRuntimeError("CONNECTOR_PUBLICATION_EXECUTION_MISSING")
        store = SopExecutionStore(self.db)
        try:
            with store.owned(instance, worker_id=worker_id):
                ExecutionControlService(self.db, store).record_external_publication_status(
                    instance,
                    publication,
                    status=delivery.status,
                    outbox_id=delivery.id,
                    error=delivery.error_json,
                )
            self.db.commit()
        except SopExecutionConflictError:
            self.db.rollback()
            return False
        return True

    def settle_execution_delivery(
        self,
        delivery: ConnectorOutboundDelivery,
        *,
        worker_id: str,
    ) -> bool:
        """将已送达 outbox 回执结算到 required publication，并在全部闭合后结束 Execution。"""

        if delivery.status != "settled" or delivery.source_type != "execution_publication":
            return False
        publication = self.db.get(ExecutionPublication, delivery.source_ref)
        if publication is None or publication.tenant_id != delivery.tenant_id:
            raise ConnectorRuntimeError("CONNECTOR_PUBLICATION_SOURCE_MISSING")
        if publication.status == "settled":
            return True
        from app.sop_runtime.execution_control import ExecutionControlService
        from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore

        instance = self.db.get(SopInstance, publication.execution_id)
        if instance is None or instance.tenant_id != delivery.tenant_id:
            raise ConnectorRuntimeError("CONNECTOR_PUBLICATION_EXECUTION_MISSING")
        store = SopExecutionStore(self.db)
        try:
            with store.owned(instance, worker_id=worker_id):
                control = ExecutionControlService(self.db, store)
                control.settle_external_publication(
                    instance,
                    publication,
                    outbox_id=delivery.id,
                    receipt=delivery.receipt_json,
                )
                control.append_execution_event(
                    instance,
                    event_type="execution_external_publication_settled",
                    causation_id=publication.id,
                    payload={"publication_id": publication.id, "outbox_id": delivery.id},
                )
                control.append_execution_event(
                    instance,
                    event_type="execution_succeeded",
                    causation_id=publication.id,
                    payload={
                        "result_id": publication.result_id,
                        "external_publication_id": publication.id,
                        "outbox_id": delivery.id,
                    },
                )
                store.complete_instance(instance)
                from app.general_skills.runtime import GeneralSkillRuntimeService

                GeneralSkillRuntimeService(self.db).settle_execution_uses(
                    execution_id=instance.id,
                    terminal_status="completed",
                    result_summary={
                        "result_id": publication.result_id,
                        "external_publication_id": publication.id,
                    },
                )
            self.db.commit()
        except SopExecutionConflictError:
            self.db.rollback()
            return False
        return True

    def reconcile_one_execution_publication(self, *, worker_id: str) -> bool:
        """恢复“外部已送达但 Execution 结算前崩溃”的单条 publication。"""

        delivery = self.db.exec(
            select(ConnectorOutboundDelivery)
            .join(
                ExecutionPublication,
                ExecutionPublication.id == ConnectorOutboundDelivery.source_ref,
            )
            .where(
                ConnectorOutboundDelivery.source_type == "execution_publication",
                ConnectorOutboundDelivery.status.in_(("settled", "unknown", "dead_letter")),
                ExecutionPublication.status != ConnectorOutboundDelivery.status,
            )
            .order_by(ConnectorOutboundDelivery.updated_at, ConnectorOutboundDelivery.id)
        ).first()
        if delivery is None:
            return False
        if delivery.status == "settled":
            return self.settle_execution_delivery(delivery, worker_id=worker_id)
        return self.sync_execution_delivery_status(delivery, worker_id=worker_id)

    def _assert_delivery_owner(
        self,
        delivery: ConnectorOutboundDelivery,
        worker_id: str,
    ) -> None:
        """拒绝过期或非当前 owner 对外发送，形成出站 fencing。"""

        if (
            delivery.status != "delivering"
            or delivery.lease_owner != worker_id
            or delivery.lease_until is None
            or delivery.lease_until <= utc_now()
        ):
            raise ConnectorRuntimeError("CONNECTOR_DELIVERY_LEASE_LOST")

    def _route_binding_active(
        self,
        event: ConnectorInboundEvent,
        route: ConnectorInboundRoute,
    ) -> bool:
        """要求入站路由仍属于同一档案的有效 Agent 连接绑定。"""

        return self.db.exec(
            select(AgentConnectionBinding.id).where(
                AgentConnectionBinding.tenant_id == event.tenant_id,
                AgentConnectionBinding.profile_id == event.profile_id,
                AgentConnectionBinding.agent_id == route.agent_id,
                AgentConnectionBinding.enabled.is_(True),
            )
        ).first() is not None

    def _assert_event_owner(self, event: ConnectorInboundEvent, worker_id: str) -> None:
        """拒绝无租约、租约过期或 fencing owner 不匹配的 inbox 修改。"""

        if (
            event.status != "processing"
            or event.lease_owner != worker_id
            or event.lease_until is None
            or event.lease_until <= utc_now()
        ):
            raise ConnectorRuntimeError("CONNECTOR_INBOUND_LEASE_LOST")
