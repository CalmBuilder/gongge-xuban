"""
@Time       : 2026/08/04 18:52
@Author     : zhanglp8181
@File       : test_artifacts.py
@CallChain  : pytest → ArtifactService/API → DB + 隔离内容存储
@Description: 验证 Artifact 登记、lineage、ACL、预览下载、篡改和路径边界。
"""

import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.artifacts import download_artifact, get_artifact, list_artifacts, preview_artifact
from app.db.models import (
    AgentProfile,
    ArtifactInputLink,
    ExecutionArtifact,
    InputResourceSnapshot,
    ManagedInputResource,
    ResourceSessionBinding,
    SopInstance,
    SopNodeExecution,
    User,
)
from app.dynamic_tasks.artifacts import (
    ArtifactAccessDenied,
    ArtifactContractError,
    ArtifactService,
)
from app.session.managed_resources import ManagedInputResourceService


@pytest.fixture
def db() -> Session:
    """创建支持完整 Artifact schema 的隔离 SQLite 会话。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _facts(db: Session):
    """建立发起人、动态 Execution、结果节点和一份精确输入快照。"""

    owner = User(
        id="artifact_owner",
        tenant_id="tenant_artifact",
        username="owner",
        password_hash="x",
    )
    outsider = User(
        id="artifact_outsider",
        tenant_id="tenant_artifact",
        username="outsider",
        password_hash="x",
    )
    agent = AgentProfile(
        id="agent_artifact",
        tenant_id="tenant_artifact",
        name="产物员工",
        owner_user_id=owner.id,
        status="active",
    )
    instance = SopInstance(
        id="execution_artifact",
        tenant_id="tenant_artifact",
        session_id="session_artifact",
        kind="dynamic_task",
        active_slot_key="dynamic:artifact",
        initiator_user_id=owner.id,
        agent_id="agent_artifact",
        goal_snapshot_json={"goal": "生成简报"},
        current_plan_revision_id="plan_artifact",
        current_plan_checksum="e" * 64,
        capability_snapshot_json={"model": {"id": "model_artifact"}},
        capability_checksum="f" * 64,
        status="running",
    )
    node = SopNodeExecution(
        id="node_artifact",
        tenant_id=instance.tenant_id,
        instance_id=instance.id,
        node_id="answer",
        step_key="answer",
        step_kind="answer",
        status="running",
    )
    snapshot = InputResourceSnapshot(
        id="snapshot_artifact",
        tenant_id=instance.tenant_id,
        execution_id=instance.id,
        source_type="managed_upload",
        source_resource_id="resource_1",
        source_version="v1",
        filename="contract.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        content_checksum="a" * 64,
        extraction_checksum="b" * 64,
        ingestion_status="ready",
        identity_checksum="c" * 64,
        storage_locator_digest="d" * 64,
        captured_acl_json={"owner": owner.id},
    )
    resource = ManagedInputResource(
        id=snapshot.source_resource_id,
        tenant_id=instance.tenant_id,
        owner_user_id=owner.id,
        agent_id=instance.agent_id,
        version=snapshot.source_version,
        filename=snapshot.filename,
        mime_type=snapshot.mime_type,
        size_bytes=snapshot.size_bytes,
        content_checksum=snapshot.content_checksum,
        extraction_checksum=snapshot.extraction_checksum,
        ingestion_status="ready",
        storage_locator="managed/input",
    )
    db.add(owner)
    db.add(outsider)
    db.add(agent)
    db.add(instance)
    db.add(node)
    db.add(snapshot)
    db.add(resource)
    db.commit()
    return owner, outsider, instance, node, snapshot


def test_register_preview_download_and_exact_input_lineage(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证已登记 Markdown 可校验读取，且 lineage 指向精确输入 snapshot。"""

    owner, _, instance, node, snapshot = _facts(db)
    monkeypatch.setattr("app.dynamic_tasks.artifacts.paths.user_data_dir", lambda: tmp_path)
    service = ArtifactService(db)
    artifact, created = service.register(
        instance=instance,
        source_node=node,
        artifact_key="risk_brief",
        filename="续约风险简报.md",
        mime_type="text/markdown",
        data="# 风险简报\n\n证据已核验。".encode(),
        input_snapshot_ids=(snapshot.id,),
    )
    service.register(
        instance=instance,
        source_node=node,
        artifact_key="risk_appendix",
        filename="风险附录.md",
        mime_type="text/markdown",
        data="# 风险附录".encode(),
        input_snapshot_ids=(snapshot.id,),
    )
    db.commit()

    metadata = get_artifact(artifact.id, instance.tenant_id, owner, db)
    preview = preview_artifact(artifact.id, instance.tenant_id, owner, db)
    download = download_artifact(artifact.id, instance.tenant_id, owner, db)
    visible = list_artifacts(instance.id, instance.tenant_id, 0, 1, owner, db)
    links = db.exec(
        select(ArtifactInputLink).where(ArtifactInputLink.artifact_id == artifact.id)
    ).all()

    assert created is True
    assert metadata.input_snapshot_ids == [snapshot.id]
    assert metadata.content_checksum == artifact.content_checksum
    assert preview.content.startswith("# 风险简报")
    assert preview.truncated is False
    assert download.body == "# 风险简报\n\n证据已核验。".encode()
    assert download.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert len(visible) == 1
    assert [(item.artifact_id, item.input_snapshot_id) for item in links] == [
        (artifact.id, snapshot.id)
    ]


def test_artifact_idempotency_acl_tamper_and_path_escape_are_fail_closed(
    db: Session,
    tmp_path: Path,
) -> None:
    """验证同 key 不可漂移、同租户旁观者不可读、篡改及逃逸 locator 均拒绝。"""

    owner, outsider, instance, node, _ = _facts(db)
    service = ArtifactService(db, storage_root=tmp_path)
    artifact, _ = service.register(
        instance=instance,
        source_node=node,
        artifact_key="brief",
        filename="brief.md",
        mime_type="text/markdown",
        data=b"# original",
    )
    replay, created = service.register(
        instance=instance,
        source_node=node,
        artifact_key="brief",
        filename="brief.md",
        mime_type="text/markdown",
        data=b"# original",
    )
    assert replay.id == artifact.id and created is False
    with pytest.raises(ArtifactContractError, match="ARTIFACT_IDENTITY_CONFLICT"):
        service.register(
            instance=instance,
            source_node=node,
            artifact_key="brief",
            filename="brief.md",
            mime_type="text/markdown",
            data=b"# original",
            input_snapshot_ids=("snapshot_artifact",),
        )
    with pytest.raises(ArtifactContractError, match="ARTIFACT_IDENTITY_CONFLICT"):
        service.register(
            instance=instance,
            source_node=node,
            artifact_key="brief",
            filename="brief.md",
            mime_type="text/markdown",
            data=b"# rewritten",
        )
    with pytest.raises(HTTPException) as denied:
        get_artifact(artifact.id, instance.tenant_id, outsider, db)
    assert denied.value.status_code == 404
    other_tenant_user = User(
        id="artifact_other_tenant",
        tenant_id="tenant_other",
        username="other-tenant",
        password_hash="x",
    )
    db.add(other_tenant_user)
    db.commit()
    with pytest.raises(HTTPException) as cross_tenant:
        get_artifact(artifact.id, other_tenant_user.tenant_id, other_tenant_user, db)
    assert cross_tenant.value.status_code == 404

    locator = tmp_path / artifact.storage_locator
    locator.write_bytes(b"tampered")
    with pytest.raises(ArtifactAccessDenied, match="ARTIFACT_INTEGRITY_FAILED"):
        service.resolve(artifact.id, tenant_id=instance.tenant_id, actor_user_id=owner.id)
    artifact.storage_locator = "../outside.md"
    db.add(artifact)
    db.commit()
    with pytest.raises(ArtifactAccessDenied, match="ARTIFACT_NOT_FOUND"):
        service.resolve(artifact.id, tenant_id=instance.tenant_id, actor_user_id=owner.id)


def test_artifact_rejects_cross_execution_lineage_and_client_paths(
    db: Session,
    tmp_path: Path,
) -> None:
    """验证输出名称不能携带路径，lineage 也不能引用其他 Execution 的输入。"""

    _, _, instance, node, snapshot = _facts(db)
    service = ArtifactService(db, storage_root=tmp_path)
    snapshot.execution_id = "another_execution"
    db.add(snapshot)
    db.commit()
    with pytest.raises(ArtifactContractError, match="ARTIFACT_INPUT_SNAPSHOT_INVALID"):
        service.register(
            instance=instance,
            source_node=node,
            artifact_key="brief",
            filename="brief.md",
            mime_type="text/markdown",
            data=b"brief",
            input_snapshot_ids=(snapshot.id,),
        )


def test_artifact_download_fails_closed_after_input_is_revoked(
    db: Session,
    tmp_path: Path,
) -> None:
    """输入撤权后关联Artifact即使blob仍在也不得继续下载。"""

    owner, _, instance, node, snapshot = _facts(db)
    service = ArtifactService(db, storage_root=tmp_path)
    artifact, _ = service.register(
        instance=instance,
        source_node=node,
        artifact_key="revoked-input-report",
        filename="report.md",
        mime_type="text/markdown",
        data=b"# derived content",
        input_snapshot_ids=(snapshot.id,),
    )
    resource = db.get(ManagedInputResource, snapshot.source_resource_id)
    resource.access_status = "revoked"
    resource.revoked_at = snapshot.created_at
    resource.acl_revision += 1
    db.add(resource)
    db.commit()

    with pytest.raises(ArtifactAccessDenied, match="ARTIFACT_NOT_FOUND"):
        service.resolve(artifact.id, tenant_id=instance.tenant_id, actor_user_id=owner.id)
    with pytest.raises(ArtifactContractError, match="ARTIFACT_FILENAME_INVALID"):
        service.register(
            instance=instance,
            source_node=node,
            artifact_key="escape",
            filename="../brief.md",
            mime_type="text/markdown",
            data=b"brief",
        )


def test_session_purge_revokes_linked_artifact_before_source_tombstone(
    db: Session,
    tmp_path: Path,
) -> None:
    """会话输入销毁必须级联撤销派生Artifact，残留blob也不能再预览或下载。"""

    owner, _, instance, node, snapshot = _facts(db)
    artifact_service = ArtifactService(db, storage_root=tmp_path / "artifacts")
    artifact, _ = artifact_service.register(
        instance=instance,
        source_node=node,
        artifact_key="purged-source-report",
        filename="report.md",
        mime_type="text/markdown",
        data=b"# derived sensitive content",
        input_snapshot_ids=(snapshot.id,),
    )
    resource = db.get(ManagedInputResource, snapshot.source_resource_id)
    assert resource is not None
    db.add(
        ResourceSessionBinding(
            tenant_id=instance.tenant_id,
            resource_id=resource.id,
            resource_version=resource.version,
            owner_user_id=owner.id,
            session_id=instance.session_id,
            agent_id=instance.agent_id,
        )
    )
    instance.status = "succeeded"
    instance.active_slot_key = None
    db.add(instance)
    db.commit()
    input_root = tmp_path / "inputs"
    (input_root / "managed").mkdir(parents=True)
    (input_root / resource.storage_locator).write_bytes(b"source")

    ManagedInputResourceService(db, storage_root=input_root).purge_session_resource(
        resource,
        session_id=instance.session_id,
        actor_user_id=owner.id,
    )
    persisted_artifact = db.get(ExecutionArtifact, artifact.id)

    assert resource.destruction_status == "purged"
    assert persisted_artifact is not None and persisted_artifact.status == "revoked"
    with pytest.raises(ArtifactAccessDenied, match="ARTIFACT_NOT_FOUND"):
        artifact_service.resolve(
            artifact.id,
            tenant_id=instance.tenant_id,
            actor_user_id=owner.id,
        )


def test_unregistered_workspace_file_is_never_promoted_to_artifact(
    db: Session,
    tmp_path: Path,
) -> None:
    """验证目录扫描不是权威来源，未登记文件即使存在也不会进入 API 列表。"""

    owner, _, instance, _, _ = _facts(db)
    (tmp_path / "plausible-report.md").write_text("# 未登记报告", encoding="utf-8")

    visible = list_artifacts(instance.id, instance.tenant_id, 0, 100, owner, db)

    assert visible == []


def test_artifact_write_rejects_symlink_escape(
    db: Session,
    tmp_path: Path,
) -> None:
    """验证受管 tenant 目录被替换为 symlink 时，写入不会逃逸到外部目录。"""

    _, _, instance, node, _ = _facts(db)
    storage_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    storage_root.mkdir()
    outside.mkdir()
    tenant_digest = hashlib.sha256(instance.tenant_id.encode()).hexdigest()[:24]
    (storage_root / tenant_digest).symlink_to(outside, target_is_directory=True)
    service = ArtifactService(db, storage_root=storage_root)

    with pytest.raises(ArtifactContractError, match="ARTIFACT_STORAGE_ESCAPE"):
        service.register(
            instance=instance,
            source_node=node,
            artifact_key="escaped",
            filename="escaped.md",
            mime_type="text/markdown",
            data=b"must stay contained",
        )

    assert list(outside.iterdir()) == []


def test_artifact_read_rejects_blob_with_external_hardlink(
    db: Session,
    tmp_path: Path,
) -> None:
    """Artifact blob出现第二目录项时下载必须fail closed，不能把外部hardlink当受管副本。"""

    owner, _, instance, node, _ = _facts(db)
    service = ArtifactService(db, storage_root=tmp_path / "artifacts")
    artifact, _ = service.register(
        instance=instance,
        source_node=node,
        artifact_key="hardlink-report",
        filename="hardlink-report.md",
        mime_type="text/markdown",
        data=b"sensitive artifact",
    )
    outside_link = tmp_path / "outside-artifact-link"
    outside_link.hardlink_to((tmp_path / "artifacts") / artifact.storage_locator)

    with pytest.raises(ArtifactAccessDenied, match="ARTIFACT_NOT_FOUND"):
        service.resolve(
            artifact.id,
            tenant_id=instance.tenant_id,
            actor_user_id=owner.id,
        )

    assert outside_link.read_bytes() == b"sensitive artifact"
