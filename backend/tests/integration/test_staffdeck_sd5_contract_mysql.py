"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_staffdeck_sd5_contract_mysql.py
@CallChain  : pytest mysql → Alembic head → SD-5 知识修订字段 → MySQL 持久回读
@Description: 在随机隔离 MySQL 验证 SD-5 首个持久合同与同租户任务幂等约束。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

from app.db.models import (
    AgentKnowledgeBranch,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Tenant,
    User,
    utc_now,
)
from app.api.knowledge import cancel_job, get_job, get_revision_job_by_client_request
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.revisions import (
    KnowledgeRevisionConflictError,
    RevisionPrepareInput,
    build_revision,
    prepare_revision,
    publish_revision,
)
from app.knowledge import revisions as revision_module
from app.knowledge.service import KnowledgeService


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把随机 MySQL 测试库升级到包含 SD-5 0086 的最新 head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def test_mysql_sd5_revision_contract_round_trips_and_rejects_duplicate_job(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 8.4 可回读修订字段并拒绝同租户同一 dedupe_key。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_contract", name="SD5 contract"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_contract",
                tenant_id="tenant_sd5_contract",
                name="SD5 contract KB",
                content_revision=2,
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_contract",
                tenant_id="tenant_sd5_contract",
                knowledge_base_id="kb_sd5_contract",
                version="1.0.0",
                scope="overall",
                name="SD5 contract version",
                publication_state="published",
                content_manifest_json={"documents": []},
            )
        )
        db.add(
            AgentKnowledgeBranch(
                id="agentkb_sd5_contract",
                tenant_id="tenant_sd5_contract",
                agent_id="agent_sd5_contract",
                knowledge_base_id="kb_sd5_contract",
                content_revision=3,
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_contract",
                tenant_id="tenant_sd5_contract",
                knowledge_base_id="kb_sd5_contract",
                knowledge_base_version_id="kbver_sd5_contract",
                lineage_id="lineage_sd5_contract",
                filename="contract.md",
                file_type="md",
                normalized_source="# 正文\nMySQL",
                source_checksum="a" * 64,
                source_kind="normalized",
            )
        )
        db.add(
            KnowledgeIngestJob(
                id="job_sd5_contract_one",
                tenant_id="tenant_sd5_contract",
                knowledge_base_id="kb_sd5_contract",
                filename="contract.md",
                job_kind="revision",
                scope="overall",
                expected_revision=2,
                client_request_id="client_sd5_contract",
                request_hash="b" * 64,
                dedupe_key="dedupe_sd5_contract",
                fence=1,
                attempt_count=1,
            )
        )
        db.commit()
        db.expire_all()
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_contract")
        document = db.get(KnowledgeDocument, "kdoc_sd5_contract")
        job = db.get(KnowledgeIngestJob, "job_sd5_contract_one")

        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert document is not None and document.normalized_source == "# 正文\nMySQL"
        assert job is not None and job.fence == 1 and job.job_kind == "revision"
        db.add(
            KnowledgeIngestJob(
                id="job_sd5_contract_two",
                tenant_id="tenant_sd5_contract",
                knowledge_base_id="kb_sd5_contract",
                filename="contract.md",
                job_kind="revision",
                scope="overall",
                dedupe_key="dedupe_sd5_contract",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    columns = {item["name"] for item in inspect(engine).get_columns("knowledge_ingest_jobs")}
    assert {"dedupe_key", "lease_until", "fence", "deadline_at"}.issubset(columns)


def test_mysql_sd5_prepare_reuses_persisted_client_identity(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 上正文 prepare 的 CAS 依据和同身份重试仍只产生一个候选。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_prepare_mysql", name="SD5 prepare MySQL"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_prepare_mysql",
                tenant_id="tenant_sd5_prepare_mysql",
                name="Prepare MySQL KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_prepare_mysql",
                tenant_id="tenant_sd5_prepare_mysql",
                knowledge_base_id="kb_sd5_prepare_mysql",
                version="1.0.0",
                name="Prepare MySQL KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_prepare_mysql",
                tenant_id="tenant_sd5_prepare_mysql",
                knowledge_base_id="kb_sd5_prepare_mysql",
                knowledge_base_version_id="kbver_sd5_prepare_mysql",
                lineage_id="lineage_sd5_prepare_mysql",
                filename="prepare.md",
                file_type="md",
                status="ready",
            )
        )
        db.commit()
        request = RevisionPrepareInput(
            tenant_id="tenant_sd5_prepare_mysql",
            knowledge_base_id="kb_sd5_prepare_mysql",
            scope="overall",
            actor_user_id="user_sd5_prepare_mysql",
            document_id="kdoc_sd5_prepare_mysql",
            content_md="# MySQL 修订",
            expected_revision=1,
            client_request_id="client_sd5_prepare_mysql",
        )
        first = prepare_revision(db, request)
        second = prepare_revision(db, request)
        assert first.id == second.id
        assert first.candidate_version_id is not None
        assert db.get(KnowledgeBase, "kb_sd5_prepare_mysql").content_revision == 1


def test_mysql_sd5_build_reconstructs_candidate_without_model(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 上候选重建创建新物理资产且不提前推进已发布 head。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_build_mysql", name="SD5 build MySQL"))
        db.add(
            User(
                id="user_sd5_build_mysql",
                tenant_id="tenant_sd5_build_mysql",
                username="sd5-build-mysql",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_build_mysql",
                tenant_id="tenant_sd5_build_mysql",
                name="Build MySQL KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_build_mysql",
                tenant_id="tenant_sd5_build_mysql",
                knowledge_base_id="kb_sd5_build_mysql",
                version="1.0.0",
                name="Build MySQL KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_build_mysql",
                tenant_id="tenant_sd5_build_mysql",
                knowledge_base_id="kb_sd5_build_mysql",
                knowledge_base_version_id="kbver_sd5_build_mysql",
                lineage_id="lineage_sd5_build_mysql",
                filename="mysql.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文\n旧事实",
                source_kind="normalized",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_build_mysql",
                knowledge_base_id="kb_sd5_build_mysql",
                scope="overall",
                actor_user_id="user_sd5_build_mysql",
                document_id="kdoc_sd5_build_mysql",
                content_md="# 新正文\nMySQL 新事实",
                expected_revision=1,
                client_request_id="client_sd5_build_mysql",
            ),
        )

        built = build_revision(db, job.id)

        assert built.status == "ready_to_publish"
        candidate_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == built.candidate_version_id
            )
        ).all()
        assert len(candidate_documents) == 1
        assert candidate_documents[0].id != "kdoc_sd5_build_mysql"
        assert candidate_documents[0].normalized_source == "# 新正文\nMySQL 新事实"
        assert db.exec(
            select(KnowledgeBucket).where(
                KnowledgeBucket.knowledge_base_version_id == built.candidate_version_id
            )
        ).all()
        assert db.exec(
            select(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_base_version_id == built.candidate_version_id
            )
        ).all()
        assert db.get(KnowledgeBase, "kb_sd5_build_mysql").content_revision == 1

        published = publish_revision(db, built.id)

        assert published.status == "completed"
        assert published.published_version_id == built.candidate_version_id
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_build_mysql")
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert knowledge_base.metadata_json["current_version"] != "1.0.0"
        candidate = db.get(KnowledgeBaseVersion, published.published_version_id)
        assert candidate is not None and candidate.publication_state == "published"


def test_mysql_sd5_knowledge_service_reads_only_bound_published_version(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 知识消费者按显式发布版本查询，不会混入历史正文。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_consumer_mysql"
    knowledge_base_id = "kb_sd5_consumer_mysql"
    old_version_id = "kbver_sd5_consumer_mysql_old"
    current_version_id = "kbver_sd5_consumer_mysql_current"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 consumer MySQL"))
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="SD5 consumer KB",
                metadata_json={"current_version": "2.0.0"},
            )
        )
        db.add_all(
            [
                KnowledgeBaseVersion(
                    id=old_version_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    version="1.0.0",
                    name="SD5 consumer KB",
                    publication_state="published",
                ),
                KnowledgeBaseVersion(
                    id=current_version_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    version="2.0.0",
                    name="SD5 consumer KB",
                    publication_state="published",
                ),
                KnowledgeDocument(
                    id="kdoc_sd5_consumer_mysql_old",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=old_version_id,
                    lineage_id="lineage_sd5_consumer_mysql",
                    filename="consumer.md",
                    file_type="md",
                    title="SD5 consumer",
                    status="ready",
                    normalized_source="# Old\nold-mysql-fact",
                    source_kind="normalized",
                    metadata_json={
                        "document_card": {"title": "SD5 consumer", "summary": "old-mysql-fact"},
                        "section_tree": [{"title": "Old", "path": "Old", "content": "old-mysql-fact"}],
                    },
                ),
                KnowledgeDocument(
                    id="kdoc_sd5_consumer_mysql_current",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=current_version_id,
                    lineage_id="lineage_sd5_consumer_mysql",
                    filename="consumer.md",
                    file_type="md",
                    title="SD5 consumer",
                    status="ready",
                    normalized_source="# Current\ncurrent-mysql-fact",
                    source_kind="normalized",
                    metadata_json={
                        "document_card": {"title": "SD5 consumer", "summary": "current-mysql-fact"},
                        "section_tree": [{
                            "title": "Current",
                            "path": "Current",
                            "content": "current-mysql-fact",
                        }],
                    },
                ),
            ]
        )
        db.add_all(
            [
                KnowledgeBucket(
                    id="kbucket_sd5_consumer_mysql_old",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=old_version_id,
                    document_id="kdoc_sd5_consumer_mysql_old",
                    bucket_key="consumer",
                    title="Old",
                    summary="old-mysql-fact",
                    status="ready",
                    metadata_json={"content": "old-mysql-fact"},
                ),
                KnowledgeBucket(
                    id="kbucket_sd5_consumer_mysql_current",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=current_version_id,
                    document_id="kdoc_sd5_consumer_mysql_current",
                    bucket_key="consumer",
                    title="Current",
                    summary="current-mysql-fact",
                    status="ready",
                    metadata_json={"content": "current-mysql-fact"},
                ),
                KnowledgeChunk(
                    id="kchunk_sd5_consumer_mysql_old",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=old_version_id,
                    document_id="kdoc_sd5_consumer_mysql_old",
                    bucket_id="kbucket_sd5_consumer_mysql_old",
                    chunk_index=0,
                    content="old-mysql-fact",
                    summary="old-mysql-fact",
                ),
                KnowledgeChunk(
                    id="kchunk_sd5_consumer_mysql_current",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=current_version_id,
                    document_id="kdoc_sd5_consumer_mysql_current",
                    bucket_id="kbucket_sd5_consumer_mysql_current",
                    chunk_index=0,
                    content="current-mysql-fact",
                    summary="current-mysql-fact",
                ),
            ]
        )
        db.commit()

        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id=tenant_id,
                query="current-mysql-fact",
                desired_evidence="current-mysql-fact",
                knowledge_base_ids=[knowledge_base_id],
                knowledge_base_version_ids=[current_version_id],
            )
        )

        assert response.outcome == "evidence_found"
        serialized = response.model_dump_json()
        assert "current-mysql-fact" in serialized
        assert "old-mysql-fact" not in serialized
        selected_bucket_ids = [bucket.id for bucket in response.selected_buckets]
        selected_buckets = [db.get(KnowledgeBucket, bucket_id) for bucket_id in selected_bucket_ids]
        assert selected_buckets
        assert all(bucket is not None and bucket.knowledge_base_version_id == current_version_id for bucket in selected_buckets)


def test_mysql_sd5_cancel_publish_race_and_management_permission(
    mysql_database_url: str,
    monkeypatch,
) -> None:
    """验证 MySQL 旧发布会话不能越过撤销终态，且正文任务取消遵循管理权限。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_race_permission"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL race permission"))
        db.add(
            User(
                id="user_sd5_mysql_race_admin",
                tenant_id=tenant_id,
                username="sd5-mysql-race-admin",
                role="admin",
                password_hash="test",
            )
        )
        member = User(
            id="user_sd5_mysql_race_member",
            tenant_id=tenant_id,
            username="sd5-mysql-race-member",
            role="member",
            password_hash="test",
        )
        db.add(member)
        db.add(
            KnowledgeBase(
                id="kb_sd5_mysql_race_permission",
                tenant_id=tenant_id,
                name="MySQL race permission KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_mysql_race_permission",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_race_permission",
                version="1.0.0",
                name="MySQL race permission KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_mysql_race_permission",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_race_permission",
                knowledge_base_version_id="kbver_sd5_mysql_race_permission",
                lineage_id="lineage_sd5_mysql_race_permission",
                filename="mysql-race.md",
                file_type="md",
                status="ready",
                normalized_source="# MySQL 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_race_permission",
                scope="overall",
                actor_user_id="user_sd5_mysql_race_admin",
                document_id="kdoc_sd5_mysql_race_permission",
                content_md="# MySQL 新正文",
                expected_revision=1,
                client_request_id="client_sd5_mysql_race",
            ),
        )
        build_revision(db, job.id)
        original_authorization = revision_module._revision_actor_still_authorized

        def cancel_after_authorization(current_db, current_job):
            """在 MySQL 发布会话已读取授权后提交撤销，模拟迟到旧请求。"""

            with Session(engine) as late_db:
                revision_module.cancel_revision(late_db, current_job.id)
            return original_authorization(current_db, current_job)

        monkeypatch.setattr(
            revision_module,
            "_revision_actor_still_authorized",
            cancel_after_authorization,
        )
        with pytest.raises(KnowledgeRevisionConflictError):
            publish_revision(db, job.id)
        db.expire_all()
        stored_job = db.get(KnowledgeIngestJob, job.id)
        stored_base = db.get(KnowledgeBase, "kb_sd5_mysql_race_permission")
        assert stored_job is not None and stored_job.status == "cancelled"
        assert stored_base is not None and stored_base.content_revision == 1

        permission_job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_race_permission",
                scope="overall",
                actor_user_id="user_sd5_mysql_race_admin",
                document_id="kdoc_sd5_mysql_race_permission",
                content_md="# MySQL 待取消",
                expected_revision=1,
                client_request_id="client_sd5_mysql_permission",
            ),
        )
        readable = get_job(
            permission_job.id,
            tenant_id=tenant_id,
            agent_id=None,
            db=db,
            current_user=member,
        )
        assert readable.id == permission_job.id
        reconciled = get_revision_job_by_client_request(
            tenant_id=tenant_id,
            knowledge_base_id=permission_job.knowledge_base_id,
            document_id=permission_job.document_id,
            client_request_id=permission_job.client_request_id,
            source_version_id=permission_job.source_version_id,
            expected_revision=permission_job.expected_revision,
            agent_id=None,
            db=db,
            current_user=member,
        )
        assert reconciled is not None and reconciled.id == permission_job.id
        with pytest.raises(HTTPException) as error:
            cancel_job(
                permission_job.id,
                tenant_id=tenant_id,
                agent_id=None,
                db=db,
                current_user=member,
            )
        assert error.value.status_code == 403
        cancelled = cancel_job(
            permission_job.id,
            tenant_id=tenant_id,
            agent_id=None,
            db=db,
            current_user=db.get(User, "user_sd5_mysql_race_admin"),
        )
        assert cancelled.status == "cancelled"

        monkeypatch.setattr(
            revision_module,
            "_revision_actor_still_authorized",
            original_authorization,
        )
        winner_job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_race_permission",
                scope="overall",
                actor_user_id="user_sd5_mysql_race_admin",
                document_id="kdoc_sd5_mysql_race_permission",
                content_md="# MySQL 发布胜出",
                expected_revision=1,
                client_request_id="client_sd5_mysql_publish_winner",
            ),
        )
        build_revision(db, winner_job.id)
        original_claim = revision_module._claim_revision_publication
        late_status: list[str] = []

        def claim_then_cancel(current_db, current_job):
            """在 MySQL 发布权已提交后用独立连接执行迟到取消。"""

            claimed = original_claim(current_db, current_job)
            with Session(engine) as late_db:
                late_status.append(revision_module.cancel_revision(late_db, current_job.id).status)
            return claimed

        monkeypatch.setattr(revision_module, "_claim_revision_publication", claim_then_cancel)
        published = publish_revision(db, winner_job.id)
        assert late_status == ["publishing"]
        assert published.status == "completed"
        assert published.published_document_id
        db.expire_all()
        stored_winner = db.get(KnowledgeIngestJob, winner_job.id)
        winner_candidate = db.get(KnowledgeBaseVersion, published.published_version_id)
        winner_document = db.get(KnowledgeDocument, published.published_document_id)
        winner_base = db.get(KnowledgeBase, "kb_sd5_mysql_race_permission")
        assert stored_winner is not None and stored_winner.status == "completed"
        assert winner_candidate is not None and winner_candidate.publication_state == "published"
        assert winner_document is not None and winner_document.status == "ready"
        assert winner_base is not None and winner_base.content_revision == 2


def test_mysql_sd5_recovery_scans_ready_and_closes_expired_deadline(
    mysql_database_url: str,
) -> None:
    """验证隔离 MySQL 的恢复扫描接管 ready，并在 deadline 到期时不延长任务。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_recovery"
    now = utc_now()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL recovery"))
        db.add_all(
            [
                KnowledgeIngestJob(
                    id="job_sd5_mysql_recovery_queued",
                    tenant_id=tenant_id,
                    knowledge_base_id="kb_sd5_mysql_recovery",
                    filename="queued.md",
                    job_kind="revision",
                    status="queued",
                    deadline_at=now + timedelta(minutes=5),
                ),
                KnowledgeIngestJob(
                    id="job_sd5_mysql_recovery_running",
                    tenant_id=tenant_id,
                    knowledge_base_id="kb_sd5_mysql_recovery",
                    filename="running.md",
                    job_kind="revision",
                    status="running",
                    lease_until=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(minutes=5),
                ),
                KnowledgeIngestJob(
                    id="job_sd5_mysql_recovery_ready",
                    tenant_id=tenant_id,
                    knowledge_base_id="kb_sd5_mysql_recovery",
                    filename="ready.md",
                    job_kind="revision",
                    status="ready_to_publish",
                    deadline_at=now + timedelta(minutes=5),
                ),
                KnowledgeIngestJob(
                    id="job_sd5_mysql_recovery_expired",
                    tenant_id=tenant_id,
                    knowledge_base_id="kb_sd5_mysql_recovery",
                    filename="expired.md",
                    job_kind="revision",
                    status="ready_to_publish",
                    deadline_at=now - timedelta(seconds=1),
                ),
            ]
        )
        db.commit()

        recovered = revision_module.recover_revision_jobs(db, now=now)

        assert set(recovered) == {
            "job_sd5_mysql_recovery_queued",
            "job_sd5_mysql_recovery_running",
            "job_sd5_mysql_recovery_ready",
        }
        db.expire_all()
        ready = db.get(KnowledgeIngestJob, "job_sd5_mysql_recovery_ready")
        expired = db.get(KnowledgeIngestJob, "job_sd5_mysql_recovery_expired")
        assert ready is not None and ready.status == "ready_to_publish"
        assert expired is not None and expired.status == "failed"
        assert expired.error_code == "KNOWLEDGE_REVISION_DEADLINE_EXCEEDED"
        assert expired.deadline_at is not None and expired.deadline_at < now


def test_mysql_sd5_recovery_does_not_revoke_active_publication_and_stale_publisher_cannot_revive(
    mysql_database_url: str,
    monkeypatch,
) -> None:
    """验证 MySQL 活动发布租约与过期旧 publisher 的 fence 竞争不会产生空 head。"""

    from app.scheduled_tasks import worker as scheduled_worker

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_publication_recovery_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL publication recovery race"))
        db.add(
            User(
                id="user_sd5_mysql_publication_recovery_race",
                tenant_id=tenant_id,
                username="sd5-mysql-publication-recovery-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_mysql_publication_recovery_race",
                tenant_id=tenant_id,
                name="MySQL publication recovery race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_mysql_publication_recovery_race",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_publication_recovery_race",
                version="1.0.0",
                name="MySQL publication recovery race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_mysql_publication_recovery_race",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_publication_recovery_race",
                knowledge_base_version_id="kbver_sd5_mysql_publication_recovery_race",
                lineage_id="lineage_sd5_mysql_publication_recovery_race",
                filename="mysql-recovery-race.md",
                file_type="md",
                status="ready",
                normalized_source="# MySQL 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_publication_recovery_race",
                scope="overall",
                actor_user_id="user_sd5_mysql_publication_recovery_race",
                document_id="kdoc_sd5_mysql_publication_recovery_race",
                content_md="# MySQL 新正文",
                expected_revision=1,
                client_request_id="client_sd5_mysql_publication_recovery_race",
            ),
        )
        build_revision(db, job.id)

    original_claim = revision_module._claim_revision_publication
    original_run = revision_module.run_revision_job

    def claim_then_recover(current_db, current_job):
        """在 MySQL 真实发布权提交后验证活动租约，再安排过期接管。"""

        claimed = original_claim(current_db, current_job)
        assert claimed
        active_now = utc_now()
        with Session(engine) as active_recovery_db:
            active_recovered = revision_module.recover_revision_jobs(
                active_recovery_db,
                now=active_now,
            )
            assert active_recovered == []
            active_recovery_db.expire_all()
            active_job = active_recovery_db.get(KnowledgeIngestJob, current_job.id)
            assert active_job is not None and active_job.status == "publishing"

        stale_now = utc_now() + timedelta(seconds=61)

        def cancel_before_recovered_run(current_job_id: str) -> None:
            """在 MySQL 过期接管后取消任务，再运行旧发布者。"""

            with Session(engine) as cancel_db:
                cancelled = revision_module.cancel_revision(cancel_db, current_job_id)
                assert cancelled.status == "cancelled"
            original_run(current_job_id)

        with monkeypatch.context() as recovery_patch:
            recovery_patch.setattr(revision_module, "utc_now", lambda: stale_now)
            recovery_patch.setattr(
                revision_module,
                "run_revision_job",
                cancel_before_recovered_run,
            )
            with Session(engine) as recovery_db:
                recovered_count = scheduled_worker._recover_knowledge_revision_jobs(recovery_db)
            assert recovered_count == 1
        return claimed

    monkeypatch.setattr(
        revision_module,
        "_claim_revision_publication",
        claim_then_recover,
    )
    with Session(engine) as publisher_db:
        with pytest.raises(KnowledgeRevisionConflictError):
            publish_revision(publisher_db, job.id)

    with Session(engine) as db:
        stored_job = db.get(KnowledgeIngestJob, job.id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_mysql_publication_recovery_race")
        candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        candidate_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == job.candidate_version_id,
            )
        ).all()
        assert stored_job is not None and stored_job.status == "cancelled"
        assert candidate is not None and candidate.publication_state != "published"
        assert not candidate_documents
        assert knowledge_base is not None
        assert knowledge_base.content_revision == 1
        assert knowledge_base.metadata_json["current_version"] == "1.0.0"


def test_mysql_sd5_late_publisher_reads_back_the_completed_winner(
    mysql_database_url: str,
    monkeypatch,
) -> None:
    """验证 MySQL 迟到兼容发布者对账返回先完成的同一任务。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_competing_publisher"
    knowledge_base_id = "kb_sd5_mysql_competing_publisher"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL competing publisher"))
        db.add(User(
            id="user_sd5_mysql_competing_publisher",
            tenant_id=tenant_id,
            username="sd5-mysql-competing-publisher",
            role="admin",
            password_hash="test",
        ))
        db.add(KnowledgeBase(
            id=knowledge_base_id,
            tenant_id=tenant_id,
            name="Competing publisher KB",
            metadata_json={"current_version": "1.0.0"},
        ))
        db.add(KnowledgeBaseVersion(
            id="kbver_sd5_mysql_competing_publisher",
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            version="1.0.0",
            name="Competing publisher KB",
            publication_state="published",
        ))
        db.add(KnowledgeDocument(
            id="kdoc_sd5_mysql_competing_publisher",
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            knowledge_base_version_id="kbver_sd5_mysql_competing_publisher",
            lineage_id="lineage_sd5_mysql_competing_publisher",
            filename="winner.md",
            file_type="md",
            status="ready",
            normalized_source="# MySQL 旧正文\n已有事实",
        ))
        db.commit()
        job = prepare_revision(db, RevisionPrepareInput(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            scope="overall",
            actor_user_id="user_sd5_mysql_competing_publisher",
            document_id="kdoc_sd5_mysql_competing_publisher",
            content_md="# MySQL 新正文\n新事实",
            expected_revision=1,
            client_request_id="client_sd5_mysql_competing_publisher",
        ))
        build_revision(db, job.id)

    original_claim = revision_module._claim_revision_publication
    winner_statuses: list[str] = []

    def let_recovery_publisher_win(current_db, current_job):
        """在独立 MySQL 会话完成相同 job 后模拟迟到方的 CAS 失败。"""

        with patch.object(revision_module, "_claim_revision_publication", original_claim):
            with Session(engine) as recovery_db:
                winner = publish_revision(recovery_db, current_job.id)
                winner_statuses.append(winner.status)
        current_db.rollback()
        return False

    monkeypatch.setattr(
        revision_module,
        "_claim_revision_publication",
        let_recovery_publisher_win,
    )
    with Session(engine) as publisher_db:
        published = publish_revision(publisher_db, job.id)
        assert published.status == "completed"
        assert published.published_version_id

    with Session(engine) as db:
        stored_job = db.get(KnowledgeIngestJob, job.id)
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        versions = db.exec(select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
            KnowledgeBaseVersion.publication_state == "published",
        )).all()
        assert winner_statuses == ["completed"]
        assert stored_job is not None and stored_job.status == "completed"
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert len(versions) == 2


def test_mysql_sd5_expired_old_publisher_cannot_overwrite_completed_successor(
    mysql_database_url: str,
    monkeypatch,
) -> None:
    """验证 MySQL 过期旧 publisher 不能把新 publisher 的 completed 改回 conflict。"""

    from app.scheduled_tasks import worker as scheduled_worker

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_terminal_publisher_race"
    knowledge_base_id = "kb_sd5_mysql_terminal_publisher_race"
    source_version_id = "kbver_sd5_mysql_terminal_publisher_race"
    document_id = "kdoc_sd5_mysql_terminal_publisher_race"
    user_id = "user_sd5_mysql_terminal_publisher_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL terminal publisher race"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-mysql-terminal-publisher-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="MySQL terminal publisher race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="MySQL terminal publisher race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id="lineage_sd5_mysql_terminal_publisher_race",
                filename="mysql-terminal-publisher.md",
                file_type="md",
                status="ready",
                normalized_source="# MySQL 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                scope="overall",
                actor_user_id=user_id,
                document_id=document_id,
                content_md="# MySQL 新正文",
                expected_revision=1,
                client_request_id="client_sd5_mysql_terminal_publisher_race",
            ),
        )
        build_revision(db, job.id)

    original_claim = revision_module._claim_revision_publication
    clock = [utc_now()]

    def claim_then_handoff(current_db, current_job):
        """让 MySQL 旧 publisher 的租约过期，再用真实维护路径完成新 publisher。"""

        claimed = original_claim(current_db, current_job)
        assert claimed and current_job.lease_until is not None
        clock[0] = current_job.lease_until + timedelta(seconds=1)
        with monkeypatch.context() as handoff_patch:
            handoff_patch.setattr(revision_module, "engine", engine)
            handoff_patch.setattr(revision_module, "utc_now", lambda: clock[0])
            handoff_patch.setattr(
                revision_module,
                "_claim_revision_publication",
                original_claim,
            )
            with Session(engine) as maintenance_db:
                assert scheduled_worker._recover_knowledge_revision_jobs(maintenance_db) == 1
        return claimed

    monkeypatch.setattr(
        revision_module,
        "_claim_revision_publication",
        claim_then_handoff,
    )
    with Session(engine) as old_publisher_db:
        with pytest.raises(KnowledgeRevisionConflictError):
            publish_revision(old_publisher_db, job.id)

    with Session(engine) as db:
        stored_job = db.get(KnowledgeIngestJob, job.id)
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        candidate_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == job.candidate_version_id,
            )
        ).all()
        assert stored_job is not None and stored_job.status == "completed"
        assert stored_job.error_code is None
        assert candidate is not None and candidate.publication_state == "published"
        assert len(candidate_documents) == 1
        assert knowledge_base is not None and knowledge_base.content_revision == 2


def test_mysql_sd5_deadline_scan_cannot_overwrite_committed_cancel(
    mysql_database_url: str,
    monkeypatch,
) -> None:
    """验证 MySQL deadline 扫描不能把独立连接已提交的 cancelled 改成 failed。"""

    from app.scheduled_tasks import worker as scheduled_worker

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_terminal_scanner_race"
    knowledge_base_id = "kb_sd5_mysql_terminal_scanner_race"
    source_version_id = "kbver_sd5_mysql_terminal_scanner_race"
    document_id = "kdoc_sd5_mysql_terminal_scanner_race"
    user_id = "user_sd5_mysql_terminal_scanner_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL terminal scanner race"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-mysql-terminal-scanner-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="MySQL terminal scanner race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="MySQL terminal scanner race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id="lineage_sd5_mysql_terminal_scanner_race",
                filename="mysql-terminal-scanner.md",
                file_type="md",
                status="ready",
                normalized_source="# MySQL 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                scope="overall",
                actor_user_id=user_id,
                document_id=document_id,
                content_md="# MySQL 新正文",
                expected_revision=1,
                client_request_id="client_sd5_mysql_terminal_scanner_race",
            ),
        )
        build_revision(db, job.id)
        assert job.deadline_at is not None
        future = job.deadline_at + timedelta(seconds=1)

    original_mark_failed = revision_module._mark_revision_failed

    def cancel_after_scan_read(current_db, stale_job, error_code, message, current_time):
        """在 MySQL 扫描读到旧任务后提交真实取消，再执行旧失败结算。"""

        with Session(engine) as cancel_db:
            cancelled = revision_module.cancel_revision(cancel_db, stale_job.id)
            assert cancelled.status == "cancelled"
        original_mark_failed(current_db, stale_job, error_code, message, current_time)

    monkeypatch.setattr(revision_module, "utc_now", lambda: future)
    monkeypatch.setattr(
        revision_module,
        "_mark_revision_failed",
        cancel_after_scan_read,
    )
    with Session(engine) as scanner_db:
        scheduled_worker._recover_knowledge_revision_jobs(scanner_db)

    with Session(engine) as db:
        stored_job = db.get(KnowledgeIngestJob, job.id)
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        assert stored_job is not None and stored_job.status == "cancelled"
        assert candidate is not None and candidate.publication_state != "published"
        assert knowledge_base is not None and knowledge_base.content_revision == 1


@pytest.mark.parametrize("cancel_between", [False, True], ids=["running-recovery-control", "running-recovery-stale-scanner"])
def test_mysql_sd5_expired_running_scan_wakes_only_after_persistent_cas(
    mysql_database_url: str,
    monkeypatch,
    cancel_between: bool,
) -> None:
    """验证 MySQL 过期 running 仅在持久条件仍成立时回到 queued 并唤醒。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_running_recovery_race"
    knowledge_base_id = "kb_sd5_mysql_running_recovery_race"
    source_version_id = "kbver_sd5_mysql_running_recovery_race"
    document_id = "kdoc_sd5_mysql_running_recovery_race"
    user_id = "user_sd5_mysql_running_recovery_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL running recovery race"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-mysql-running-recovery-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="MySQL running recovery race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="MySQL running recovery race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id="lineage_sd5_mysql_running_recovery_race",
                filename="mysql-running-recovery.md",
                file_type="md",
                status="ready",
                normalized_source="# MySQL 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                scope="overall",
                actor_user_id=user_id,
                document_id=document_id,
                content_md="# MySQL 新正文",
                expected_revision=1,
                client_request_id="client_sd5_mysql_running_recovery_race",
            ),
        )
        claimed = revision_module.claim_revision_job(
            db,
            worker_id="sd5-mysql-running-recovery-worker",
            job_id=job.id,
        )
        assert claimed is not None and claimed.status == "running"
        assert claimed.lease_until is not None
        job_id = job.id
        candidate_id = job.candidate_version_id
        future = claimed.lease_until + timedelta(seconds=1)
        secondary_job_id = "job_sd5_mysql_running_recovery_secondary"
        db.add(
            KnowledgeIngestJob(
                id=secondary_job_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                filename="secondary-mysql-running-recovery.md",
                job_kind="revision",
                status="running",
                stage="build",
                lease_owner="secondary-mysql-running-worker",
                lease_until=claimed.lease_until - timedelta(seconds=1),
                fence=7,
                attempt_count=1,
                deadline_at=utc_now() + timedelta(minutes=5),
            )
        )
        db.commit()

    with Session(engine) as scanner_db:
        original_exec = scanner_db.exec
        select_seen = [False]

        def select_then_cancel(statement, *args, **kwargs):
            """暂停真实 MySQL 扫描读行，并按场景让独立连接先提交取消。"""

            result = original_exec(statement, *args, **kwargs)
            if select_seen[0]:
                return result
            select_seen[0] = True
            rows = result.all()
            assert {row.id for row in rows} == {job_id, secondary_job_id}
            assert all(row.status == "running" for row in rows)
            if cancel_between:
                with Session(engine) as cancel_db:
                    cancelled = revision_module.cancel_revision(cancel_db, job_id)
                    assert cancelled.status == "cancelled"
            return SimpleNamespace(all=lambda: rows)

        monkeypatch.setattr(revision_module, "utc_now", lambda: future)
        with patch.object(scanner_db, "exec", side_effect=select_then_cancel):
            wake_ids = revision_module.recover_revision_jobs(scanner_db)

    with Session(engine) as db:
        stored_job = db.get(KnowledgeIngestJob, job_id)
        secondary_job = db.get(KnowledgeIngestJob, secondary_job_id)
        candidate = db.get(KnowledgeBaseVersion, candidate_id)
        assert stored_job is not None
        assert secondary_job is not None and secondary_job.status == "queued"
        assert candidate is not None
        expected_status = "cancelled" if cancel_between else "queued"
        assert stored_job.status == expected_status
        assert stored_job.error_code == (
            "KNOWLEDGE_REVISION_CANCELLED" if cancel_between else None
        )
        assert stored_job.lease_owner is None and stored_job.lease_until is None
        assert candidate.publication_state != "published"
    expected_wake_ids = {secondary_job_id} if cancel_between else {job_id, secondary_job_id}
    assert set(wake_ids) == expected_wake_ids


def test_mysql_sd5_concurrent_claim_has_one_persistent_winner(
    mysql_database_url: str,
) -> None:
    """验证隔离 MySQL 两个并发 worker 只能由一个递增正文任务 fence。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_sd5_mysql_concurrent_claim"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 MySQL concurrent claim"))
        db.add(KnowledgeBase(id="kb_sd5_mysql_concurrent_claim", tenant_id=tenant_id, name="Concurrent claim KB"))
        db.add(
            KnowledgeIngestJob(
                id="job_sd5_mysql_concurrent_claim",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_mysql_concurrent_claim",
                filename="concurrent.md",
                job_kind="revision",
                status="queued",
                deadline_at=utc_now() + timedelta(minutes=5),
            )
        )
        db.commit()

    def claim(worker_id: str) -> str | None:
        """使用独立 MySQL 连接尝试领取同一持久任务。"""

        with Session(engine) as worker_db:
            claimed = revision_module.claim_revision_job(
                worker_db,
                worker_id=worker_id,
                job_id="job_sd5_mysql_concurrent_claim",
            )
            return claimed.id if claimed else None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ("worker_sd5_mysql_one", "worker_sd5_mysql_two")))

    assert sorted(item or "" for item in results) == ["", "job_sd5_mysql_concurrent_claim"]
    with Session(engine) as db:
        stored = db.get(KnowledgeIngestJob, "job_sd5_mysql_concurrent_claim")
        assert stored is not None and stored.status == "running"
        assert stored.fence == 1 and stored.attempt_count == 1
