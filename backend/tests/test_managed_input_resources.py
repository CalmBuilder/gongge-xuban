"""
@Time       : 2026/08/03 23:39
@Author     : zhanglp8181
@File       : test_managed_input_resources.py
@CallChain  : pytest → ManagedInputResourceService/SopExecutionStore → filesystem + SQLite
@Description: 验证附件持久身份、输入快照、实时撤权、防篡改及机械上下文投影。
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.datastructures import UploadFile

from app.api.chat import upload_chat_attachments
from app.db.models import AgentProfile, AgentUsage, ManagedInputResource, Message, Tenant, User
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.session.managed_resources import (
    InputResourceAccessDenied,
    ManagedInputResourceService,
)
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore


def _test_session() -> Session:
    """创建资源与统一 Execution 共用元数据的内存数据库。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _plan() -> NormalizedPlan:
    """构造会消费合同输入的最小计划。"""

    return NormalizedPlan(
        goal="读取合同并生成风险摘要",
        success_criteria=(
            SuccessCriterion(id="risk_summary", type="assertion", spec={"required": True}),
        ),
        steps=(PlanStep(step_key="read_contract", title="读取合同", kind="input.read"),),
    )


def _start(store: SopExecutionStore):
    """创建与上传者和 Agent 一致的动态 Execution。"""

    if store.db.get(User, "user_owner") is None:
        store.db.add(
            User(
                id="user_owner",
                tenant_id="tenant_demo",
                username="owner",
                password_hash="x",
            )
        )
    if store.db.get(AgentProfile, "agent_legal") is None:
        store.db.add(
            AgentProfile(
                id="agent_legal",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id="user_owner",
            )
        )
    store.db.flush()

    return store.start_dynamic_instance(
        tenant_id="tenant_demo",
        session_id="session_resource",
        agent_id="agent_legal",
        initiator_user_id="user_owner",
        plan=_plan(),
        capability_snapshot={"general_skills": []},
    )


def test_upload_persists_content_addressed_resource_and_keeps_locator_private(tmp_path) -> None:
    """验证上传返回兼容解析内容和资源引用，但不会把内部 locator 暴露给客户端。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, response = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data="合同正文".encode(),
        )
        db.commit()
        locator = tmp_path / resource.storage_locator

    assert locator.read_bytes() == "合同正文".encode()
    assert response.resource_id == resource.id
    assert response.resource_version == resource.version
    assert response.content_checksum == resource.content_checksum
    assert response.ingestion_status == "ready"
    assert "storage_locator" not in response.model_dump(mode="json")


def test_snapshot_rechecks_live_acl_and_projection_excludes_blob_and_acl_evidence(tmp_path) -> None:
    """验证 snapshot 只冻结身份证据，撤权后无法凭快照读取且模型投影不泄露 locator/ACL。"""

    with _test_session() as db:
        resource_service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, response = resource_service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data="合同正文".encode(),
        )
        store = SopExecutionStore(db)
        instance, _ = _start(store)
        message = Message(
            id="message-user-1",
            tenant_id="tenant_demo",
            session_id="session_resource",
            role="user",
            content="请分析合同",
            metadata_json={"attachments": [response.model_dump(mode="json")]},
        )
        db.add(message)
        db.flush()
        resolved, data = resource_service.resolve_for_execution(resource.id, instance=instance)
        assert data == "合同正文".encode()
        with store.owned(instance, worker_id="worker-resource"):
            snapshot, created = store.snapshot_input_resource(
                instance,
                resolved,
                source_message_id="message-user-1",
            )
            execution = store.enter_node(
                instance,
                "read_contract",
                step_key="read_contract",
                plan_revision_id=instance.current_plan_revision_id,
                step_kind="input.read",
            )
            store.complete_node(
                instance,
                execution,
                output={"audit_only_secret": "never-project-raw-output"},
            )
            execution_id = execution.id
        db.commit()
        projection = build_execution_context_projection(
            db,
            tenant_id="tenant_demo",
            execution_id=instance.id,
        )
        projection_json = projection.model_dump(mode="json")
        snapshot.source_version = "f" * 64
        db.flush()
        with pytest.raises(ValueError, match="快照已损坏"):
            build_execution_context_projection(
                db,
                tenant_id="tenant_demo",
                execution_id=instance.id,
            )
        db.rollback()
        db.refresh(resource)
        db.refresh(snapshot)
        resource_service.revoke(resource, actor_user_id="user_owner")
        db.commit()
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            resource_service.resolve_snapshot(snapshot, instance=instance)

    assert created is True
    assert projection.input_resources[0]["source_resource_id"] == resource.id
    assert "storage_locator" not in str(projection_json)
    assert "captured_acl" not in str(projection_json)
    assert "合同正文" not in str(projection_json)
    assert "never-project-raw-output" not in str(projection_json)
    assert projection.completed_steps[0]["output_ref"]["node_execution_id"] == execution_id


def test_snapshot_read_rejects_cross_tenant_agent_and_disk_tampering(tmp_path) -> None:
    """验证跨租户/错误 Agent 与内容被替换均在进入模型前返回统一不可用。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"trusted",
        )
        store = SopExecutionStore(db)
        instance, _ = _start(store)
        wrong_tenant, _ = store.start_dynamic_instance(
            tenant_id="tenant_other",
            session_id="session_other",
            agent_id="agent_legal",
            initiator_user_id="user_owner",
            plan=_plan(),
            capability_snapshot={"general_skills": []},
        )
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=wrong_tenant)
        instance.agent_id = "agent_other"
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)
        instance.agent_id = "agent_legal"
        locator = Path(tmp_path, resource.storage_locator)
        locator.write_bytes(b"tampered")
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)


def test_execution_read_rechecks_current_member_and_agent_access(tmp_path) -> None:
    """验证成员停用或 Agent 所有权撤回后，既有 Execution 不能继续读取输入。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"trusted",
        )
        instance, _ = _start(SopExecutionStore(db))
        service.resolve_for_execution(resource.id, instance=instance)
        actor = db.get(User, "user_owner")
        assert actor is not None
        actor.membership_status = "suspended"
        db.flush()
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)
        actor.membership_status = "active"
        agent = db.get(AgentProfile, "agent_legal")
        assert agent is not None
        agent.owner_user_id = "user_other"
        db.flush()
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)


def test_published_agent_usage_relation_is_rechecked_on_every_read(tmp_path) -> None:
    """验证非所有者通过已发布 Agent 使用关系读取，关系删除后既有任务立即失去访问。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"trusted",
        )
        instance, _ = _start(SopExecutionStore(db))
        agent = db.get(AgentProfile, "agent_legal")
        assert agent is not None
        agent.owner_user_id = "user_publisher"
        agent.published_to_gallery = True
        usage = AgentUsage(
            tenant_id="tenant_demo",
            user_id="user_owner",
            agent_id="agent_legal",
        )
        db.add(usage)
        db.flush()
        service.resolve_for_execution(resource.id, instance=instance)
        db.delete(usage)
        db.flush()
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)


def test_snapshot_creation_cannot_bypass_current_agent_access(tmp_path) -> None:
    """验证调用 Store 直接建快照也必须复核 Agent 当前访问关系，不能绕过 resolver。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, response = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"trusted",
        )
        store = SopExecutionStore(db)
        instance, _ = _start(store)
        db.add(
            Message(
                id="message-access-revoked",
                tenant_id="tenant_demo",
                session_id="session_resource",
                role="user",
                content="读取合同",
                metadata_json={"attachments": [response.model_dump(mode="json")]},
            )
        )
        agent = db.get(AgentProfile, "agent_legal")
        assert agent is not None
        agent.owner_user_id = "user_other"
        db.flush()
        with store.owned(instance, worker_id="worker-resource"):
            with pytest.raises(SopExecutionConflictError, match="输入资源不可用"):
                store.snapshot_input_resource(
                    instance,
                    resource,
                    source_message_id="message-access-revoked",
                )


def test_same_filename_uploads_have_immutable_distinct_versions(tmp_path) -> None:
    """验证同名文件重复上传不会覆盖旧内容，每个资源版本均有独立身份和摘要。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        first, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"version-one",
        )
        second, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"version-two",
        )
        instance, _ = _start(SopExecutionStore(db))
        _, first_data = service.resolve_for_execution(first.id, instance=instance)
        _, second_data = service.resolve_for_execution(second.id, instance=instance)

    assert first.id != second.id
    assert first.version != second.version
    assert first.content_checksum != second.content_checksum
    assert first_data == b"version-one"
    assert second_data == b"version-two"


def test_snapshot_rejects_client_forged_resource_reference_without_authoritative_message(
    tmp_path,
) -> None:
    """验证仅有客户端 resource id/checksum 不足以形成快照，必须由同会话用户消息引用。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="contract.txt",
            content_type="text/plain",
            data=b"trusted",
        )
        store = SopExecutionStore(db)
        instance, _ = _start(store)
        forged = Message(
            id="message-forged",
            tenant_id="tenant_demo",
            session_id="session_resource",
            role="user",
            content="伪造附件引用",
            metadata_json={
                "attachments": [
                    {
                        "resource_id": resource.id,
                        "resource_version": resource.version,
                        "content_checksum": "0" * 64,
                    }
                ]
            },
        )
        db.add(forged)
        db.flush()
        with store.owned(instance, worker_id="worker-resource"):
            with pytest.raises(ValueError, match="权威用户消息"):
                store.snapshot_input_resource(
                    instance,
                    resource,
                    source_message_id=forged.id,
                )


def test_unsupported_binary_is_retained_for_compatibility_but_cannot_be_snapshotted(tmp_path) -> None:
    """验证普通问答仍可展示二进制上传，而动态任务只能消费 ready 资源。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, response = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            filename="archive.bin",
            content_type="application/octet-stream",
            data=b"\x00\x01",
        )
        store = SopExecutionStore(db)
        instance, _ = _start(store)
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)
        with store.owned(instance, worker_id="worker-resource"):
            with pytest.raises(ValueError, match="ready"):
                store.snapshot_input_resource(instance, resource)

    assert response.kind == "binary"
    assert response.preview
    assert response.ingestion_status == "failed"


def test_chat_attachment_api_commits_server_resource_reference(tmp_path, monkeypatch) -> None:
    """验证真实上传入口提交受管资源，而不是只返回客户端可伪造的解析文本。"""

    monkeypatch.setattr(
        "app.session.managed_resources.paths.user_data_dir",
        lambda: tmp_path,
    )
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        db.add(user)
        db.commit()
        response = asyncio.run(
            upload_chat_attachments(
                tenant_id="tenant_demo",
                files=[
                    UploadFile(
                        filename="contract.txt",
                        file=BytesIO("合同正文".encode()),
                        headers={"content-type": "text/plain"},
                    )
                ],
                current_user=user,
                db=db,
            )
        )
        resource = db.get(ManagedInputResource, response[0].resource_id)

    assert resource is not None
    assert response[0].content_checksum == resource.content_checksum
    assert (tmp_path / "input-resources" / resource.storage_locator).is_file()


def test_chat_attachment_api_removes_blob_when_database_commit_fails(tmp_path, monkeypatch) -> None:
    """验证数据库提交失败不会留下可被误认作权威输入的孤立上传文件。"""

    monkeypatch.setattr(
        "app.session.managed_resources.paths.user_data_dir",
        lambda: tmp_path,
    )
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        db.add(user)
        db.commit()

        def fail_commit() -> None:
            """模拟文件已写完但数据库事务无法提交。"""

            raise RuntimeError("commit failed")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            asyncio.run(
                upload_chat_attachments(
                    tenant_id="tenant_demo",
                    files=[
                        UploadFile(
                            filename="contract.txt",
                            file=BytesIO(b"contract"),
                            headers={"content-type": "text/plain"},
                        )
                    ],
                    current_user=user,
                    db=db,
                )
            )
        rows = db.exec(select(ManagedInputResource)).all()

    files = [path for path in (tmp_path / "input-resources").rglob("*") if path.is_file()]
    assert rows == []
    assert files == []


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("invoice.txt", "text/plain", b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"),
        ("fake.png", "image/png", b"<html>not an image</html>"),
        ("active.svg", "image/svg+xml", b"<svg><script>alert(1)</script></svg>"),
    ],
)
def test_suspicious_or_active_content_is_quarantined_before_dynamic_use(
    tmp_path,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    """验证恶意测试签名、伪造图片和主动 SVG 在形成动态快照前隔离。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, response = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            filename=filename,
            content_type=content_type,
            data=data,
        )
        instance, _ = _start(SopExecutionStore(db))
        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)

    assert response.ingestion_status == "quarantined"
    assert resource.extraction_metadata_json["pipeline"][-1] == "quarantined"
