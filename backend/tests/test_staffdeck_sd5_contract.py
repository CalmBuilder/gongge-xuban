"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_staffdeck_sd5_contract.py
@CallChain  : pytest → SD-5 知识修订模型／Alembic → 知识版本与持久任务合同
@Description: 以行为断言锁定 SD-5 首个子目标的持久版本、正文来源和构建任务字段。
"""

from dataclasses import replace
from datetime import timedelta
import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.db.models import (
    AgentProfile,
    AgentKnowledgeBranch,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Tenant,
    User,
    utc_now,
)
from app.agents.branching import (
    clone_knowledge_version_assets,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    promote_knowledge_branch_to_overall,
    rollback_knowledge_branch,
    sync_knowledge_branch_from_overall,
)
from app.api import knowledge as knowledge_api
from app.api import knowledge_bases as knowledge_bases_api
from app.api.knowledge import (
    cancel_job,
    get_job,
    get_revision_job_by_client_request,
    import_okf_bundle,
    list_documents,
    update_document,
)
from app.api.knowledge_bases import _ensure_okf_concepts_for_version
from app.knowledge.schema import (
    KnowledgeBaseUpdateRequest,
    KnowledgeDocumentUpdateRequest,
    KnowledgeConceptUpdateRequest,
    KnowledgeOkfImportRequest,
    KnowledgeRevisionAccepted,
)
from app.knowledge.revisions import (
    KnowledgeRevisionConflictError,
    RevisionPrepareInput,
    build_revision,
    normalize_revision_source,
    publish_revision,
    prepare_revision,
    run_compatibility_revision,
)
from app.knowledge import revisions as revision_module


def test_sd5_revision_contract_has_persistent_scope_and_source_fields() -> None:
    """验证版本 CAS、候选发布、正文 lineage 和构建任务身份不藏在 metadata 中。"""

    assert {
        "content_revision",
    }.issubset(KnowledgeBase.__table__.columns.keys())
    assert {
        "content_revision",
    }.issubset(AgentKnowledgeBranch.__table__.columns.keys())
    assert {
        "publication_state",
        "scope",
        "parent_version_id",
        "build_job_id",
        "content_manifest_json",
        "content_manifest_hash",
    }.issubset(KnowledgeBaseVersion.__table__.columns.keys())
    assert {
        "lineage_id",
        "parent_document_id",
        "normalized_source",
        "source_checksum",
        "source_kind",
    }.issubset(KnowledgeDocument.__table__.columns.keys())
    assert {
        "job_kind",
        "request_payload_json",
        "actor_user_id",
        "scope",
        "source_version_id",
        "expected_revision",
        "candidate_version_id",
        "client_request_id",
        "request_hash",
        "dedupe_key",
        "lease_owner",
        "lease_until",
        "fence",
        "attempt_count",
        "deadline_at",
        "error_code",
        "published_version_id",
        "published_document_id",
    }.issubset(KnowledgeIngestJob.__table__.columns.keys())


def test_sd5_revision_migration_is_chained_after_sd4() -> None:
    """验证首个 SD-5 schema 迁移存在且紧接 SD-4 的 0085。"""

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260928_0086_knowledge_revision_contract.py"
    )
    assert migration.exists()
    source = migration.read_text(encoding="utf-8")
    assert 'revision: str = "20260928_0086"' in source
    assert 'down_revision: str | None = "20260928_0085"' in source


def test_sd5_revision_migration_preserves_legacy_rows_and_dedupe_contract(tmp_path) -> None:
    """验证 0086 在 SQLite 历史表上可升级、回填 lineage 并保护任务同键冲突。"""

    database_url = f"sqlite:///{tmp_path / 'sd5-revision-contract.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260928_0085')"))
        connection.execute(
            text(
                "CREATE TABLE knowledge_bases ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "name VARCHAR(191) NOT NULL, revision INTEGER NOT NULL, "
                "status VARCHAR(64) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_base_versions ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "knowledge_base_id VARCHAR(512) NOT NULL, version VARCHAR(64) NOT NULL, "
                "name VARCHAR(191) NOT NULL, status VARCHAR(64) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_knowledge_branches ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "agent_id VARCHAR(128) NOT NULL, knowledge_base_id VARCHAR(512) NOT NULL, "
                "base_version VARCHAR(64) NOT NULL, head_version VARCHAR(64) NOT NULL, "
                "status VARCHAR(64) NOT NULL, sync_state VARCHAR(64) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_documents ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "knowledge_base_id VARCHAR(512) NOT NULL, filename VARCHAR(191) NOT NULL, "
                "file_type VARCHAR(64) NOT NULL, status VARCHAR(64) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_ingest_jobs ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "knowledge_base_id VARCHAR(512) NOT NULL, filename VARCHAR(191) NOT NULL, "
                "status VARCHAR(64) NOT NULL, stage VARCHAR(64) NOT NULL, "
                "progress FLOAT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_documents "
                "(id, tenant_id, knowledge_base_id, filename, file_type, status) "
                "VALUES ('doc_legacy', 'tenant_demo', 'kb_demo', 'same.md', 'md', 'ready')"
            )
        )

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260928_0086")

    with engine.begin() as connection:
        columns = {item["name"] for item in inspect(engine).get_columns("knowledge_documents")}
        assert {"lineage_id", "normalized_source", "source_checksum"}.issubset(columns)
        assert connection.execute(
            text("SELECT lineage_id FROM knowledge_documents WHERE id='doc_legacy'")
        ).scalar_one() == "doc_legacy"
        connection.execute(
            text(
                "INSERT INTO knowledge_ingest_jobs "
                "(id, tenant_id, knowledge_base_id, filename, status, stage, progress, dedupe_key) "
                "VALUES ('job_one', 'tenant_demo', 'kb_demo', 'same.md', 'queued', 'queued', 0, 'dup')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO knowledge_ingest_jobs "
                    "(id, tenant_id, knowledge_base_id, filename, status, stage, progress, dedupe_key) "
                    "VALUES ('job_two', 'tenant_demo', 'kb_demo', 'same.md', 'queued', 'queued', 0, 'dup')"
                )
            )

    command.downgrade(config, "20260928_0085")
    assert "lineage_id" not in {item["name"] for item in inspect(engine).get_columns("knowledge_documents")}


def test_sd5_prepare_normalizes_source_and_reuses_same_client_identity() -> None:
    """验证正文受理固定 source/CAS，候选不可见且同客户端重试不重复建版。"""

    assert normalize_revision_source("\ufeff# 标题\r\n\r\n正文  \r\n") == "# 标题\n\n正文"
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_prepare", name="SD5 prepare"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_prepare",
                tenant_id="tenant_sd5_prepare",
                name="Prepare KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_prepare",
                tenant_id="tenant_sd5_prepare",
                knowledge_base_id="kb_sd5_prepare",
                version="1.0.0",
                name="Prepare KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_prepare",
                tenant_id="tenant_sd5_prepare",
                knowledge_base_id="kb_sd5_prepare",
                knowledge_base_version_id="kbver_sd5_prepare",
                lineage_id="lineage_sd5_prepare",
                filename="policy.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文\n旧事实",
            )
        )
        db.commit()
        request = RevisionPrepareInput(
            tenant_id="tenant_sd5_prepare",
            knowledge_base_id="kb_sd5_prepare",
            scope="overall",
            actor_user_id="user_sd5_prepare",
            document_id="kdoc_sd5_prepare",
            content_md="# 新正文\n新事实",
            expected_revision=1,
            client_request_id="client_sd5_prepare",
        )

        first = prepare_revision(db, request)
        db.get(KnowledgeBase, "kb_sd5_prepare").content_revision = 9
        db.commit()
        second = prepare_revision(db, request)

        assert first.id == second.id
        assert first.job_kind == "revision"
        assert first.status == "queued"
        assert first.candidate_version_id
        assert first.source_version_id == "kbver_sd5_prepare"
        assert db.get(KnowledgeBase, "kb_sd5_prepare").content_revision == 9
        source_version = db.get(KnowledgeBaseVersion, "kbver_sd5_prepare")
        assert source_version is not None and source_version.publication_state == "published"
        candidate = db.get(KnowledgeBaseVersion, first.candidate_version_id)
        assert candidate is not None and candidate.publication_state == "candidate"
        assert candidate.parent_version_id == source_version.id

        with pytest.raises(KnowledgeRevisionConflictError):
            prepare_revision(db, replace(request, content_md="# 被篡改正文"))


def test_sd5_prepare_rejects_stale_cas_without_candidate_and_supports_agent_scope() -> None:
    """验证过期 CAS 不创建候选，并让 Agent 分支使用独立内容 revision。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_scope", name="SD5 scope"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_scope",
                tenant_id="tenant_sd5_scope",
                name="Scope KB",
                content_revision=3,
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_scope_overall",
                tenant_id="tenant_sd5_scope",
                knowledge_base_id="kb_sd5_scope",
                version="1.0.0",
                name="Scope KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_scope_overall",
                tenant_id="tenant_sd5_scope",
                knowledge_base_id="kb_sd5_scope",
                knowledge_base_version_id="kbver_sd5_scope_overall",
                lineage_id="lineage_sd5_scope_overall",
                filename="overall.md",
                file_type="md",
                status="ready",
            )
        )
        db.commit()
        stale = RevisionPrepareInput(
            tenant_id="tenant_sd5_scope",
            knowledge_base_id="kb_sd5_scope",
            scope="overall",
            actor_user_id="user_sd5_scope",
            document_id="kdoc_sd5_scope_overall",
            content_md="新正文",
            expected_revision=2,
            client_request_id="client_sd5_scope_stale",
        )
        with pytest.raises(KnowledgeRevisionConflictError):
            prepare_revision(db, stale)
        assert db.exec(select(KnowledgeIngestJob)).all() == []

        db.add(
            AgentKnowledgeBranch(
                id="agentkb_sd5_scope",
                tenant_id="tenant_sd5_scope",
                agent_id="agent_sd5_scope",
                knowledge_base_id="kb_sd5_scope",
                base_version="1.0.0",
                head_version="1.0.0",
                content_revision=7,
                status="active",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_scope_agent",
                tenant_id="tenant_sd5_scope",
                knowledge_base_id="kb_sd5_scope",
                knowledge_base_version_id="kbver_sd5_scope_overall",
                lineage_id="lineage_sd5_scope_agent",
                filename="agent.md",
                file_type="md",
                status="ready",
            )
        )
        db.commit()
        agent_job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_scope",
                knowledge_base_id="kb_sd5_scope",
                scope="agent:agent_sd5_scope",
                actor_user_id="user_sd5_scope",
                document_id="kdoc_sd5_scope_agent",
                content_md="Agent 新正文",
                expected_revision=7,
                client_request_id="client_sd5_scope_agent",
            ),
        )
        assert agent_job.scope == "agent:agent_sd5_scope"
        assert db.get(KnowledgeBase, "kb_sd5_scope").content_revision == 3


def test_sd5_build_reconstructs_candidate_snapshot_without_model() -> None:
    """验证整库候选复制和目标正文无模型重建不会混入旧版本物理引用。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_build", name="SD5 build"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_build",
                tenant_id="tenant_sd5_build",
                name="Build KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_build",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                version="1.0.0",
                name="Build KB",
                publication_state="published",
            )
        )
        common_document = {
            "section_tree": [
                {
                    "section_id": "section_one",
                    "path": "一、正文",
                    "title": "正文",
                    "content": "旧内容",
                    "summary": "旧摘要",
                }
            ],
            "document_card": {"title": "目标文档", "summary": "旧摘要"},
        }
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_build_target",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                lineage_id="lineage_sd5_build_target",
                filename="same.md",
                file_type="md",
                title="相同标题",
                status="ready",
                normalized_source="# 旧正文\n旧内容",
                source_kind="normalized",
                metadata_json=common_document,
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_build_other",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                lineage_id="lineage_sd5_build_other",
                filename="same.md",
                file_type="md",
                title="相同标题",
                status="ready",
                normalized_source="# 另一篇\n其他事实",
                source_kind="normalized",
                metadata_json={
                    **common_document,
                    "document_card": {"title": "另一篇", "summary": "其他摘要"},
                },
            )
        )
        db.add(
            KnowledgeBucket(
                id="kbucket_sd5_build_other",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                document_id="kdoc_sd5_build_other",
                bucket_key="other_topic",
                title="其他主题",
                summary="其他摘要",
                metadata_json={
                    "section_ids": ["section_one"],
                    "section_paths": ["一、正文"],
                    "content": "其他事实",
                    "representative_chunk_ids": ["kchunk_sd5_build_other"],
                },
            )
        )
        db.add(
            KnowledgeChunk(
                id="kchunk_sd5_build_other",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                document_id="kdoc_sd5_build_other",
                bucket_id="kbucket_sd5_build_other",
                chunk_index=0,
                content="其他事实",
                source_ref="same.md / 其他主题",
                metadata_json={
                    "related_chunk_ids": ["kchunk_sd5_build_other"],
                    "related_previous_chunk_id": None,
                    "related_next_chunk_id": None,
                },
            )
        )
        db.add(
            KnowledgeConcept(
                id="kconcept_sd5_build_target_manual",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                document_id="kdoc_sd5_build_target",
                concept_id="manual/target-note",
                concept_type="Topic",
                title="目标人工备注",
                content_md="---\norigin: manual\n---\n目标旧备注",
                frontmatter_json={"origin": "manual"},
                source_refs_json=[{"document_id": "kdoc_sd5_build_target"}],
            )
        )
        db.add(
            KnowledgeConcept(
                id="kconcept_sd5_build_target_old_generated",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                document_id="kdoc_sd5_build_target",
                concept_id="topics/old-target-topic",
                concept_type="Topic",
                title="旧目标主题",
                content_md="---\ntype: Topic\ntitle: 旧目标主题\n---\n旧目标事实",
                source_refs_json=[{"document_id": "kdoc_sd5_build_target"}],
            )
        )
        db.add(
            KnowledgeConcept(
                id="kconcept_sd5_build_other",
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                knowledge_base_version_id="kbver_sd5_build",
                document_id="kdoc_sd5_build_other",
                concept_id="topics/other-topic",
                concept_type="Topic",
                title="其他主题",
                content_md="---\ntype: Topic\ntitle: 其他主题\n---\n其他事实",
                source_refs_json=[
                    {"document_id": "kdoc_sd5_build_other", "bucket_id": "kbucket_sd5_build_other"}
                ],
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_build",
                knowledge_base_id="kb_sd5_build",
                scope="overall",
                actor_user_id="user_sd5_build",
                document_id="kdoc_sd5_build_target",
                content_md="# 新正文\n新事实不能丢",
                expected_revision=1,
                client_request_id="client_sd5_build",
            ),
        )

        built = build_revision(db, job.id)

        assert built.status == "ready_to_publish"
        candidate = db.get(KnowledgeBaseVersion, built.candidate_version_id)
        assert candidate is not None and candidate.publication_state == "candidate"
        assert db.get(KnowledgeBase, "kb_sd5_build").content_revision == 1
        candidate_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == candidate.id
            )
        ).all()
        assert len(candidate_documents) == 2
        target = next(row for row in candidate_documents if row.lineage_id == "lineage_sd5_build_target")
        other = next(row for row in candidate_documents if row.lineage_id == "lineage_sd5_build_other")
        assert target.id != "kdoc_sd5_build_target"
        assert target.normalized_source == "# 新正文\n新事实不能丢"
        assert other.normalized_source == "# 另一篇\n其他事实"
        candidate_buckets = db.exec(
            select(KnowledgeBucket).where(KnowledgeBucket.knowledge_base_version_id == candidate.id)
        ).all()
        candidate_chunks = db.exec(
            select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_version_id == candidate.id)
        ).all()
        manifest = candidate.content_manifest_json
        candidate_concepts = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == candidate.id
            )
        ).all()
        assert manifest["document_count"] == len(candidate_documents)
        assert manifest["bucket_count"] == len(candidate_buckets)
        assert manifest["chunk_count"] == len(candidate_chunks)
        assert manifest["concept_count"] == len(candidate_concepts)
        assert set(manifest["document_id_map"].values()) == {row.id for row in candidate_documents}
        assert set(manifest["bucket_id_map"].values()) <= {row.id for row in candidate_buckets}
        assert set(manifest["chunk_id_map"].values()) <= {row.id for row in candidate_chunks}
        assert {
            value for value in manifest["concept_id_map"].values() if value is not None
        } <= {row.id for row in candidate_concepts}
        assert "kconcept_sd5_build_target_old_generated" in manifest["concept_id_map"]
        assert manifest["concept_id_map"]["kconcept_sd5_build_target_old_generated"] is None
        assert candidate_buckets and candidate_chunks
        assert {row.document_id for row in candidate_buckets} == {target.id, other.id}
        assert all(row.document_id in {target.id, other.id} for row in candidate_chunks)
        copied_other = next(row for row in candidate_chunks if row.document_id == other.id)
        assert copied_other.id != "kchunk_sd5_build_other"
        assert copied_other.metadata_json["related_chunk_ids"] == [copied_other.id]
        target_manual = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == candidate.id,
                KnowledgeConcept.concept_id == "manual/target-note",
            )
        ).one()
        assert target_manual.content_md.endswith("目标旧备注")
        assert target_manual.status == "source_stale"
        assert revision_module._revision_manifest_is_publishable(db, job, candidate)
        invalid_bucket_map = dict(manifest["bucket_id_map"])
        invalid_bucket_map["kbucket_sd5_build_other"] = "kbucket_sd5_build_other"
        candidate.content_manifest_json = {
            **manifest,
            "bucket_id_map": invalid_bucket_map,
        }
        db.add(candidate)
        db.flush()
        assert not revision_module._revision_manifest_is_publishable(db, job, candidate)
        assert build_revision(db, job.id).id == job.id
        assert len(
            db.exec(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_version_id == candidate.id
                )
            ).all()
        ) == 2


def test_sd5_document_update_accepts_content_as_persistent_revision_job(monkeypatch) -> None:
    """验证正文更新入口只受理持久修订任务，并保留旧字段同步入口。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    enqueued: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "app.api.knowledge.enqueue_async_job",
        lambda *args, **kwargs: enqueued.append(args),
    )
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_api", name="SD5 API"))
        db.add(
            User(
                id="user_sd5_api",
                tenant_id="tenant_sd5_api",
                username="sd5-api",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_api",
                tenant_id="tenant_sd5_api",
                name="API KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_api",
                tenant_id="tenant_sd5_api",
                knowledge_base_id="kb_sd5_api",
                version="1.0.0",
                name="API KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_api",
                tenant_id="tenant_sd5_api",
                knowledge_base_id="kb_sd5_api",
                knowledge_base_version_id="kbver_sd5_api",
                lineage_id="lineage_sd5_api",
                filename="api.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as missing_version:
            update_document(
                "kdoc_sd5_api",
                KnowledgeDocumentUpdateRequest(
                    tenant_id="tenant_sd5_api",
                    content_md="# 缺少源版本",
                    expected_revision=1,
                    client_request_id="client_sd5_api_missing_version",
                ),
                db,
                User(id="user_sd5_api", tenant_id="tenant_sd5_api", username="sd5-api", role="admin"),
            )
        assert missing_version.value.status_code == 422

        accepted = update_document(
            "kdoc_sd5_api",
            KnowledgeDocumentUpdateRequest(
                tenant_id="tenant_sd5_api",
                content_md="# 新正文\n通过持久任务构建",
                expected_version_id="kbver_sd5_api",
                expected_revision=1,
                client_request_id="client_sd5_api",
            ),
            db,
            User(id="user_sd5_api", tenant_id="tenant_sd5_api", username="sd5-api", role="admin"),
        )

        assert isinstance(accepted, KnowledgeRevisionAccepted)
        assert accepted.status == "queued"
        assert accepted.job_id
        assert enqueued and enqueued[0][0] == "knowledge_revision"
        job = db.get(KnowledgeIngestJob, accepted.job_id)
        assert job is not None and job.job_kind == "revision"
        assert job.candidate_version_id
        assert db.get(KnowledgeBase, "kb_sd5_api").content_revision == 1

        retried = update_document(
            "kdoc_sd5_api",
            KnowledgeDocumentUpdateRequest(
                tenant_id="tenant_sd5_api",
                content_md="# 新正文\n通过持久任务构建",
                expected_version_id="kbver_sd5_api",
                expected_revision=1,
                client_request_id="client_sd5_api",
            ),
            db,
            User(id="user_sd5_api", tenant_id="tenant_sd5_api", username="sd5-api", role="admin"),
        )
        assert isinstance(retried, KnowledgeRevisionAccepted)
        assert retried.job_id == accepted.job_id
        assert len(enqueued) == 1

        with pytest.raises(Exception) as conflict:
            update_document(
                "kdoc_sd5_api",
                KnowledgeDocumentUpdateRequest(
                    tenant_id="tenant_sd5_api",
                    content_md="# 另一个正文",
                    expected_version_id="kbver_sd5_api",
                    expected_revision=1,
                    client_request_id="client_sd5_api",
                ),
                db,
                User(id="user_sd5_api", tenant_id="tenant_sd5_api", username="sd5-api", role="admin"),
            )
        assert getattr(conflict.value, "status_code", None) == 409


def test_sd5_legacy_document_fields_publish_a_new_snapshot_when_source_is_available() -> None:
    """验证旧标题字段入口也通过候选发布，不原地改写当前已发布快照。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            id="user_sd5_legacy_fields",
            tenant_id="tenant_sd5_legacy_fields",
            username="legacy-fields",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_sd5_legacy_fields", name="SD5 legacy fields"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_legacy_fields",
                tenant_id="tenant_sd5_legacy_fields",
                name="Legacy fields KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_legacy_fields",
                tenant_id="tenant_sd5_legacy_fields",
                knowledge_base_id="kb_sd5_legacy_fields",
                version="1.0.0",
                name="Legacy fields KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_legacy_fields",
                tenant_id="tenant_sd5_legacy_fields",
                knowledge_base_id="kb_sd5_legacy_fields",
                knowledge_base_version_id="kbver_sd5_legacy_fields",
                lineage_id="lineage_sd5_legacy_fields",
                filename="legacy.md",
                file_type="md",
                title="旧标题",
                status="ready",
                normalized_source="# 正文\n不可丢失的事实",
            )
        )
        db.commit()

        updated = update_document(
            "kdoc_sd5_legacy_fields",
            KnowledgeDocumentUpdateRequest(
                tenant_id="tenant_sd5_legacy_fields",
                title="新标题",
                metadata={"custom_label": "new"},
            ),
            db,
            user,
        )

        assert updated.id != "kdoc_sd5_legacy_fields"
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_legacy_fields")
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert knowledge_base.metadata_json["current_version"] != "1.0.0"
        assert db.get(KnowledgeDocument, "kdoc_sd5_legacy_fields").title == "旧标题"
        current_document = db.get(KnowledgeDocument, updated.id)
        assert current_document is not None and current_document.title == "新标题"
        assert current_document.metadata_json["custom_label"] == "new"


def test_sd5_bucket_update_publishes_candidate_instead_of_mutating_current_asset() -> None:
    """验证旧分桶编辑入口在有正文时也创建新物理分桶并推进内容 CAS。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            id="user_sd5_bucket_fields",
            tenant_id="tenant_sd5_bucket_fields",
            username="bucket-fields",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_sd5_bucket_fields", name="SD5 bucket fields"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_bucket_fields",
                tenant_id="tenant_sd5_bucket_fields",
                name="Bucket fields KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_bucket_fields",
                tenant_id="tenant_sd5_bucket_fields",
                knowledge_base_id="kb_sd5_bucket_fields",
                version="1.0.0",
                name="Bucket fields KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_bucket_fields",
                tenant_id="tenant_sd5_bucket_fields",
                knowledge_base_id="kb_sd5_bucket_fields",
                knowledge_base_version_id="kbver_sd5_bucket_fields",
                lineage_id="lineage_sd5_bucket_fields",
                filename="bucket.md",
                file_type="md",
                title="桶文档",
                status="ready",
                normalized_source="# 正文\n桶事实",
            )
        )
        db.add(
            KnowledgeBucket(
                id="kbucket_sd5_bucket_fields",
                tenant_id="tenant_sd5_bucket_fields",
                knowledge_base_id="kb_sd5_bucket_fields",
                knowledge_base_version_id="kbver_sd5_bucket_fields",
                document_id="kdoc_sd5_bucket_fields",
                bucket_key="bucket",
                title="旧桶标题",
                summary="旧摘要",
            )
        )
        db.add(
            KnowledgeChunk(
                id="kchunk_sd5_bucket_fields",
                tenant_id="tenant_sd5_bucket_fields",
                knowledge_base_id="kb_sd5_bucket_fields",
                knowledge_base_version_id="kbver_sd5_bucket_fields",
                document_id="kdoc_sd5_bucket_fields",
                bucket_id="kbucket_sd5_bucket_fields",
                chunk_index=0,
                content="旧桶事实",
                summary="旧分块摘要",
            )
        )
        db.commit()

        updated = knowledge_api.update_bucket(
            "kbucket_sd5_bucket_fields",
            knowledge_api.KnowledgeBucketUpdateRequest(
                tenant_id="tenant_sd5_bucket_fields",
                title="新桶标题",
                summary="新摘要",
            ),
            db,
            user,
        )

        assert updated.id != "kbucket_sd5_bucket_fields"
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_bucket_fields")
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert db.get(KnowledgeBucket, "kbucket_sd5_bucket_fields").title == "旧桶标题"
        current_bucket = db.get(KnowledgeBucket, updated.id)
        assert current_bucket is not None and current_bucket.title == "新桶标题"
        current_chunk = db.exec(
            select(KnowledgeChunk).where(KnowledgeChunk.bucket_id == updated.id)
        ).one()
        updated_chunk = knowledge_api.update_chunk(
            current_chunk.id,
            knowledge_api.KnowledgeChunkUpdateRequest(
                tenant_id="tenant_sd5_bucket_fields",
                content="新桶事实",
                summary="新分块摘要",
            ),
            db,
            user,
        )
        assert updated_chunk.id != current_chunk.id
        assert db.get(KnowledgeChunk, current_chunk.id).content == "桶事实"
        assert db.get(KnowledgeChunk, updated_chunk.id).content == "新桶事实"


def test_sd5_upload_with_existing_head_is_queued_as_revision_candidate(monkeypatch) -> None:
    """验证上传兼容入口在非空知识库中只创建修订任务，不直接追加到 published head。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    enqueued: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "app.api.knowledge.enqueue_async_job",
        lambda *args, **kwargs: enqueued.append(args),
    )
    with Session(engine) as db:
        user = User(
            id="user_sd5_upload",
            tenant_id="tenant_sd5_upload",
            username="upload-user",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_sd5_upload", name="SD5 upload"))
        db.add(user)
        db.add(
            AgentProfile(
                id="agent_sd5_upload_overall",
                tenant_id="tenant_sd5_upload",
                name="整体助手",
                is_overall=True,
                status="active",
                published_to_gallery=True,
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_upload",
                tenant_id="tenant_sd5_upload",
                name="Upload KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_upload",
                tenant_id="tenant_sd5_upload",
                knowledge_base_id="kb_sd5_upload",
                version="1.0.0",
                name="Upload KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_upload_old",
                tenant_id="tenant_sd5_upload",
                knowledge_base_id="kb_sd5_upload",
                knowledge_base_version_id="kbver_sd5_upload",
                lineage_id="lineage_sd5_upload_old",
                filename="old.md",
                file_type="md",
                title="旧文档",
                status="ready",
                normalized_source="# 旧文档\n旧事实",
            )
        )
        db.flush()
        ensure_open_gallery_binding(
            db,
            "tenant_sd5_upload",
            "knowledge_base",
            "kb_sd5_upload",
        )
        db.commit()

        response = knowledge_api.upload_document(
            knowledge_api.KnowledgeDocumentUploadRequest(
                tenant_id="tenant_sd5_upload",
                knowledge_base_id="kb_sd5_upload",
                filename="new.md",
                title="新文档",
                content_base64=base64.b64encode("# 新文档\n新事实".encode()).decode(),
            ),
            agent_id=None,
            db=db,
            current_user=user,
        )

        assert response.job_kind == "revision"
        assert enqueued and enqueued[0][0] == "knowledge_revision"
        job = db.get(KnowledgeIngestJob, response.id)
        assert job is not None and job.status == "queued"
        assert db.get(KnowledgeBase, "kb_sd5_upload").content_revision == 1
        revision_module.build_revision(db, job.id)
        revision_module.publish_revision(db, job.id)
        db.refresh(job)
        assert job.published_document_id
        uploaded = db.get(KnowledgeDocument, job.published_document_id)
        assert uploaded is not None and uploaded.normalized_source == "# 新文档\n新事实"
        assert db.get(KnowledgeDocument, "kdoc_sd5_upload_old").normalized_source == "# 旧文档\n旧事实"
        concept = knowledge_bases_api.upsert_okf_concept(
            "kb_sd5_upload",
            "manual/upload-note",
            knowledge_bases_api.KnowledgeConceptUpdateRequest(
                tenant_id="tenant_sd5_upload",
                document_id=uploaded.id,
                content_md="---\ntype: Topic\ntitle: 上传备注\n---\n上传后的人工备注",
            ),
            agent_id=None,
            db=db,
            current_user=user,
        )
        assert concept.concept_id == "manual/upload-note"
        assert concept.knowledge_base_version_id != job.published_version_id
        assert db.get(KnowledgeBase, "kb_sd5_upload").content_revision == 3


def test_sd5_first_upload_into_empty_head_uses_revision_candidate(monkeypatch) -> None:
    """验证空知识库首次上传也经过持久 candidate，而不是向 published head 直写。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    enqueued: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "app.api.knowledge.enqueue_async_job",
        lambda *args, **kwargs: enqueued.append(args),
    )
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_initial_upload", name="SD5 initial upload"))
        user = User(
            id="user_sd5_initial_upload",
            tenant_id="tenant_sd5_initial_upload",
            username="initial-upload",
            role="admin",
            password_hash="test",
        )
        db.add(user)
        db.add(
            AgentProfile(
                id="agent_sd5_initial_overall",
                tenant_id="tenant_sd5_initial_upload",
                name="Overall",
                is_overall=True,
                status="active",
                published_to_gallery=True,
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_initial_upload",
                tenant_id="tenant_sd5_initial_upload",
                name="Initial upload KB",
                metadata_json={
                    "current_version": "1.0.0",
                    "scope": "open_gallery",
                    "visibility": "open_gallery",
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_initial_upload",
                tenant_id="tenant_sd5_initial_upload",
                knowledge_base_id="kb_sd5_initial_upload",
                version="1.0.0",
                name="Initial upload KB",
                publication_state="published",
            )
        )
        ensure_open_gallery_binding(
            db,
            "tenant_sd5_initial_upload",
            "knowledge_base",
            "kb_sd5_initial_upload",
            "active",
        )
        db.commit()

        job_read = knowledge_api.upload_document(
            knowledge_api.KnowledgeDocumentUploadRequest(
                tenant_id="tenant_sd5_initial_upload",
                knowledge_base_id="kb_sd5_initial_upload",
                filename="initial.md",
                title="Initial",
                content_base64=base64.b64encode(b"# Initial\nfirst fact").decode("ascii"),
            ),
            agent_id=None,
            db=db,
            current_user=user,
        )
        job = db.get(KnowledgeIngestJob, job_read.id)
        assert job is not None and job.job_kind == "revision"
        assert enqueued and enqueued[0][0] == "knowledge_revision"
        assert db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == "kbver_sd5_initial_upload"
            )
        ).all() == []

        revision_module.build_revision(db, job.id)
        published = revision_module.publish_revision(db, job.id)
        assert published.status == "completed"
        assert published.published_document_id
        assert db.get(KnowledgeBase, "kb_sd5_initial_upload").content_revision == 2


def test_sd5_first_okf_import_into_empty_head_uses_revision_candidate() -> None:
    """验证空知识库首次 OKF 导入也通过同一候选发布边界。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_initial_okf"
        knowledge_base_id = "kb_sd5_initial_okf"
        user = User(
            id="user_sd5_initial_okf",
            tenant_id=tenant_id,
            username="initial-okf",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id=tenant_id, name="SD5 initial OKF"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Initial OKF KB",
                metadata_json={
                    "current_version": "1.0.0",
                    "scope": "open_gallery",
                    "visibility": "open_gallery",
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_initial_okf",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Initial OKF KB",
                publication_state="published",
            )
        )
        ensure_open_gallery_binding(
            db,
            tenant_id,
            "knowledge_base",
            knowledge_base_id,
            "active",
        )
        db.commit()

        result = import_okf_bundle(
            KnowledgeOkfImportRequest(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                filename="initial.md",
                content_base64=base64.b64encode(
                    b"---\ntype: Topic\ntitle: Initial\n---\n\n# Initial\nFirst imported fact"
                ).decode("ascii"),
            ),
            db,
            user,
        )

        assert result["status"] == "imported"
        assert result["knowledge_base_version_id"] != "kbver_sd5_initial_okf"
        assert result["document_id"]
        assert db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == result["knowledge_base_version_id"]
            )
        ).all()
        assert db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == result["knowledge_base_version_id"]
            )
        ).all()
        assert db.get(KnowledgeBase, knowledge_base_id).content_revision == 2


def test_sd5_first_manual_okf_write_into_empty_head_uses_revision_candidate() -> None:
    """验证空知识库的手工 OKF 写入不直接修改已发布 head。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_initial_manual_okf"
        knowledge_base_id = "kb_sd5_initial_manual_okf"
        user = User(
            id="user_sd5_initial_manual_okf",
            tenant_id=tenant_id,
            username="initial-manual-okf",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id=tenant_id, name="SD5 initial manual OKF"))
        db.add(user)
        db.add(
            AgentProfile(
                id="agent_sd5_initial_manual_okf_overall",
                tenant_id=tenant_id,
                name="Overall",
                is_overall=True,
                status="active",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Initial manual OKF KB",
                metadata_json={
                    "current_version": "1.0.0",
                    "scope": "open_gallery",
                    "visibility": "open_gallery",
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_initial_manual_okf",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Initial manual OKF KB",
                publication_state="published",
            )
        )
        ensure_open_gallery_binding(
            db,
            tenant_id,
            "knowledge_base",
            knowledge_base_id,
            "active",
        )
        db.commit()

        result = knowledge_bases_api.upsert_okf_concept(
            knowledge_base_id,
            "manual/first-note",
            KnowledgeConceptUpdateRequest(
                tenant_id=tenant_id,
                content_md="---\ntype: Topic\ntitle: First note\n---\n\nFirst manual fact",
            ),
            agent_id=None,
            db=db,
            current_user=user,
        )

        assert result.knowledge_base_version_id != "kbver_sd5_initial_manual_okf"
        assert result.concept_id == "manual/first-note"
        assert db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == "kbver_sd5_initial_manual_okf"
            )
        ).all() == []
        assert db.get(KnowledgeBase, knowledge_base_id).content_revision == 2


def test_sd5_historical_okf_document_identity_never_falls_back_by_filename() -> None:
    """验证历史 OKF 文档在同名多文档 head 中不能按 filename 猜当前物理身份。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_okf_identity"
        knowledge_base_id = "kb_sd5_okf_identity"
        db.add(Tenant(id=tenant_id, name="SD5 OKF identity"))
        db.add(KnowledgeBase(id=knowledge_base_id, tenant_id=tenant_id, name="Identity KB"))
        db.add_all(
            [
                KnowledgeBaseVersion(
                    id="kbver_sd5_okf_identity_old",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    version="1.0.0",
                    name="Identity KB",
                    publication_state="published",
                ),
                KnowledgeBaseVersion(
                    id="kbver_sd5_okf_identity_current",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    version="2.0.0",
                    name="Identity KB",
                    publication_state="published",
                ),
            ]
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_okf_identity_old",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id="kbver_sd5_okf_identity_old",
                lineage_id="lineage_sd5_okf_identity_old",
                filename="同名.md",
                file_type="md",
                title="同名",
                status="ready",
            )
        )
        db.add_all(
            [
                KnowledgeDocument(
                    id=f"kdoc_sd5_okf_identity_current_{index}",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id="kbver_sd5_okf_identity_current",
                    lineage_id=f"lineage_sd5_okf_identity_current_{index}",
                    filename="同名.md",
                    file_type="md",
                    title="同名",
                    status="ready",
                )
                for index in (1, 2)
            ]
        )
        db.commit()

        with pytest.raises(HTTPException) as error:
            knowledge_bases_api._document_id_for_version(
                db,
                tenant_id,
                knowledge_base_id,
                "kbver_sd5_okf_identity_current",
                "kdoc_sd5_okf_identity_old",
            )
        assert error.value.status_code == 409


def test_sd5_build_maps_target_bucket_chunk_and_manual_concept_references() -> None:
    """验证目标文档重建也映射旧桶、旧分块及人工概念的物理引用闭包。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_target_refs", name="SD5 target refs"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_target_refs",
                tenant_id="tenant_sd5_target_refs",
                name="Target refs KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_target_refs",
                tenant_id="tenant_sd5_target_refs",
                knowledge_base_id="kb_sd5_target_refs",
                version="1.0.0",
                name="Target refs KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_target_refs",
                tenant_id="tenant_sd5_target_refs",
                knowledge_base_id="kb_sd5_target_refs",
                knowledge_base_version_id="kbver_sd5_target_refs",
                lineage_id="lineage_sd5_target_refs",
                filename="target.md",
                file_type="md",
                title="目标文档",
                status="ready",
                normalized_source="# 旧正文\n旧事实",
                metadata_json={
                    "section_tree": [
                        {
                            "section_id": "target_intro",
                            "path": "介绍",
                            "title": "介绍",
                            "content": "旧事实",
                        }
                    ]
                },
            )
        )
        db.add(
            KnowledgeBucket(
                id="kbucket_sd5_target_refs",
                tenant_id="tenant_sd5_target_refs",
                knowledge_base_id="kb_sd5_target_refs",
                knowledge_base_version_id="kbver_sd5_target_refs",
                document_id="kdoc_sd5_target_refs",
                bucket_key="target_intro",
                title="介绍",
                summary="旧摘要",
                metadata_json={"representative_chunk_ids": ["kchunk_sd5_target_refs"]},
            )
        )
        db.add(
            KnowledgeChunk(
                id="kchunk_sd5_target_refs",
                tenant_id="tenant_sd5_target_refs",
                knowledge_base_id="kb_sd5_target_refs",
                knowledge_base_version_id="kbver_sd5_target_refs",
                document_id="kdoc_sd5_target_refs",
                bucket_id="kbucket_sd5_target_refs",
                chunk_index=0,
                content="旧事实",
                source_ref="kdoc_sd5_target_refs/kbucket_sd5_target_refs/kchunk_sd5_target_refs",
                metadata_json={"related_chunk_ids": ["kchunk_sd5_target_refs"]},
            )
        )
        db.add(
            KnowledgeConcept(
                id="kconcept_sd5_target_refs",
                tenant_id="tenant_sd5_target_refs",
                knowledge_base_id="kb_sd5_target_refs",
                knowledge_base_version_id="kbver_sd5_target_refs",
                document_id="kdoc_sd5_target_refs",
                concept_id="manual/target-refs",
                concept_type="Topic",
                title="人工目标备注",
                content_md="---\norigin: manual\n---\n旧备注",
                frontmatter_json={"origin": "manual"},
                source_refs_json=[
                    {
                        "document_id": "kdoc_sd5_target_refs",
                        "bucket_id": "kbucket_sd5_target_refs",
                        "chunk_id": "kchunk_sd5_target_refs",
                    }
                ],
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_target_refs",
                knowledge_base_id="kb_sd5_target_refs",
                scope="overall",
                actor_user_id="user_sd5_target_refs",
                document_id="kdoc_sd5_target_refs",
                content_md="# 新正文\n新事实",
                expected_revision=1,
                client_request_id="client_sd5_target_refs",
            ),
        )

        built = build_revision(db, job.id)
        manifest = db.get(KnowledgeBaseVersion, built.candidate_version_id).content_manifest_json
        assert manifest["bucket_id_map"]["kbucket_sd5_target_refs"]
        assert manifest["chunk_id_map"]["kchunk_sd5_target_refs"]
        assert manifest["concept_id_map"]["kconcept_sd5_target_refs"]
        candidate_concept = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == built.candidate_version_id,
                KnowledgeConcept.concept_id == "manual/target-refs",
            )
        ).one()
        reference_text = json.dumps(candidate_concept.source_refs_json, ensure_ascii=False)
        assert "kdoc_sd5_target_refs" not in reference_text
        assert "kbucket_sd5_target_refs" not in reference_text
        assert "kchunk_sd5_target_refs" not in reference_text


def test_sd5_publish_promotes_ready_candidate_with_scope_cas() -> None:
    """验证 ready candidate 只在发布 CAS 成功后替换 overall head。"""

    assert callable(getattr(revision_module, "publish_revision", None))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_publish", name="SD5 publish"))
        db.add(
            User(
                id="user_sd5_publish",
                tenant_id="tenant_sd5_publish",
                username="publish-user",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_publish",
                tenant_id="tenant_sd5_publish",
                name="Publish KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_publish",
                tenant_id="tenant_sd5_publish",
                knowledge_base_id="kb_sd5_publish",
                version="1.0.0",
                name="Publish KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_publish",
                tenant_id="tenant_sd5_publish",
                knowledge_base_id="kb_sd5_publish",
                knowledge_base_version_id="kbver_sd5_publish",
                lineage_id="lineage_sd5_publish",
                filename="publish.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_publish",
                knowledge_base_id="kb_sd5_publish",
                scope="overall",
                actor_user_id="user_sd5_publish",
                document_id="kdoc_sd5_publish",
                content_md="# 已发布新正文",
                expected_revision=1,
                client_request_id="client_sd5_publish",
            ),
        )
        build_revision(db, job.id)

        published = revision_module.publish_revision(db, job.id)

        assert published.status == "completed"
        assert published.published_version_id
        assert published.published_document_id
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_publish")
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert knowledge_base.metadata_json["current_version"] != "1.0.0"
        candidate = db.get(KnowledgeBaseVersion, published.published_version_id)
        assert candidate is not None and candidate.publication_state == "published"
        assert candidate.version == knowledge_base.metadata_json["current_version"]
        assert candidate.content_manifest_hash == sha256(
            json.dumps(
                candidate.content_manifest_json,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert db.get(KnowledgeBaseVersion, "kbver_sd5_publish").publication_state == "published"


def test_sd5_publish_cas_conflict_keeps_old_head_and_candidate_hidden() -> None:
    """验证发布前内容 revision 竞争进入 conflict，而不是覆盖别人的 head。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_publish_conflict", name="SD5 conflict"))
        db.add(
            User(
                id="user_sd5_publish_conflict",
                tenant_id="tenant_sd5_publish_conflict",
                username="publish-conflict-user",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_publish_conflict",
                tenant_id="tenant_sd5_publish_conflict",
                name="Conflict KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_publish_conflict",
                tenant_id="tenant_sd5_publish_conflict",
                knowledge_base_id="kb_sd5_publish_conflict",
                version="1.0.0",
                name="Conflict KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_publish_conflict",
                tenant_id="tenant_sd5_publish_conflict",
                knowledge_base_id="kb_sd5_publish_conflict",
                knowledge_base_version_id="kbver_sd5_publish_conflict",
                lineage_id="lineage_sd5_publish_conflict",
                filename="conflict.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_publish_conflict",
                knowledge_base_id="kb_sd5_publish_conflict",
                scope="overall",
                actor_user_id="user_sd5_publish_conflict",
                document_id="kdoc_sd5_publish_conflict",
                content_md="# 竞争正文",
                expected_revision=1,
                client_request_id="client_sd5_publish_conflict",
            ),
        )
        build_revision(db, job.id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_publish_conflict")
        assert knowledge_base is not None
        knowledge_base.content_revision = 2
        db.commit()

        with pytest.raises(KnowledgeRevisionConflictError):
            revision_module.publish_revision(db, job.id)

        db.refresh(job)
        assert job.status == "conflict"
        assert job.error_code == "KNOWLEDGE_REVISION_CAS_CONFLICT"
        assert db.get(KnowledgeBase, "kb_sd5_publish_conflict").metadata_json["current_version"] == "1.0.0"
        candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        assert candidate is not None and candidate.publication_state == "candidate"


def test_sd5_agent_scope_publish_advances_only_agent_head() -> None:
    """验证 Agent 私有正文发布不自动升级 overall，也使用独立内容 revision。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_agent_publish", name="SD5 agent publish"))
        db.add(
            User(
                id="user_sd5_agent_publish",
                tenant_id="tenant_sd5_agent_publish",
                username="agent-publish-user",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_agent_publish",
                tenant_id="tenant_sd5_agent_publish",
                name="Agent KB",
                content_revision=4,
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_agent_publish",
                tenant_id="tenant_sd5_agent_publish",
                knowledge_base_id="kb_sd5_agent_publish",
                version="1.0.0",
                name="Agent KB",
                publication_state="published",
            )
        )
        db.add(
            AgentKnowledgeBranch(
                id="agentkb_sd5_agent_publish",
                tenant_id="tenant_sd5_agent_publish",
                agent_id="agent_sd5_agent_publish",
                knowledge_base_id="kb_sd5_agent_publish",
                head_version="1.0.0",
                base_version="1.0.0",
                content_revision=6,
                status="active",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_agent_publish",
                tenant_id="tenant_sd5_agent_publish",
                knowledge_base_id="kb_sd5_agent_publish",
                knowledge_base_version_id="kbver_sd5_agent_publish",
                lineage_id="lineage_sd5_agent_publish",
                filename="agent.md",
                file_type="md",
                status="ready",
                normalized_source="# Agent 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_agent_publish",
                knowledge_base_id="kb_sd5_agent_publish",
                scope="agent:agent_sd5_agent_publish",
                actor_user_id="user_sd5_agent_publish",
                document_id="kdoc_sd5_agent_publish",
                content_md="# Agent 新正文",
                expected_revision=6,
                client_request_id="client_sd5_agent_publish",
            ),
        )
        build_revision(db, job.id)
        published = revision_module.publish_revision(db, job.id)

        branch = db.get(AgentKnowledgeBranch, "agentkb_sd5_agent_publish")
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_agent_publish")
        assert published.status == "completed"
        assert branch is not None and branch.content_revision == 7
        assert branch.head_version == db.get(KnowledgeBaseVersion, published.published_version_id).version
        assert knowledge_base is not None
        assert knowledge_base.content_revision == 4
        assert knowledge_base.metadata_json["current_version"] == "1.0.0"


def test_sd5_persistent_runner_builds_and_publishes_after_queue_wakeup(monkeypatch) -> None:
    """验证进程队列只唤醒持久任务，独立会话完成 build 与 publish。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_runner", name="SD5 runner"))
        db.add(
            User(
                id="user_sd5_runner",
                tenant_id="tenant_sd5_runner",
                username="runner-user",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_runner",
                tenant_id="tenant_sd5_runner",
                name="Runner KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_runner",
                tenant_id="tenant_sd5_runner",
                knowledge_base_id="kb_sd5_runner",
                version="1.0.0",
                name="Runner KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_runner",
                tenant_id="tenant_sd5_runner",
                knowledge_base_id="kb_sd5_runner",
                knowledge_base_version_id="kbver_sd5_runner",
                lineage_id="lineage_sd5_runner",
                filename="runner.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_runner",
                knowledge_base_id="kb_sd5_runner",
                scope="overall",
                actor_user_id="user_sd5_runner",
                document_id="kdoc_sd5_runner",
                content_md="# runner 新正文",
                expected_revision=1,
                client_request_id="client_sd5_runner",
            ),
        )
        monkeypatch.setattr(revision_module, "engine", engine)

        revision_module.run_revision_job(job.id)

        db.expire_all()
        completed = db.get(KnowledgeIngestJob, job.id)
        assert completed is not None and completed.status == "completed"
        assert completed.published_version_id
        assert db.get(KnowledgeBase, "kb_sd5_runner").content_revision == 2


def test_sd5_revision_claim_and_recovery_use_fence_and_bounded_attempts() -> None:
    """验证持久 scanner 只接管过期任务，超过预算后不再把 running 复活。"""

    assert callable(getattr(revision_module, "claim_revision_job", None))
    assert callable(getattr(revision_module, "recover_revision_jobs", None))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_claim", name="SD5 claim"))
        db.add(KnowledgeBase(id="kb_sd5_claim", tenant_id="tenant_sd5_claim", name="Claim KB"))
        db.add(
            KnowledgeIngestJob(
                id="job_sd5_claim",
                tenant_id="tenant_sd5_claim",
                knowledge_base_id="kb_sd5_claim",
                filename="claim.md",
                job_kind="revision",
                status="queued",
                stage="prepare",
                deadline_at=utc_now() + timedelta(minutes=5),
            )
        )
        db.commit()

        claimed = revision_module.claim_revision_job(
            db,
            worker_id="worker_sd5_claim",
            job_id="job_sd5_claim",
        )
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.fence == 1
        assert claimed.attempt_count == 1
        assert revision_module.claim_revision_job(
            db,
            worker_id="worker_sd5_other",
            job_id="job_sd5_claim",
        ) is None

        claimed.lease_until = utc_now() - timedelta(seconds=1)
        db.add(claimed)
        db.commit()
        recovered = revision_module.recover_revision_jobs(db, now=utc_now())
        assert recovered == ["job_sd5_claim"]
        reclaimed = revision_module.claim_revision_job(
            db,
            worker_id="worker_sd5_other",
            job_id="job_sd5_claim",
        )
        assert reclaimed is not None and reclaimed.fence == 2

        reclaimed.lease_until = utc_now() - timedelta(seconds=1)
        reclaimed.attempt_count = 3
        db.add(reclaimed)
        db.commit()
        assert revision_module.recover_revision_jobs(db, now=utc_now()) == []
        db.refresh(reclaimed)
        assert reclaimed.status == "failed"
        assert reclaimed.error_code == "KNOWLEDGE_REVISION_RETRY_EXHAUSTED"


def test_sd5_concurrent_claim_has_one_persistent_winner(tmp_path) -> None:
    """验证两个独立 SQLite worker 同时接管同一任务时只有一个递增 fence。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-concurrent-claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_concurrent_claim", name="SD5 concurrent claim"))
        db.add(KnowledgeBase(id="kb_sd5_concurrent_claim", tenant_id="tenant_sd5_concurrent_claim", name="Concurrent claim KB"))
        db.add(
            KnowledgeIngestJob(
                id="job_sd5_concurrent_claim",
                tenant_id="tenant_sd5_concurrent_claim",
                knowledge_base_id="kb_sd5_concurrent_claim",
                filename="concurrent.md",
                job_kind="revision",
                status="queued",
                deadline_at=utc_now() + timedelta(minutes=5),
            )
        )
        db.commit()

    barrier = Barrier(2)
    select_lock = Lock()
    select_count = 0

    def pause_after_claim_select(_conn, _cursor, statement, _parameters, _context, _executemany):
        """让两个 worker 都先读到同一个 queued 快照，再进入真实 claim。"""

        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT") and "knowledge_ingest_jobs" in statement:
            with select_lock:
                if select_count >= 2:
                    return
                select_count += 1
            barrier.wait(timeout=10)

    event.listen(engine, "after_cursor_execute", pause_after_claim_select)

    def claim(worker_id: str) -> str | None:
        """使用独立会话尝试接管同一持久任务。"""

        with Session(engine) as worker_db:
            claimed = revision_module.claim_revision_job(
                worker_db,
                worker_id=worker_id,
                job_id="job_sd5_concurrent_claim",
            )
            return claimed.id if claimed else None

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(claim, ("worker_sd5_one", "worker_sd5_two")))
    finally:
        event.remove(engine, "after_cursor_execute", pause_after_claim_select)

    assert sorted(item or "" for item in results) == ["", "job_sd5_concurrent_claim"]
    with Session(engine) as db:
        stored = db.get(KnowledgeIngestJob, "job_sd5_concurrent_claim")
        assert stored is not None and stored.status == "running"
        assert stored.fence == 1 and stored.attempt_count == 1


def test_sd5_late_publish_cannot_overturn_cancel_winner(monkeypatch, tmp_path) -> None:
    """验证撤销先赢时，已读取旧 ready 状态的发布会话不能推进 head 或复活候选。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-cancel-publish-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_cancel_publish_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 cancel publish race"))
        db.add(
            User(
                id="user_sd5_cancel_publish_race",
                tenant_id=tenant_id,
                username="sd5-cancel-publish-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_cancel_publish_race",
                tenant_id=tenant_id,
                name="Cancel publish race KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_cancel_publish_race",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_cancel_publish_race",
                version="1.0.0",
                name="Cancel publish race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_cancel_publish_race",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_cancel_publish_race",
                knowledge_base_version_id="kbver_sd5_cancel_publish_race",
                lineage_id="lineage_sd5_cancel_publish_race",
                filename="race.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文\n旧事实",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_cancel_publish_race",
                scope="overall",
                actor_user_id="user_sd5_cancel_publish_race",
                document_id="kdoc_sd5_cancel_publish_race",
                content_md="# 新正文\n新事实",
                expected_revision=1,
                client_request_id="client_sd5_cancel_publish_race",
            ),
        )
        build_revision(db, job.id)

        original_authorization = revision_module._revision_actor_still_authorized

        def cancel_after_old_session_validates(current_db, current_job):
            """在旧发布会话完成授权读取后，让撤销会话先提交终态。"""

            with Session(engine) as late_db:
                revision_module.cancel_revision(late_db, current_job.id)
            return original_authorization(current_db, current_job)

        monkeypatch.setattr(
            revision_module,
            "_revision_actor_still_authorized",
            cancel_after_old_session_validates,
        )

        with pytest.raises(KnowledgeRevisionConflictError):
            revision_module.publish_revision(db, job.id)

        db.expire_all()
        stored_job = db.get(KnowledgeIngestJob, job.id)
        stored_candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_cancel_publish_race")
        assert stored_job is not None and stored_job.status == "cancelled"
        assert stored_candidate is not None and stored_candidate.publication_state == "failed"
        assert knowledge_base is not None
        assert knowledge_base.content_revision == 1
        assert knowledge_base.metadata_json["current_version"] == "1.0.0"


def test_sd5_publish_winner_rejects_late_cancel_without_deleting_assets(monkeypatch, tmp_path) -> None:
    """验证发布会话取得持久所有权后，迟到取消只返回 publishing 而保留最终资产。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-publish-cancel-winner.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_publish_cancel_winner"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 publish cancel winner"))
        db.add(
            User(
                id="user_sd5_publish_cancel_winner",
                tenant_id=tenant_id,
                username="sd5-publish-cancel-winner",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_publish_cancel_winner",
                tenant_id=tenant_id,
                name="Publish cancel winner KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_publish_cancel_winner",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publish_cancel_winner",
                version="1.0.0",
                name="Publish cancel winner KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_publish_cancel_winner",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publish_cancel_winner",
                knowledge_base_version_id="kbver_sd5_publish_cancel_winner",
                lineage_id="lineage_sd5_publish_cancel_winner",
                filename="winner.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publish_cancel_winner",
                scope="overall",
                actor_user_id="user_sd5_publish_cancel_winner",
                document_id="kdoc_sd5_publish_cancel_winner",
                content_md="# 发布正文",
                expected_revision=1,
                client_request_id="client_sd5_publish_cancel_winner",
            ),
        )
        build_revision(db, job.id)
        original_claim = revision_module._claim_revision_publication
        late_status: list[str] = []

        def claim_then_cancel(current_db, current_job):
            """在发布权提交后用独立连接执行迟到取消。"""

            claimed = original_claim(current_db, current_job)
            with Session(engine) as late_db:
                late_status.append(revision_module.cancel_revision(late_db, current_job.id).status)
            return claimed

        monkeypatch.setattr(revision_module, "_claim_revision_publication", claim_then_cancel)
        published = publish_revision(db, job.id)

        assert late_status == ["publishing"]
        assert published.status == "completed"
        assert published.published_document_id
        db.expire_all()
        stored_job = db.get(KnowledgeIngestJob, job.id)
        candidate = db.get(KnowledgeBaseVersion, published.published_version_id)
        published_document = db.get(KnowledgeDocument, published.published_document_id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_publish_cancel_winner")
        assert stored_job is not None and stored_job.status == "completed"
        assert candidate is not None and candidate.publication_state == "published"
        assert published_document is not None and published_document.status == "ready"
        assert knowledge_base is not None and knowledge_base.content_revision == 2


def test_sd5_publisher_losing_cas_returns_the_completed_winner(monkeypatch, tmp_path) -> None:
    """并发恢复先完成同一发布时，迟到兼容调用应对账返回而非报发布冲突。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-competing-publisher-winner.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_competing_publisher_winner"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 competing publisher winner"))
        db.add(User(
            id="user_sd5_competing_publisher_winner", tenant_id=tenant_id,
            username="sd5-competing-publisher-winner", role="admin", password_hash="test",
        ))
        db.add(KnowledgeBase(
            id="kb_sd5_competing_publisher_winner", tenant_id=tenant_id,
            name="Competing publisher KB", metadata_json={"current_version": "1.0.0"},
        ))
        db.add(KnowledgeBaseVersion(
            id="kbver_sd5_competing_publisher_winner", tenant_id=tenant_id,
            knowledge_base_id="kb_sd5_competing_publisher_winner", version="1.0.0",
            name="Competing publisher KB", publication_state="published",
        ))
        db.add(KnowledgeDocument(
            id="kdoc_sd5_competing_publisher_winner", tenant_id=tenant_id,
            knowledge_base_id="kb_sd5_competing_publisher_winner",
            knowledge_base_version_id="kbver_sd5_competing_publisher_winner",
            lineage_id="lineage_sd5_competing_publisher_winner", filename="winner.md",
            file_type="md", status="ready", normalized_source="# 旧正文\n原有知识事实",
        ))
        db.commit()
        job = prepare_revision(db, RevisionPrepareInput(
            tenant_id=tenant_id, knowledge_base_id="kb_sd5_competing_publisher_winner",
            scope="overall", actor_user_id="user_sd5_competing_publisher_winner",
            document_id="kdoc_sd5_competing_publisher_winner", content_md="# 新正文\n新知识事实",
            expected_revision=1, client_request_id="client_sd5_competing_publisher_winner",
        ))
        build_revision(db, job.id)
        original_claim = revision_module._claim_revision_publication
        winner_finished: list[str] = []

        def let_recovery_publisher_win(current_db, current_job):
            """用第二连接先发布同一job，再让原连接按冻结fence竞争并读回胜者。"""

            with patch.object(
                revision_module, "_claim_revision_publication", original_claim,
            ):
                with Session(engine) as recovery_db:
                    winner = publish_revision(recovery_db, current_job.id)
                    winner_finished.append(winner.status)
            current_db.rollback()
            return False

        monkeypatch.setattr(
            revision_module, "_claim_revision_publication", let_recovery_publisher_win,
        )
        published = publish_revision(db, job.id)

        assert winner_finished == ["completed"]
        assert published.status == "completed"
        assert published.published_version_id
        db.expire_all()
        stored_job = db.get(KnowledgeIngestJob, job.id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_competing_publisher_winner")
        versions = db.exec(select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.knowledge_base_id == "kb_sd5_competing_publisher_winner",
            KnowledgeBaseVersion.publication_state == "published",
        )).all()
        assert stored_job is not None and stored_job.status == "completed"
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        assert len(versions) == 2


def test_sd5_recovery_does_not_revoke_active_publication_and_stale_publisher_cannot_revive(
    monkeypatch,
    tmp_path,
) -> None:
    """验证活动发布租约不被恢复撤销，过期旧发布者也不能在取消后推进空 head。"""

    from app.scheduled_tasks import worker as scheduled_worker

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-publication-recovery-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_publication_recovery_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 publication recovery race"))
        db.add(
            User(
                id="user_sd5_publication_recovery_race",
                tenant_id=tenant_id,
                username="sd5-publication-recovery-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_publication_recovery_race",
                tenant_id=tenant_id,
                name="Publication recovery race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_publication_recovery_race",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publication_recovery_race",
                version="1.0.0",
                name="Publication recovery race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_publication_recovery_race",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publication_recovery_race",
                knowledge_base_version_id="kbver_sd5_publication_recovery_race",
                lineage_id="lineage_sd5_publication_recovery_race",
                filename="recovery-race.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publication_recovery_race",
                scope="overall",
                actor_user_id="user_sd5_publication_recovery_race",
                document_id="kdoc_sd5_publication_recovery_race",
                content_md="# 新正文",
                expected_revision=1,
                client_request_id="client_sd5_publication_recovery_race",
            ),
        )
        build_revision(db, job.id)

    original_claim = revision_module._claim_revision_publication
    original_run = revision_module.run_revision_job

    def claim_then_recover(current_db, current_job):
        """在真实发布权提交后先验证活动租约，再安排租约失效交错。"""

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
            """在过期发布权被恢复扫描接管后插入真实取消，再运行旧发布者。"""

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
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_publication_recovery_race")
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


def test_sd5_expired_old_publisher_cannot_overwrite_completed_successor(
    monkeypatch,
    tmp_path,
) -> None:
    """验证过期旧 publisher 不能把新 publisher 的 completed 改回 conflict。"""

    from app.scheduled_tasks import worker as scheduled_worker

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-terminal-publisher-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_terminal_publisher_race"
    knowledge_base_id = "kb_sd5_terminal_publisher_race"
    source_version_id = "kbver_sd5_terminal_publisher_race"
    document_id = "kdoc_sd5_terminal_publisher_race"
    user_id = "user_sd5_terminal_publisher_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 terminal publisher race"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-terminal-publisher-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Terminal publisher race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Terminal publisher race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id="lineage_sd5_terminal_publisher_race",
                filename="terminal-publisher.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
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
                content_md="# 新正文",
                expected_revision=1,
                client_request_id="client_sd5_terminal_publisher_race",
            ),
        )
        build_revision(db, job.id)

    original_claim = revision_module._claim_revision_publication
    clock = [utc_now()]

    def claim_then_handoff(current_db, current_job):
        """让旧 publisher 的租约过期，再用真实维护路径完成新 publisher。"""

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


def test_sd5_deadline_scan_cannot_overwrite_committed_cancel(
    monkeypatch,
    tmp_path,
) -> None:
    """验证 deadline 扫描读取旧 ready 后不能把独立连接已提交的 cancelled 改成 failed。"""

    from app.scheduled_tasks import worker as scheduled_worker

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-terminal-scanner-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_terminal_scanner_race"
    knowledge_base_id = "kb_sd5_terminal_scanner_race"
    source_version_id = "kbver_sd5_terminal_scanner_race"
    document_id = "kdoc_sd5_terminal_scanner_race"
    user_id = "user_sd5_terminal_scanner_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 terminal scanner race"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-terminal-scanner-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Terminal scanner race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Terminal scanner race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id="lineage_sd5_terminal_scanner_race",
                filename="terminal-scanner.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
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
                content_md="# 新正文",
                expected_revision=1,
                client_request_id="client_sd5_terminal_scanner_race",
            ),
        )
        build_revision(db, job.id)
        assert job.deadline_at is not None
        future = job.deadline_at + timedelta(seconds=1)

    original_mark_failed = revision_module._mark_revision_failed

    def cancel_after_scan_read(current_db, stale_job, error_code, message, current_time):
        """在扫描读到旧任务后用独立连接提交真实取消，再执行旧失败结算。"""

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
def test_sd5_expired_running_scan_wakes_only_after_persistent_cas(
    monkeypatch,
    tmp_path,
    cancel_between: bool,
) -> None:
    """验证过期 running 仅在状态、owner、fence 和租约条件仍成立时回到 queued 并唤醒。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sd5-running-recovery-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_running_recovery_race"
    knowledge_base_id = "kb_sd5_running_recovery_race"
    source_version_id = "kbver_sd5_running_recovery_race"
    document_id = "kdoc_sd5_running_recovery_race"
    user_id = "user_sd5_running_recovery_race"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 running recovery race"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-running-recovery-race",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Running recovery race KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Running recovery race KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id="lineage_sd5_running_recovery_race",
                filename="running-recovery.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
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
                content_md="# 新正文",
                expected_revision=1,
                client_request_id="client_sd5_running_recovery_race",
            ),
        )
        claimed = revision_module.claim_revision_job(
            db,
            worker_id="sd5-running-recovery-worker",
            job_id=job.id,
        )
        assert claimed is not None and claimed.status == "running"
        assert claimed.lease_until is not None
        job_id = job.id
        candidate_id = job.candidate_version_id
        future = claimed.lease_until + timedelta(seconds=1)
        secondary_job_id = "job_sd5_running_recovery_secondary"
        db.add(
            KnowledgeIngestJob(
                id=secondary_job_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                filename="secondary-running-recovery.md",
                job_kind="revision",
                status="running",
                stage="build",
                lease_owner="secondary-running-worker",
                lease_until=claimed.lease_until - timedelta(seconds=1),
                fence=7,
                attempt_count=1,
                deadline_at=utc_now() + timedelta(minutes=5),
            )
        )
        db.commit()

    original_exec = None
    with Session(engine) as scanner_db:
        original_exec = scanner_db.exec
        select_seen = [False]

        def select_then_cancel(statement, *args, **kwargs):
            """暂停真实扫描读行，并按场景让独立连接先提交取消。"""

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


def test_sd5_readable_member_cannot_cancel_revision_but_admin_can() -> None:
    """验证正文任务的读取权限不等于取消权限，管理员仍可取消同一持久任务。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_cancel_permission"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 cancel permission"))
        db.add(
            User(
                id="user_sd5_cancel_admin",
                tenant_id=tenant_id,
                username="sd5-cancel-admin",
                role="admin",
                password_hash="test",
            )
        )
        member = User(
            id="user_sd5_cancel_member",
            tenant_id=tenant_id,
            username="sd5-cancel-member",
            role="member",
            password_hash="test",
        )
        db.add(member)
        db.add(
            KnowledgeBase(
                id="kb_sd5_cancel_permission",
                tenant_id=tenant_id,
                name="Cancel permission KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_cancel_permission",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_cancel_permission",
                version="1.0.0",
                name="Cancel permission KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_cancel_permission",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_cancel_permission",
                knowledge_base_version_id="kbver_sd5_cancel_permission",
                lineage_id="lineage_sd5_cancel_permission",
                filename="permission.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_cancel_permission",
                scope="overall",
                actor_user_id="user_sd5_cancel_admin",
                document_id="kdoc_sd5_cancel_permission",
                content_md="# 待取消正文",
                expected_revision=1,
                client_request_id="client_sd5_cancel_permission",
            ),
        )

        readable = get_job(
            job.id,
            tenant_id=tenant_id,
            agent_id=None,
            db=db,
            current_user=member,
        )
        assert readable.id == job.id

        with pytest.raises(HTTPException) as error:
            cancel_job(
                job.id,
                tenant_id=tenant_id,
                agent_id=None,
                db=db,
                current_user=member,
            )
        assert error.value.status_code == 403

        cancelled = cancel_job(
            job.id,
            tenant_id=tenant_id,
            agent_id=None,
            db=db,
            current_user=db.get(User, "user_sd5_cancel_admin"),
        )
        assert cancelled.status == "cancelled"


def test_sd5_production_maintenance_wakes_ready_revision_jobs(monkeypatch) -> None:
    """验证生产维护轮询会唤醒 queued、过期 running 和 ready_to_publish 任务，而非只扫请求队列。"""

    from app.scheduled_tasks import worker as scheduled_worker

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    now = utc_now()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_recovery_wakeup", name="SD5 recovery wakeup"))
        db.add_all(
            [
                KnowledgeIngestJob(
                    id="job_sd5_recovery_queued",
                    tenant_id="tenant_sd5_recovery_wakeup",
                    knowledge_base_id="kb_sd5_recovery_wakeup",
                    filename="queued.md",
                    job_kind="revision",
                    status="queued",
                    deadline_at=now + timedelta(minutes=5),
                ),
                KnowledgeIngestJob(
                    id="job_sd5_recovery_running",
                    tenant_id="tenant_sd5_recovery_wakeup",
                    knowledge_base_id="kb_sd5_recovery_wakeup",
                    filename="running.md",
                    job_kind="revision",
                    status="running",
                    lease_until=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(minutes=5),
                ),
                KnowledgeIngestJob(
                    id="job_sd5_recovery_ready",
                    tenant_id="tenant_sd5_recovery_wakeup",
                    knowledge_base_id="kb_sd5_recovery_wakeup",
                    filename="ready.md",
                    job_kind="revision",
                    status="ready_to_publish",
                    deadline_at=now + timedelta(minutes=5),
                ),
                KnowledgeIngestJob(
                    id="job_sd5_recovery_deadline",
                    tenant_id="tenant_sd5_recovery_wakeup",
                    knowledge_base_id="kb_sd5_recovery_wakeup",
                    filename="deadline.md",
                    job_kind="revision",
                    status="ready_to_publish",
                    deadline_at=now - timedelta(seconds=1),
                ),
            ]
        )
        db.commit()
        woken: list[str] = []
        monkeypatch.setattr(
            revision_module,
            "run_revision_job",
            lambda job_id: woken.append(job_id),
        )
        monkeypatch.setattr(scheduled_worker, "poll_due_external_operations", lambda _db: 0)
        monkeypatch.setattr(scheduled_worker, "_process_due_dynamic_signals", lambda _db, once: 0)

        processed = scheduled_worker._process_external_task_maintenance(db, once=True)

        assert processed == 3
        assert set(woken) == {
            "job_sd5_recovery_queued",
            "job_sd5_recovery_running",
            "job_sd5_recovery_ready",
        }
        db.expire_all()
        ready = db.get(KnowledgeIngestJob, "job_sd5_recovery_ready")
        deadline = db.get(KnowledgeIngestJob, "job_sd5_recovery_deadline")
        assert ready is not None and ready.status == "ready_to_publish"
        assert deadline is not None and deadline.status == "failed"
        assert deadline.error_code == "KNOWLEDGE_REVISION_DEADLINE_EXCEEDED"


def test_sd5_agent_manager_can_cancel_agent_revision_but_other_reader_cannot() -> None:
    """验证 Agent 私有正文由 owner 管理者可取消，其他可读成员仍只能读取进度。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_agent_cancel_permission"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 agent cancel permission"))
        owner = User(
            id="user_sd5_agent_cancel_owner",
            tenant_id=tenant_id,
            username="sd5-agent-cancel-owner",
            role="member",
            password_hash="test",
        )
        other = User(
            id="user_sd5_agent_cancel_other",
            tenant_id=tenant_id,
            username="sd5-agent-cancel-other",
            role="member",
            password_hash="test",
        )
        db.add(owner)
        db.add(other)
        db.add(
            AgentProfile(
                id="agent_sd5_cancel_manager",
                tenant_id=tenant_id,
                name="SD5 cancel manager",
                owner_user_id=owner.id,
                status="active",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_agent_cancel_permission",
                tenant_id=tenant_id,
                name="Agent cancel permission KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_agent_cancel_permission",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_agent_cancel_permission",
                version="1.0.0",
                scope="agent:agent_sd5_cancel_manager",
                name="Agent cancel permission KB",
                publication_state="published",
            )
        )
        db.add(
            AgentKnowledgeBranch(
                id="agentkb_sd5_agent_cancel_permission",
                tenant_id=tenant_id,
                agent_id="agent_sd5_cancel_manager",
                knowledge_base_id="kb_sd5_agent_cancel_permission",
                head_version="1.0.0",
                status="active",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_agent_cancel_permission",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_agent_cancel_permission",
                knowledge_base_version_id="kbver_sd5_agent_cancel_permission",
                lineage_id="lineage_sd5_agent_cancel_permission",
                filename="agent-permission.md",
                file_type="md",
                status="ready",
                normalized_source="# Agent 旧正文",
            )
        )
        ensure_private_resource_binding(
            db,
            tenant_id,
            "agent_sd5_cancel_manager",
            "knowledge_base",
            "kb_sd5_agent_cancel_permission",
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_agent_cancel_permission",
                scope="agent:agent_sd5_cancel_manager",
                actor_user_id=owner.id,
                document_id="kdoc_sd5_agent_cancel_permission",
                content_md="# Agent 待取消正文",
                expected_revision=1,
                client_request_id="client_sd5_agent_cancel_permission",
            ),
        )

        readable = get_job(
            job.id,
            tenant_id=tenant_id,
            agent_id="agent_sd5_cancel_manager",
            db=db,
            current_user=other,
        )
        assert readable.id == job.id
        with pytest.raises(HTTPException) as error:
            cancel_job(
                job.id,
                tenant_id=tenant_id,
                agent_id="agent_sd5_cancel_manager",
                db=db,
                current_user=other,
            )
        assert error.value.status_code == 403

        cancelled = cancel_job(
            job.id,
            tenant_id=tenant_id,
            agent_id="agent_sd5_cancel_manager",
            db=db,
            current_user=owner,
        )
        assert cancelled.status == "cancelled"


def test_sd5_publish_rejects_persisted_deadline_after_ready_state() -> None:
    """验证 ready_to_publish 超过持久 deadline 时发布本身也会失败且不延长 deadline。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_publish_deadline"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 publish deadline"))
        db.add(
            User(
                id="user_sd5_publish_deadline",
                tenant_id=tenant_id,
                username="sd5-publish-deadline",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_publish_deadline",
                tenant_id=tenant_id,
                name="Publish deadline KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_publish_deadline",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publish_deadline",
                version="1.0.0",
                name="Publish deadline KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_publish_deadline",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publish_deadline",
                knowledge_base_version_id="kbver_sd5_publish_deadline",
                lineage_id="lineage_sd5_publish_deadline",
                filename="deadline.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_publish_deadline",
                scope="overall",
                actor_user_id="user_sd5_publish_deadline",
                document_id="kdoc_sd5_publish_deadline",
                content_md="# 过期正文",
                expected_revision=1,
                client_request_id="client_sd5_publish_deadline",
            ),
        )
        build_revision(db, job.id)
        deadline = utc_now() - timedelta(seconds=1)
        job.deadline_at = deadline
        db.add(job)
        db.commit()

        with pytest.raises(KnowledgeRevisionConflictError):
            revision_module.publish_revision(db, job.id)

        db.expire_all()
        stored = db.get(KnowledgeIngestJob, job.id)
        assert stored is not None and stored.status == "failed"
        assert stored.error_code == "KNOWLEDGE_REVISION_DEADLINE_EXCEEDED"
        assert stored.deadline_at == deadline
        assert db.get(KnowledgeBase, "kb_sd5_publish_deadline").content_revision == 1


def test_sd5_ready_revision_recovers_in_a_new_runner_without_new_job(monkeypatch) -> None:
    """验证 ready_to_publish 在原进程退出后由维护 worker 继续同一 job 并只发布一次。"""

    from app.scheduled_tasks import worker as scheduled_worker

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    tenant_id = "tenant_sd5_ready_recovery"
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, name="SD5 ready recovery"))
        db.add(
            User(
                id="user_sd5_ready_recovery",
                tenant_id=tenant_id,
                username="sd5-ready-recovery",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_sd5_ready_recovery",
                tenant_id=tenant_id,
                name="Ready recovery KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_ready_recovery",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_ready_recovery",
                version="1.0.0",
                name="Ready recovery KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_ready_recovery",
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_ready_recovery",
                knowledge_base_version_id="kbver_sd5_ready_recovery",
                lineage_id="lineage_sd5_ready_recovery",
                filename="recovery.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id=tenant_id,
                knowledge_base_id="kb_sd5_ready_recovery",
                scope="overall",
                actor_user_id="user_sd5_ready_recovery",
                document_id="kdoc_sd5_ready_recovery",
                content_md="# 新正文",
                expected_revision=1,
                client_request_id="client_sd5_ready_recovery",
            ),
        )
        build_revision(db, job.id)
        assert job.status == "ready_to_publish"
        monkeypatch.setattr(revision_module, "engine", engine)
        monkeypatch.setattr(scheduled_worker, "poll_due_external_operations", lambda _db: 0)
        monkeypatch.setattr(scheduled_worker, "_process_due_dynamic_signals", lambda _db, once: 0)

        processed = scheduled_worker._process_external_task_maintenance(db, once=True)

        assert processed == 1
        db.expire_all()
        completed = db.get(KnowledgeIngestJob, job.id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_ready_recovery")
        assert completed is not None and completed.status == "completed"
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        published_version_id = completed.published_version_id

        assert scheduled_worker._recover_knowledge_revision_jobs(db) == 0
        db.refresh(completed)
        assert completed.status == "completed"
        assert completed.published_version_id == published_version_id


def test_sd5_document_content_reads_only_current_editable_head() -> None:
    """验证正文读取返回规范源、scope head 和 content CAS，而不是摘要或旧版本。"""

    assert callable(getattr(knowledge_api, "get_document_content", None))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_content", name="SD5 content"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_content",
                tenant_id="tenant_sd5_content",
                name="Content KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_content",
                tenant_id="tenant_sd5_content",
                knowledge_base_id="kb_sd5_content",
                version="1.0.0",
                name="Content KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_content",
                tenant_id="tenant_sd5_content",
                knowledge_base_id="kb_sd5_content",
                knowledge_base_version_id="kbver_sd5_content",
                lineage_id="lineage_sd5_content",
                filename="content.md",
                file_type="md",
                status="ready",
                normalized_source="# 可编辑正文",
                source_checksum="c" * 64,
                source_kind="normalized",
            )
        )
        db.commit()

        content = knowledge_api.get_document_content(
            "kdoc_sd5_content",
            tenant_id="tenant_sd5_content",
            agent_id=None,
            db=db,
            current_user=User(
                id="user_sd5_content",
                tenant_id="tenant_sd5_content",
                username="content-user",
                role="admin",
                password_hash="test",
            ),
        )

        assert content.document_id == "kdoc_sd5_content"
        assert content.lineage_id == "lineage_sd5_content"
        assert content.knowledge_base_version_id == "kbver_sd5_content"
        assert content.head_version_id == "kbver_sd5_content"
        assert content.scope == "overall"
        assert content.content_revision == 1
        assert content.content_md == "# 可编辑正文"
        assert content.source_kind == "normalized"


def test_sd5_include_all_versions_never_returns_candidate_assets() -> None:
    """验证历史版本列表也先排除 candidate，不能把管理查询当作内容越权开关。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            id="user_sd5_visibility",
            tenant_id="tenant_sd5_visibility",
            username="visibility-user",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_sd5_visibility", name="SD5 visibility"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_visibility",
                tenant_id="tenant_sd5_visibility",
                name="Visibility KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_visibility",
                tenant_id="tenant_sd5_visibility",
                knowledge_base_id="kb_sd5_visibility",
                version="1.0.0",
                name="Visibility KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_visibility",
                tenant_id="tenant_sd5_visibility",
                knowledge_base_id="kb_sd5_visibility",
                knowledge_base_version_id="kbver_sd5_visibility",
                lineage_id="lineage_sd5_visibility",
                filename="visibility.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_visibility",
                knowledge_base_id="kb_sd5_visibility",
                scope="overall",
                actor_user_id=user.id,
                document_id="kdoc_sd5_visibility",
                content_md="# 候选正文",
                expected_revision=1,
                client_request_id="client_sd5_visibility",
            ),
        )
        build_revision(db, job.id)

        rows = list_documents(
            tenant_id="tenant_sd5_visibility",
            knowledge_base_id="kb_sd5_visibility",
            agent_id=None,
            include_all_versions=True,
            db=db,
            current_user=user,
        )

        assert {row.knowledge_base_version_id for row in rows} == {"kbver_sd5_visibility"}


def test_sd5_completed_revision_job_remains_queryable_after_head_publish() -> None:
    """验证发布后任务查询按当前知识库授权，不因冻结源版本成为历史而丢失。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            id="user_sd5_job_read",
            tenant_id="tenant_sd5_job_read",
            username="job-reader",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_sd5_job_read", name="SD5 job read"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_job_read",
                tenant_id="tenant_sd5_job_read",
                name="Job read KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_job_read",
                tenant_id="tenant_sd5_job_read",
                knowledge_base_id="kb_sd5_job_read",
                version="1.0.0",
                name="Job read KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_job_read",
                tenant_id="tenant_sd5_job_read",
                knowledge_base_id="kb_sd5_job_read",
                knowledge_base_version_id="kbver_sd5_job_read",
                lineage_id="lineage_sd5_job_read",
                filename="job.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_job_read",
                knowledge_base_id="kb_sd5_job_read",
                scope="overall",
                actor_user_id=user.id,
                document_id="kdoc_sd5_job_read",
                content_md="# 已发布正文",
                expected_revision=1,
                client_request_id="client_sd5_job_read",
            ),
        )
        build_revision(db, job.id)
        revision_module.publish_revision(db, job.id)

        read = get_job(
            job.id,
            tenant_id="tenant_sd5_job_read",
            agent_id=None,
            db=db,
            current_user=user,
        )

        assert read.id == job.id
        assert read.status == "completed"
        assert read.published_version_id == job.candidate_version_id
        assert read.published_document_id
        assert read.content_revision == 2
        assert read.manifest_hash
        assert "content_md" not in read.metadata

        reconciled = get_revision_job_by_client_request(
            tenant_id="tenant_sd5_job_read",
            knowledge_base_id="kb_sd5_job_read",
            document_id="kdoc_sd5_job_read",
            client_request_id="client_sd5_job_read",
            source_version_id="kbver_sd5_job_read",
            expected_revision=1,
            agent_id=None,
            db=db,
            current_user=user,
        )

        assert reconciled.id == job.id
        assert reconciled.scope == "overall"
        assert reconciled.status == "completed"
        missing_request = get_revision_job_by_client_request(
            tenant_id="tenant_sd5_job_read",
            knowledge_base_id="kb_sd5_job_read",
            document_id="kdoc_sd5_job_read",
            client_request_id="client_sd5_job_read",
            source_version_id="kbver_sd5_job_read",
            expected_revision=2,
            agent_id=None,
            db=db,
            current_user=user,
        )
        assert missing_request is None


def test_sd5_legacy_metadata_write_cannot_mutate_candidate_or_historical_document() -> None:
    """验证旧 title/status 入口至少不能绕过 candidate 发布边界。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            id="user_sd5_write_guard",
            tenant_id="tenant_sd5_write_guard",
            username="write-guard",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_sd5_write_guard", name="SD5 write guard"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_write_guard",
                tenant_id="tenant_sd5_write_guard",
                name="Write guard KB",
                access_scope="tenant",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_write_guard",
                tenant_id="tenant_sd5_write_guard",
                knowledge_base_id="kb_sd5_write_guard",
                version="1.0.0",
                name="Write guard KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_write_guard",
                tenant_id="tenant_sd5_write_guard",
                knowledge_base_id="kb_sd5_write_guard",
                knowledge_base_version_id="kbver_sd5_write_guard",
                lineage_id="lineage_sd5_write_guard",
                filename="guard.md",
                file_type="md",
                status="ready",
                normalized_source="# 旧正文",
            )
        )
        db.commit()
        job = prepare_revision(
            db,
            RevisionPrepareInput(
                tenant_id="tenant_sd5_write_guard",
                knowledge_base_id="kb_sd5_write_guard",
                scope="overall",
                actor_user_id=user.id,
                document_id="kdoc_sd5_write_guard",
                content_md="# 候选正文",
                expected_revision=1,
                client_request_id="client_sd5_write_guard",
            ),
        )
        build_revision(db, job.id)

        candidate_document_id = (
            db.get(KnowledgeBaseVersion, job.candidate_version_id)
            .content_manifest_json["document_id_map"]["kdoc_sd5_write_guard"]
        )
        with pytest.raises(Exception) as error:
            update_document(
                candidate_document_id,
                KnowledgeDocumentUpdateRequest(
                    tenant_id="tenant_sd5_write_guard",
                    title="不应直接改候选",
                ),
                db,
                user,
            )
        assert getattr(error.value, "status_code", None) == 409


def test_sd5_authenticated_legacy_fields_require_reconstructible_source() -> None:
    """验证历史正文不可重建时，认证写入口不原地修改 published 快照。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_source_unavailable"
        knowledge_base_id = "kb_sd5_source_unavailable"
        user = User(
            id="user_sd5_source_unavailable",
            tenant_id=tenant_id,
            username="source-unavailable",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id=tenant_id, name="SD5 source unavailable"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Unavailable KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_source_unavailable",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Unavailable KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_source_unavailable",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id="kbver_sd5_source_unavailable",
                lineage_id="lineage_sd5_source_unavailable",
                filename="legacy.md",
                file_type="md",
                title="旧标题",
                status="ready",
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as error:
            update_document(
                "kdoc_sd5_source_unavailable",
                KnowledgeDocumentUpdateRequest(
                    tenant_id=tenant_id,
                    title="不应原地修改",
                ),
                db,
                user,
            )
        assert error.value.status_code == 422
        assert db.get(KnowledgeDocument, "kdoc_sd5_source_unavailable").title == "旧标题"


def test_sd5_branch_head_operations_copy_published_assets_and_reject_unknown_rollback() -> None:
    """验证 promote/sync/rollback 只操作已发布 head，且推广不搬走个人历史资产。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_branch_head", name="SD5 branch head"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_branch_head",
                tenant_id="tenant_sd5_branch_head",
                name="Branch head KB",
                metadata_json={
                    "current_version": "1.0.0",
                    "scope": "open_gallery",
                    "visibility": "open_gallery",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_sd5_branch_head",
                tenant_id="tenant_sd5_branch_head",
                name="Branch agent",
            )
        )
        db.add(
            AgentProfile(
                id="agent_sd5_branch_head_overall",
                tenant_id="tenant_sd5_branch_head",
                name="Overall agent",
                is_overall=True,
            )
        )
        overall = KnowledgeBaseVersion(
            id="kbver_sd5_branch_head_overall",
            tenant_id="tenant_sd5_branch_head",
            knowledge_base_id="kb_sd5_branch_head",
            version="1.0.0",
            name="Branch head KB",
            publication_state="published",
        )
        branch_version = KnowledgeBaseVersion(
            id="kbver_sd5_branch_head_agent",
            tenant_id="tenant_sd5_branch_head",
            knowledge_base_id="kb_sd5_branch_head",
            version="1.0.0-branch.agent_sd5_branch_head.1",
            scope="agent:agent_sd5_branch_head",
            name="Branch head KB",
            publication_state="published",
            parent_version_id=overall.id,
        )
        db.add(overall)
        db.add(branch_version)
        db.add(
            AgentKnowledgeBranch(
                id="agentkb_sd5_branch_head",
                tenant_id="tenant_sd5_branch_head",
                agent_id="agent_sd5_branch_head",
                knowledge_base_id="kb_sd5_branch_head",
                base_version="1.0.0",
                head_version=branch_version.version,
                status="active",
                sync_state="diverged",
            )
        )
        source_document = KnowledgeDocument(
            id="kdoc_sd5_branch_head_agent",
            tenant_id="tenant_sd5_branch_head",
            knowledge_base_id="kb_sd5_branch_head",
            knowledge_base_version_id=branch_version.id,
            lineage_id="lineage_sd5_branch_head",
            filename="branch.md",
            file_type="md",
            title="Branch document",
            status="ready",
            normalized_source="# Branch\nbranch fact",
            source_kind="normalized",
        )
        source_bucket = KnowledgeBucket(
            id="kbucket_sd5_branch_head_agent",
            tenant_id="tenant_sd5_branch_head",
            knowledge_base_id="kb_sd5_branch_head",
            knowledge_base_version_id=branch_version.id,
            document_id=source_document.id,
            bucket_key="branch-topic",
            title="Branch topic",
            summary="Branch summary",
            metadata_json={"representative_chunk_ids": ["kchunk_sd5_branch_head_agent"]},
        )
        source_chunk = KnowledgeChunk(
            id="kchunk_sd5_branch_head_agent",
            tenant_id="tenant_sd5_branch_head",
            knowledge_base_id="kb_sd5_branch_head",
            knowledge_base_version_id=branch_version.id,
            document_id=source_document.id,
            bucket_id=source_bucket.id,
            chunk_index=0,
            content="branch fact",
            metadata_json={"related_chunk_ids": ["kchunk_sd5_branch_head_agent"]},
        )
        db.add(source_document)
        db.add(source_bucket)
        db.add(source_chunk)
        ensure_open_gallery_binding(
            db,
            "tenant_sd5_branch_head",
            "knowledge_base",
            "kb_sd5_branch_head",
            "active",
        )
        db.commit()

        promoted = promote_knowledge_branch_to_overall(
            db,
            "tenant_sd5_branch_head",
            "agent_sd5_branch_head",
            "kb_sd5_branch_head",
        )
        db.commit()

        source_after = db.get(KnowledgeDocument, source_document.id)
        assert source_after is not None
        assert source_after.knowledge_base_version_id == branch_version.id
        copied_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == promoted.id
            )
        ).all()
        assert len(copied_documents) == 1
        copied_bucket = db.exec(
            select(KnowledgeBucket).where(
                KnowledgeBucket.knowledge_base_version_id == promoted.id
            )
        ).one()
        copied_chunk = db.exec(
            select(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_base_version_id == promoted.id
            )
        ).one()
        assert copied_documents[0].id != source_document.id
        assert copied_documents[0].lineage_id == source_document.lineage_id
        assert copied_bucket.document_id == copied_documents[0].id
        assert copied_chunk.bucket_id == copied_bucket.id
        assert copied_chunk.metadata_json["related_chunk_ids"] == [copied_chunk.id]
        assert promoted.publication_state == "published"
        assert db.get(KnowledgeBase, "kb_sd5_branch_head").content_revision == 2

        branch = db.get(AgentKnowledgeBranch, "agentkb_sd5_branch_head")
        assert branch is not None
        assert branch.head_version == promoted.version
        assert branch.content_revision == 2

        with pytest.raises(HTTPException) as error:
            rollback_knowledge_branch(
                db,
                "tenant_sd5_branch_head",
                "agent_sd5_branch_head",
                "kb_sd5_branch_head",
                "9.9.9",
            )
        assert error.value.status_code == 404
        assert db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.knowledge_base_id == "kb_sd5_branch_head",
                KnowledgeBaseVersion.version == "9.9.9",
            )
        ).first() is None

        synced = sync_knowledge_branch_from_overall(
            db,
            "tenant_sd5_branch_head",
            "agent_sd5_branch_head",
            "kb_sd5_branch_head",
        )
        assert synced.head_version == promoted.version
        assert synced.content_revision == 2


def test_sd5_branch_clone_resumes_partial_assets_without_filename_guessing() -> None:
    """验证分支复制按 lineage 恢复部分候选，并补齐同名文档的全部派生资产。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_clone_resume"
        knowledge_base_id = "kb_sd5_clone_resume"
        source_version_id = "kbver_sd5_clone_resume_source"
        target_version_id = "kbver_sd5_clone_resume_target"
        db.add(Tenant(id=tenant_id, name="SD5 clone resume"))
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="Clone resume KB",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=source_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="Clone resume KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id=target_version_id,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.1-branch.resume",
                name="Clone resume KB",
                publication_state="candidate",
                parent_version_id=source_version_id,
            )
        )
        source_documents = [
            KnowledgeDocument(
                id=f"kdoc_sd5_clone_resume_source_{index}",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                lineage_id=f"lineage_sd5_clone_resume_{index}",
                filename="同名.md",
                file_type="md",
                title="同名文档",
                status="ready",
                normalized_source=f"# 文档 {index}\n事实 {index}",
                source_kind="normalized",
            )
            for index in (1, 2)
        ]
        db.add_all(source_documents)
        for index, document in enumerate(source_documents, start=1):
            bucket = KnowledgeBucket(
                id=f"kbucket_sd5_clone_resume_source_{index}",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=source_version_id,
                document_id=document.id,
                bucket_key="same-bucket",
                title="同名桶",
                summary=f"摘要 {index}",
                metadata_json={"representative_chunk_ids": [f"kchunk_sd5_clone_resume_source_{index}"]},
            )
            db.add(bucket)
            db.add(
                KnowledgeChunk(
                    id=f"kchunk_sd5_clone_resume_source_{index}",
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_version_id=source_version_id,
                    document_id=document.id,
                    bucket_id=bucket.id,
                    chunk_index=0,
                    content=f"事实 {index}",
                    source_ref=f"{document.id}/{bucket.id}",
                    metadata_json={"related_chunk_ids": [f"kchunk_sd5_clone_resume_source_{index}"]},
                )
            )
        # 目标候选只留下一个 lineage 正确但物理 ID 不同的文档，模拟复制中断。
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_clone_resume_target_partial",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                lineage_id="lineage_sd5_clone_resume_1",
                parent_document_id=source_documents[0].id,
                filename="同名.md",
                file_type="md",
                title="同名文档",
                status="ready",
                normalized_source=source_documents[0].normalized_source,
                source_kind="normalized",
            )
        )
        db.commit()

        clone_knowledge_version_assets(
            db,
            tenant_id,
            knowledge_base_id,
            source_version_id,
            target_version_id,
        )
        db.commit()

        target_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == target_version_id
            )
        ).all()
        target_buckets = db.exec(
            select(KnowledgeBucket).where(
                KnowledgeBucket.knowledge_base_version_id == target_version_id
            )
        ).all()
        target_chunks = db.exec(
            select(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_base_version_id == target_version_id
            )
        ).all()
        assert {row.lineage_id for row in target_documents} == {
            "lineage_sd5_clone_resume_1",
            "lineage_sd5_clone_resume_2",
        }
        assert len(target_buckets) == 2
        assert len(target_chunks) == 2
        assert {row.document_id for row in target_buckets} == {row.id for row in target_documents}
        target_chunk_ids = {row.id for row in target_chunks}
        assert all(row.bucket_id in {bucket.id for bucket in target_buckets} for row in target_chunks)
        assert all(row.metadata_json["related_chunk_ids"][0] in target_chunk_ids for row in target_chunks)
        assert all(source_documents[0].id not in (row.source_ref or "") for row in target_chunks)


def test_sd5_knowledge_base_fields_publish_a_new_snapshot() -> None:
    """验证知识库字段兼容写入不直接覆盖旧发布版本或旁路 content CAS。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_kb_fields", name="SD5 KB fields"))
        user = User(
            id="user_sd5_kb_fields",
            tenant_id="tenant_sd5_kb_fields",
            username="sd5-kb-fields",
            role="admin",
            password_hash="test",
        )
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_kb_fields",
                tenant_id="tenant_sd5_kb_fields",
                name="旧知识库",
                metadata_json={
                    "current_version": "1.0.0",
                    "scope": "open_gallery",
                    "visibility": "open_gallery",
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_kb_fields",
                tenant_id="tenant_sd5_kb_fields",
                knowledge_base_id="kb_sd5_kb_fields",
                version="1.0.0",
                name="旧知识库",
                description="旧描述",
                publication_state="published",
                metadata_json={"creator": "keep"},
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_kb_fields",
                tenant_id="tenant_sd5_kb_fields",
                knowledge_base_id="kb_sd5_kb_fields",
                knowledge_base_version_id="kbver_sd5_kb_fields",
                lineage_id="lineage_sd5_kb_fields",
                filename="fields.md",
                file_type="md",
                title="字段文档",
                status="ready",
                normalized_source="# 正文\n事实",
                source_kind="normalized",
            )
        )
        db.commit()

        job = run_compatibility_revision(
            db,
            tenant_id="tenant_sd5_kb_fields",
            knowledge_base_id="kb_sd5_kb_fields",
            scope="overall",
            actor_user_id=user.id,
            document_id="kdoc_sd5_kb_fields",
            operation_kind="version_fields",
            mutation_payload={
                "name": "新知识库",
                "description": "新描述",
                "metadata": {"creator": "keep", "label": "new"},
            },
        )

        assert job.status == "completed"
        assert job.published_version_id
        old_version = db.get(KnowledgeBaseVersion, "kbver_sd5_kb_fields")
        new_version = db.get(KnowledgeBaseVersion, job.published_version_id)
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_kb_fields")
        assert old_version is not None and old_version.name == "旧知识库"
        assert new_version is not None and new_version.name == "新知识库"
        assert new_version.description == "新描述"
        assert new_version.metadata_json["label"] == "new"
        assert knowledge_base is not None
        assert knowledge_base.name == "新知识库"
        assert knowledge_base.description == "新描述"
        assert knowledge_base.content_revision == 2


def test_sd5_knowledge_base_update_api_uses_revision_snapshot_for_fields() -> None:
    """验证真实知识库 PUT 入口更新字段时也只发布新整库快照。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_kb_api_fields"
        knowledge_base_id = "kb_sd5_kb_api_fields"
        user = User(
            id="user_sd5_kb_api_fields",
            tenant_id=tenant_id,
            username="sd5-kb-api-fields",
            role="admin",
            password_hash="test",
        )
        db.add(Tenant(id=tenant_id, name="SD5 KB API fields"))
        db.add(user)
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="API 旧名称",
                metadata_json={"current_version": "1.0.0", "scope": "open_gallery"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_kb_api_fields",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="API 旧名称",
                description="API 旧描述",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_kb_api_fields",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id="kbver_sd5_kb_api_fields",
                lineage_id="lineage_sd5_kb_api_fields",
                filename="api-fields.md",
                file_type="md",
                status="ready",
                normalized_source="# API fields\n事实",
            )
        )
        db.commit()

        response = knowledge_bases_api.update_knowledge_base(
            knowledge_base_id,
            KnowledgeBaseUpdateRequest(
                tenant_id=tenant_id,
                name="API 新名称",
                description="API 新描述",
                metadata={"label": "new"},
            ),
            agent_id=None,
            db=db,
            current_user=user,
        )

        assert response.name == "API 新名称"
        old_version = db.get(KnowledgeBaseVersion, "kbver_sd5_kb_api_fields")
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        assert old_version is not None and old_version.name == "API 旧名称"
        assert knowledge_base is not None and knowledge_base.content_revision == 2
        current_version_id = knowledge_base.metadata_json["current_version"]
        assert current_version_id != "1.0.0"
        current_version = db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseVersion.version == current_version_id,
            )
        ).one()
        assert current_version.name == "API 新名称"


def test_sd5_empty_head_field_update_still_uses_candidate_revision() -> None:
    """验证没有正文资产的兼容字段写入也不直接覆盖已发布版本。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant_id = "tenant_sd5_empty_fields"
        user_id = "user_sd5_empty_fields"
        knowledge_base_id = "kb_sd5_empty_fields"
        agent_id = "agent_sd5_empty_fields"
        db.add(Tenant(id=tenant_id, name="SD5 empty fields"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username="sd5-empty-fields",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            AgentProfile(
                id=agent_id,
                tenant_id=tenant_id,
                name="空正文员工",
                owner_user_id=user_id,
            )
        )
        db.add(
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="空正文知识库",
                metadata_json={"current_version": "1.0.0"},
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_empty_fields",
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                version="1.0.0",
                name="空正文知识库",
                publication_state="published",
            )
        )
        db.add(
            AgentKnowledgeBranch(
                id="branch_sd5_empty_fields",
                tenant_id=tenant_id,
                agent_id=agent_id,
                knowledge_base_id=knowledge_base_id,
                base_version="1.0.0",
                head_version="1.0.0",
            )
        )
        db.commit()

        response = knowledge_bases_api.update_knowledge_base(
            knowledge_base_id,
            KnowledgeBaseUpdateRequest(
                tenant_id=tenant_id,
                metadata={"label": "candidate-only"},
            ),
            agent_id=agent_id,
            db=db,
            current_user=db.get(User, user_id),
        )

        assert response.metadata["label"] == "candidate-only"
        branch = db.get(AgentKnowledgeBranch, "branch_sd5_empty_fields")
        old_version = db.get(KnowledgeBaseVersion, "kbver_sd5_empty_fields")
        assert branch is not None and branch.head_version != "1.0.0" and branch.content_revision == 2
        assert old_version is not None and old_version.name == "空正文知识库"
        published_versions = db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseVersion.publication_state == "published",
            )
        ).all()
        assert len(published_versions) == 2


def test_sd5_okf_import_publishes_into_a_new_snapshot() -> None:
    """验证 OKF 导入不会向当前已发布版本追加概念或覆盖历史文档。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_okf_import", name="SD5 OKF import"))
        user = User(
            id="user_sd5_okf_import",
            tenant_id="tenant_sd5_okf_import",
            username="sd5-okf-import",
            role="admin",
            password_hash="test",
        )
        db.add(user)
        db.add(
            KnowledgeBase(
                id="kb_sd5_okf_import",
                tenant_id="tenant_sd5_okf_import",
                name="OKF KB",
                metadata_json={
                    "current_version": "1.0.0",
                    "scope": "open_gallery",
                    "visibility": "open_gallery",
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_okf_import",
                tenant_id="tenant_sd5_okf_import",
                knowledge_base_id="kb_sd5_okf_import",
                version="1.0.0",
                name="OKF KB",
                publication_state="published",
            )
        )
        db.add(
            KnowledgeDocument(
                id="kdoc_sd5_okf_import_source",
                tenant_id="tenant_sd5_okf_import",
                knowledge_base_id="kb_sd5_okf_import",
                knowledge_base_version_id="kbver_sd5_okf_import",
                lineage_id="lineage_sd5_okf_import_source",
                filename="source.md",
                file_type="md",
                title="Source",
                status="ready",
                normalized_source="# Source\nExisting fact",
                source_kind="normalized",
            )
        )
        db.commit()
        request = KnowledgeOkfImportRequest(
            tenant_id="tenant_sd5_okf_import",
            knowledge_base_id="kb_sd5_okf_import",
            filename="bundle.md",
            content_base64=base64.b64encode(
                b"---\ntype: Topic\ntitle: Imported\n---\n\n# Summary\nImported fact"
            ).decode("ascii"),
        )

        result = import_okf_bundle(request, db, user)

        assert result["status"] == "imported"
        assert result["knowledge_base_version_id"] != "kbver_sd5_okf_import"
        old_concepts = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == "kbver_sd5_okf_import"
            )
        ).all()
        assert old_concepts == []
        knowledge_base = db.get(KnowledgeBase, "kb_sd5_okf_import")
        assert knowledge_base is not None
        assert knowledge_base.content_revision == 2


def test_sd5_okf_read_does_not_mutate_a_published_snapshot() -> None:
    """验证 OKF 读路径缺少派生概念时只返回不完整事实，不偷偷修写历史版本。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_sd5_okf_read", name="SD5 OKF read"))
        db.add(
            KnowledgeBase(
                id="kb_sd5_okf_read",
                tenant_id="tenant_sd5_okf_read",
                name="OKF read KB",
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_sd5_okf_read",
                tenant_id="tenant_sd5_okf_read",
                knowledge_base_id="kb_sd5_okf_read",
                version="1.0.0",
                name="OKF read KB",
                publication_state="published",
            )
        )
        document = KnowledgeDocument(
            id="kdoc_sd5_okf_read",
            tenant_id="tenant_sd5_okf_read",
            knowledge_base_id="kb_sd5_okf_read",
            knowledge_base_version_id="kbver_sd5_okf_read",
            lineage_id="lineage_sd5_okf_read",
            filename="read.md",
            file_type="md",
            title="Read document",
            status="ready",
            normalized_source="# Read\nA fact",
            metadata_json={
                "section_tree": [
                    {"section_id": "one", "title": "Read", "path": "Read", "content": "A fact"}
                ]
            },
        )
        db.add(document)
        db.add(
            KnowledgeBucket(
                id="kbucket_sd5_okf_read",
                tenant_id="tenant_sd5_okf_read",
                knowledge_base_id="kb_sd5_okf_read",
                knowledge_base_version_id="kbver_sd5_okf_read",
                document_id=document.id,
                bucket_key="read",
                title="Read",
                summary="A fact",
            )
        )
        db.commit()

        _ensure_okf_concepts_for_version(
            db,
            "tenant_sd5_okf_read",
            "kb_sd5_okf_read",
            "kbver_sd5_okf_read",
        )

        assert db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == "kbver_sd5_okf_read"
            )
        ).all() == []
