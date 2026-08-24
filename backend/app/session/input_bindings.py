"""
@Time       : 2026/08/13 21:18
@Author     : zhanglp8181
@File       : input_bindings.py
@CallChain  : Chat API/AgentLoop → 上传binding/Turn ref → ResourceSessionBinding/MessageLink
@Description: 以服务端权威资源、用户、Agent与会话CAS取代客户端附件正文和状态字段。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    DraftUploadBinding,
    ManagedInputResource,
    MessageInputResourceLink,
    ResourceSessionBinding,
    utc_now,
)
from app.session.session_schema import ChatAttachmentRead, ChatAttachmentRef


class InputBindingError(RuntimeError):
    """表示附件引用不存在、归属冲突或上传授权失效。"""

    def __init__(self, code: str, detail: str = "附件不可用。") -> None:
        """保存稳定错误码和对外等价说明，避免资源枚举。"""

        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class UploadBindingSecret:
    """返回给当前浏览器的一次性上传binding身份和原始nonce。"""

    binding_id: str
    nonce: str
    expires_at: str


class InputBindingService:
    """管理上传请求claim、资源会话归属和消息引用的权威事务。"""

    def __init__(self, db: Session) -> None:
        """绑定调用方数据库事务。"""

        self.db = db

    def issue_upload_binding(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        agent_id: str,
        idempotency_key: str,
        session_id: str | None = None,
        draft_conversation_id: str | None = None,
        ttl_seconds: int = 600,
    ) -> UploadBindingSecret:
        """签发一次上传请求可claim的高熵binding，重放幂等键返回既有未过期事实。"""

        existing = self.db.exec(
            select(DraftUploadBinding).where(
                DraftUploadBinding.tenant_id == tenant_id,
                DraftUploadBinding.idempotency_key == idempotency_key,
            )
        ).first()
        if existing is not None:
            raise InputBindingError("ATTACHMENT_UPLOAD_BINDING_REPLAY")
        nonce = secrets.token_urlsafe(32)
        row = DraftUploadBinding(
            binding_id=f"upload_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            session_id=session_id,
            draft_conversation_id=draft_conversation_id,
            nonce_checksum=hashlib.sha256(nonce.encode()).hexdigest(),
            idempotency_key=idempotency_key,
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        self.db.add(row)
        self.db.flush()
        return UploadBindingSecret(row.binding_id, nonce, row.expires_at.isoformat())

    def claim_upload_binding(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        binding_id: str,
        nonce: str,
        worker_id: str,
    ) -> DraftUploadBinding:
        """在读取multipart正文前以owner、nonce、状态和到期时间CAS领取binding。"""

        nonce_checksum = hashlib.sha256(nonce.encode()).hexdigest()
        result = self.db.exec(
            update(DraftUploadBinding)
            .where(
                DraftUploadBinding.tenant_id == tenant_id,
                DraftUploadBinding.binding_id == binding_id,
                DraftUploadBinding.owner_user_id == owner_user_id,
                DraftUploadBinding.nonce_checksum == nonce_checksum,
                DraftUploadBinding.status == "active",
                DraftUploadBinding.expires_at > utc_now(),
            )
            .values(
                status="claimed",
                lease_owner=worker_id,
                claimed_at=utc_now(),
                revision=DraftUploadBinding.revision + 1,
                updated_at=utc_now(),
            )
        )
        if result.rowcount != 1:
            raise InputBindingError("ATTACHMENT_UPLOAD_BINDING_INVALID")
        self.db.flush()
        row = self.db.exec(
            select(DraftUploadBinding).where(
                DraftUploadBinding.tenant_id == tenant_id,
                DraftUploadBinding.binding_id == binding_id,
            )
        ).one()
        return row

    def consume_upload_binding(
        self,
        binding: DraftUploadBinding,
        *,
        resource_ids: list[str],
    ) -> None:
        """记录本次全有或全无资源集合并终结binding，集合摘要用于草稿提升校验。"""

        binding.resource_set_checksum = hashlib.sha256(
            "\n".join(resource_ids).encode("utf-8")
        ).hexdigest()
        binding.status = "consumed"
        binding.consumed_at = utc_now()
        binding.lease_owner = None
        binding.updated_at = utc_now()
        self.db.add(binding)
        self.db.flush()

    def expire_upload_binding(self, binding_id: str, *, tenant_id: str) -> None:
        """上传失败或断连后终结已claim binding，禁止旧nonce再次灌入资源。"""

        binding = self.db.exec(
            select(DraftUploadBinding).where(
                DraftUploadBinding.tenant_id == tenant_id,
                DraftUploadBinding.binding_id == binding_id,
            )
        ).first()
        if binding is not None and binding.status in {"active", "claimed"}:
            binding.status = "expired"
            binding.lease_owner = None
            binding.updated_at = utc_now()
            self.db.add(binding)
            self.db.flush()

    def resolve_turn_refs(
        self,
        refs: list[ChatAttachmentRef],
        *,
        tenant_id: str,
        owner_user_id: str,
        session_id: str,
        agent_id: str,
        draft_conversation_id: str | None = None,
    ) -> list[tuple[ManagedInputResource, ResourceSessionBinding, ChatAttachmentRead]]:
        """批量回查当前用户资源并CAS会话归属，返回不含正文和data URL的规范投影。"""

        resolved = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            identity = (ref.resource_id, ref.resource_version)
            if identity in seen:
                continue
            seen.add(identity)
            resource = self.db.exec(
                select(ManagedInputResource).where(
                    ManagedInputResource.id == ref.resource_id,
                    ManagedInputResource.version == ref.resource_version,
                    ManagedInputResource.tenant_id == tenant_id,
                    ManagedInputResource.owner_user_id == owner_user_id,
                )
            ).first()
            if resource is None or resource.access_status != "active" or resource.revoked_at:
                raise InputBindingError("ATTACHMENT_NOT_AVAILABLE")
            if resource.agent_id is not None and resource.agent_id != agent_id:
                raise InputBindingError("ATTACHMENT_NOT_AVAILABLE")
            upload_binding = self.db.exec(
                select(DraftUploadBinding).where(
                    DraftUploadBinding.tenant_id == tenant_id,
                    DraftUploadBinding.binding_id == resource.upload_binding_id,
                    DraftUploadBinding.owner_user_id == owner_user_id,
                    DraftUploadBinding.agent_id == agent_id,
                    DraftUploadBinding.status == "consumed",
                )
            ).first()
            if upload_binding is None:
                raise InputBindingError("ATTACHMENT_NOT_AVAILABLE")
            if upload_binding.session_id and upload_binding.session_id != session_id:
                raise InputBindingError("ATTACHMENT_SESSION_CONFLICT")
            if upload_binding.draft_conversation_id:
                if upload_binding.draft_conversation_id != draft_conversation_id:
                    raise InputBindingError("ATTACHMENT_SESSION_CONFLICT")
            binding = self.db.exec(
                select(ResourceSessionBinding).where(
                    ResourceSessionBinding.tenant_id == tenant_id,
                    ResourceSessionBinding.resource_id == resource.id,
                    ResourceSessionBinding.resource_version == resource.version,
                )
            ).first()
            if binding is None:
                binding = ResourceSessionBinding(
                    tenant_id=tenant_id,
                    resource_id=resource.id,
                    resource_version=resource.version,
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                )
                self.db.add(binding)
                try:
                    self.db.flush()
                except IntegrityError as exc:
                    raise InputBindingError("ATTACHMENT_SESSION_CONFLICT") from exc
            if binding.session_id != session_id or binding.agent_id != agent_id:
                raise InputBindingError("ATTACHMENT_SESSION_CONFLICT")
            kind = str(resource.extraction_metadata_json.get("kind") or "binary")
            if kind not in {"text", "pdf", "image", "binary"}:
                kind = "binary"
            projection = ChatAttachmentRead(
                id=resource.id,
                filename=resource.filename,
                content_type=resource.mime_type,
                size=resource.size_bytes,
                kind=kind,
                preview=str(resource.extraction_metadata_json.get("preview") or "") or None,
                resource_id=resource.id,
                resource_version=resource.version,
                content_checksum=resource.content_checksum,
                ingestion_status=resource.ingestion_status,
            )
            resolved.append((resource, binding, projection))
        return resolved

    def link_message(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        resolved: list[tuple[ManagedInputResource, ResourceSessionBinding, ChatAttachmentRead]],
    ) -> list[MessageInputResourceLink]:
        """为已生成的权威用户消息追加资源Link，正文和data URL不进入消息元数据。"""

        links = []
        for ordinal, (resource, binding, _projection) in enumerate(resolved):
            link = MessageInputResourceLink(
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=message_id,
                resource_binding_id=binding.id,
                resource_id=resource.id,
                resource_version=resource.version,
                content_checksum=resource.content_checksum,
                ordinal=ordinal,
            )
            self.db.add(link)
            links.append(link)
        self.db.flush()
        return links
