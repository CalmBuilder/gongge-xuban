"""
@Time       : 2026/08/03 23:08
@Author     : zhanglp8181
@File       : managed_resources.py
@CallChain  : Chat attachment API/Dynamic execution → ManagedInputResourceService → filesystem + DB
@Description: 持久化受管聊天输入，校验内容摘要并在快照或读取前复核当前访问权。
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from sqlalchemy import update
from sqlmodel import Session, select

from app import paths
from app.agents.identity import agent_is_published, agent_owner_user_id
from app.db.models import (
    AgentProfile,
    AgentUsage,
    ArtifactInputLink,
    ArtifactRendererJob,
    AttachmentUploadCleanupJob,
    ChatSession,
    InputDocumentElement,
    InputResourceExtraction,
    InputResourceExtractionAttempt,
    InputResourcePurgeJob,
    InputResourcePurgeTombstone,
    InputResourceSnapshot,
    ExecutionArtifact,
    ManagedInputResource,
    MessageInputResourceLink,
    ResourceSessionBinding,
    SelectedResourceExtraction,
    SopInstance,
    User,
    new_id,
    utc_now,
)
from app.session.attachments import parse_chat_attachment
from app.session.session_schema import ChatAttachmentRead
from app.security.managed_storage import (
    ManagedStorageError,
    managed_open_read_fd,
    managed_read_bytes,
    managed_unlink,
    managed_validate_path,
    managed_write_bytes,
    managed_write_from_path,
)


MAX_MANAGED_INPUT_BYTES = 12 * 1024 * 1024
_EICAR_MARKER = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"


class InputResourceAccessDenied(RuntimeError):
    """表示资源不存在、越权、撤销、损坏或尚未达到 ready 状态。"""


def assert_input_resource_access(
    db: Session,
    resource: ManagedInputResource,
    *,
    instance: SopInstance,
) -> None:
    """统一复核资源归属、成员状态及当前聊天 Agent 使用关系，供快照和读取共用。"""

    if resource.tenant_id != instance.tenant_id:
        raise InputResourceAccessDenied("输入资源不可用。")
    actor_user_id = instance.initiator_user_id
    if instance.kind == "sop" and actor_user_id is None:
        session = db.get(ChatSession, instance.session_id)
        actor_user_id = session.user_id if session is not None else None
    if instance.kind not in {"dynamic_task", "sop"} or resource.owner_user_id != actor_user_id:
        raise InputResourceAccessDenied("输入资源不可用。")
    if resource.agent_id is not None and resource.agent_id != instance.agent_id:
        raise InputResourceAccessDenied("输入资源不可用。")
    actor = db.exec(
        select(User).where(
            User.id == actor_user_id,
            User.tenant_id == instance.tenant_id,
            User.membership_status == "active",
        )
    ).first()
    agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.id == instance.agent_id,
            AgentProfile.tenant_id == instance.tenant_id,
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        )
    ).first()
    if actor is None or agent is None:
        raise InputResourceAccessDenied("输入资源不可用。")
    if agent_owner_user_id(agent) == actor.id:
        return
    if not agent_is_published(agent):
        raise InputResourceAccessDenied("输入资源不可用。")
    usage = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == instance.tenant_id,
            AgentUsage.user_id == actor.id,
            AgentUsage.agent_id == agent.id,
        )
    ).first()
    if usage is None:
        raise InputResourceAccessDenied("输入资源不可用。")


def _clear_purged_resource_identifiers(resource: ManagedInputResource) -> None:
    """把永久墓碑中的原始文件身份清空，避免删除后继续保留可复用定位信息。"""

    resource.filename = "[purged]"
    resource.mime_type = "application/octet-stream"
    resource.size_bytes = 0
    resource.content_checksum = "[purged]"
    resource.storage_locator = f"purged/{resource.id}"


class ManagedInputResourceService:
    """以内容寻址文件和数据库身份管理动态任务可引用的聊天上传资源。"""

    def __init__(self, db: Session, *, storage_root: Path | None = None) -> None:
        """绑定事务和受管存储根；测试可注入隔离目录。"""

        self.db = db
        self.storage_root = storage_root or paths.user_data_dir() / "input-resources"

    def _record_purge_tombstone(
        self,
        resource: ManagedInputResource,
        *,
        event_kind: str,
        purge_job_id: str | None = None,
        session_id: str | None = None,
        requested_by_user_id: str | None = None,
    ) -> InputResourcePurgeTombstone:
        """幂等写入最小销毁审计身份，不复制filename、locator或原始checksum。"""

        existing = self.db.exec(
            select(InputResourcePurgeTombstone).where(
                InputResourcePurgeTombstone.tenant_id == resource.tenant_id,
                InputResourcePurgeTombstone.resource_id == resource.id,
                InputResourcePurgeTombstone.resource_version == resource.version,
            )
        ).first()
        if existing is not None:
            return existing
        tombstone = InputResourcePurgeTombstone(
            tenant_id=resource.tenant_id,
            resource_id=resource.id,
            resource_version=resource.version,
            purge_job_id=purge_job_id,
            session_id=session_id,
            requested_by_user_id=requested_by_user_id,
            event_kind=event_kind,
        )
        self.db.add(tombstone)
        self.db.flush()
        return tombstone

    def persist_upload(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        filename: str,
        content_type: str | None,
        data: bytes,
        agent_id: str | None = None,
        upload_binding_id: str | None = None,
        defer_extraction_format: str | None = None,
    ) -> tuple[ManagedInputResource, ChatAttachmentRead]:
        """解析并原子落盘单个上传；不支持动态消费的二进制仍可供普通问答展示。"""

        if len(data) > MAX_MANAGED_INPUT_BYTES:
            raise ValueError("输入资源超过平台大小限制。")
        parsed = (
            ChatAttachmentRead(
                id=new_id("attachment"),
                filename=filename,
                content_type=content_type or "application/octet-stream",
                size=len(data),
                kind=(
                    "image"
                    if defer_extraction_format == "image"
                    else "pdf"
                    if defer_extraction_format == "pdf"
                    else "text"
                ),
                preview="正在进行隔离解析",
                ingestion_status="extracting",
            )
            if defer_extraction_format
            else parse_chat_attachment(filename, content_type, data)
        )
        checksum = hashlib.sha256(data).hexdigest()
        version = checksum
        extraction_text = parsed.text or ""
        extraction_checksum = hashlib.sha256(extraction_text.encode("utf-8")).hexdigest()
        status = "extracting" if defer_extraction_format else _ingestion_status(parsed, data)
        locator = self._locator(tenant_id, parsed.id, checksum)
        relative_locator = locator.relative_to(self.storage_root).as_posix()
        try:
            managed_write_bytes(self.storage_root, relative_locator, data)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc
        resource = ManagedInputResource(
            id=parsed.id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            upload_binding_id=upload_binding_id,
            version=version,
            filename=parsed.filename,
            mime_type=parsed.content_type,
            size_bytes=len(data),
            content_checksum=checksum,
            extraction_checksum=extraction_checksum,
            ingestion_status=status,
            storage_locator=relative_locator,
            extracted_text=extraction_text or None,
            extraction_metadata_json={
                "kind": parsed.kind,
                "preview": parsed.preview,
                "python_summary": parsed.python_summary,
                "error": parsed.error,
                "pipeline": ["uploaded", "scanning", "extracting", status],
            },
        )
        self.db.add(resource)
        try:
            self.db.flush()
        except Exception:
            managed_unlink(self.storage_root, relative_locator, missing_ok=True)
            raise
        response = parsed.model_copy(
            update={
                "resource_id": resource.id,
                "resource_version": resource.version,
                "content_checksum": resource.content_checksum,
                "ingestion_status": resource.ingestion_status,
            }
        )
        return resource, response

    def persist_upload_path(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        filename: str,
        content_type: str | None,
        source_path: Path,
        agent_id: str,
        upload_binding_id: str,
        extraction_format: str,
    ) -> tuple[ManagedInputResource, ChatAttachmentRead]:
        """从有界临时文件流式复制到受管存储，不把完整上传重新聚合进API内存。"""

        size = source_path.stat().st_size
        if size <= 0 or size > MAX_MANAGED_INPUT_BYTES:
            raise ValueError("输入资源超过平台大小限制。")
        digest = hashlib.sha256()
        with source_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        checksum = digest.hexdigest()
        resource_id = new_id("attachment")
        mime_type = content_type or "application/octet-stream"
        attachment = ChatAttachmentRead(
            id=resource_id,
            filename=filename,
            content_type=mime_type,
            size=size,
            kind="image" if extraction_format == "image" else "pdf" if extraction_format == "pdf" else "text",
            preview="正在进行隔离解析",
            ingestion_status="extracting",
        )
        locator = self._locator(tenant_id, resource_id, checksum)
        relative_locator = locator.relative_to(self.storage_root).as_posix()
        try:
            managed_write_from_path(self.storage_root, relative_locator, source_path)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc
        resource = ManagedInputResource(
            id=resource_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            upload_binding_id=upload_binding_id,
            version=checksum,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size,
            content_checksum=checksum,
            extraction_checksum=hashlib.sha256(b"").hexdigest(),
            ingestion_status="extracting",
            storage_locator=relative_locator,
            extraction_metadata_json={
                "kind": attachment.kind,
                "preview": attachment.preview,
                "pipeline": ["uploaded", "scanning", "extracting"],
            },
        )
        self.db.add(resource)
        try:
            self.db.flush()
        except Exception:
            managed_unlink(self.storage_root, relative_locator, missing_ok=True)
            raise
        return resource, attachment.model_copy(
            update={
                "resource_id": resource.id,
                "resource_version": resource.version,
                "content_checksum": resource.content_checksum,
            }
        )

    def parser_input_path(self, resource: ManagedInputResource) -> Path:
        """仅向受信解析worker返回已校验位于受管根内的blob路径。"""

        try:
            return managed_validate_path(self.storage_root, resource.storage_locator)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc

    @contextmanager
    def parser_input_descriptor(self, resource: ManagedInputResource) -> Iterator[int]:
        """安全打开受管blob并在解析结束后关闭fd，子进程继承inode而非重新解析路径。"""

        try:
            descriptor = managed_open_read_fd(self.storage_root, resource.storage_locator)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def resolve_for_execution(
        self,
        resource_id: str,
        *,
        instance: SopInstance,
    ) -> tuple[ManagedInputResource, bytes]:
        """按当前 tenant、发起人、Agent、状态和磁盘 checksum 重新授权并读取资源。"""

        resource = self.db.get(ManagedInputResource, resource_id)
        if resource is None:
            raise InputResourceAccessDenied("输入资源不可用。")
        assert_input_resource_access(self.db, resource, instance=instance)
        if resource.ingestion_status != "ready" or resource.revoked_at is not None:
            raise InputResourceAccessDenied("输入资源不可用。")
        try:
            data = managed_read_bytes(self.storage_root, resource.storage_locator)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc
        if hashlib.sha256(data).hexdigest() != resource.content_checksum:
            raise InputResourceAccessDenied("输入资源不可用。")
        return resource, data

    def revoke(self, resource: ManagedInputResource, *, actor_user_id: str) -> None:
        """仅允许上传者撤销资源，撤销后既有 snapshot 也不能继续读取正文。"""

        if actor_user_id != resource.owner_user_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        resource.ingestion_status = "revoked"
        resource.revoked_at = utc_now()
        resource.acl_revision += 1
        resource.updated_at = utc_now()
        self.db.add(resource)
        self.db.flush()

    def discard_unreferenced(self, resource: ManagedInputResource, *, actor_user_id: str) -> None:
        """未形成MessageLink的Composer附件可立即物理清理，已引用资源只能走撤权/purge。"""

        from app.db.models import MessageInputResourceLink

        if actor_user_id != resource.owner_user_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        linked = self.db.exec(
            select(MessageInputResourceLink.id).where(
                MessageInputResourceLink.tenant_id == resource.tenant_id,
                MessageInputResourceLink.resource_id == resource.id,
                MessageInputResourceLink.resource_version == resource.version,
            )
        ).first()
        if linked is not None:
            raise InputResourceAccessDenied("已发送附件不能直接丢弃。")
        resource.access_status = "revoked"
        resource.destruction_status = "purged"
        resource.ingestion_status = "revoked"
        resource.acl_revision += 1
        resource.revoked_at = utc_now()
        resource.updated_at = utc_now()
        try:
            managed_unlink(self.storage_root, resource.storage_locator, missing_ok=True)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc
        _clear_purged_resource_identifiers(resource)
        self._record_purge_tombstone(
            resource,
            event_kind="composer_discard",
            requested_by_user_id=actor_user_id,
        )
        self.db.add(resource)
        self.db.flush()

    def purge_session_resource(
        self,
        resource: ManagedInputResource,
        *,
        session_id: str,
        actor_user_id: str,
    ) -> None:
        """通过持久租约作业幂等销毁会话资源，崩溃后允许维护worker安全接管。"""

        if actor_user_id != resource.owner_user_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        job = self.db.exec(
            select(InputResourcePurgeJob).where(
                InputResourcePurgeJob.tenant_id == resource.tenant_id,
                InputResourcePurgeJob.resource_id == resource.id,
                InputResourcePurgeJob.resource_version == resource.version,
            )
        ).first()
        if job is not None and job.status == "succeeded":
            return
        now = utc_now()
        if job is None:
            job = InputResourcePurgeJob(
                tenant_id=resource.tenant_id,
                resource_id=resource.id,
                resource_version=resource.version,
                session_id=session_id,
                requested_by_user_id=actor_user_id,
            )
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
        elif job.session_id != session_id or job.requested_by_user_id != actor_user_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        worker_id = f"purge:{actor_user_id}"
        claimable = job.status in {"pending", "failed"} or (
            job.status in {"claimed", "purging"}
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if not claimable:
            raise InputResourceAccessDenied("附件销毁作业正在执行。")
        claimed = self.db.exec(
            update(InputResourcePurgeJob)
            .where(
                InputResourcePurgeJob.id == job.id,
                InputResourcePurgeJob.status == job.status,
                InputResourcePurgeJob.fencing_token == job.fencing_token,
            )
            .values(
                status="claimed",
                attempt_no=InputResourcePurgeJob.attempt_no + 1,
                lease_owner=worker_id,
                fencing_token=InputResourcePurgeJob.fencing_token + 1,
                lease_expires_at=now + timedelta(seconds=60),
                started_at=now,
                updated_at=now,
                error_code=None,
                error_detail_json={},
            )
        )
        if getattr(claimed, "rowcount", 0) != 1:
            self.db.rollback()
            raise InputResourceAccessDenied("附件销毁作业已被其他worker接管。")
        self.db.commit()
        self.db.refresh(job)
        job.status = "purging"
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()
        try:
            self._purge_session_resource_now(
                resource,
                session_id=session_id,
                actor_user_id=actor_user_id,
                purge_job_id=job.id,
            )
            job.status = "succeeded"
            job.finished_at = utc_now()
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = utc_now()
            self.db.add(job)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            job = self.db.get(InputResourcePurgeJob, job.id)
            if job is not None and job.status in {"claimed", "purging"}:
                job.status = "failed"
                job.error_code = (
                    "ATTACHMENT_PURGE_BLOCKED"
                    if isinstance(exc, InputResourceAccessDenied)
                    else "ATTACHMENT_PURGE_FAILED"
                )
                job.error_detail_json = {"detail": str(exc)[:500]}
                job.finished_at = utc_now()
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = utc_now()
                self.db.add(job)
                self.db.commit()
            raise

    def _purge_session_resource_now(
        self,
        resource: ManagedInputResource,
        *,
        session_id: str,
        actor_user_id: str,
        purge_job_id: str | None = None,
    ) -> None:
        """在已取得销毁作业fencing后级联清理在线副本并保留资源墓碑。"""

        if actor_user_id != resource.owner_user_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        binding = self.db.exec(
            select(ResourceSessionBinding).where(
                ResourceSessionBinding.tenant_id == resource.tenant_id,
                ResourceSessionBinding.resource_id == resource.id,
                ResourceSessionBinding.resource_version == resource.version,
                ResourceSessionBinding.session_id == session_id,
            )
        ).first()
        if binding is None:
            raise InputResourceAccessDenied("输入资源不可用。")
        active_snapshot = self.db.exec(
            select(InputResourceSnapshot.id)
            .join(SopInstance, SopInstance.id == InputResourceSnapshot.execution_id)
            .where(
                InputResourceSnapshot.tenant_id == resource.tenant_id,
                InputResourceSnapshot.source_resource_id == resource.id,
                InputResourceSnapshot.ingestion_status == "ready",
                SopInstance.tenant_id == resource.tenant_id,
                SopInstance.status.in_(("created", "running", "waiting")),
            )
        ).first()
        if active_snapshot is not None:
            raise InputResourceAccessDenied("附件仍被活动执行引用。")
        linked_artifacts = self.db.exec(
            select(ExecutionArtifact)
            .join(ArtifactInputLink, ArtifactInputLink.artifact_id == ExecutionArtifact.id)
            .join(InputResourceSnapshot, InputResourceSnapshot.id == ArtifactInputLink.input_snapshot_id)
            .where(
                ExecutionArtifact.tenant_id == resource.tenant_id,
                InputResourceSnapshot.tenant_id == resource.tenant_id,
                InputResourceSnapshot.source_resource_id == resource.id,
                ExecutionArtifact.status == "ready",
            )
        ).all()
        for artifact in linked_artifacts:
            artifact.status = "revoked"
            artifact.revoked_at = utc_now()
            artifact.updated_at = utc_now()
            self.db.add(artifact)
        linked_jobs = self.db.exec(
            select(ArtifactRendererJob)
            .join(ArtifactInputLink, ArtifactInputLink.artifact_id == ArtifactRendererJob.artifact_id)
            .join(InputResourceSnapshot, InputResourceSnapshot.id == ArtifactInputLink.input_snapshot_id)
            .where(
                ArtifactRendererJob.tenant_id == resource.tenant_id,
                InputResourceSnapshot.tenant_id == resource.tenant_id,
                InputResourceSnapshot.source_resource_id == resource.id,
                ArtifactRendererJob.status.in_(("pending", "claimed", "rendering", "staged")),
            )
        ).all()
        for job in linked_jobs:
            job.status = "cancelled"
            job.lease_owner = None
            job.lease_expires_at = None
            job.error_code = "ATTACHMENT_COUNTERMANDED"
            job.updated_at = utc_now()
            self.db.add(job)
        selections = self.db.exec(
            select(SelectedResourceExtraction).where(
                SelectedResourceExtraction.tenant_id == resource.tenant_id,
                SelectedResourceExtraction.resource_id == resource.id,
                SelectedResourceExtraction.resource_version == resource.version,
            )
        ).all()
        extraction_ids = [row.extraction_id for row in selections]
        for row in self.db.exec(
            select(InputDocumentElement).where(
                InputDocumentElement.tenant_id == resource.tenant_id,
                InputDocumentElement.extraction_id.in_(extraction_ids),
            )
        ).all() if extraction_ids else []:
            self.db.delete(row)
        for row in self.db.exec(
            select(InputResourceExtraction).where(
                InputResourceExtraction.tenant_id == resource.tenant_id,
                InputResourceExtraction.resource_id == resource.id,
                InputResourceExtraction.resource_version == resource.version,
            )
        ).all():
            self.db.delete(row)
        for row in self.db.exec(
            select(InputResourceExtractionAttempt).where(
                InputResourceExtractionAttempt.tenant_id == resource.tenant_id,
                InputResourceExtractionAttempt.resource_id == resource.id,
                InputResourceExtractionAttempt.resource_version == resource.version,
            )
        ).all():
            self.db.delete(row)
        for row in selections:
            self.db.delete(row)
        for row in self.db.exec(
            select(MessageInputResourceLink).where(
                MessageInputResourceLink.tenant_id == resource.tenant_id,
                MessageInputResourceLink.resource_id == resource.id,
                MessageInputResourceLink.resource_version == resource.version,
            )
        ).all():
            self.db.delete(row)
        self.db.delete(binding)
        try:
            managed_unlink(self.storage_root, resource.storage_locator, missing_ok=True)
        except ManagedStorageError as exc:
            raise InputResourceAccessDenied("输入资源不可用。") from exc
        resource.access_status = "revoked"
        resource.destruction_status = "purged"
        resource.ingestion_status = "revoked"
        resource.extracted_text = None
        resource.extraction_checksum = None
        resource.extraction_metadata_json = {}
        _clear_purged_resource_identifiers(resource)
        self._record_purge_tombstone(
            resource,
            event_kind="session_purge",
            purge_job_id=purge_job_id,
            session_id=session_id,
            requested_by_user_id=actor_user_id,
        )
        resource.acl_revision += 1
        resource.revoked_at = utc_now()
        resource.updated_at = utc_now()
        self.db.add(resource)
        self.db.flush()

    def resolve_snapshot(
        self,
        snapshot: InputResourceSnapshot,
        *,
        instance: SopInstance,
    ) -> tuple[ManagedInputResource, bytes]:
        """按当前 ACL 解析历史 snapshot，并拒绝版本、checksum 或归属发生漂移。"""

        if snapshot.execution_id != instance.id or snapshot.tenant_id != instance.tenant_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        resource, data = self.resolve_for_execution(
            snapshot.source_resource_id,
            instance=instance,
        )
        if (
            resource.version != snapshot.source_version
            or resource.content_checksum != snapshot.content_checksum
            or resource.extraction_checksum != snapshot.extraction_checksum
            or resource.acl_revision != snapshot.resource_acl_revision_at_snapshot
        ):
            raise InputResourceAccessDenied("输入资源不可用。")
        return resource, data

    def replay_purge_tombstones(self, *, tenant_id: str) -> int:
        """在备份恢复后按租户重放永久墓碑，阻止旧资源、Extraction和Artifact复活。"""

        tombstones = self.db.exec(
            select(InputResourcePurgeTombstone).where(
                InputResourcePurgeTombstone.tenant_id == tenant_id,
            )
        ).all()
        replayed = 0
        for tombstone in tombstones:
            resource = self.db.get(ManagedInputResource, tombstone.resource_id)
            if resource is None or resource.tenant_id != tenant_id:
                continue
            if resource.version != tombstone.resource_version:
                continue
            if resource.destruction_status != "purged":
                if not resource.storage_locator.startswith("purged/"):
                    try:
                        managed_unlink(self.storage_root, resource.storage_locator, missing_ok=True)
                    except ManagedStorageError as exc:
                        raise InputResourceAccessDenied("恢复后的附件销毁重放失败。") from exc
                extraction_ids = [
                    row.id
                    for row in self.db.exec(
                        select(InputResourceExtraction.id).where(
                            InputResourceExtraction.tenant_id == tenant_id,
                            InputResourceExtraction.resource_id == resource.id,
                            InputResourceExtraction.resource_version == resource.version,
                        )
                    ).all()
                ]
                if extraction_ids:
                    for element in self.db.exec(
                        select(InputDocumentElement).where(
                            InputDocumentElement.tenant_id == tenant_id,
                            InputDocumentElement.extraction_id.in_(extraction_ids),
                        )
                    ).all():
                        self.db.delete(element)
                for extraction in self.db.exec(
                    select(InputResourceExtraction).where(
                        InputResourceExtraction.tenant_id == tenant_id,
                        InputResourceExtraction.resource_id == resource.id,
                        InputResourceExtraction.resource_version == resource.version,
                    )
                ).all():
                    self.db.delete(extraction)
                for attempt in self.db.exec(
                    select(InputResourceExtractionAttempt).where(
                        InputResourceExtractionAttempt.tenant_id == tenant_id,
                        InputResourceExtractionAttempt.resource_id == resource.id,
                        InputResourceExtractionAttempt.resource_version == resource.version,
                    )
                ).all():
                    self.db.delete(attempt)
                for link in self.db.exec(
                    select(MessageInputResourceLink).where(
                        MessageInputResourceLink.tenant_id == tenant_id,
                        MessageInputResourceLink.resource_id == resource.id,
                        MessageInputResourceLink.resource_version == resource.version,
                    )
                ).all():
                    self.db.delete(link)
                for binding in self.db.exec(
                    select(ResourceSessionBinding).where(
                        ResourceSessionBinding.tenant_id == tenant_id,
                        ResourceSessionBinding.resource_id == resource.id,
                        ResourceSessionBinding.resource_version == resource.version,
                    )
                ).all():
                    self.db.delete(binding)
                for artifact in self.db.exec(
                    select(ExecutionArtifact)
                    .join(ArtifactInputLink, ArtifactInputLink.artifact_id == ExecutionArtifact.id)
                    .join(InputResourceSnapshot, InputResourceSnapshot.id == ArtifactInputLink.input_snapshot_id)
                    .where(
                        ExecutionArtifact.tenant_id == tenant_id,
                        InputResourceSnapshot.tenant_id == tenant_id,
                        InputResourceSnapshot.source_resource_id == resource.id,
                        ExecutionArtifact.status == "ready",
                    )
                ).all():
                    artifact.status = "revoked"
                    artifact.revoked_at = utc_now()
                    artifact.updated_at = utc_now()
                    self.db.add(artifact)
                resource.access_status = "revoked"
                resource.destruction_status = "purged"
                resource.ingestion_status = "revoked"
                resource.extracted_text = None
                resource.extraction_checksum = None
                resource.extraction_metadata_json = {}
                resource.revoked_at = resource.revoked_at or utc_now()
                resource.acl_revision += 1
                _clear_purged_resource_identifiers(resource)
                self.db.add(resource)
                replayed += 1
            self._record_purge_tombstone(
                resource,
                event_kind="replay",
                purge_job_id=tombstone.purge_job_id,
                session_id=tombstone.session_id,
                requested_by_user_id=tombstone.requested_by_user_id,
            )
        self.db.flush()
        return replayed

    def discard_uncommitted(self, resources: list[ManagedInputResource]) -> None:
        """在上传事务失败时清理尚未形成权威 DB 身份的内容文件。"""

        for resource in resources:
            try:
                managed_unlink(self.storage_root, resource.storage_locator, missing_ok=True)
            except ManagedStorageError:
                continue

    def schedule_upload_failure_cleanup(
        self,
        resources: list[ManagedInputResource],
        *,
        tenant_id: str,
        owner_user_id: str,
        upload_binding_id: str,
    ) -> AttachmentUploadCleanupJob:
        """先持久撤权所有已落盘资源并建立可恢复清理作业，不在本事务删除blob。"""

        manifest = [
            {"resource_id": resource.id, "storage_locator": resource.storage_locator}
            for resource in resources
        ]
        supplied_resources = {resource.id: resource for resource in resources}
        job = self.db.exec(
            select(AttachmentUploadCleanupJob).where(
                AttachmentUploadCleanupJob.tenant_id == tenant_id,
                AttachmentUploadCleanupJob.upload_binding_id == upload_binding_id,
            )
        ).first()
        if job is None:
            job = AttachmentUploadCleanupJob(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                upload_binding_id=upload_binding_id,
                resource_manifest_json=manifest,
            )
        elif job.owner_user_id != owner_user_id:
            raise InputResourceAccessDenied("输入资源不可用。")
        else:
            known = {str(item.get("resource_id")) for item in job.resource_manifest_json}
            additions = [item for item in manifest if str(item["resource_id"]) not in known]
            job.resource_manifest_json = [
                *job.resource_manifest_json,
                *additions,
            ]
            if job.status != "succeeded" or additions:
                job.status = "pending"
                job.lease_owner = None
                job.lease_expires_at = None
                job.finished_at = None
                job.updated_at = utc_now()
        for item in manifest:
            resource = self.db.get(ManagedInputResource, str(item["resource_id"]))
            if resource is None:
                resource = supplied_resources[str(item["resource_id"])]
                self.db.add(resource)
            if resource.tenant_id != tenant_id or resource.owner_user_id != owner_user_id:
                raise InputResourceAccessDenied("输入资源不可用。")
            if (
                resource.upload_binding_id != upload_binding_id
                or resource.storage_locator != str(item["storage_locator"])
            ):
                raise InputResourceAccessDenied("输入资源不可用。")
            resource.access_status = "revoked"
            resource.ingestion_status = "revoked"
            resource.acl_revision += 1
            resource.revoked_at = utc_now()
            resource.updated_at = utc_now()
            self.db.add(resource)
        self.db.add(job)
        self.db.flush()
        return job

    def run_upload_failure_cleanup(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        """以lease/fencing领取失败上传作业，逐blob幂等删除并逐资源提交purged墓碑。"""

        job = self.db.get(AttachmentUploadCleanupJob, job_id)
        if job is None:
            raise InputResourceAccessDenied("输入资源不可用。")
        now = utc_now()
        claimable = job.status in {"pending", "failed"} or (
            job.status in {"claimed", "purging"}
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if job.status == "succeeded":
            return
        if not claimable:
            raise InputResourceAccessDenied("附件清理作业正在执行。")
        claimed = self.db.exec(
            update(AttachmentUploadCleanupJob)
            .where(
                AttachmentUploadCleanupJob.id == job.id,
                AttachmentUploadCleanupJob.status == job.status,
                AttachmentUploadCleanupJob.fencing_token == job.fencing_token,
            )
            .values(
                status="purging",
                attempt_no=AttachmentUploadCleanupJob.attempt_no + 1,
                fencing_token=AttachmentUploadCleanupJob.fencing_token + 1,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                error_code=None,
                error_detail_json={},
                updated_at=now,
            )
        )
        if getattr(claimed, "rowcount", 0) != 1:
            self.db.rollback()
            raise InputResourceAccessDenied("附件清理作业已被其他worker接管。")
        self.db.commit()
        self.db.refresh(job)
        fencing_token = job.fencing_token
        try:
            for item in job.resource_manifest_json:
                renewed = self.db.exec(
                    update(AttachmentUploadCleanupJob)
                    .where(
                        AttachmentUploadCleanupJob.id == job.id,
                        AttachmentUploadCleanupJob.status == "purging",
                        AttachmentUploadCleanupJob.fencing_token == fencing_token,
                        AttachmentUploadCleanupJob.lease_owner == worker_id,
                        AttachmentUploadCleanupJob.lease_expires_at > utc_now(),
                    )
                    .values(
                        lease_expires_at=utc_now() + timedelta(seconds=lease_seconds),
                        updated_at=utc_now(),
                    )
                )
                if getattr(renewed, "rowcount", 0) != 1:
                    raise InputResourceAccessDenied("附件清理作业已被其他worker接管。")
                self.db.commit()
                resource = self.db.get(ManagedInputResource, str(item["resource_id"]))
                if (
                    resource is None
                    or resource.tenant_id != job.tenant_id
                    or resource.owner_user_id != job.owner_user_id
                    or resource.upload_binding_id != job.upload_binding_id
                    or resource.access_status != "revoked"
                ):
                    raise InputResourceAccessDenied("上传清理manifest与资源授权不一致。")
                if resource.destruction_status == "purged":
                    continue
                if resource.storage_locator != str(item["storage_locator"]):
                    raise InputResourceAccessDenied("上传清理manifest与资源授权不一致。")
                locator = str(item["storage_locator"])
                managed_unlink(self.storage_root, locator, missing_ok=True)
                post_unlink = self.db.exec(
                    update(AttachmentUploadCleanupJob)
                    .where(
                        AttachmentUploadCleanupJob.id == job.id,
                        AttachmentUploadCleanupJob.status == "purging",
                        AttachmentUploadCleanupJob.fencing_token == fencing_token,
                        AttachmentUploadCleanupJob.lease_owner == worker_id,
                        AttachmentUploadCleanupJob.lease_expires_at > utc_now(),
                    )
                    .values(updated_at=utc_now())
                )
                if getattr(post_unlink, "rowcount", 0) != 1:
                    self.db.rollback()
                    raise InputResourceAccessDenied("附件清理作业已被其他worker接管。")
                self.db.commit()
                job = self.db.get(AttachmentUploadCleanupJob, job.id)
                if (
                    job is None
                    or job.status != "purging"
                    or job.fencing_token != fencing_token
                    or job.lease_owner != worker_id
                    or job.lease_expires_at is None
                    or job.lease_expires_at <= utc_now()
                ):
                    raise InputResourceAccessDenied("附件清理作业已被其他worker接管。")
                resource = self.db.get(ManagedInputResource, str(item["resource_id"]))
                if (
                    resource is None
                    or resource.tenant_id != job.tenant_id
                    or resource.owner_user_id != job.owner_user_id
                    or resource.upload_binding_id != job.upload_binding_id
                    or resource.access_status != "revoked"
                ):
                    raise InputResourceAccessDenied("上传清理manifest与资源授权不一致。")
                if resource.destruction_status == "purged":
                    continue
                if resource.storage_locator != locator:
                    raise InputResourceAccessDenied("上传清理manifest与资源授权不一致。")
                resource.ingestion_status = "revoked"
                resource.destruction_status = "purged"
                resource.revoked_at = resource.revoked_at or utc_now()
                resource.updated_at = utc_now()
                _clear_purged_resource_identifiers(resource)
                self._record_purge_tombstone(
                    resource,
                    event_kind="upload_cleanup",
                    purge_job_id=job.id,
                    requested_by_user_id=job.owner_user_id,
                )
                self.db.add(resource)
                self.db.commit()
            job = self.db.get(AttachmentUploadCleanupJob, job.id)
            if (
                job is None
                or job.status != "purging"
                or job.fencing_token != fencing_token
                or job.lease_owner != worker_id
            ):
                raise InputResourceAccessDenied("附件清理作业已被其他worker接管。")
            job.status = "succeeded"
            job.lease_owner = None
            job.lease_expires_at = None
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            self.db.add(job)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            job = self.db.get(AttachmentUploadCleanupJob, job.id)
            if (
                job is not None
                and job.status == "purging"
                and job.fencing_token == fencing_token
                and job.lease_owner == worker_id
            ):
                job.status = "failed"
                job.lease_owner = None
                job.lease_expires_at = None
                job.error_code = "ATTACHMENT_UPLOAD_CLEANUP_FAILED"
                job.error_detail_json = {"detail": str(exc)[:500]}
                job.finished_at = utc_now()
                job.updated_at = utc_now()
                self.db.add(job)
                self.db.commit()
            raise

    def _locator(self, tenant_id: str, resource_id: str, checksum: str) -> Path:
        """使用 tenant 摘要和平台生成 id 构造不会接受用户路径片段的内容地址。"""

        tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        return self.storage_root / tenant_digest / resource_id / checksum

def _ingestion_status(parsed: ChatAttachmentRead, data: bytes) -> str:
    """执行首期确定性内容策略，恶意签名、伪造图片和主动 SVG 一律隔离。"""

    if _EICAR_MARKER in data.upper():
        return "quarantined"
    if parsed.error:
        return "failed"
    if parsed.kind == "binary":
        return "failed"
    if parsed.kind == "pdf":
        return "ready" if data.startswith(b"%PDF-") else "quarantined"
    if parsed.kind == "image":
        if parsed.content_type == "image/svg+xml":
            return "quarantined"
        if parsed.content_type == "image/webp":
            return "ready" if data.startswith(b"RIFF") and data[8:12] == b"WEBP" else "quarantined"
        signatures = {
            "image/png": (b"\x89PNG\r\n\x1a\n",),
            "image/jpeg": (b"\xff\xd8\xff",),
            "image/gif": (b"GIF87a", b"GIF89a"),
            "image/bmp": (b"BM",),
        }
        expected = signatures.get(parsed.content_type)
        if expected is None or not any(data.startswith(signature) for signature in expected):
            return "quarantined"
    return "ready"
