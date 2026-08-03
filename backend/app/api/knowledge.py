"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : knowledge.py
@CallChain  : FastAPI Router → knowledge handlers → SQLModel Session → knowledge tables
@Description: 提供知识库 API 及跨方言的旧内容修复查询。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import LargeBinary, cast, delete, func, select as sa_select
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_open_gallery_binding,
    is_open_gallery_resource,
    knowledge_version_for_upload,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    user_creator_metadata,
)
from app.async_jobs import enqueue_async_job
from app.audit.service import append_user_management_audit
from app.db import get_session
from app.db.models import (
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    KnowledgeBase,
    KnowledgeBaseVersion,
    ModelConfig,
    User,
    utc_now,
)
from app.knowledge.schema import (
    KnowledgeBucketRead,
    KnowledgeChunkRead,
    KnowledgeChunkUpdateRequest,
    KnowledgeDiscoveryRead,
    KnowledgeDocumentRead,
    KnowledgeDocumentUpdateRequest,
    KnowledgeDocumentUploadRequest,
    KnowledgeBucketUpdateRequest,
    KnowledgeOkfImportRequest,
    KnowledgeIngestJobRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.knowledge.okf import (
    build_okf_for_document,
    create_concept_evidence_rows,
    parse_okf_bundle,
    upsert_concepts,
)
from app.knowledge.access import (
    accessible_knowledge_base_versions,
    resolve_knowledge_access,
)
from app.knowledge.service import (
    IngestPayload,
    KnowledgeDiscoveryConflictError,
    KnowledgeDiscoveryValidationError,
    KnowledgeService,
    bucket_read,
    chunk_read,
    validate_discovered_skill,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/knowledge",
    tags=["enterprise:knowledge"],
    dependencies=[Depends(get_current_user)],
)


@router.post("/documents", response_model=KnowledgeIngestJobRead)
def upload_document(
    request: KnowledgeDocumentUploadRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeIngestJobRead:
    """上传文档，解析写入目标知识库版本，并返回已排队的异步摄取任务。"""
    ensure_tenant(db, request.tenant_id)
    creator_metadata = user_creator_metadata(current_user, request.metadata or {})
    knowledge_base = _resolve_upload_knowledge_base(
        db,
        request,
        agent_id,
        current_user,
        creator_metadata=creator_metadata,
    )
    version = knowledge_version_for_upload(
        db,
        request.tenant_id,
        knowledge_base.id,
        agent_id,
        metadata_json=creator_metadata,
    )
    db.commit()
    service = KnowledgeService(db)
    job = service.create_ingest_job(
        IngestPayload(
            tenant_id=request.tenant_id,
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename=request.filename,
            content_base64=request.content_base64,
            title=request.title,
            metadata=creator_metadata,
        )
    )
    enqueue_async_job(
        "knowledge_ingest",
        service.run_ingest_job,
        job.id,
        metadata={"tenant_id": request.tenant_id, "filename": request.filename},
    )
    return job_read(job)


@router.post("/okf/import")
def import_okf_bundle(
    request: KnowledgeOkfImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """导入 OKF 压缩包，创建文档与概念证据，并返回导入结果标识及数量。"""
    ensure_tenant(db, request.tenant_id)
    try:
        content = base64.b64decode(request.content_base64)
        parsed_docs = parse_okf_bundle(request.filename, content)
    except Exception as exc:  # noqa: BLE001 - surface stable import failures.
        raise HTTPException(status_code=400, detail=f"OKF import failed: {exc}") from exc
    if not parsed_docs:
        raise HTTPException(
            status_code=400, detail="OKF bundle does not contain concept markdown files"
        )

    upload_request = KnowledgeDocumentUploadRequest(
        tenant_id=request.tenant_id,
        knowledge_base_id=request.knowledge_base_id,
        filename=request.filename,
        title=Path(request.filename).stem or "OKF Bundle",
        content_base64="",
        metadata={"okf_import": True, "source_filename": request.filename},
    )
    creator_metadata = user_creator_metadata(current_user, upload_request.metadata or {})
    knowledge_base = _resolve_upload_knowledge_base(
        db,
        upload_request,
        request.agent_id,
        current_user,
        creator_metadata=creator_metadata,
    )
    version = knowledge_version_for_upload(
        db,
        request.tenant_id,
        knowledge_base.id,
        request.agent_id,
        metadata_json=creator_metadata,
    )
    document = KnowledgeDocument(
        tenant_id=request.tenant_id,
        knowledge_base_id=knowledge_base.id,
        knowledge_base_version_id=version.id,
        filename=request.filename,
        file_type="okf",
        title=Path(request.filename).stem or request.filename,
        status="processing",
        metadata_json={
            **creator_metadata,
            "okf_import": True,
            "document_card": {
                "title": Path(request.filename).stem or request.filename,
                "filename": request.filename,
                "file_type": "okf",
                "summary": f"从 OKF bundle 导入 {len(parsed_docs)} 个概念页。",
                "outline": [
                    {
                        "section_id": item.concept_id,
                        "title": item.frontmatter.get("title") or item.concept_id,
                        "path": item.concept_id,
                        "level": 1,
                        "summary": item.frontmatter.get("description") or "",
                    }
                    for item in parsed_docs[:80]
                ],
                "applicable_scenarios": ["OKF Wiki", "业务知识检索"],
                "key_entities": sorted(
                    {str(item.frontmatter.get("type") or "Topic") for item in parsed_docs}
                ),
                "section_count": len(parsed_docs),
            },
            "okf": {"version": "0.1", "concept_count": len(parsed_docs)},
        },
    )
    db.add(document)
    db.flush()
    concept_rows = upsert_concepts(
        db,
        request.tenant_id,
        knowledge_base.id,
        version.id,
        [
            {
                "concept_id": item.concept_id,
                "content_md": item.content_md,
                "document_id": document.id,
                "source_refs": [{"document_id": document.id, "okf_file": f"{item.concept_id}.md"}],
            }
            for item in parsed_docs
        ],
    )
    create_concept_evidence_rows(
        db, request.tenant_id, knowledge_base.id, version.id, document, concept_rows
    )
    return {
        "status": "imported",
        "knowledge_base_id": knowledge_base.id,
        "knowledge_base_version_id": version.id,
        "version": version.version,
        "document_id": document.id,
        "concept_count": len(concept_rows),
    }


def _resolve_upload_knowledge_base(
    db: Session,
    request: KnowledgeDocumentUploadRequest,
    agent_id: str | None,
    current_user: object | None = None,
    creator_metadata: dict[str, Any] | None = None,
) -> KnowledgeBase:
    """解析上传目标知识库；必要时创建知识库及对应的私有或开放资源绑定。"""
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    if request.knowledge_base_id:
        knowledge_base = db.get(KnowledgeBase, request.knowledge_base_id)
        if (
            not knowledge_base
            or knowledge_base.tenant_id != request.tenant_id
            or knowledge_base.status == "archived"
        ):
            raise HTTPException(status_code=404, detail="Knowledge base not found")
        if not (agent and not agent.is_overall):
            _ensure_open_gallery_knowledge_admin(
                db, request.tenant_id, knowledge_base.id, current_user
            )
        return knowledge_base

    if not (agent and not agent.is_overall):
        ensure_open_gallery_admin(request.tenant_id, current_user)
    base_name = _knowledge_base_name_from_upload(request)
    name = _unique_knowledge_base_name(db, request.tenant_id, base_name)
    knowledge_base = KnowledgeBase(
        tenant_id=request.tenant_id,
        name=name,
        description=f"由文档 {request.filename} 创建",
        owner_user_id=getattr(current_user, "id", None),
        access_scope="owner",
        download_policy="restricted",
        revision=1,
        status="active",
        metadata_json={
            **(creator_metadata or user_creator_metadata(current_user, request.metadata or {})),
            "created_from_document_upload": True,
            "source_filename": request.filename,
        },
    )
    db.add(knowledge_base)
    db.flush()

    if agent and not agent.is_overall:
        mark_resource_private_for_agent(knowledge_base, agent.id, creator_metadata)
        ensure_agent_private_knowledge_branch(
            db,
            request.tenant_id,
            agent.id,
            knowledge_base,
            metadata_json=creator_metadata,
        )
    else:
        mark_resource_open_gallery(knowledge_base, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            knowledge_base.id,
            "active",
            metadata_json=creator_metadata,
        )
    return knowledge_base


def _knowledge_base_name_from_upload(request: KnowledgeDocumentUploadRequest) -> str:
    """按标题、文件主名和文件名的优先级生成上传知识库名称。"""
    title = (request.title or "").strip()
    if title:
        return title
    stem = Path(request.filename).stem.strip()
    return stem or request.filename.strip() or "未命名知识库"


def _unique_knowledge_base_name(db: Session, tenant_id: str, base_name: str) -> str:
    """在租户的 knowledge_bases 名称中追加序号以生成不重复名称。"""
    normalized_base = base_name.strip() or "未命名知识库"
    existing_names = set(
        db.exec(select(KnowledgeBase.name).where(KnowledgeBase.tenant_id == tenant_id)).all()
    )
    if normalized_base not in existing_names:
        return normalized_base
    index = 2
    while True:
        candidate = f"{normalized_base} {index}"
        if candidate not in existing_names:
            return candidate
        index += 1


@router.get(
    "/jobs",
    response_model=list[KnowledgeIngestJobRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_jobs(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(8, ge=1, le=50),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeIngestJobRead]:
    """列出当前范围可见的知识摄取任务。

    超时的取消请求会持久化为已取消；存在部分摄取文档时，还会删除文档及关联的发现建议、概念、分块和分桶数据。
    """
    ensure_tenant(db, tenant_id)
    KnowledgeService(db).finalize_stale_cancel_requested_jobs(tenant_id)
    visible_versions = _accessible_knowledge_versions(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        agent_id=agent_id,
    )
    visible_version_ids = [version.id for version in visible_versions.values()]
    if not visible_version_ids:
        return []
    statement = select(KnowledgeIngestJob).where(
        KnowledgeIngestJob.tenant_id == tenant_id,
        KnowledgeIngestJob.knowledge_base_version_id.in_(visible_version_ids),
    )
    if status:
        statuses = [item.strip() for item in status.split(",") if item.strip()]
        if statuses:
            statement = statement.where(KnowledgeIngestJob.status.in_(statuses))
    rows = db.exec(
        statement.order_by(
            KnowledgeIngestJob.created_at.desc(), KnowledgeIngestJob.id.desc()
        ).limit(limit)
    ).all()
    return [job_read(row) for row in rows]


@router.get(
    "/jobs/{job_id}",
    response_model=KnowledgeIngestJobRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_job(
    job_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeIngestJobRead:
    """读取当前范围可见的指定知识摄取任务。

    若取消请求已超时，则持久化取消状态；存在部分摄取文档时，还会删除文档及关联的发现建议、概念、分块和分桶数据。
    """
    ensure_tenant(db, tenant_id)
    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge ingest job not found")
    _ensure_knowledge_version_visible(
        db,
        tenant_id,
        job.knowledge_base_version_id,
        agent_id,
        current_user,
    )
    KnowledgeService(db).finalize_stale_cancel_requested_job(job)
    return job_read(job)


@router.post("/jobs/{job_id}/cancel", response_model=KnowledgeIngestJobRead)
def cancel_job(
    job_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeIngestJobRead:
    """请求取消指定租户的知识摄取任务，并返回取消后的任务状态。"""
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    existing = db.get(KnowledgeIngestJob, job_id)
    if existing and existing.tenant_id == tenant_id:
        _ensure_open_gallery_knowledge_admin(
            db, tenant_id, existing.knowledge_base_id, current_user
        )
    job = KnowledgeService(db).cancel_ingest_job(job_id, tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail="Knowledge ingest job not found")
    return job_read(job)


@router.get(
    "/documents",
    response_model=list[KnowledgeDocumentRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_documents(
    tenant_id: str = Query(...),
    knowledge_base_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    include_all_versions: bool = Query(False),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeDocumentRead]:
    """列出当前范围可见的知识文档，可限定知识库及是否包含全部版本。"""
    ensure_tenant(db, tenant_id)
    visible_versions = _accessible_knowledge_versions(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        agent_id=agent_id,
        requested_knowledge_base_ids=[knowledge_base_id] if knowledge_base_id else None,
    )
    if not visible_versions:
        return []
    if knowledge_base_id and knowledge_base_id not in visible_versions:
        return []
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant_id)
    if include_all_versions and not agent_id:
        visible_knowledge_base_ids = (
            [knowledge_base_id] if knowledge_base_id else list(visible_versions)
        )
        stmt = stmt.where(KnowledgeDocument.knowledge_base_id.in_(visible_knowledge_base_ids))
    else:
        visible_version_ids = [
            version.id
            for base_id, version in visible_versions.items()
            if not knowledge_base_id or base_id == knowledge_base_id
        ]
        stmt = stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(visible_version_ids))
    rows = db.exec(stmt.order_by(KnowledgeDocument.created_at.desc())).all()
    return [document_read(row) for row in rows]


@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_document(
    document_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeDocumentRead:
    """获取租户内指定文档，并在确认其知识库版本可见后返回文档详情。"""
    row = _get_document(db, tenant_id, document_id)
    _ensure_knowledge_version_visible(
        db,
        tenant_id,
        row.knowledge_base_version_id,
        agent_id,
        current_user,
    )
    return document_read(row)


@router.put("/documents/{document_id}", response_model=KnowledgeDocumentRead)
def update_document(
    document_id: str,
    request: KnowledgeDocumentUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeDocumentRead:
    """提交文档字段；文档存在章节或分桶时，刷新并持久化来源类 OKF 概念后返回详情。"""
    row = _get_document(db, request.tenant_id, document_id)
    _ensure_open_gallery_knowledge_admin(db, request.tenant_id, row.knowledge_base_id, current_user)
    metadata = dict(row.metadata_json or {})
    if request.metadata is not None:
        metadata = metadata_preserving_creator(row.metadata_json, request.metadata)
    if request.title is not None:
        row.title = request.title.strip() or row.filename
        document_card = (
            metadata.get("document_card") if isinstance(metadata.get("document_card"), dict) else {}
        )
        metadata["document_card"] = {**document_card, "title": row.title}
    if request.status is not None:
        row.status = request.status
    if request.metadata is not None or request.title is not None:
        row.metadata_json = metadata
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    _refresh_document_okf_concepts(db, row)
    return document_read(row)


@router.get(
    "/documents/{document_id}/buckets",
    response_model=list[KnowledgeBucketRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_document_buckets(
    document_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeBucketRead]:
    """列出文档的知识分桶及分块统计，并兼容读取 SQLite 遗留字节字段。"""
    document = _get_document(db, tenant_id, document_id)
    _ensure_knowledge_version_visible(
        db,
        tenant_id,
        document.knowledge_base_version_id,
        agent_id,
        current_user,
    )
    rows = _safe_document_bucket_rows(db, tenant_id, document_id)
    chunk_counts = dict(
        db.exec(
            select(KnowledgeChunk.bucket_id, func.count(KnowledgeChunk.id))
            .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.document_id == document_id)
            .group_by(KnowledgeChunk.bucket_id)
        ).all()
    )
    return [
        _bucket_read_mapping_with_stats(row, int(chunk_counts.get(str(row.get("id")), 0)))
        for row in rows
    ]


@router.put("/buckets/{bucket_id}", response_model=KnowledgeBucketRead)
def update_bucket(
    bucket_id: str,
    request: KnowledgeBucketUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBucketRead:
    """更新并提交分桶；找到所属文档时，刷新并持久化来源类 OKF 概念后返回统计结果。"""
    ensure_tenant(db, request.tenant_id)
    row = db.get(KnowledgeBucket, bucket_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge bucket not found")
    _ensure_open_gallery_knowledge_admin(db, request.tenant_id, row.knowledge_base_id, current_user)
    if request.title is not None:
        row.title = request.title.strip() or row.title
    if request.summary is not None:
        row.summary = request.summary
    if request.metadata is not None:
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            request.metadata,
        )
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    document = db.get(KnowledgeDocument, row.document_id)
    if document:
        _refresh_document_okf_concepts(db, document)
    chunk_count = db.exec(
        select(func.count(KnowledgeChunk.id)).where(
            KnowledgeChunk.tenant_id == request.tenant_id,
            KnowledgeChunk.bucket_id == bucket_id,
        )
    ).one()
    return bucket_read_with_stats(row, int(chunk_count or 0))


@router.get(
    "/buckets/{bucket_id}/chunks",
    response_model=list[KnowledgeChunkRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_bucket_chunks(
    bucket_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeChunkRead]:
    """列出指定分桶的知识分块，并将 SQLite 遗留字节内容规范化为文本。"""
    ensure_tenant(db, tenant_id)
    bucket = db.get(KnowledgeBucket, bucket_id)
    if not bucket or bucket.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge bucket not found")
    _ensure_knowledge_version_visible(
        db,
        tenant_id,
        bucket.knowledge_base_version_id,
        agent_id,
        current_user,
    )
    rows = _safe_bucket_chunk_rows(db, tenant_id, bucket_id)
    return [_chunk_read_mapping(row) for row in rows]


@router.put("/chunks/{chunk_id}", response_model=KnowledgeChunkRead)
def update_chunk(
    chunk_id: str,
    request: KnowledgeChunkUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeChunkRead:
    """更新分块并重建所属分桶汇总；找到所属文档时，刷新并持久化来源类 OKF 概念后返回详情。"""
    ensure_tenant(db, request.tenant_id)
    row = db.get(KnowledgeChunk, chunk_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge chunk not found")
    _ensure_open_gallery_knowledge_admin(db, request.tenant_id, row.knowledge_base_id, current_user)
    if request.content is not None:
        row.content = request.content
    if request.summary is not None:
        row.summary = request.summary
    if request.metadata is not None:
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            request.metadata,
        )
    row.updated_at = utc_now()
    db.add(row)
    bucket = _sync_bucket_content_from_chunks(db, request.tenant_id, row.bucket_id)
    db.commit()
    db.refresh(row)
    if bucket:
        db.refresh(bucket)
        document = db.get(KnowledgeDocument, bucket.document_id)
        if document:
            _refresh_document_okf_concepts(db, document)
    return chunk_read(row)


@router.post("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    request: KnowledgeSearchRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeSearchResponse:
    """在智能体可见的知识库版本中执行检索，并返回检索结果与路由轨迹。"""
    require_agent_scope_viewer(request.tenant_id, request.agent_id, current_user, db)
    ensure_tenant(db, request.tenant_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    access = resolve_knowledge_access(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        agent_id=request.agent_id,
        requested_knowledge_base_ids=(
            request.knowledge_base_ids if request.knowledge_base_ids else None
        ),
    )
    visible_versions = accessible_knowledge_base_versions(db, resolution=access)
    if not visible_versions:
        _append_knowledge_access_denied(
            db,
            tenant_id=request.tenant_id,
            current_user=current_user,
            action="knowledge.search",
            resource_type="agent_profile",
            resource_id=request.agent_id,
            reason="empty_access_intersection",
            detail={"requested_knowledge_base_count": len(request.knowledge_base_ids or [])},
        )
        db.commit()
        return _no_accessible_knowledge_response(
            "当前成员、数字员工与请求范围没有共同可用知识"
        )
    request.knowledge_base_ids = list(visible_versions)
    visible_version_ids = [version.id for version in visible_versions.values()]
    explicitly_requested_version_ids = bool(request.knowledge_base_version_ids)
    if explicitly_requested_version_ids:
        allowed_ids = set(visible_version_ids)
        request.knowledge_base_version_ids = [
            version_id
            for version_id in request.knowledge_base_version_ids
            if version_id in allowed_ids
        ]
        if not request.knowledge_base_version_ids:
            _append_knowledge_access_denied(
                db,
                tenant_id=request.tenant_id,
                current_user=current_user,
                action="knowledge.search",
                resource_type="agent_profile",
                resource_id=request.agent_id,
                reason="requested_versions_outside_intersection",
                detail={
                    "requested_knowledge_base_count": len(
                        request.knowledge_base_ids or []
                    )
                },
            )
            db.commit()
            return _no_accessible_knowledge_response(
                "请求指定的知识版本不在当前成员与数字员工的共同可用范围"
            )
    else:
        request.knowledge_base_version_ids = visible_version_ids
    return KnowledgeService(db).search(request, model_config)


def _no_accessible_knowledge_response(message: str) -> KnowledgeSearchResponse:
    """构造稳定的无共同知识响应，避免空过滤条件被下游解释为不限制。"""

    trace = [{"phase": "no_accessible_knowledge", "message": message}]
    return KnowledgeSearchResponse(trace=trace, route_trace=trace)


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig | None:
    """从 model_configs 获取租户已启用的默认模型配置。"""
    return db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()


def _get_request_model(
    db: Session, tenant_id: str, model_config_id: str | None = None
) -> ModelConfig | None:
    """解析请求指定的已启用模型配置；未指定时返回租户默认模型。"""
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or not model_config.enabled:
        raise HTTPException(status_code=404, detail="Model config not found")
    return model_config


@router.get(
    "/discoveries",
    response_model=list[KnowledgeDiscoveryRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_discoveries(
    tenant_id: str = Query(...),
    knowledge_base_id: str | None = Query(None),
    status: str | None = Query(None),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeDiscoveryRead]:
    """列出当前范围可见的知识发现建议，可按知识库和状态筛选。"""
    ensure_tenant(db, tenant_id)
    visible_versions = _accessible_knowledge_versions(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        agent_id=agent_id,
        requested_knowledge_base_ids=[knowledge_base_id] if knowledge_base_id else None,
    )
    if knowledge_base_id and knowledge_base_id not in visible_versions:
        return []
    visible_version_ids = [
        version.id
        for base_id, version in visible_versions.items()
        if not knowledge_base_id or base_id == knowledge_base_id
    ]
    if not visible_version_ids:
        return []
    stmt = select(KnowledgeDiscoverySuggestion).where(
        KnowledgeDiscoverySuggestion.tenant_id == tenant_id,
        KnowledgeDiscoverySuggestion.knowledge_base_version_id.in_(visible_version_ids),
    )
    if status:
        stmt = stmt.where(KnowledgeDiscoverySuggestion.status == status)
    rows = db.exec(stmt.order_by(KnowledgeDiscoverySuggestion.created_at.desc())).all()
    visible_rows: list[KnowledgeDiscoverySuggestion] = []
    for row in rows:
        if row.status == "pending" and row.suggestion_type == "skill":
            payload = row.payload_json or {}
            skill_payload = (
                payload.get("draft_skill")
                if isinstance(payload.get("draft_skill"), dict)
                else payload
            )
            try:
                validate_discovered_skill(skill_payload)
            except KnowledgeDiscoveryValidationError:
                continue
        visible_rows.append(row)
    return [discovery_read(row) for row in visible_rows]


@router.post("/discoveries/{suggestion_id}/confirm")
def confirm_discovery(
    suggestion_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """确认知识发现建议，执行服务层落库操作并返回确认结果。"""
    row = _get_discovery(db, tenant_id, suggestion_id)
    _ensure_open_gallery_knowledge_admin(db, tenant_id, row.knowledge_base_id, current_user)
    try:
        result = KnowledgeService(db).confirm_discovery(row)
    except KnowledgeDiscoveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KnowledgeDiscoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "confirmed", "result": result}


@router.post("/discoveries/{suggestion_id}/reject")
def reject_discovery(
    suggestion_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """拒绝知识发现建议，持久化拒绝状态并返回操作结果。"""
    row = _get_discovery(db, tenant_id, suggestion_id)
    _ensure_open_gallery_knowledge_admin(db, tenant_id, row.knowledge_base_id, current_user)
    try:
        KnowledgeService(db).reject_discovery(row)
    except KnowledgeDiscoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "rejected"}


def _refresh_document_okf_concepts(db: Session, document: KnowledgeDocument) -> None:
    """无章节且无分桶时直接返回；否则删除旧来源概念、构建新概念，并由 upsert_concepts 提交事务。"""
    metadata = document.metadata_json or {}
    section_nodes = (
        metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
    )
    buckets = db.exec(
        select(KnowledgeBucket)
        .where(
            KnowledgeBucket.tenant_id == document.tenant_id,
            KnowledgeBucket.knowledge_base_id == document.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == document.knowledge_base_version_id,
            KnowledgeBucket.document_id == document.id,
        )
        .order_by(KnowledgeBucket.created_at.asc())
    ).all()
    if not section_nodes and not buckets:
        return
    db.exec(
        delete(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == document.tenant_id,
            KnowledgeConcept.knowledge_base_id == document.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == document.knowledge_base_version_id,
            KnowledgeConcept.document_id == document.id,
            KnowledgeConcept.concept_type.in_(["Source Document", "Source Section"]),
        )
    )
    db.flush()
    upsert_concepts(
        db,
        document.tenant_id,
        document.knowledge_base_id,
        document.knowledge_base_version_id,
        build_okf_for_document(document, section_nodes, buckets),
    )


def _sync_bucket_content_from_chunks(
    db: Session,
    tenant_id: str,
    bucket_id: str,
) -> KnowledgeBucket | None:
    """按分块顺序重建 knowledge_buckets 的内容摘要元数据并暂存更新，不自行提交。"""
    bucket = db.get(KnowledgeBucket, bucket_id)
    if not bucket or bucket.tenant_id != tenant_id:
        return None
    chunks = db.exec(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.bucket_id == bucket_id)
        .order_by(KnowledgeChunk.chunk_index.asc())
    ).all()
    content = "\n\n".join(chunk.content for chunk in chunks if chunk.content.strip()).strip()
    metadata = dict(bucket.metadata_json or {})
    metadata["content"] = content[:6000]
    metadata["chunk_count"] = len(chunks)
    metadata["representative_chunk_ids"] = [chunk.id for chunk in chunks[:3]]
    bucket.metadata_json = metadata
    bucket.token_estimate = max(1, len(content) // 2) if content else bucket.token_estimate
    bucket.updated_at = utc_now()
    db.add(bucket)
    return bucket


def job_read(row: KnowledgeIngestJob) -> KnowledgeIngestJobRead:
    """将知识摄取任务模型转换为响应对象，并从元数据中移除原始内容载荷。"""
    return KnowledgeIngestJobRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        filename=row.filename,
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        error=row.error,
        metadata={
            key: value
            for key, value in (row.metadata_json or {}).items()
            if key != "content_base64"
        },
        created_at=row.created_at.isoformat(),
        started_at=row.started_at.isoformat() if row.started_at else None,
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
        updated_at=row.updated_at.isoformat(),
    )


def document_read(row: KnowledgeDocument) -> KnowledgeDocumentRead:
    """将知识文档模型转换为包含 ISO 时间文本的响应对象。"""
    return KnowledgeDocumentRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_base_version_id=row.knowledge_base_version_id,
        filename=row.filename,
        file_type=row.file_type,
        title=row.title,
        status=row.status,
        bucket_count=row.bucket_count,
        chunk_count=row.chunk_count,
        metadata=row.metadata_json or {},
        error=row.error,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def bucket_read_with_stats(row: KnowledgeBucket, chunk_count: int) -> KnowledgeBucketRead:
    """将知识分桶转换为响应对象，并依据分块数和摘要补充统计与就绪状态。"""
    item = bucket_read(row)
    item.chunk_count = chunk_count
    item.status = "ready" if chunk_count > 0 and row.summary.strip() else "incomplete"
    return item


def _legacy_bytes_column(column, dialect_name: str):
    """将 SQLite 遗留文本列转为二进制以保留原始字节；MySQL 等方言保持原列。"""
    if dialect_name == "sqlite":
        return cast(column, LargeBinary).label(column.name)
    return column


def _document_bucket_statement(dialect_name: str, tenant_id: str, document_id: str):
    """构造 knowledge_buckets 查询，按租户和文档筛选；SQLite 以字节读取遗留文本，MySQL 保持文本列。"""
    columns = KnowledgeBucket.__table__.c

    def legacy(column):
        """按当前数据库方言为 knowledge_buckets 遗留文本列选择兼容表达式。"""
        return _legacy_bytes_column(column, dialect_name)

    return (
        sa_select(
            columns.id,
            columns.tenant_id,
            columns.knowledge_base_id,
            columns.knowledge_base_version_id,
            columns.document_id,
            legacy(columns.bucket_key),
            legacy(columns.title),
            legacy(columns.summary),
            columns.token_estimate,
            legacy(columns.metadata_json),
            columns.created_at,
            columns.updated_at,
        )
        .where(columns.tenant_id == tenant_id, columns.document_id == document_id)
        .order_by(columns.created_at.asc())
    )


def _bucket_chunk_statement(dialect_name: str, tenant_id: str, bucket_id: str):
    """构造 knowledge_chunks 查询，按租户和分桶筛选；SQLite 以字节读取遗留文本，MySQL 保持文本列。"""
    columns = KnowledgeChunk.__table__.c

    def legacy(column):
        """按当前数据库方言为 knowledge_chunks 遗留文本列选择兼容表达式。"""
        return _legacy_bytes_column(column, dialect_name)

    return (
        sa_select(
            columns.id,
            columns.tenant_id,
            columns.knowledge_base_id,
            columns.knowledge_base_version_id,
            columns.document_id,
            columns.bucket_id,
            columns.chunk_index,
            legacy(columns.content),
            legacy(columns.summary),
            legacy(columns.source_ref),
            legacy(columns.metadata_json),
            columns.created_at,
            columns.updated_at,
        )
        .where(columns.tenant_id == tenant_id, columns.bucket_id == bucket_id)
        .order_by(columns.chunk_index.asc())
    )


def _safe_document_bucket_rows(
    db: Session, tenant_id: str, document_id: str
) -> list[Mapping[str, Any]]:
    """执行 knowledge_buckets 的跨方言安全查询并返回映射行。"""
    bind = db.get_bind()
    return list(
        db.execute(_document_bucket_statement(bind.dialect.name, tenant_id, document_id))
        .mappings().all()
    )


def _safe_bucket_chunk_rows(db: Session, tenant_id: str, bucket_id: str) -> list[Mapping[str, Any]]:
    """执行 knowledge_chunks 的跨方言安全查询并返回映射行。"""
    bind = db.get_bind()
    return list(
        db.execute(_bucket_chunk_statement(bind.dialect.name, tenant_id, bucket_id))
        .mappings().all()
    )


def _bucket_read_mapping_with_stats(
    row: Mapping[str, Any], chunk_count: int
) -> KnowledgeBucketRead:
    """将含遗留字节值的分桶映射规范化为文本、对象和整数后生成响应。"""
    summary = _safe_text(row.get("summary"))
    metadata = _safe_json_object(row.get("metadata_json"))
    return KnowledgeBucketRead(
        id=_safe_text(row.get("id")),
        tenant_id=_safe_text(row.get("tenant_id")),
        knowledge_base_id=_safe_text(row.get("knowledge_base_id")),
        document_id=_safe_text(row.get("document_id")),
        bucket_key=_safe_text(row.get("bucket_key")),
        title=_safe_text(row.get("title"), "未命名片段"),
        summary=summary,
        token_estimate=_safe_int(row.get("token_estimate")),
        chunk_count=chunk_count
        or int(
            metadata.get("chunk_count") or len(metadata.get("representative_chunk_ids") or []) or 0
        ),
        status="ready" if chunk_count > 0 and summary.strip() else "incomplete",
        metadata=metadata,
        created_at=_safe_datetime_text(row.get("created_at")),
        updated_at=_safe_datetime_text(row.get("updated_at")),
    )


def _chunk_read_mapping(row: Mapping[str, Any]) -> KnowledgeChunkRead:
    """将含遗留字节值的分块映射规范化为文本、对象和整数后生成响应。"""
    return KnowledgeChunkRead(
        id=_safe_text(row.get("id")),
        tenant_id=_safe_text(row.get("tenant_id")),
        knowledge_base_id=_safe_text(row.get("knowledge_base_id")),
        document_id=_safe_text(row.get("document_id")),
        bucket_id=_safe_text(row.get("bucket_id")),
        chunk_index=_safe_int(row.get("chunk_index")),
        content=_safe_text(row.get("content")),
        summary=_safe_optional_text(row.get("summary")),
        source_ref=_safe_optional_text(row.get("source_ref")),
        metadata=_safe_json_object(row.get("metadata_json")),
        created_at=_safe_datetime_text(row.get("created_at")),
        updated_at=_safe_datetime_text(row.get("updated_at")),
    )


def _safe_text(value: Any, fallback: str = "") -> str:
    """将遗留字符串或字节值按 UTF-8、GB18030 顺序解码为规范文本，失败时替换坏字节。"""
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")
    return str(value)


def _safe_optional_text(value: Any) -> str | None:
    """将可空的遗留字符串或字节值规范化为非空文本，否则返回空值。"""
    if value is None:
        return None
    text_value = _safe_text(value)
    return text_value if text_value else None


def _safe_json_object(value: Any) -> dict[str, Any]:
    """将字典、JSON 文本或遗留 JSON 字节规范化为对象，非法或非对象输入返回空字典。"""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        parsed = json.loads(_safe_text(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_int(value: Any) -> int:
    """将遗留数值输入转为整数；空值及触发 TypeError 或 ValueError 的输入返回零。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_datetime_text(value: Any) -> str:
    """将日期时间转为 ISO 文本，其他遗留字符串或字节值按安全文本规则规范化。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_text(value)


def discovery_read(row: KnowledgeDiscoverySuggestion) -> KnowledgeDiscoveryRead:
    """将知识发现建议模型转换为包含 ISO 时间文本的响应对象。"""
    return KnowledgeDiscoveryRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        bucket_id=row.bucket_id,
        suggestion_type=row.suggestion_type,  # type: ignore[arg-type]
        title=row.title,
        status=row.status,
        payload=row.payload_json or {},
        source_refs=row.source_refs_json or [],
        reason=row.reason,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _get_document(db: Session, tenant_id: str, document_id: str) -> KnowledgeDocument:
    """获取租户内的 knowledge_documents 记录，不存在或跨租户时返回 404。"""
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeDocument, document_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return row


def _ensure_knowledge_version_visible(
    db: Session,
    tenant_id: str,
    knowledge_base_version_id: str | None,
    agent_id: str | None,
    current_user: User,
) -> None:
    """确认版本同时命中活动成员范围、Agent 当前绑定和知识库当前版本。"""
    if not knowledge_base_version_id:
        _append_knowledge_access_denied(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            action="knowledge.read",
            resource_type="knowledge_base_version",
            resource_id=None,
            reason="missing_version_binding",
        )
        db.commit()
        raise HTTPException(
            status_code=404,
            detail="Knowledge resource has no version binding; re-ingest the document or restore its knowledge-base version",
        )
    version = db.get(KnowledgeBaseVersion, knowledge_base_version_id)
    if not version or version.tenant_id != tenant_id:
        _append_knowledge_access_denied(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            action="knowledge.read",
            resource_type="knowledge_base_version",
            resource_id=knowledge_base_version_id,
            reason="version_not_found",
        )
        db.commit()
        raise HTTPException(
            status_code=404,
            detail=f"Knowledge-base version {knowledge_base_version_id} does not exist in tenant {tenant_id}",
        )
    visible_versions = _accessible_knowledge_versions(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        agent_id=agent_id,
        requested_knowledge_base_ids=[version.knowledge_base_id],
    )
    visible_version = visible_versions.get(version.knowledge_base_id)
    if visible_version is None or visible_version.id != version.id:
        knowledge_base = db.get(KnowledgeBase, version.knowledge_base_id)
        _append_knowledge_access_denied(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            action="knowledge.read",
            resource_type="knowledge_base_version",
            resource_id=version.id,
            target_org_unit_id=(
                knowledge_base.responsible_org_unit_id if knowledge_base else None
            ),
            reason="outside_access_intersection",
            detail={"knowledge_base_id": version.knowledge_base_id},
        )
        db.commit()
        raise HTTPException(
            status_code=404,
            detail="Knowledge resource version is not visible in the current intersection",
        )


def _append_knowledge_access_denied(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    action: str,
    resource_type: str,
    resource_id: str | None,
    reason: str,
    target_org_unit_id: str | None = None,
    detail: Mapping[str, object] | None = None,
) -> None:
    """追加不含查询词和知识正文的访问拒绝审计，由调用方提交当前事务。"""

    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=tenant_id,
        permission_code="knowledge.read",
        action=action,
        action_kind="read",
        outcome="denied",
        resource_type=resource_type,
        resource_id=resource_id,
        target_org_unit_id=target_org_unit_id,
        detail={"reason": reason, **(detail or {})},
    )


def _accessible_knowledge_versions(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    agent_id: str | None,
    requested_knowledge_base_ids: list[str] | None = None,
) -> dict[str, KnowledgeBaseVersion]:
    """计算成员、Agent 与请求限定交集，并返回每库唯一当前版本。"""

    resolution = resolve_knowledge_access(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        agent_id=agent_id,
        requested_knowledge_base_ids=requested_knowledge_base_ids,
    )
    return accessible_knowledge_base_versions(db, resolution=resolution)


def _get_discovery(db: Session, tenant_id: str, suggestion_id: str) -> KnowledgeDiscoverySuggestion:
    """获取租户内的 knowledge_discovery_suggestions 记录，不存在时返回 404。"""
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeDiscoverySuggestion, suggestion_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge discovery not found")
    return row


def _ensure_open_gallery_knowledge_admin(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    current_user: object | None,
) -> None:
    """按知识库归属要求智能体管理员或开放资源库管理员权限。"""
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    metadata = (
        knowledge_base.metadata_json
        if knowledge_base and isinstance(knowledge_base.metadata_json, dict)
        else {}
    )
    owner_agent_id = metadata.get("owner_agent_id")
    if isinstance(owner_agent_id, str) and owner_agent_id:
        ensure_agent_scope_manager(db, tenant_id, owner_agent_id, current_user)
        return
    if (
        knowledge_base
        and knowledge_base.tenant_id == tenant_id
        and is_open_gallery_resource(db, tenant_id, "knowledge_base", knowledge_base)
    ):
        ensure_open_gallery_admin(tenant_id, current_user)
