"""
@Time       : 2026/08/13 20:18
@Author     : zhanglp8181
@File       : test_attachment_contract_models.py
@CallChain  : pytest → SQLModel附件契约 → SQLite/MySQL约束
@Description: 验证附件权威绑定、不可变提取、Turn快照和外发attempt的唯一性及状态约束。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sqlalchemy_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    DraftUploadBinding,
    InputResourceExtraction,
    InputResourceExtractionAttempt,
    ProviderInputDispatchGroup,
    ProviderInputDispatchReceipt,
    ResourceSessionBinding,
    ScannerEvidence,
    TurnInputSnapshot,
    utc_now,
)
from app.session.input_bindings import InputBindingError, InputBindingService


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _session() -> Session:
    """创建启用全部当前模型约束的隔离 SQLite 会话。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_resource_version_can_only_bind_one_session() -> None:
    """同一资源版本被一个会话领取后，另一会话不能建立第二个权威归属。"""

    with _session() as db:
        db.add(
            ResourceSessionBinding(
                tenant_id="tenant_demo",
                resource_id="input_a",
                resource_version="a" * 64,
                owner_user_id="user_a",
                session_id="session_a",
                agent_id="agent_a",
            )
        )
        db.commit()
        db.add(
            ResourceSessionBinding(
                tenant_id="tenant_demo",
                resource_id="input_a",
                resource_version="a" * 64,
                owner_user_id="user_a",
                session_id="session_b",
                agent_id="agent_a",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_extraction_retry_is_append_only_and_publish_is_idempotent() -> None:
    """失败重试必须追加 attempt，且一个物理 attempt 只能发布一份不可变 Extraction。"""

    with _session() as db:
        base = {
            "tenant_id": "tenant_demo",
            "resource_id": "input_a",
            "resource_version": "a" * 64,
            "parser_name": "csv",
            "parser_version": "1.0.0",
            "parser_config_checksum": "b" * 64,
        }
        first = InputResourceExtractionAttempt(**base, attempt_no=1, status="failed")
        second = InputResourceExtractionAttempt(**base, attempt_no=2, status="succeeded")
        db.add(first)
        db.add(second)
        db.commit()
        published = InputResourceExtraction(
            **base,
            content_checksum="a" * 64,
            extraction_checksum="c" * 64,
            element_manifest_checksum="d" * 64,
            published_from_attempt_id=second.id,
            element_count=2,
        )
        db.add(published)
        db.commit()
        db.add(
            InputResourceExtraction(
                **base,
                content_checksum="a" * 64,
                extraction_checksum="e" * 64,
                element_manifest_checksum="f" * 64,
                published_from_attempt_id=second.id,
                element_count=3,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_turn_snapshot_handle_and_dispatch_token_are_tenant_unique() -> None:
    """裸资源ID不能替代Turn句柄，且每个物理外发attempt必须具备tenant内唯一token。"""

    with _session() as db:
        snapshot = TurnInputSnapshot(
            tenant_id="tenant_demo",
            turn_id="msg_turn",
            session_id="session_a",
            message_resource_link_id="link_a",
            resource_id="input_a",
            resource_version="a" * 64,
            extraction_id="extract_a",
            extraction_checksum="b" * 64,
            element_manifest_checksum="c" * 64,
            opaque_handle="d" * 64,
        )
        group = ProviderInputDispatchGroup(
            tenant_id="tenant_demo",
            consumer_kind="turn",
            causation_id="msg_turn",
        )
        db.add(snapshot)
        db.add(group)
        db.commit()
        common = {
            "tenant_id": "tenant_demo",
            "dispatch_group_id": group.id,
            "consumer_kind": "turn",
            "turn_id": "msg_turn",
            "resource_id": "input_a",
            "extraction_id": "extract_a",
            "slice_checksum": "e" * 64,
            "egress_policy_checksum": "f" * 64,
            "dispatch_token": "1" * 64,
            "deadline_at": utc_now() + timedelta(minutes=1),
            "causation_id": "msg_turn",
        }
        db.add(ProviderInputDispatchReceipt(**common))
        db.commit()
        db.add(
            ProviderInputDispatchReceipt(
                **{**common, "resource_id": "input_b", "extraction_id": "extract_b"}
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_scanner_evidence_persists_replayable_freshness_inputs() -> None:
    """扫描事实必须持久化权威发布时间、扫描时间和当时最大年龄，才能审计复算。"""

    now = utc_now()
    evidence = ScannerEvidence(
        tenant_id="tenant_demo",
        resource_id="input_a",
        resource_version="a" * 64,
        assurance_level="format_verified",
        engine="builtin-format",
        engine_version="1.0.0",
        definition_version="formats-20260813",
        definition_published_at=now - timedelta(hours=1),
        scanned_at=now,
        freshness_policy_checksum="b" * 64,
        max_age_at_scan_seconds=7200,
        verdict="accepted",
    )
    assert (evidence.scanned_at - evidence.definition_published_at).total_seconds() == 3600
    assert evidence.max_age_at_scan_seconds == 7200


def test_upload_binding_requires_session_or_draft_target() -> None:
    """上传请求若未绑定正式会话或草稿会话，数据库必须拒绝建立悬空授权。"""

    with _session() as db:
        db.add(
            DraftUploadBinding(
                binding_id="binding_a",
                tenant_id="tenant_demo",
                owner_user_id="user_a",
                agent_id="agent_a",
                nonce_checksum="a" * 64,
                idempotency_key="b" * 64,
                expires_at=utc_now() + timedelta(minutes=5),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_upload_binding_is_single_request_claim_and_cannot_replay() -> None:
    """正向claim一次成功，错误nonce和第二worker重放均稳定拒绝。"""

    with _session() as db:
        service = InputBindingService(db)
        secret = service.issue_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id="user_a",
            agent_id="agent_a",
            draft_conversation_id="draft:agent_a",
            idempotency_key="upload-one",
        )
        db.commit()
        with pytest.raises(InputBindingError) as nonce_error:
            service.claim_upload_binding(
                tenant_id="tenant_demo",
                owner_user_id="user_a",
                binding_id=secret.binding_id,
                nonce="forged",
                worker_id="worker-forged",
            )
        claimed = service.claim_upload_binding(
            tenant_id="tenant_demo",
            owner_user_id="user_a",
            binding_id=secret.binding_id,
            nonce=secret.nonce,
            worker_id="worker-a",
        )
        db.commit()
        claimed_status = claimed.status
        with pytest.raises(InputBindingError) as replay:
            service.claim_upload_binding(
                tenant_id="tenant_demo",
                owner_user_id="user_a",
                binding_id=secret.binding_id,
                nonce=secret.nonce,
                worker_id="worker-b",
            )

    assert nonce_error.value.code == "ATTACHMENT_UPLOAD_BINDING_INVALID"
    assert claimed_status == "claimed"
    assert replay.value.code == "ATTACHMENT_UPLOAD_BINDING_INVALID"


def test_attachment_contract_migration_round_trips_sqlite(tmp_path: Path) -> None:
    """从0066最小前置结构升级0067并降级，证明SQLite批次迁移独立可逆。"""

    database_path = tmp_path / "attachment-contract.db"
    engine = create_sqlalchemy_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260815_0066')"))
        connection.execute(
            text(
                "CREATE TABLE managed_input_resources ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE input_resource_snapshots ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL)"
            )
        )
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite:///{database_path}"
    command.upgrade(config, "20260816_0067")
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(managed_input_resources)"))
        }
    assert "input_resource_extractions" in tables
    assert {"access_status", "security_status", "destruction_status"}.issubset(columns)

    command.downgrade(config, "20260815_0066")
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "input_resource_extractions" not in tables
