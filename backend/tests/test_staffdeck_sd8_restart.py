"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_staffdeck_sd8_restart.py
@CallChain  : pytest → SQLite初始化/遗留迁移 → 知识版本与物理派生身份
@Description: 防止重启把修订快照拆库，同时保留真正多文档遗留库的幂等迁移。
"""

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentKnowledgeBranch,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
    Tenant,
)
from app.db.sqlite_legacy import _knowledge_base_version_id, initialize_sqlite_database


def test_restart_preserves_published_and_candidate_snapshots(tmp_path):
    """完整重启两次不得移动原/新/隐藏快照、派生ID、manifest或员工发布头。"""
    engine = create_engine("sqlite:///" + str(tmp_path / "snapshots.sqlite3"))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="restart", name="Synthetic restart"))
        db.commit()
    initialize_sqlite_database(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id="kb-restart", tenant_id="restart", name="Snapshot policy"))
        db.add(AgentKnowledgeBranch(id="agentkb_owner_kb-restart", tenant_id="restart", agent_id="owner",
                                   knowledge_base_id="kb-restart", head_version="2.0.0"))
        for ordinal, state in enumerate(("published", "published", "candidate")):
            version = f"v-{ordinal}"
            document = f"doc-{ordinal}"
            bucket = f"bucket-{ordinal}"
            db.add(KnowledgeBaseVersion(id=version, tenant_id="restart",
                knowledge_base_id="kb-restart", name=version, version=f"{ordinal + 1}.0.0",
                scope="agent:owner", publication_state=state, build_job_id=f"job-{ordinal}",
                parent_version_id="v-0" if ordinal else None,
                content_manifest_json={"document_id_map": {"doc-0": document}},
                content_manifest_hash=f"manifest-{ordinal}"))
            db.add(KnowledgeDocument(id=document, tenant_id="restart",
                knowledge_base_id="kb-restart", knowledge_base_version_id=version,
                lineage_id="original-document", parent_document_id="doc-0" if ordinal else None,
                filename="policy.md", file_type="md", status="ready",
                normalized_source=f"immutable source {ordinal}"))
            db.add(KnowledgeBucket(id=bucket, tenant_id="restart", knowledge_base_id="kb-restart",
                knowledge_base_version_id=version, document_id=document,
                bucket_key="policy", title="Policy", summary="Immutable summary"))
            db.add(KnowledgeChunk(id=f"chunk-{ordinal}", tenant_id="restart",
                knowledge_base_id="kb-restart", knowledge_base_version_id=version,
                document_id=document, bucket_id=bucket, chunk_index=0,
                content=f"immutable source {ordinal}"))
            db.add(KnowledgeConcept(id=f"concept-{ordinal}", tenant_id="restart",
                knowledge_base_id="kb-restart", knowledge_base_version_id=version,
                document_id=document, concept_id="logical-policy", concept_type="Business Rule",
                title="Policy", content_md=f"immutable source {ordinal}"))
        db.commit()
    with engine.connect() as conn:
        before = _snapshot(conn)
    initialize_sqlite_database(engine)
    initialize_sqlite_database(engine)
    with engine.connect() as conn:
        assert _snapshot(conn) == before
    engine.dispose()


def _snapshot(conn):
    """读取合成租户相关整行，连同非目标发布状态和正文验证原字节保持。"""
    return {name: [tuple(row) for row in conn.execute(text(
        f"SELECT * FROM {name} WHERE tenant_id = 'restart' ORDER BY id"
    ))] for name in ("knowledge_bases", "knowledge_base_versions", "agent_knowledge_branches",
                    "knowledge_documents", "knowledge_buckets", "knowledge_chunks",
                    "knowledge_concepts")}


@pytest.mark.parametrize("identity", [
    {"build_job_id": "original-build"},
    {"parent_version_id": "original-parent"},
    {"content_manifest_json": {"document_id_map": {"original": "copy"}}},
    {"content_manifest_hash": "original-manifest-hash"},
    {"publication_state": "candidate"},
    {"scope": "agent:owner"},
], ids=["build", "parent", "manifest", "manifest-hash", "hidden", "agent-scope"])
def test_single_modern_version_is_not_legacy_multi_document(tmp_path, identity):
    """单个已有明确版本身份的整库也不能拆；不只保护多版本或单一文档。"""
    engine = create_engine("sqlite:///" + str(tmp_path / "single-modern.sqlite3"))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="restart", name="Synthetic single modern"))
        db.commit()
    initialize_sqlite_database(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id="kb-modern", tenant_id="restart", name="Modern multi document"))
        version_id = _knowledge_base_version_id("kb-modern", "1.0.0")
        db.add(KnowledgeBaseVersion(id=version_id, tenant_id="restart",
            knowledge_base_id="kb-modern", name="Modern", version="1.0.0", **identity))
        for ordinal in range(2):
            db.add(KnowledgeDocument(id=f"modern-doc-{ordinal}", tenant_id="restart",
                knowledge_base_id="kb-modern", knowledge_base_version_id=version_id,
                filename=f"modern-{ordinal}.md", file_type="md", status="ready"))
        db.commit()
    with engine.connect() as conn:
        before = _snapshot(conn)
    initialize_sqlite_database(engine)
    initialize_sqlite_database(engine)
    with engine.connect() as conn:
        assert _snapshot(conn) == before
    engine.dispose()


def test_legacy_multi_document_split_is_published_and_idempotent(tmp_path):
    """无修订身份的真正旧多文档库仍拆分，当前非空scope及重复启动均合法。"""
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.sqlite3"))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="restart", name="Synthetic legacy"))
        db.add(KnowledgeBase(id="kb-legacy", tenant_id="restart", name="Legacy multi document"))
        version_id = _knowledge_base_version_id("kb-legacy", "1.0.0")
        db.add(KnowledgeBaseVersion(id=version_id, tenant_id="restart",
            knowledge_base_id="kb-legacy", name="Legacy", version="1.0.0"))
        for ordinal in range(2):
            db.add(KnowledgeDocument(id=f"legacy-doc-{ordinal}", tenant_id="restart",
                knowledge_base_id="kb-legacy", knowledge_base_version_id=version_id,
                filename=f"old-{ordinal}.md", file_type="md", status="ready",
                normalized_source=f"legacy immutable source {ordinal}"))
        db.commit()
    initialize_sqlite_database(engine)
    with Session(engine) as db:
        docs = db.exec(select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == "restart")).all()
        assert len(docs) == 2
        assert len({doc.knowledge_base_id for doc in docs}) == 2
        assert all(doc.knowledge_base_id != "kb-legacy" for doc in docs)
        for doc in docs:
            version = db.get(KnowledgeBaseVersion, doc.knowledge_base_version_id)
            assert version is not None
            assert version.knowledge_base_id == doc.knowledge_base_id
            assert version.scope == "overall" and version.publication_state == "published"
    with engine.connect() as conn:
        before = _snapshot(conn)
    initialize_sqlite_database(engine)
    with engine.connect() as conn:
        assert _snapshot(conn) == before
    engine.dispose()


def test_legacy_original_version_id_closes_all_unversioned_assets(tmp_path):
    """旧库唯一1.0.0原ID贯穿文档、桶、片段、概念回填，重复启动不造悬空版本。"""
    engine = create_engine("sqlite:///" + str(tmp_path / "original-identity.sqlite3"))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="restart", name="Synthetic original identity"))
        db.commit()
    initialize_sqlite_database(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id="kb-identity", tenant_id="restart", name="Original identity"))
        db.add(KnowledgeBaseVersion(id="original-version", tenant_id="restart",
            knowledge_base_id="kb-identity", name="Original", version="1.0.0"))
        db.add(KnowledgeDocument(id="original-document", tenant_id="restart",
            knowledge_base_id="kb-identity", filename="legacy.md", file_type="md", status="ready"))
        db.add(KnowledgeBucket(id="original-bucket", tenant_id="restart",
            knowledge_base_id="kb-identity", document_id="original-document",
            bucket_key="original", title="Original", summary="Original"))
        db.add(KnowledgeChunk(id="original-chunk", tenant_id="restart",
            knowledge_base_id="kb-identity", document_id="original-document",
            bucket_id="original-bucket", chunk_index=0, content="Original"))
        db.add(KnowledgeConcept(id="original-concept", tenant_id="restart",
            knowledge_base_id="kb-identity", document_id="original-document",
            concept_id="original", concept_type="Topic", title="Original", content_md="Original"))
        db.commit()
    initialize_sqlite_database(engine)
    with Session(engine) as db:
        for model, identity in [(KnowledgeDocument, "original-document"),
                                (KnowledgeBucket, "original-bucket"),
                                (KnowledgeChunk, "original-chunk"),
                                (KnowledgeConcept, "original-concept")]:
            asset = db.get(model, identity)
            assert asset.knowledge_base_id == "kb-identity"
            assert asset.knowledge_base_version_id == "original-version"
            assert db.get(KnowledgeBaseVersion, asset.knowledge_base_version_id) is not None
        versions = db.exec(select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.knowledge_base_id == "kb-identity")).all()
        assert [version.id for version in versions] == ["original-version"]
    with engine.connect() as conn:
        before = _snapshot(conn)
    initialize_sqlite_database(engine)
    with engine.connect() as conn:
        assert _snapshot(conn) == before
    engine.dispose()
