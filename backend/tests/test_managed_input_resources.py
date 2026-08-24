"""
@Time       : 2026/08/03 23:39
@Author     : zhanglp8181
@File       : test_managed_input_resources.py
@CallChain  : pytest → ManagedInputResourceService/SopExecutionStore → filesystem + SQLite
@Description: 验证附件持久身份、输入快照、实时撤权、防篡改及机械上下文投影。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.datastructures import FormData, UploadFile

from app.api.chat import (
    chat_attachment_extraction,
    chat_attachment_status,
    discard_chat_attachment,
    upload_chat_attachments,
)
from app.config import get_settings
from app.db.models import (
    AgentProfile,
    AgentUsage,
    AttachmentUploadCleanupJob,
    AttachmentUploadDailyUsage,
    AttachmentUploadQuotaLease,
    AttachmentUploadQuotaReservation,
    DraftUploadBinding,
    InputDocumentElement,
    InputResourceExtraction,
    InputResourceExtractionAttempt,
    InputResourcePurgeJob,
    InputResourcePurgeTombstone,
    InputResourceSnapshot,
    ManagedInputResource,
    Message,
    MessageInputResourceLink,
    ResourceSessionBinding,
    SelectedResourceExtraction,
    SopInstance,
    Tenant,
    User,
    utc_now,
)
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.session.managed_resources import (
    InputResourceAccessDenied,
    ManagedInputResourceService,
)
from app.security.managed_storage import ManagedStorageError
from app.security.managed_storage import managed_write_bytes
from app.session.input_bindings import InputBindingService
from app.session.upload_quotas import AttachmentUploadQuotaError, AttachmentUploadQuotaService
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


class _MultipartRequest:
    """为直接调用上传路由的领域测试提供延迟解析multipart表单。"""

    def __init__(
        self,
        files: list[UploadFile],
        *,
        before_form: Callable[[], None] | None = None,
    ) -> None:
        """保存测试文件，直到路由取得配额后调用form才暴露。"""

        self.files = files
        self.before_form = before_form

    async def form(
        self,
        *,
        max_files: int,
        max_fields: int,
        max_part_size: int,
    ) -> FormData:
        """按真实multipart字段名返回可重复读取的UploadFile集合。"""

        assert max_files == 8
        assert max_fields == 4
        assert max_part_size == 64 * 1024
        if self.before_form is not None:
            self.before_form()
        return FormData([("files", item) for item in self.files])


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


def _link_ready_resource(
    db: Session,
    resource: ManagedInputResource,
    *,
    message_id: str,
) -> None:
    """为旧资源测试补齐新生产契约要求的MessageLink与不可变Extraction。"""

    extraction = InputResourceExtraction(
        id=f"extract-{resource.id}",
        tenant_id=resource.tenant_id,
        resource_id=resource.id,
        resource_version=resource.version,
        content_checksum=resource.content_checksum,
        parser_name="test-text",
        parser_version="1",
        parser_config_checksum="config-test",
        extraction_checksum=resource.extraction_checksum or "extract-test",
        element_manifest_checksum="manifest-test",
        published_from_attempt_id=f"attempt-{resource.id}",
        element_count=1,
    )
    binding = ResourceSessionBinding(
        tenant_id=resource.tenant_id,
        resource_id=resource.id,
        resource_version=resource.version,
        owner_user_id=resource.owner_user_id,
        session_id="session_resource",
        agent_id=resource.agent_id or "agent_legal",
    )
    db.add(extraction)
    db.add(
        InputDocumentElement(
            tenant_id=resource.tenant_id,
            extraction_id=extraction.id,
            element_index=0,
            element_type="paragraph",
            text=resource.extracted_text,
            locator_json={"kind": "test", "index": 0},
            content_checksum="element-test",
        )
    )
    db.add(
        SelectedResourceExtraction(
            tenant_id=resource.tenant_id,
            resource_id=resource.id,
            resource_version=resource.version,
            profile_key="default",
            extraction_id=extraction.id,
        )
    )
    db.add(binding)
    db.flush()
    db.add(
        MessageInputResourceLink(
            tenant_id=resource.tenant_id,
            session_id="session_resource",
            message_id=message_id,
            resource_binding_id=binding.id,
            resource_id=resource.id,
            resource_version=resource.version,
            content_checksum=resource.content_checksum,
        )
    )
    db.flush()


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


def test_discard_api_does_not_enumerate_same_tenant_foreign_resource(tmp_path) -> None:
    """同租户非owner与不存在资源必须返回相同404，不能用409泄露资源存在性。"""

    with _test_session() as db:
        owner = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        outsider = User(
            id="user_outsider",
            tenant_id="tenant_demo",
            username="outsider",
            password_hash="x",
        )
        db.add(owner)
        db.add(outsider)
        resource, _ = ManagedInputResourceService(db, storage_root=tmp_path).persist_upload(
            tenant_id="tenant_demo",
            owner_user_id=owner.id,
            agent_id="agent_legal",
            filename="private.txt",
            content_type="text/plain",
            data=b"private",
        )
        db.commit()

        statuses = []
        details = []
        for resource_id in (resource.id, "input_missing"):
            with pytest.raises(HTTPException) as exc_info:
                discard_chat_attachment(
                    resource_id=resource_id,
                    tenant_id="tenant_demo",
                    current_user=outsider,
                    db=db,
                )
            statuses.append(exc_info.value.status_code)
            details.append(exc_info.value.detail)

        assert statuses == [404, 404]
        assert details == ["附件不可用", "附件不可用"]
        assert db.get(ManagedInputResource, resource.id).access_status == "active"


def test_attachment_read_apis_do_not_enumerate_foreign_or_missing_resources(tmp_path) -> None:
    """状态与解析接口对同租户非owner、跨租户和不存在资源保持同一404契约。"""

    with _test_session() as db:
        owner = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        outsider = User(
            id="user_outsider",
            tenant_id="tenant_demo",
            username="outsider",
            password_hash="x",
        )
        db.add(owner)
        db.add(outsider)
        own_tenant_resource, _ = ManagedInputResourceService(
            db, storage_root=tmp_path
        ).persist_upload(
            tenant_id="tenant_demo",
            owner_user_id=owner.id,
            agent_id="agent_legal",
            filename="private.txt",
            content_type="text/plain",
            data=b"private",
        )
        foreign_tenant_resource, _ = ManagedInputResourceService(
            db, storage_root=tmp_path
        ).persist_upload(
            tenant_id="tenant_other",
            owner_user_id="foreign_owner",
            agent_id="agent_other",
            filename="foreign.txt",
            content_type="text/plain",
            data=b"foreign",
        )
        db.commit()

        for endpoint in (chat_attachment_status, chat_attachment_extraction):
            details = []
            for resource_id in (
                own_tenant_resource.id,
                foreign_tenant_resource.id,
                "input_missing",
            ):
                with pytest.raises(HTTPException) as exc_info:
                    endpoint(
                        resource_id=resource_id,
                        tenant_id="tenant_demo",
                        current_user=outsider,
                        db=db,
                    )
                assert exc_info.value.status_code == 404
                details.append(exc_info.value.detail)
            assert details == ["附件不可用", "附件不可用", "附件不可用"]


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
        _link_ready_resource(db, resource, message_id=message.id)
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

    monkeypatch.setenv("ATTACHMENT_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENT_PARSER_WORKER_ENABLED", "true")
    get_settings.cache_clear()
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
        db.add(
            AgentProfile(
                id="agent_legal",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id=user.id,
            )
        )
        db.commit()
        secret = InputBindingService(db).issue_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id="agent_legal",
            draft_conversation_id="draft:agent_legal",
            idempotency_key="upload-contract",
        )
        db.commit()
        observed_active_reservation = False

        def assert_reserved_before_form() -> None:
            """证明路由调用multipart解析前已提交数据库权威reservation。"""

            nonlocal observed_active_reservation
            reservation = db.exec(select(AttachmentUploadQuotaReservation)).one()
            observed_active_reservation = reservation.status == "active"

        response = asyncio.run(
            upload_chat_attachments(
                request=_MultipartRequest(
                    [
                        UploadFile(
                            filename="contract.txt",
                            file=BytesIO("合同正文".encode()),
                            headers={"content-type": "text/plain"},
                        ),
                        UploadFile(
                            filename="notes.md",
                            file=BytesIO(b"renewal notes"),
                            headers={"content-type": "text/markdown"},
                        ),
                    ],
                    before_form=assert_reserved_before_form,
                ),
                tenant_id="tenant_demo",
                upload_binding_id=secret.binding_id,
                upload_binding_nonce=secret.nonce,
                current_user=user,
                db=db,
            )
        )
        resource = db.get(ManagedInputResource, response[0].resource_id)
        reservation = db.exec(select(AttachmentUploadQuotaReservation)).one()
        usage = db.exec(select(AttachmentUploadDailyUsage)).all()
        leases = db.exec(select(AttachmentUploadQuotaLease)).all()
        attempts = db.exec(select(InputResourceExtractionAttempt)).all()

    assert resource is not None
    assert len(response) == 2
    assert observed_active_reservation is True
    assert response[0].content_checksum == resource.content_checksum
    assert all(item.ingestion_status == "extracting" for item in response)
    assert len(attempts) == 2
    assert all(item.status == "pending" for item in attempts)
    assert (tmp_path / "input-resources" / resource.storage_locator).is_file()
    assert reservation.status == "completed"
    assert reservation.actual_bytes == len("合同正文".encode()) + len(b"renewal notes")
    assert leases == []
    assert all(item.reserved_bytes == 0 for item in usage)
    assert all(item.consumed_bytes == reservation.actual_bytes for item in usage)


def test_chat_attachment_api_removes_blob_when_database_commit_fails(tmp_path, monkeypatch) -> None:
    """验证数据库提交失败不会留下可被误认作权威输入的孤立上传文件。"""

    monkeypatch.setenv("ATTACHMENT_ANALYSIS_ENABLED", "true")
    get_settings.cache_clear()
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
        db.add(
            AgentProfile(
                id="agent_legal",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id=user.id,
            )
        )
        db.commit()
        secret = InputBindingService(db).issue_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id="agent_legal",
            draft_conversation_id="draft:agent_legal",
            idempotency_key="upload-failure",
        )
        db.commit()

        def fail_commit() -> None:
            """模拟文件已写完但数据库事务无法提交。"""

            raise RuntimeError("commit failed")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            asyncio.run(
                upload_chat_attachments(
                    request=_MultipartRequest(
                        [
                            UploadFile(
                                filename="contract.txt",
                                file=BytesIO(b"contract"),
                                headers={"content-type": "text/plain"},
                            )
                        ]
                    ),
                    tenant_id="tenant_demo",
                    upload_binding_id=secret.binding_id,
                    upload_binding_nonce=secret.nonce,
                    current_user=user,
                    db=db,
                )
            )
        rows = db.exec(select(ManagedInputResource)).all()

    files = [path for path in (tmp_path / "input-resources").rglob("*") if path.is_file()]
    assert rows == []
    assert files == []


def test_chat_attachment_api_releases_quota_when_multipart_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    """multipart解析后格式校验失败也必须在finally释放两级slot和日字节预留。"""

    monkeypatch.setenv("ATTACHMENT_ANALYSIS_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        db.add(user)
        db.add(
            AgentProfile(
                id="agent_legal",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id=user.id,
            )
        )
        db.commit()
        secret = InputBindingService(db).issue_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id="agent_legal",
            draft_conversation_id="draft:agent_legal",
            idempotency_key="upload-rejected-format",
        )
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                upload_chat_attachments(
                    request=_MultipartRequest(
                        [
                            UploadFile(
                                filename="malware.exe",
                                file=BytesIO(b"MZ"),
                                headers={"content-type": "application/octet-stream"},
                            )
                        ]
                    ),
                    tenant_id="tenant_demo",
                    upload_binding_id=secret.binding_id,
                    upload_binding_nonce=secret.nonce,
                    current_user=user,
                    db=db,
                )
            )
        reservation = db.exec(select(AttachmentUploadQuotaReservation)).one()
        leases = db.exec(select(AttachmentUploadQuotaLease)).all()
        usage = db.exec(select(AttachmentUploadDailyUsage)).all()
        failed_binding = db.exec(select(DraftUploadBinding)).one()

    assert exc_info.value.status_code == 415
    assert reservation.status == "released"
    assert leases == []
    assert all(item.reserved_bytes == 0 for item in usage)
    assert all(item.consumed_bytes == 0 for item in usage)
    assert failed_binding.status == "expired"


@pytest.mark.parametrize(
    ("failure_mode", "expected_status"),
    (
        ("request_too_large", 413),
        ("parser_rejected", 422),
        ("disk_full", 507),
        ("client_disconnected", None),
    ),
)
def test_chat_attachment_api_releases_quota_for_all_request_failure_paths(
    tmp_path,
    monkeypatch,
    failure_mode: str,
    expected_status: int | None,
) -> None:
    """413、422、507及multipart断连都必须释放配额并保持资源终态一致。"""

    monkeypatch.setenv("ATTACHMENT_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENT_PARSER_WORKER_ENABLED", "false")
    if failure_mode == "request_too_large":
        monkeypatch.setenv("ATTACHMENT_MAX_FILE_BYTES", "2048")
        monkeypatch.setenv("ATTACHMENT_MAX_REQUEST_BYTES", "1024")
    get_settings.cache_clear()
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)

    if failure_mode == "parser_rejected":

        def reject_parser(*_args, **_kwargs):
            """模拟隔离解析器在文件已经接收后拒绝文档。"""

            raise RuntimeError("parser rejected")

        monkeypatch.setattr(
            "app.api.chat.run_attachment_parser_fd_isolated",
            reject_parser,
        )
    if failure_mode == "disk_full":

        def reject_managed_write(*_args, **_kwargs) -> None:
            """模拟受管存储在原子发布临时inode时耗尽空间。"""

            raise ManagedStorageError("MANAGED_STORAGE_WRITE_FAILED")

        monkeypatch.setattr(
            "app.session.managed_resources.managed_write_from_path",
            reject_managed_write,
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
        db.add(
            AgentProfile(
                id="agent_legal",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id=user.id,
            )
        )
        db.commit()
        secret = InputBindingService(db).issue_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id="agent_legal",
            draft_conversation_id="draft:agent_legal",
            idempotency_key=f"upload-{failure_mode}",
        )
        db.commit()

        def disconnect_before_form() -> None:
            """模拟客户端在服务端取得reservation后、multipart解析前断连。"""

            raise RuntimeError("client disconnected")

        request = _MultipartRequest(
            [
                UploadFile(
                    filename="contract.txt",
                    file=BytesIO(b"x" * 1025),
                    headers={"content-type": "text/plain"},
                )
            ],
            before_form=disconnect_before_form if failure_mode == "client_disconnected" else None,
        )
        expected_error = HTTPException if expected_status is not None else RuntimeError
        with pytest.raises(expected_error) as exc_info:
            asyncio.run(
                upload_chat_attachments(
                    request=request,
                    tenant_id="tenant_demo",
                    upload_binding_id=secret.binding_id,
                    upload_binding_nonce=secret.nonce,
                    current_user=user,
                    db=db,
                )
            )
        if expected_status is not None:
            assert isinstance(exc_info.value, HTTPException)
            assert exc_info.value.status_code == expected_status
        reservation = db.exec(select(AttachmentUploadQuotaReservation)).one()
        leases = db.exec(select(AttachmentUploadQuotaLease)).all()
        failed_binding = db.exec(select(DraftUploadBinding)).one()
        usage = db.exec(select(AttachmentUploadDailyUsage)).all()
        resources = db.exec(select(ManagedInputResource)).all()

    get_settings.cache_clear()
    assert reservation.status == "released"
    assert leases == []
    assert failed_binding.status == "expired"
    assert all(item.reserved_bytes == 0 for item in usage)
    assert all(item.consumed_bytes == 0 for item in usage)
    if failure_mode == "parser_rejected":
        assert len(resources) == 1
        assert resources[0].ingestion_status == "revoked"
        assert resources[0].access_status == "revoked"
    else:
        assert resources == []


@pytest.mark.parametrize("failure_stage", ("heartbeat", "success_settle"))
def test_quota_failure_purges_committed_async_resource_and_blob(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    """异步资源已commit后心跳或成功结算失败，都必须撤权物理清理且不能返回成功。"""

    monkeypatch.setenv("ATTACHMENT_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENT_PARSER_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)

    def fail_heartbeat(_heartbeat) -> None:
        """模拟multipart落盘和异步attempt提交后上传租约已丢失。"""

        raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_HEARTBEAT_FAILED")

    if failure_stage == "heartbeat":
        monkeypatch.setattr(
            "app.session.upload_quotas.AttachmentUploadQuotaHeartbeat.ensure_healthy",
            fail_heartbeat,
        )
    else:
        original_settle = AttachmentUploadQuotaService.settle

        def fail_success_settle(
            service,
            reservation,
            *,
            succeeded: bool,
            actual_bytes: int,
        ) -> None:
            """只拒绝成功结算，允许finally失败释放以验证最终事务不会伪成功。"""

            if succeeded:
                raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_RESERVATION_FENCED")
            original_settle(
                service,
                reservation,
                succeeded=succeeded,
                actual_bytes=actual_bytes,
            )

        monkeypatch.setattr(AttachmentUploadQuotaService, "settle", fail_success_settle)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        db.add(user)
        db.add(
            AgentProfile(
                id="agent_legal",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id=user.id,
            )
        )
        db.commit()
        secret = InputBindingService(db).issue_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id="agent_legal",
            draft_conversation_id="draft:agent_legal",
            idempotency_key=f"upload-{failure_stage}-failure",
        )
        db.commit()

        with pytest.raises(
            AttachmentUploadQuotaError,
            match="ATTACHMENT_UPLOAD_(HEARTBEAT_FAILED|RESERVATION_FENCED)",
        ):
            asyncio.run(
                upload_chat_attachments(
                    request=_MultipartRequest(
                        [
                            UploadFile(
                                filename="contract.txt",
                                file=BytesIO(b"committed before heartbeat failure"),
                                headers={"content-type": "text/plain"},
                            )
                        ]
                    ),
                    tenant_id="tenant_demo",
                    upload_binding_id=secret.binding_id,
                    upload_binding_nonce=secret.nonce,
                    current_user=user,
                    db=db,
                )
            )
        resource = db.exec(select(ManagedInputResource)).one()
        reservation = db.exec(select(AttachmentUploadQuotaReservation)).one()
        leases = db.exec(select(AttachmentUploadQuotaLease)).all()
        failed_binding = db.exec(select(DraftUploadBinding)).one()

    get_settings.cache_clear()
    assert resource.access_status == "revoked"
    assert resource.destruction_status == "purged"
    assert resource.ingestion_status == "revoked"
    assert not (tmp_path / "input-resources" / resource.storage_locator).exists()
    assert reservation.status == "released"
    assert leases == []
    assert failed_binding.status == "expired"


def test_unreferenced_composer_attachment_is_physically_discarded(tmp_path) -> None:
    """Composer移除未发送附件时立刻撤权并删除在线blob，不留下可再次读取的孤儿。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            filename="draft.csv",
            content_type="text/csv",
            data=b"name,value\na,1",
        )
        locator = tmp_path / resource.storage_locator
        assert locator.is_file()

        service.discard_unreferenced(resource, actor_user_id="user_owner")
        db.commit()
        access_status = resource.access_status
        destruction_status = resource.destruction_status

    assert access_status == "revoked"
    assert destruction_status == "purged"
    assert not locator.exists()


def _scheduled_upload_cleanup(db: Session, tmp_path: Path, *, count: int):
    """建立指定数量已落盘资源并持久化fail-closed上传清理作业。"""

    service = ManagedInputResourceService(db, storage_root=tmp_path)
    resources = []
    for index in range(count):
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            filename=f"failed-{index}.txt",
            content_type="text/plain",
            data=f"payload-{index}".encode(),
            upload_binding_id="binding-cleanup",
        )
        resources.append(resource)
    db.commit()
    job = service.schedule_upload_failure_cleanup(
        resources,
        tenant_id="tenant_demo",
        owner_user_id="user_owner",
        upload_binding_id="binding-cleanup",
    )
    db.commit()
    return service, job.id, [resource.id for resource in resources]


def test_upload_cleanup_failure_keeps_persistent_job_and_fail_closed_resource(
    tmp_path,
    monkeypatch,
) -> None:
    """物理删除失败时作业进入failed，资源已撤权且blob保留供恢复而非重新激活。"""

    with _test_session() as db:
        service, job_id, resource_ids = _scheduled_upload_cleanup(db, tmp_path, count=1)

        def fail_unlink(*_args, **_kwargs) -> None:
            """模拟对象存储或文件系统删除失败。"""

            raise RuntimeError("storage unavailable")

        monkeypatch.setattr("app.session.managed_resources.managed_unlink", fail_unlink)
        with pytest.raises(RuntimeError, match="storage unavailable"):
            service.run_upload_failure_cleanup(job_id, worker_id="cleanup-1")
        job = db.get(AttachmentUploadCleanupJob, job_id)
        resource = db.get(ManagedInputResource, resource_ids[0])

    assert job is not None and job.status == "failed"
    assert resource is not None and resource.access_status == "revoked"
    assert resource.destruction_status == "retained"
    assert (tmp_path / resource.storage_locator).is_file()


def test_upload_cleanup_partial_delete_preserves_consistent_per_resource_tombstones(
    tmp_path,
    monkeypatch,
) -> None:
    """多资源第二次删除失败时，首资源已purged，第二资源仍是撤权retained且作业可重试。"""

    with _test_session() as db:
        service, job_id, resource_ids = _scheduled_upload_cleanup(db, tmp_path, count=2)
        original_unlink = __import__(
            "app.session.managed_resources",
            fromlist=["managed_unlink"],
        ).managed_unlink
        calls = 0

        def fail_second_unlink(*args, **kwargs) -> None:
            """先真实删除第一个blob，再让第二个删除失败。"""

            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second delete failed")
            original_unlink(*args, **kwargs)

        monkeypatch.setattr("app.session.managed_resources.managed_unlink", fail_second_unlink)
        with pytest.raises(RuntimeError, match="second delete failed"):
            service.run_upload_failure_cleanup(job_id, worker_id="cleanup-partial")
        first = db.get(ManagedInputResource, resource_ids[0])
        second = db.get(ManagedInputResource, resource_ids[1])
        job = db.get(AttachmentUploadCleanupJob, job_id)

    assert first is not None and first.destruction_status == "purged"
    assert second is not None and second.destruction_status == "retained"
    assert first.access_status == second.access_status == "revoked"
    assert not (tmp_path / first.storage_locator).exists()
    assert (tmp_path / second.storage_locator).is_file()
    assert job is not None and job.status == "failed"


def test_upload_cleanup_worker_recovers_partial_failure_idempotently(
    tmp_path,
    monkeypatch,
) -> None:
    """failed作业由新worker接管时重复删除缺失blob并完成剩余资源，最终全部purged。"""

    with _test_session() as db:
        service, job_id, resource_ids = _scheduled_upload_cleanup(db, tmp_path, count=2)
        original_unlink = __import__(
            "app.session.managed_resources",
            fromlist=["managed_unlink"],
        ).managed_unlink
        calls = 0

        def fail_once(*args, **kwargs) -> None:
            """首次运行删除一个blob后失败，后续恢复运行全部放行。"""

            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("transient delete failure")
            original_unlink(*args, **kwargs)

        monkeypatch.setattr("app.session.managed_resources.managed_unlink", fail_once)
        with pytest.raises(RuntimeError, match="transient delete failure"):
            service.run_upload_failure_cleanup(job_id, worker_id="cleanup-first")
        monkeypatch.setattr("app.session.managed_resources.managed_unlink", original_unlink)
        service.run_upload_failure_cleanup(job_id, worker_id="cleanup-recovery")
        job = db.get(AttachmentUploadCleanupJob, job_id)
        resources = [db.get(ManagedInputResource, resource_id) for resource_id in resource_ids]

    assert job is not None and job.status == "succeeded"
    assert all(resource is not None for resource in resources)
    assert all(resource.destruction_status == "purged" for resource in resources if resource)
    assert all(not (tmp_path / resource.storage_locator).exists() for resource in resources if resource)


def test_upload_cleanup_rejects_cross_tenant_manifest_without_unlink(tmp_path) -> None:
    """篡改作业指向其他tenant资源时必须零删除并把作业收敛为failed。"""

    with _test_session() as db:
        service, job_id, _ = _scheduled_upload_cleanup(db, tmp_path, count=1)
        foreign, _ = service.persist_upload(
            tenant_id="tenant-foreign",
            owner_user_id="user-foreign",
            filename="foreign.txt",
            content_type="text/plain",
            data=b"foreign payload",
            upload_binding_id="binding-foreign",
        )
        db.commit()
        foreign_locator = tmp_path / foreign.storage_locator
        job = db.get(AttachmentUploadCleanupJob, job_id)
        assert job is not None
        job.resource_manifest_json = [
            {"resource_id": foreign.id, "storage_locator": foreign.storage_locator}
        ]
        db.add(job)
        db.commit()

        with pytest.raises(InputResourceAccessDenied, match="manifest"):
            service.run_upload_failure_cleanup(job_id, worker_id="cleanup-attacker")
        job = db.get(AttachmentUploadCleanupJob, job_id)

    assert job is not None and job.status == "failed"
    assert foreign_locator.is_file()


def test_upload_cleanup_rejects_locator_drift_without_unlink(tmp_path) -> None:
    """manifest locator与资源权威locator漂移时不得尝试任一物理删除。"""

    with _test_session() as db:
        service, job_id, resource_ids = _scheduled_upload_cleanup(db, tmp_path, count=1)
        resource = db.get(ManagedInputResource, resource_ids[0])
        job = db.get(AttachmentUploadCleanupJob, job_id)
        assert resource is not None and job is not None
        locator = tmp_path / resource.storage_locator
        job.resource_manifest_json = [
            {"resource_id": resource.id, "storage_locator": "foreign/drifted/blob"}
        ]
        db.add(job)
        db.commit()

        with pytest.raises(InputResourceAccessDenied, match="manifest"):
            service.run_upload_failure_cleanup(job_id, worker_id="cleanup-drift")
        job = db.get(AttachmentUploadCleanupJob, job_id)

    assert job is not None and job.status == "failed"
    assert locator.is_file()


def test_upload_cleanup_does_not_publish_purged_after_fencing_changes_during_unlink(
    tmp_path,
    monkeypatch,
) -> None:
    """unlink期间作业被新owner fence后，旧worker不能把资源墓碑写成purged。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cleanup-fencing.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        service, job_id, resource_ids = _scheduled_upload_cleanup(db, tmp_path, count=1)
        resource = db.get(ManagedInputResource, resource_ids[0])
        assert resource is not None
        locator = tmp_path / resource.storage_locator
        original_unlink = __import__(
            "app.session.managed_resources",
            fromlist=["managed_unlink"],
        ).managed_unlink

        def fence_while_unlinking(*args, **kwargs) -> None:
            """模拟新worker在旧worker完成unlink前取得更高fencing token。"""

            with Session(engine) as competing_db:
                competing_db.exec(
                    update(AttachmentUploadCleanupJob)
                    .where(AttachmentUploadCleanupJob.id == job_id)
                    .values(
                        fencing_token=AttachmentUploadCleanupJob.fencing_token + 1,
                        lease_owner="cleanup-new-owner",
                        lease_expires_at=utc_now() + timedelta(seconds=60),
                    )
                )
                competing_db.commit()
            original_unlink(*args, **kwargs)

        monkeypatch.setattr(
            "app.session.managed_resources.managed_unlink",
            fence_while_unlinking,
        )
        with pytest.raises(InputResourceAccessDenied, match="其他worker"):
            service.run_upload_failure_cleanup(job_id, worker_id="cleanup-old-owner")
        db.expire_all()
        resource = db.get(ManagedInputResource, resource_ids[0])
        job = db.get(AttachmentUploadCleanupJob, job_id)

    assert not locator.exists()
    assert resource is not None and resource.destruction_status == "retained"
    assert resource.access_status == "revoked"
    assert job is not None and job.lease_owner == "cleanup-new-owner"
    assert job.fencing_token == 2


def test_sent_attachment_cannot_be_discarded_as_composer_draft(tmp_path) -> None:
    """已形成MessageLink的资源拒绝草稿删除，必须进入显式撤权和purge状态机。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="sent.txt",
            content_type="text/plain",
            data=b"sent",
        )
        message = Message(
            id="message-sent",
            tenant_id="tenant_demo",
            session_id="session_resource",
            role="user",
            content="已发送",
        )
        db.add(message)
        db.flush()
        _link_ready_resource(db, resource, message_id=message.id)

        with pytest.raises(InputResourceAccessDenied, match="不能直接丢弃"):
            service.discard_unreferenced(resource, actor_user_id="user_owner")

    assert (tmp_path / resource.storage_locator).exists()


def test_session_purge_removes_elements_and_keeps_only_resource_tombstone(tmp_path) -> None:
    """删除专属会话输入时清除Extraction/Element/Link/blob，墓碑不得继续携带正文。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="sent.txt",
            content_type="text/plain",
            data=b"sensitive sent content",
        )
        message = Message(
            id="message-purge",
            tenant_id="tenant_demo",
            session_id="session_resource",
            role="user",
            content="已发送",
        )
        db.add(message)
        db.flush()
        _link_ready_resource(db, resource, message_id=message.id)
        locator = tmp_path / resource.storage_locator

        service.purge_session_resource(
            resource,
            session_id="session_resource",
            actor_user_id="user_owner",
        )
        db.commit()

        assert resource.access_status == "revoked"
        assert resource.destruction_status == "purged"
        assert resource.filename == "[purged]"
        assert resource.content_checksum == "[purged]"
        assert resource.storage_locator == f"purged/{resource.id}"
        assert resource.size_bytes == 0
        assert resource.extracted_text is None
        assert resource.extraction_checksum is None
        assert not locator.exists()
        assert db.exec(select(InputResourceExtraction)).all() == []
        assert db.exec(select(InputDocumentElement)).all() == []
        assert db.exec(select(SelectedResourceExtraction)).all() == []
        assert db.exec(select(MessageInputResourceLink)).all() == []
        assert db.exec(select(ResourceSessionBinding)).all() == []
        purge_job = db.exec(select(InputResourcePurgeJob)).one()
        assert purge_job.status == "succeeded"
        assert purge_job.fencing_token == 1
        assert purge_job.lease_owner is None
        tombstone = db.exec(select(InputResourcePurgeTombstone)).one()
        assert tombstone.tenant_id == "tenant_demo"
        assert tombstone.resource_id == resource.id
        assert tombstone.resource_version == resource.version
        assert tombstone.event_kind == "session_purge"
        assert not hasattr(tombstone, "filename")
        assert not hasattr(tombstone, "storage_locator")


def test_backup_restore_replay_cannot_resurrect_purged_resource_or_blob(tmp_path) -> None:
    """备份恢复把旧资源行和blob带回后，墓碑重放仍必须再次撤权并删除在线副本。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="backup.txt",
            content_type="text/plain",
            data=b"backup secret",
        )
        message = Message(
            id="message-backup-replay",
            tenant_id="tenant_demo",
            session_id="session_resource",
            role="user",
            content="备份恢复",
        )
        db.add(message)
        db.flush()
        _link_ready_resource(db, resource, message_id=message.id)
        old_locator = resource.storage_locator
        old_version = resource.version

        service.purge_session_resource(
            resource,
            session_id="session_resource",
            actor_user_id="user_owner",
        )
        db.commit()

        # 模拟不可信备份恢复：仅恢复旧资源身份和blob，不恢复墓碑表。
        resource.filename = "backup.txt"
        resource.mime_type = "text/plain"
        resource.size_bytes = len(b"backup secret")
        resource.content_checksum = "backup-checksum"
        resource.storage_locator = old_locator
        resource.access_status = "active"
        resource.destruction_status = "retained"
        resource.ingestion_status = "ready"
        resource.extracted_text = "backup secret"
        resource.extraction_checksum = "restored-extraction"
        resource.revoked_at = None
        db.add(resource)
        db.commit()
        managed_write_bytes(tmp_path, old_locator, b"backup secret")
        assert (tmp_path / old_locator).exists()

        assert service.replay_purge_tombstones(tenant_id="tenant_demo") == 1
        db.commit()
        restored = db.get(ManagedInputResource, resource.id)
        tombstone = db.exec(select(InputResourcePurgeTombstone)).one()

    assert restored is not None
    assert restored.version == old_version
    assert restored.access_status == "revoked"
    assert restored.destruction_status == "purged"
    assert restored.filename == "[purged]"
    assert restored.storage_locator == f"purged/{restored.id}"
    assert restored.extracted_text is None
    assert not (tmp_path / old_locator).exists()
    assert tombstone.event_kind == "session_purge"


def test_session_purge_ignores_snapshot_from_terminal_execution(tmp_path) -> None:
    """验证成功Execution的历史快照不永久阻止用户删除会话附件。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="terminal.txt",
            content_type="text/plain",
            data=b"terminal execution content",
        )
        message = Message(
            id="message-terminal-purge",
            tenant_id="tenant_demo",
            session_id="session_resource",
            role="user",
            content="已发送",
        )
        instance = SopInstance(
            id="execution-terminal-purge",
            tenant_id="tenant_demo",
            session_id="session_resource",
                kind="sop",
                skill_id="skill_terminal_purge",
                skill_version_id="skillver_terminal_purge",
                skill_version="1.0.0",
                definition_checksum="d" * 64,
            status="succeeded",
            initiator_user_id="user_owner",
            agent_id="agent_legal",
        )
        db.add_all([message, instance])
        db.flush()
        _link_ready_resource(db, resource, message_id=message.id)
        db.add(
            InputResourceSnapshot(
                tenant_id="tenant_demo",
                execution_id=instance.id,
                source_type="managed_input",
                source_resource_id=resource.id,
                source_version=resource.version,
                source_message_id=message.id,
                filename=resource.filename,
                mime_type=resource.mime_type,
                size_bytes=resource.size_bytes,
                content_checksum=resource.content_checksum,
                extraction_checksum=resource.extraction_checksum,
                ingestion_status="ready",
                identity_checksum="f" * 64,
                storage_locator_digest="e" * 64,
                captured_acl_json={},
            )
        )
        db.flush()

        service.purge_session_resource(
            resource,
            session_id="session_resource",
            actor_user_id="user_owner",
        )

        assert resource.destruction_status == "purged"


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


def test_managed_input_read_rejects_blob_with_external_hardlink(tmp_path: Path) -> None:
    """输入blob出现第二目录项时实时读取必须拒绝，避免受管内容被外部路径持续引用。"""

    with _test_session() as db:
        service = ManagedInputResourceService(db, storage_root=tmp_path / "inputs")
        resource, _ = service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_owner",
            agent_id="agent_legal",
            filename="hardlink.txt",
            content_type="text/plain",
            data=b"sensitive managed input",
        )
        instance, _ = _start(SopExecutionStore(db))
        outside_link = tmp_path / "outside-input-link"
        outside_link.hardlink_to((tmp_path / "inputs") / resource.storage_locator)

        with pytest.raises(InputResourceAccessDenied, match="不可用"):
            service.resolve_for_execution(resource.id, instance=instance)

    assert outside_link.read_bytes() == b"sensitive managed input"
