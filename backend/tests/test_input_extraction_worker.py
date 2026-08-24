"""
@Time       : 2026/08/14 09:52
@Author     : zhanglp8181
@File       : test_input_extraction_worker.py
@CallChain  : pytest → 过期ExtractionAttempt → 恢复worker → 隔离parser/Published Extraction
@Description: 验证上传请求或worker崩溃后解析事实可恢复，旧Attempt不会被覆盖或伪装成功。
"""

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    InputResourceExtraction,
    InputResourceExtractionAttempt,
    ManagedInputResource,
    utc_now,
)
from app.session.input_extraction import (
    InputExtractionError,
    InputExtractionService,
    ParsedElement,
)
from app.session.input_extraction_worker import run_extraction_maintenance_once
from app.session.managed_resources import ManagedInputResourceService


def test_expired_claim_is_appended_and_recovered_without_overwriting_history(
    tmp_path,
    monkeypatch,
) -> None:
    """解析worker丢失租约后必须失败旧Attempt、追加新Attempt并发布唯一Extraction。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.session.input_extraction_worker.engine", engine)
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        resource, _ = ManagedInputResourceService(db).persist_upload(
            tenant_id="tenant-worker",
            owner_user_id="user-worker",
            agent_id="agent-worker",
            filename="sales.csv",
            content_type="text/csv",
            data=b"Region,Target\nEast,100\n",
            defer_extraction_format="csv",
        )
        service = InputExtractionService(db)
        first = service.ensure_attempt(resource, file_format="csv")
        service.claim(first, worker_id="dead-worker", lease_seconds=30)
        first.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(first)
        db.commit()
        resource_id = resource.id

    progressed = run_extraction_maintenance_once(worker_id="recovery-worker")

    with Session(engine) as db:
        attempts = db.exec(
            select(InputResourceExtractionAttempt).order_by(
                InputResourceExtractionAttempt.attempt_no
            )
        ).all()
        extraction = db.exec(select(InputResourceExtraction)).one()
        resource = db.get(ManagedInputResource, resource_id)

    assert progressed == 2
    assert [(item.attempt_no, item.status) for item in attempts] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert attempts[0].error_code == "ATTACHMENT_EXTRACTION_WORKER_LOST"
    assert extraction.published_from_attempt_id == attempts[1].id
    assert resource is not None and resource.ingestion_status == "ready"
    assert resource.extraction_metadata_json["preview"] == "Region,Target\nEast,100"
    assert resource.extraction_metadata_json["pipeline"][-1] == "ready"


def test_revoked_resource_cannot_be_resurrected_by_late_parser_publish(
    tmp_path,
    monkeypatch,
) -> None:
    """用户移除附件先提交后，迟到parser不得把墓碑重新覆盖为ready。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as parser_db:
        resource, _ = ManagedInputResourceService(parser_db).persist_upload(
            tenant_id="tenant-revoke-race",
            owner_user_id="user-revoke-race",
            agent_id="agent-revoke-race",
            filename="sales.csv",
            content_type="text/csv",
            data=b"Region,Target\nEast,100\n",
            defer_extraction_format="csv",
        )
        service = InputExtractionService(parser_db)
        attempt = service.ensure_attempt(resource, file_format="csv")
        service.claim(attempt, worker_id="late-parser", lease_seconds=30)
        parser_db.commit()
        resource_id = resource.id

        with Session(engine) as delete_db:
            current = delete_db.get(ManagedInputResource, resource_id)
            assert current is not None
            ManagedInputResourceService(delete_db).discard_unreferenced(
                current,
                actor_user_id="user-revoke-race",
            )
            delete_db.commit()

        with pytest.raises(InputExtractionError) as captured:
            service.publish(
                attempt,
                resource,
                [
                    ParsedElement(
                        element_type="table",
                        text="Region | Target\nEast | 100",
                        table={"headers": ["Region", "Target"]},
                        locator={"kind": "csv", "rows": [1, 2]},
                    )
                ],
                file_format="csv",
                worker_id="late-parser",
                fencing_token=attempt.fencing_token,
            )
        assert captured.value.code == "ATTACHMENT_EXTRACTION_COUNTERMANDED"
        parser_db.rollback()

    with Session(engine) as verify_db:
        tombstone = verify_db.get(ManagedInputResource, resource_id)
        assert tombstone is not None
        assert tombstone.access_status == "revoked"
        assert tombstone.destruction_status == "purged"
        assert tombstone.ingestion_status == "revoked"
        assert verify_db.exec(select(InputResourceExtraction)).all() == []
