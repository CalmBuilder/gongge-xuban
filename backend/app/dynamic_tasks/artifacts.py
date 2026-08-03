"""
@Time       : 2026/08/04 18:25
@Author     : zhanglp8181
@File       : artifacts.py
@CallChain  : DynamicTaskAgent/Artifact API → ArtifactService → DB + content-addressed storage
@Description: 登记、授权并校验 Execution Artifact 及其精确输入快照血缘。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlmodel import Session, select

from app import paths
from app.db.models import (
    ArtifactInputLink,
    ExecutionArtifact,
    InputResourceSnapshot,
    SopInstance,
    SopNodeExecution,
    User,
)


MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
PREVIEWABLE_MIME_TYPES = {
    "text/markdown",
    "text/plain",
    "text/csv",
    "application/json",
}


class ArtifactAccessDenied(RuntimeError):
    """表示 Artifact 不存在、越权、损坏或不再可用。"""


class ArtifactContractError(ValueError):
    """表示 Artifact 身份、类型、大小或 lineage 不满足持久契约。"""


class ArtifactService:
    """以数据库登记为权威、内容地址为载体管理动态任务交付物。"""

    def __init__(self, db: Session, *, storage_root: Path | None = None) -> None:
        """绑定事务和受管输出根目录，测试可注入隔离路径。"""

        self.db = db
        self.storage_root = storage_root or paths.user_data_dir() / "execution-artifacts"

    def register(
        self,
        *,
        instance: SopInstance,
        source_node: SopNodeExecution,
        artifact_key: str,
        filename: str,
        mime_type: str,
        data: bytes,
        input_snapshot_ids: tuple[str, ...] = (),
    ) -> tuple[ExecutionArtifact, bool]:
        """校验来源、内容和输入快照后幂等登记 Artifact 与有方向 lineage。"""

        if instance.kind != "dynamic_task" or source_node.instance_id != instance.id:
            raise ArtifactContractError("ARTIFACT_SOURCE_INVALID")
        if source_node.status not in {"running", "succeeded"}:
            raise ArtifactContractError("ARTIFACT_SOURCE_NOT_ACTIVE")
        if not artifact_key.strip() or len(artifact_key) > 128:
            raise ArtifactContractError("ARTIFACT_KEY_INVALID")
        if not filename.strip() or len(filename) > 191 or "/" in filename or "\\" in filename:
            raise ArtifactContractError("ARTIFACT_FILENAME_INVALID")
        if mime_type not in PREVIEWABLE_MIME_TYPES and mime_type != "application/pdf":
            raise ArtifactContractError("ARTIFACT_MIME_UNSUPPORTED")
        if not data or len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactContractError("ARTIFACT_SIZE_INVALID")
        checksum = hashlib.sha256(data).hexdigest()
        snapshots = self._input_snapshots(instance, input_snapshot_ids)
        requested_snapshot_ids = [item.id for item in snapshots]
        existing = self.db.exec(
            select(ExecutionArtifact).where(
                ExecutionArtifact.tenant_id == instance.tenant_id,
                ExecutionArtifact.execution_id == instance.id,
                ExecutionArtifact.artifact_key == artifact_key,
            )
        ).first()
        if existing is not None:
            if (
                existing.content_checksum != checksum
                or existing.filename != filename
                or existing.mime_type != mime_type
                or existing.source_node_execution_id != source_node.id
                or sorted(item.input_snapshot_id for item in self.lineage(existing))
                != sorted(requested_snapshot_ids)
            ):
                raise ArtifactContractError("ARTIFACT_IDENTITY_CONFLICT")
            return existing, False
        artifact = ExecutionArtifact(
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            source_node_execution_id=source_node.id,
            source_step_key=source_node.step_key,
            artifact_key=artifact_key,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(data),
            content_checksum=checksum,
            storage_locator="pending",
            acl_json={"user_ids": [instance.initiator_user_id], "scope": "explicit_users"},
            lineage_json={
                "source_step_key": source_node.step_key,
                "input_snapshot_ids": [item.id for item in snapshots],
            },
        )
        locator = self._locator(instance.tenant_id, artifact.id, checksum)
        self._write_blob(locator, data)
        artifact.storage_locator = locator.relative_to(self.storage_root).as_posix()
        self.db.add(artifact)
        self.db.flush()
        for snapshot in snapshots:
            self.db.add(
                ArtifactInputLink(
                    tenant_id=instance.tenant_id,
                    execution_id=instance.id,
                    artifact_id=artifact.id,
                    input_snapshot_id=snapshot.id,
                )
            )
        self.db.flush()
        return artifact, True

    def resolve(
        self,
        artifact_id: str,
        *,
        tenant_id: str,
        actor_user_id: str,
    ) -> tuple[ExecutionArtifact, bytes]:
        """按 tenant、当前成员和显式 ACL 授权，并逐次核对磁盘大小与 checksum。"""

        artifact = self.authorize(
            artifact_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )
        locator = self._safe_locator(artifact.storage_locator)
        try:
            data = locator.read_bytes()
        except OSError as exc:
            raise ArtifactAccessDenied("ARTIFACT_NOT_FOUND") from exc
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.content_checksum:
            raise ArtifactAccessDenied("ARTIFACT_INTEGRITY_FAILED")
        return artifact, data

    def authorize(
        self,
        artifact_id: str,
        *,
        tenant_id: str,
        actor_user_id: str,
    ) -> ExecutionArtifact:
        """仅校验元数据状态、tenant、当前成员和显式 ACL，供有界列表使用。"""

        artifact = self.db.get(ExecutionArtifact, artifact_id)
        if artifact is None or artifact.tenant_id != tenant_id or artifact.status != "ready":
            raise ArtifactAccessDenied("ARTIFACT_NOT_FOUND")
        actor = self.db.exec(
            select(User).where(
                User.id == actor_user_id,
                User.tenant_id == tenant_id,
                User.membership_status == "active",
            )
        ).first()
        allowed = artifact.acl_json.get("user_ids") if isinstance(artifact.acl_json, dict) else []
        if actor is None or not isinstance(allowed, list) or actor.id not in allowed:
            raise ArtifactAccessDenied("ARTIFACT_NOT_FOUND")
        self._safe_locator(artifact.storage_locator)
        return artifact

    def lineage(self, artifact: ExecutionArtifact) -> list[ArtifactInputLink]:
        """返回同 tenant、同 Execution 的精确输入血缘边。"""

        return list(
            self.db.exec(
                select(ArtifactInputLink).where(
                    ArtifactInputLink.tenant_id == artifact.tenant_id,
                    ArtifactInputLink.execution_id == artifact.execution_id,
                    ArtifactInputLink.artifact_id == artifact.id,
                ).order_by(ArtifactInputLink.input_snapshot_id)
            ).all()
        )

    def _input_snapshots(
        self,
        instance: SopInstance,
        snapshot_ids: tuple[str, ...],
    ) -> list[InputResourceSnapshot]:
        """解析去重后的精确输入快照，并拒绝跨 Execution/tenant 伪造 lineage。"""

        snapshots: list[InputResourceSnapshot] = []
        for snapshot_id in dict.fromkeys(snapshot_ids):
            snapshot = self.db.get(InputResourceSnapshot, snapshot_id)
            if (
                snapshot is None
                or snapshot.tenant_id != instance.tenant_id
                or snapshot.execution_id != instance.id
            ):
                raise ArtifactContractError("ARTIFACT_INPUT_SNAPSHOT_INVALID")
            snapshots.append(snapshot)
        return snapshots

    def _locator(self, tenant_id: str, artifact_id: str, checksum: str) -> Path:
        """仅使用服务端身份和摘要生成内容地址，不接受客户端路径。"""

        tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        return self.storage_root / tenant_digest / artifact_id / checksum

    def _safe_locator(self, relative_locator: str) -> Path:
        """解析受管 locator 并拒绝数据库记录逃离 Artifact 根目录。"""

        root = self.storage_root.resolve()
        candidate = (root / relative_locator).resolve()
        if candidate == root or root not in candidate.parents:
            raise ArtifactAccessDenied("ARTIFACT_NOT_FOUND")
        return candidate

    def _write_blob(self, locator: Path, data: bytes) -> None:
        """拒绝受管根目录内 symlink，并以 0700/0600 权限原子写入内容。"""

        self.storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = self.storage_root.resolve()
        os.chmod(root, 0o700)
        relative = locator.relative_to(self.storage_root)
        parent = root
        for component in relative.parts[:-1]:
            parent = parent / component
            parent.mkdir(exist_ok=True, mode=0o700)
            if parent.is_symlink():
                raise ArtifactContractError("ARTIFACT_STORAGE_ESCAPE")
            os.chmod(parent, 0o700)
        target = parent / relative.name
        if target.is_symlink():
            raise ArtifactContractError("ARTIFACT_STORAGE_ESCAPE")
        with NamedTemporaryFile(dir=parent, prefix=".artifact-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
