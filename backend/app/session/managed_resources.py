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
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlmodel import Session, select

from app import paths
from app.agents.identity import agent_is_published, agent_owner_user_id
from app.db.models import (
    AgentProfile,
    AgentUsage,
    InputResourceSnapshot,
    ManagedInputResource,
    SopInstance,
    User,
    utc_now,
)
from app.session.attachments import parse_chat_attachment
from app.session.session_schema import ChatAttachmentRead


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
    if instance.kind != "dynamic_task" or resource.owner_user_id != instance.initiator_user_id:
        raise InputResourceAccessDenied("输入资源不可用。")
    if resource.agent_id is not None and resource.agent_id != instance.agent_id:
        raise InputResourceAccessDenied("输入资源不可用。")
    actor = db.exec(
        select(User).where(
            User.id == instance.initiator_user_id,
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


class ManagedInputResourceService:
    """以内容寻址文件和数据库身份管理动态任务可引用的聊天上传资源。"""

    def __init__(self, db: Session, *, storage_root: Path | None = None) -> None:
        """绑定事务和受管存储根；测试可注入隔离目录。"""

        self.db = db
        self.storage_root = storage_root or paths.user_data_dir() / "input-resources"

    def persist_upload(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        filename: str,
        content_type: str | None,
        data: bytes,
        agent_id: str | None = None,
    ) -> tuple[ManagedInputResource, ChatAttachmentRead]:
        """解析并原子落盘单个上传；不支持动态消费的二进制仍可供普通问答展示。"""

        if len(data) > MAX_MANAGED_INPUT_BYTES:
            raise ValueError("输入资源超过平台大小限制。")
        parsed = parse_chat_attachment(filename, content_type, data)
        checksum = hashlib.sha256(data).hexdigest()
        version = checksum
        extraction_text = parsed.text or ""
        extraction_checksum = hashlib.sha256(extraction_text.encode("utf-8")).hexdigest()
        status = _ingestion_status(parsed, data)
        locator = self._locator(tenant_id, parsed.id, checksum)
        self._write_blob(locator, data)
        resource = ManagedInputResource(
            id=parsed.id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            version=version,
            filename=parsed.filename,
            mime_type=parsed.content_type,
            size_bytes=len(data),
            content_checksum=checksum,
            extraction_checksum=extraction_checksum,
            ingestion_status=status,
            storage_locator=locator.relative_to(self.storage_root).as_posix(),
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
            locator.unlink(missing_ok=True)
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
        locator = self._safe_locator(resource.storage_locator)
        try:
            data = locator.read_bytes()
        except OSError as exc:
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
        ):
            raise InputResourceAccessDenied("输入资源不可用。")
        return resource, data

    def discard_uncommitted(self, resources: list[ManagedInputResource]) -> None:
        """在上传事务失败时清理尚未形成权威 DB 身份的内容文件。"""

        for resource in resources:
            try:
                self._safe_locator(resource.storage_locator).unlink(missing_ok=True)
            except InputResourceAccessDenied:
                continue

    def _locator(self, tenant_id: str, resource_id: str, checksum: str) -> Path:
        """使用 tenant 摘要和平台生成 id 构造不会接受用户路径片段的内容地址。"""

        tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        return self.storage_root / tenant_digest / resource_id / checksum

    def _safe_locator(self, relative_locator: str) -> Path:
        """解析数据库 locator 并拒绝逃离受管根目录的损坏记录。"""

        root = self.storage_root.resolve()
        candidate = (root / relative_locator).resolve()
        if candidate == root or root not in candidate.parents:
            raise InputResourceAccessDenied("输入资源不可用。")
        return candidate

    @staticmethod
    def _write_blob(locator: Path, data: bytes) -> None:
        """先 fsync 临时文件再原子替换，确保已提交资源不会指向半写文件。"""

        locator.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(locator.parent, 0o700)
        with NamedTemporaryFile(dir=locator.parent, prefix=".upload-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, locator)
            finally:
                temporary.unlink(missing_ok=True)


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
