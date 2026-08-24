"""
@Time       : 2026/08/13 20:38
@Author     : zhanglp8181
@File       : test_provider_input_dispatch.py
@CallChain  : pytest → ProviderInputDispatchGateway → ACL CAS/Receipt/Group
@Description: 正反向验证附件模型外发的授权线性化、unknown和撤权零新dispatch契约。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    InputResourceExtraction,
    InputResourceExtractionAttempt,
    InputResourceSnapshot,
    ManagedInputResource,
    ModelConfig,
    ProviderInputDispatchGroup,
    ProviderInputDispatchReceipt,
    ScannerEvidence,
    SopInstance,
    SopOperation,
    TurnInputReadReceipt,
    TurnInputSnapshot,
    utc_now,
)
from app.session.input_bindings import InputBindingError
from app.session.provider_input_dispatch import ProviderInputDispatchGateway
from app.dynamic_tasks.capability_catalog import capability_checksum


def _session() -> Session:
    """创建隔离SQLite外发事实库。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> tuple[ManagedInputResource, TurnInputReadReceipt]:
    """建立已完成本地读取但尚未对模型披露的附件切片。"""

    resource = ManagedInputResource(
        id="input-1",
        tenant_id="tenant-a",
        owner_user_id="user-a",
        version="resource-v1",
        filename="sales.csv",
        mime_type="text/csv",
        size_bytes=10,
        content_checksum="resource-v1",
        ingestion_status="ready",
        security_status="format_verified",
        storage_locator="tenant-a/blob",
    )
    snapshot = TurnInputSnapshot(
        id="snapshot-1",
        tenant_id="tenant-a",
        turn_id="turn-1",
        session_id="session-1",
        message_resource_link_id="link-1",
        resource_id=resource.id,
        resource_version=resource.version,
        extraction_id="extract-1",
        extraction_checksum="extract-v1",
        element_manifest_checksum="manifest-v1",
        resource_acl_revision_at_snapshot=0,
        opaque_handle="opaque-handle",
    )
    read = TurnInputReadReceipt(
        id="read-1",
        tenant_id="tenant-a",
        turn_id="turn-1",
        snapshot_id=snapshot.id,
        slice_checksum="slice-v1",
        receipt_checksum="receipt-v1",
        status="succeeded",
    )
    attempt = InputResourceExtractionAttempt(
        id="extract-attempt-1",
        tenant_id="tenant-a",
        resource_id=resource.id,
        resource_version=resource.version,
        parser_name="builtin-csv",
        parser_version="1.0.0",
        parser_config_checksum="parser-v1",
        status="succeeded",
    )
    extraction = InputResourceExtraction(
        id="extract-1",
        tenant_id="tenant-a",
        resource_id=resource.id,
        resource_version=resource.version,
        content_checksum=resource.content_checksum,
        parser_name="builtin-csv",
        parser_version="1.0.0",
        parser_config_checksum="parser-v1",
        extraction_checksum="extract-v1",
        element_manifest_checksum="manifest-v1",
        published_from_attempt_id=attempt.id,
    )
    scanned_at = utc_now()
    scanner = ScannerEvidence(
        tenant_id="tenant-a",
        resource_id=resource.id,
        resource_version=resource.version,
        assurance_level="format_verified",
        engine="builtin-format-verifier",
        engine_version="1.0.0",
        definition_version="1.0.0",
        definition_published_at=scanned_at,
        scanned_at=scanned_at,
        freshness_policy_checksum="scan-policy-v1",
        max_age_at_scan_seconds=0,
        verdict="accepted",
    )
    db.add(resource)
    db.add(snapshot)
    db.add(read)
    db.add(attempt)
    db.add(extraction)
    db.add(scanner)
    db.commit()
    return resource, read


def _seed_dynamic_read(
    db: Session,
    *,
    execution_id: str,
    snapshot_id: str,
    elements: list[dict[str, object]] | None = None,
) -> str:
    """保存Dynamic真实input.read事实并返回由元素正文机械计算的切片摘要。"""

    persisted_elements = elements or [{"element_id": "element-1", "text": "sales"}]
    read_checksum = capability_checksum(persisted_elements)
    disclosure_checksum = capability_checksum(
        {
            "snapshot_id": snapshot_id,
            "element_ids": [item.get("element_id") for item in persisted_elements],
            "element_checksums": [item.get("content_checksum") for item in persisted_elements],
            "native_content_checksum": None,
        }
    )
    db.add(
        SopOperation(
            tenant_id="tenant-a",
            instance_id=execution_id,
            node_execution_id=f"node-{snapshot_id}",
            operation_name="input.read",
            idempotency_key=f"read-{snapshot_id}",
            logical_action_id=f"read-action-{snapshot_id}",
            request_fingerprint="c" * 64,
            effect_kind="read",
            status="succeeded",
            result_json={
                "data": {
                    "snapshot_id": snapshot_id,
                    "elements": persisted_elements,
                    "slice_checksum": read_checksum,
                }
            },
        )
    )
    db.flush()
    return disclosure_checksum


def test_delivered_dispatch_has_unique_group_and_settled_receipt() -> None:
    """正向模型动作仅创建一个Group，成功后逐资源Receipt确定性settled。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        gateway.authorize(group, worker_id="worker-1")
        db.commit()
        gateway.settle_delivered(group)
        db.commit()
        group_status = group.status
        receipts = db.exec(select(ProviderInputDispatchReceipt)).all()

    assert group_status == "settled"
    assert len(receipts) == 1
    assert receipts[0].status == "settled"
    assert receipts[0].dispatch_token


def test_revoke_before_authorize_creates_zero_dispatching_receipts() -> None:
    """撤权先提交时授权CAS fail closed，模型外呼调用方不得开始。"""

    with _session() as db:
        resource, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        resource.access_status = "revoked"
        resource.acl_revision += 1
        db.add(resource)
        db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")
        receipts = db.exec(select(ProviderInputDispatchReceipt)).all()

    assert exc_info.value.code == "ATTACHMENT_COUNTERMANDED"
    assert [item.status for item in receipts] == ["prepared"]


def test_network_failure_after_authorization_is_unknown_and_not_reused() -> None:
    """授权后崩溃按可能披露收敛unknown，同attempt不能重新authorize或伪装未发送。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        gateway.authorize(group, worker_id="worker-1")
        db.commit()
        gateway.mark_unknown(group)
        db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-2")
        receipt = db.exec(select(ProviderInputDispatchReceipt)).one()

    assert exc_info.value.code == "ATTACHMENT_DISPATCH_FENCED"
    assert receipt.status == "unknown"
    assert receipt.deadline_at > utc_now() - timedelta(hours=1)


def test_authorize_rejects_extraction_identity_drift() -> None:
    """Receipt引用的Extraction不是当前资源冻结版本时必须在网络调用前拒绝。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        receipt = db.exec(select(ProviderInputDispatchReceipt)).one()
        receipt.extraction_id = "extract-forged"
        db.add(receipt)
        db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")

    assert exc_info.value.code == "ATTACHMENT_EXTRACTION_DRIFT"


def test_authorize_requires_accepted_scanner_evidence() -> None:
    """缺失或拒绝的扫描保证不能仅凭ready资源和读取回执向模型披露。"""

    with _session() as db:
        _, read = _seed(db)
        evidence = db.exec(select(ScannerEvidence)).one()
        evidence.verdict = "rejected"
        db.add(evidence)
        db.commit()
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")

    assert exc_info.value.code == "ATTACHMENT_SCAN_REQUIRED"


def test_authorize_rejects_scanner_definition_older_than_frozen_policy() -> None:
    """扫描定义在扫描时超过冻结最大年龄必须fail closed并保持Receipt prepared。"""

    with _session() as db:
        _, read = _seed(db)
        evidence = db.exec(select(ScannerEvidence)).one()
        evidence.definition_published_at = evidence.scanned_at - timedelta(seconds=2)
        evidence.max_age_at_scan_seconds = 1
        db.add(evidence)
        db.commit()
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")
        receipt = db.exec(select(ProviderInputDispatchReceipt)).one()

    assert exc_info.value.code == "ATTACHMENT_SCAN_STALE"
    assert receipt.status == "prepared"


def test_authorize_rejects_unknown_egress_policy() -> None:
    """未部署的tenant外发策略不得只作为Receipt字符串而绕过真实授权门。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="forged-policy",
        )

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")

    assert exc_info.value.code == "ATTACHMENT_EGRESS_POLICY_DENIED"


def test_authorize_rejects_arbitrary_sha_shaped_turn_egress_policy() -> None:
    """仅有64位摘要外观不代表tenant已授权该Turn外发策略。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="f" * 64,
        )

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")

    assert exc_info.value.code == "ATTACHMENT_EGRESS_POLICY_DENIED"


def test_authorize_rejects_scanner_evidence_when_resource_security_status_drifted() -> None:
    """旧accepted扫描记录不能授权已回退pending_scan或隔离状态的资源。"""

    with _session() as db:
        resource, read = _seed(db)
        resource.security_status = "pending_scan"
        db.add(resource)
        db.commit()
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")

    assert exc_info.value.code == "ATTACHMENT_SCAN_REQUIRED"


def test_dynamic_egress_policy_and_slice_must_match_frozen_facts() -> None:
    """Dynamic外发策略和切片必须分别锚定冻结模型与已持久化input.read事实。"""

    with _session() as db:
        resource, _ = _seed(db)
        model = ModelConfig(
            id="model-egress-1",
            tenant_id="tenant-a",
            name="Egress Model",
            provider="openai_compatible",
            api_key_encrypted="encrypted",
            model="test-model",
            preflight_status="ready",
            enabled=True,
        )
        instance = SopInstance(
            id="execution-egress-1",
            tenant_id="tenant-a",
            session_id="session-egress-1",
            kind="dynamic_task",
            active_slot_key="dynamic:egress",
            initiator_user_id="user-a",
            agent_id="agent-a",
            goal_snapshot_json={"goal": "analyze"},
            current_plan_revision_id="plan-egress-1",
            current_plan_checksum="a" * 64,
            capability_snapshot_json={
                "model": {"model_config_id": model.id, "checksum": "model-capability"}
            },
            capability_checksum="b" * 64,
            status="running",
        )
        snapshot = InputResourceSnapshot(
            id="execution-egress-snapshot",
            tenant_id="tenant-a",
            execution_id=instance.id,
            source_type="chat_upload",
            source_resource_id=resource.id,
            source_version=resource.version,
            filename=resource.filename,
            mime_type=resource.mime_type,
            size_bytes=resource.size_bytes,
            content_checksum=resource.content_checksum,
            extraction_checksum="extract-v1",
            extraction_id="extract-1",
            parser_name="builtin-csv",
            parser_version="1.0.0",
            parser_config_checksum="parser-v1",
            element_manifest_checksum="manifest-v1",
            resource_acl_revision_at_snapshot=resource.acl_revision,
            ingestion_status="ready",
            identity_checksum="identity-egress-v1",
            storage_locator_digest="locator-egress-v1",
            captured_acl_json={"owner_user_id": "user-a"},
        )
        db.add_all([model, instance, snapshot])
        slice_checksum = _seed_dynamic_read(
            db,
            execution_id=instance.id,
            snapshot_id=snapshot.id,
        )
        db.commit()
        expected_policy = capability_checksum(
            {
                "provider": model.provider,
                "model": model.model,
                "mode": "reviewed_elements",
            }
        )
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_execution_group(
            tenant_id="tenant-a",
            execution_id=instance.id,
            causation_id="step:answer:proposal:1",
            slices=[(snapshot.id, slice_checksum)],
            egress_policy_checksum=expected_policy,
        )
        receipt = db.get(ProviderInputDispatchReceipt, group.ordered_receipt_ids_json[0])
        assert receipt is not None
        receipt.egress_policy_checksum = "0" * 64
        db.add(receipt)
        db.flush()
        with pytest.raises(InputBindingError) as policy_error:
            gateway.authorize(group, worker_id="worker-forged-policy")
        assert policy_error.value.code == "ATTACHMENT_EGRESS_POLICY_DENIED"
        receipt.egress_policy_checksum = expected_policy
        receipt.slice_checksum = "f" * 64
        db.add(receipt)
        db.flush()
        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-forged-slice")
        assert exc_info.value.code == "ATTACHMENT_DISPATCH_CAUSATION_INVALID"
        receipt.slice_checksum = slice_checksum
        db.add(receipt)
        db.flush()
        gateway.authorize(group, worker_id="worker-dynamic")

    assert group.status == "dispatching"


def test_expired_dispatching_group_becomes_unknown_without_replay() -> None:
    """授权后worker崩溃超过deadline时sweeper按可能披露收敛且不重建attempt。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        gateway.authorize(group, worker_id="dead-worker")
        receipt = db.exec(select(ProviderInputDispatchReceipt)).one()
        receipt.deadline_at = utc_now() - timedelta(microseconds=1)
        db.add(receipt)
        db.commit()

        assert gateway.expire_dispatching_to_unknown() == 1
        db.commit()
        receipt_count = len(db.exec(select(ProviderInputDispatchReceipt)).all())
        group_status = group.status
        receipt_status = receipt.status

    assert group_status == "unknown"
    assert receipt_status == "unknown"
    assert receipt_count == 1


def test_dynamic_execution_dispatch_uses_snapshot_acl_and_causation() -> None:
    """Dynamic模型动作必须由Execution快照建立唯一Receipt并实时复核资源ACL。"""

    with _session() as db:
        resource, _ = _seed(db)
        model = ModelConfig(
            id="model-execution-1",
            tenant_id="tenant-a",
            name="Execution Model",
            provider="openai_compatible",
            api_key_encrypted="encrypted",
            model="test-model",
            preflight_status="ready",
            enabled=True,
        )
        instance = SopInstance(
            id="execution-1",
            tenant_id="tenant-a",
            session_id="session-execution-1",
            kind="dynamic_task",
            active_slot_key="dynamic:execution",
            initiator_user_id="user-a",
            agent_id="agent-a",
            goal_snapshot_json={"goal": "analyze"},
            current_plan_revision_id="plan-execution-1",
            current_plan_checksum="d" * 64,
            capability_snapshot_json={"model": {"model_config_id": model.id}},
            capability_checksum="e" * 64,
            status="running",
        )
        snapshot = InputResourceSnapshot(
            id="execution-snapshot-1",
            tenant_id="tenant-a",
            execution_id="execution-1",
            source_type="chat_upload",
            source_resource_id=resource.id,
            source_version=resource.version,
            filename=resource.filename,
            mime_type=resource.mime_type,
            size_bytes=resource.size_bytes,
            content_checksum=resource.content_checksum,
            extraction_checksum="extract-v1",
            extraction_id="extract-1",
            parser_name="builtin-csv",
            parser_version="1.0.0",
            parser_config_checksum="parser-v1",
            element_manifest_checksum="manifest-v1",
            resource_acl_revision_at_snapshot=resource.acl_revision,
            ingestion_status="ready",
            identity_checksum="identity-v1",
            storage_locator_digest="locator-v1",
            captured_acl_json={"owner_user_id": "user-a"},
        )
        db.add_all([model, instance, snapshot])
        slice_checksum = _seed_dynamic_read(
            db,
            execution_id=instance.id,
            snapshot_id=snapshot.id,
        )
        db.commit()
        expected_policy = capability_checksum(
            {
                "provider": model.provider,
                "model": model.model,
                "mode": "reviewed_elements",
            }
        )
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_execution_group(
            tenant_id="tenant-a",
            execution_id="execution-1",
            causation_id="step:answer:attempt:1",
            slices=[(snapshot.id, slice_checksum)],
            egress_policy_checksum=expected_policy,
        )
        gateway.authorize(group, worker_id="worker-dynamic")
        gateway.settle_delivered(group)
        db.commit()
        receipt = db.exec(
            select(ProviderInputDispatchReceipt).where(
                ProviderInputDispatchReceipt.execution_id == "execution-1"
            )
        ).one()
        group_consumer_kind = group.consumer_kind
        group_status = group.status
        resource_id = resource.id
        receipt_values = (
            receipt.consumer_kind,
            receipt.resource_id,
            receipt.causation_id,
            receipt.status,
        )

    assert group_consumer_kind == "dynamic_task"
    assert group_status == "settled"
    assert receipt_values == (
        "dynamic_task",
        resource_id,
        "step:answer:attempt:1",
        "settled",
    )


def test_prepare_turn_group_rejects_cross_tenant_read_and_snapshot() -> None:
    """外发Group不得把其他租户的读取回执或快照洗入当前tenant。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)

        with pytest.raises(InputBindingError) as exc_info:
            gateway.prepare_turn_group(
                tenant_id="tenant-b",
                turn_id="turn-1",
                read_receipt_ids=[read.id],
                egress_policy_checksum="inline-model-default-v1",
            )
        groups = db.exec(select(ProviderInputDispatchGroup)).all()

    assert exc_info.value.code == "ATTACHMENT_DISPATCH_CAUSATION_INVALID"
    assert groups == []


def test_multi_resource_authorization_is_atomic_when_later_resource_is_revoked() -> None:
    """任一资源撤权时整组保持prepared，禁止先授权的Receipt形成半组披露。"""

    with _session() as db:
        first_resource, first_read = _seed(db)
        second_resource = ManagedInputResource(
            id="input-2",
            tenant_id="tenant-a",
            owner_user_id="user-a",
            version="resource-v2",
            filename="actuals.csv",
            mime_type="text/csv",
            size_bytes=10,
            content_checksum="resource-v2",
            ingestion_status="ready",
            storage_locator="tenant-a/blob-2",
        )
        second_snapshot = TurnInputSnapshot(
            id="snapshot-2",
            tenant_id="tenant-a",
            turn_id="turn-1",
            session_id="session-1",
            message_resource_link_id="link-2",
            resource_id=second_resource.id,
            resource_version=second_resource.version,
            extraction_id="extract-2",
            extraction_checksum="extract-v2",
            element_manifest_checksum="manifest-v2",
            resource_acl_revision_at_snapshot=0,
            opaque_handle="opaque-handle-2",
        )
        second_read = TurnInputReadReceipt(
            id="read-2",
            tenant_id="tenant-a",
            turn_id="turn-1",
            snapshot_id=second_snapshot.id,
            slice_checksum="slice-v2",
            receipt_checksum="receipt-v2",
            status="succeeded",
        )
        db.add(second_resource)
        db.add(second_snapshot)
        db.add(second_read)
        db.commit()
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[first_read.id, second_read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        second_resource.access_status = "revoked"
        second_resource.acl_revision += 1
        db.add(second_resource)
        db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            gateway.authorize(group, worker_id="worker-1")
        receipts = db.exec(
            select(ProviderInputDispatchReceipt).order_by(ProviderInputDispatchReceipt.id)
        ).all()
        first_status = first_resource.access_status
        group_status = group.status

    assert first_status == "active"
    assert exc_info.value.code == "ATTACHMENT_COUNTERMANDED"
    assert group_status == "prepared"
    assert [item.status for item in receipts] == ["prepared", "prepared"]


def test_settle_delivered_requires_all_receipts_dispatching() -> None:
    """未执行授权的prepared Group不得伪造settled终态。"""

    with _session() as db:
        _, read = _seed(db)
        gateway = ProviderInputDispatchGateway(db)
        group = gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )

        with pytest.raises(InputBindingError) as exc_info:
            gateway.settle_delivered(group)
        receipt = db.exec(select(ProviderInputDispatchReceipt)).one()
        group_status = group.status

    assert exc_info.value.code == "ATTACHMENT_DISPATCH_STATE_INVALID"
    assert group_status == "prepared"
    assert receipt.status == "prepared"


def test_late_settle_cannot_overwrite_unknown_after_sweeper(tmp_path) -> None:
    """超时sweeper先提交unknown后，持旧对象的worker不得把终态覆盖回settled。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-fencing.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as worker_db:
        _, read = _seed(worker_db)
        worker_gateway = ProviderInputDispatchGateway(worker_db)
        stale_group = worker_gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        worker_gateway.authorize(stale_group, worker_id="slow-worker")
        receipt = worker_db.exec(select(ProviderInputDispatchReceipt)).one()
        receipt.deadline_at = utc_now() - timedelta(microseconds=1)
        worker_db.add(receipt)
        worker_db.commit()

        with Session(engine) as sweeper_db:
            assert ProviderInputDispatchGateway(
                sweeper_db
            ).expire_dispatching_to_unknown() == 1
            sweeper_db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            worker_gateway.settle_delivered(stale_group)
        worker_db.rollback()

    with Session(engine) as verify_db:
        group = verify_db.exec(select(ProviderInputDispatchGroup)).one()
        receipt = verify_db.exec(select(ProviderInputDispatchReceipt)).one()
        assert group.status == "unknown"
        assert receipt.status == "unknown"
        assert receipt.lease_owner is None
    assert exc_info.value.code == "ATTACHMENT_DISPATCH_FENCED"


def test_late_settle_rejects_resource_revoked_after_authorize(tmp_path) -> None:
    """授权后资源先被撤销时，迟到模型结果不得结算或进入后续回答。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-revoke.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as worker_db:
        resource, read = _seed(worker_db)
        worker_gateway = ProviderInputDispatchGateway(worker_db)
        stale_group = worker_gateway.prepare_turn_group(
            tenant_id="tenant-a",
            turn_id="turn-1",
            read_receipt_ids=[read.id],
            egress_policy_checksum="inline-model-default-v1",
        )
        worker_gateway.authorize(stale_group, worker_id="slow-worker")
        worker_db.commit()

        with Session(engine) as revoke_db:
            current = revoke_db.get(ManagedInputResource, resource.id)
            assert current is not None
            current.access_status = "revoked"
            current.revoked_at = utc_now()
            current.acl_revision += 1
            revoke_db.add(current)
            revoke_db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            worker_gateway.settle_delivered(stale_group)
        worker_db.rollback()

    with Session(engine) as verify_db:
        group = verify_db.exec(select(ProviderInputDispatchGroup)).one()
        receipt = verify_db.exec(select(ProviderInputDispatchReceipt)).one()
        assert group.status == "dispatching"
        assert receipt.status == "dispatching"
    assert exc_info.value.code == "ATTACHMENT_DISPATCH_FENCED"
