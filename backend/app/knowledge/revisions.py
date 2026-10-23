"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : revisions.py
@CallChain  : Knowledge API → revision prepare → KnowledgeIngestJob/KnowledgeBaseVersion
@Description: 承载 SD-5 正文修订的规范化、CAS 依据和持久幂等受理合同。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import time
import unicodedata

from sqlalchemy import and_, delete, or_, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from sqlmodel import select

from app.agents.branching import ensure_knowledge_base_version
from app.db import engine
from app.db.models import (
    AgentKnowledgeBranch,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    new_id,
    User,
    utc_now,
)


MAX_REVISION_SOURCE_CHARS = 2_000_000
REVISION_BUILDER_VERSION = "sd5-revision-v1"
REVISION_MAX_ATTEMPTS = 3
REVISION_LEASE_SECONDS = 60
REVISION_PUBLICATION_LEASE_SECONDS = 60
REVISION_PUBLICATION_RECONCILE_SECONDS = 3.0
REVISION_RECOVERY_BATCH_SIZE = 100
INITIAL_REVISION_DOCUMENT_ID = "__initial_revision_document__"


class KnowledgeRevisionInputError(ValueError):
    """正文修订输入不满足规范化、作用域或身份合同。"""


class KnowledgeRevisionConflictError(ValueError):
    """正文修订依据已过期或同一客户端身份提交了不同内容。"""


@dataclass(frozen=True, slots=True)
class RevisionPrepareInput:
    """描述一次正文修订在受理前冻结的租户、作用域、版本和正文意图。"""

    tenant_id: str
    knowledge_base_id: str
    scope: str
    actor_user_id: str
    document_id: str
    content_md: str
    expected_revision: int
    client_request_id: str
    source_version_id: str | None = None
    title: str | None = None
    operation_kind: str = "content"
    mutation_payload: dict[str, object] | None = None


def normalize_revision_source(content_md: str) -> str:
    """规范化正文换行与边界空白，拒绝空白正文和超过合同上限的输入。"""

    if not isinstance(content_md, str):
        raise KnowledgeRevisionInputError("正文必须是文本。")
    normalized = unicodedata.normalize("NFC", content_md)
    normalized = normalized.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if not normalized:
        raise KnowledgeRevisionInputError("正文不能为空。")
    if len(normalized) > MAX_REVISION_SOURCE_CHARS:
        raise KnowledgeRevisionInputError(
            f"正文长度不能超过 {MAX_REVISION_SOURCE_CHARS:,} 个字符。"
        )
    return normalized


def revision_request_hash(request: RevisionPrepareInput, normalized_source: str) -> str:
    """计算绑定作用域、目标文档、CAS 依据和规范正文的稳定请求摘要。"""

    payload = {
        "tenant_id": request.tenant_id,
        "knowledge_base_id": request.knowledge_base_id,
        "scope": request.scope,
        "actor_user_id": request.actor_user_id,
        "document_id": request.document_id,
        "content_md": normalized_source,
        "expected_revision": request.expected_revision,
        "client_request_id": request.client_request_id,
        "source_version_id": request.source_version_id,
        "title": request.title,
        "operation_kind": request.operation_kind,
        "mutation_payload": request.mutation_payload or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def prepare_revision(db: Session, request: RevisionPrepareInput) -> KnowledgeIngestJob:
    """以短事务受理一次正文修订，并在不推进 head 的前提下创建候选版本和任务。"""

    normalized_source = normalize_revision_source(request.content_md)
    tenant_id = request.tenant_id.strip()
    knowledge_base_id = request.knowledge_base_id.strip()
    scope = request.scope.strip()
    actor_user_id = request.actor_user_id.strip()
    document_id = request.document_id.strip()
    client_request_id = request.client_request_id.strip()
    if not all((tenant_id, knowledge_base_id, scope, actor_user_id, document_id, client_request_id)):
        raise KnowledgeRevisionInputError("正文修订缺少租户、作用域、操作者、文档或客户端身份。")
    if request.expected_revision < 1:
        raise KnowledgeRevisionInputError("正文修订 expected_revision 必须为正整数。")
    if len(client_request_id) > 64:
        raise KnowledgeRevisionInputError("正文修订 client_request_id 不能超过 64 个字符。")
    if scope != "overall" and not scope.startswith("agent:"):
        raise KnowledgeRevisionInputError("正文修订 scope 只能是 overall 或 agent:<id>。")
    if request.operation_kind not in {
        "content",
        "document_fields",
        "bucket_fields",
        "chunk_fields",
        "concept_upsert",
        "upload",
        "version_fields",
        "okf_import",
    }:
        raise KnowledgeRevisionInputError("正文修订 operation_kind 不受支持。")

    normalized_request = RevisionPrepareInput(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        scope=scope,
        actor_user_id=actor_user_id,
        document_id=document_id,
        content_md=normalized_source,
        expected_revision=request.expected_revision,
        client_request_id=client_request_id,
        source_version_id=request.source_version_id.strip() if request.source_version_id else None,
        title=request.title.strip() if request.title else None,
        operation_kind=request.operation_kind,
        mutation_payload=dict(request.mutation_payload or {}),
    )
    request_hash = revision_request_hash(normalized_request, normalized_source)
    dedupe_key = _revision_dedupe_key(normalized_request)
    existing = _revision_job_by_dedupe(db, tenant_id, dedupe_key)
    if existing:
        if existing.request_hash == request_hash and existing.job_kind == "revision":
            return existing
        raise KnowledgeRevisionConflictError("同一客户端身份已经提交了不同的正文修订。")

    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if not knowledge_base or knowledge_base.tenant_id != tenant_id:
        raise KnowledgeRevisionInputError("知识库不存在或不属于当前租户。")
    if knowledge_base.status != "active":
        raise KnowledgeRevisionInputError("归档知识库不能创建正文修订。")
    current_revision, source_version = _current_scope_revision_and_version(
        db,
        knowledge_base,
        scope,
    )
    if current_revision != request.expected_revision:
        raise KnowledgeRevisionConflictError("正文修订依据已过期，请重新读取当前版本。")
    if normalized_request.source_version_id and normalized_request.source_version_id != source_version.id:
        raise KnowledgeRevisionConflictError("正文修订 source_version_id 与当前 head 不一致。")

    initial_revision = (
        document_id == INITIAL_REVISION_DOCUMENT_ID
        and request.operation_kind in {"upload", "okf_import", "concept_upsert", "version_fields"}
    )
    document = db.get(KnowledgeDocument, document_id) if not initial_revision else None
    if initial_revision:
        existing_source_asset = db.exec(
            select(KnowledgeDocument.id).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                KnowledgeDocument.knowledge_base_version_id == source_version.id,
            )
        ).first()
        if existing_source_asset:
            raise KnowledgeRevisionInputError("首次上传只能用于没有正文文档的已发布 head。")
    elif (
        not document
        or document.tenant_id != tenant_id
        or document.knowledge_base_id != knowledge_base_id
        or document.knowledge_base_version_id != source_version.id
    ):
        raise KnowledgeRevisionInputError("正文修订目标必须属于当前作用域的已发布文档。")
    job_id = new_id("kjob")
    candidate_id = new_id("kbver")
    candidate = KnowledgeBaseVersion(
        id=candidate_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        version=f"candidate.{job_id.removeprefix('kjob_')}",
        scope=scope,
        name=source_version.name,
        description=source_version.description,
        status="active",
        publication_state="candidate",
        parent_version_id=source_version.id,
        build_job_id=job_id,
        content_manifest_json={
            "builder_version": REVISION_BUILDER_VERSION,
            "source_version_id": source_version.id,
            "target_document_id": document.id if document else INITIAL_REVISION_DOCUMENT_ID,
            "target_lineage_id": document.lineage_id if document else None,
            "initial_revision": initial_revision,
            "source_checksum": sha256(normalized_source.encode("utf-8")).hexdigest(),
            "source_char_count": len(normalized_source),
        },
    )
    job = KnowledgeIngestJob(
        id=job_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        knowledge_base_version_id=source_version.id,
        document_id=document.id if document else INITIAL_REVISION_DOCUMENT_ID,
        filename=(
            document.filename
            if document
            else str((normalized_request.mutation_payload or {}).get("filename") or "initial-upload")
        ),
        job_kind="revision",
        request_payload_json={
            "content_md": normalized_source,
            "document_id": document.id if document else INITIAL_REVISION_DOCUMENT_ID,
            "scope": scope,
            "title": normalized_request.title,
            "operation_kind": normalized_request.operation_kind,
            "mutation_payload": normalized_request.mutation_payload or {},
        },
        actor_user_id=actor_user_id,
        scope=scope,
        source_version_id=source_version.id,
        expected_revision=request.expected_revision,
        candidate_version_id=candidate_id,
        client_request_id=client_request_id,
        request_hash=request_hash,
        dedupe_key=dedupe_key,
        fence=0,
        attempt_count=0,
        deadline_at=utc_now() + timedelta(minutes=30),
        status="queued",
        stage="prepare",
        metadata_json={
            "revision_contract": REVISION_BUILDER_VERSION,
            "source_checksum": candidate.content_manifest_json["source_checksum"],
        },
    )
    # prepare 只提交候选和冻结输入；head/content_revision 由后续 publish CAS 推进。
    db.add(candidate)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _revision_job_by_dedupe(db, tenant_id, dedupe_key)
        if existing and existing.request_hash == request_hash and existing.job_kind == "revision":
            return existing
        raise
    db.refresh(job)
    return job


def claim_revision_job(
    db: Session,
    *,
    worker_id: str,
    job_id: str | None = None,
    now: datetime | None = None,
    lease_seconds: int = REVISION_LEASE_SECONDS,
) -> KnowledgeIngestJob | None:
    """以数据库租约和递增 fence 抢占一个正文任务，不依赖进程内队列状态。"""

    normalized_worker = worker_id.strip()
    if not normalized_worker:
        raise KnowledgeRevisionInputError("正文修订 worker_id 不能为空。")
    if lease_seconds <= 0:
        raise KnowledgeRevisionInputError("正文修订 lease_seconds 必须为正数。")
    current_time = now or utc_now()
    statement = select(KnowledgeIngestJob).where(
        KnowledgeIngestJob.job_kind == "revision",
        or_(
            KnowledgeIngestJob.status == "queued",
            and_(
                KnowledgeIngestJob.status == "running",
                KnowledgeIngestJob.lease_until.is_not(None),
                KnowledgeIngestJob.lease_until <= current_time,
            ),
        ),
    )
    if job_id:
        statement = statement.where(KnowledgeIngestJob.id == job_id)
    candidates = db.exec(
        statement.order_by(KnowledgeIngestJob.created_at.asc(), KnowledgeIngestJob.id.asc())
        .with_for_update()
    ).all()
    for job in candidates:
        previous_attempt_count = int(job.attempt_count or 0)
        previous_fence = int(job.fence or 0)
        claimable = [
            KnowledgeIngestJob.id == job.id,
            KnowledgeIngestJob.job_kind == "revision",
            KnowledgeIngestJob.attempt_count == previous_attempt_count,
            KnowledgeIngestJob.fence == previous_fence,
            or_(
                KnowledgeIngestJob.status == "queued",
                and_(
                    KnowledgeIngestJob.status == "running",
                    KnowledgeIngestJob.lease_until.is_not(None),
                    KnowledgeIngestJob.lease_until <= current_time,
                ),
            ),
        ]
        if job.deadline_at and job.deadline_at <= current_time:
            db.execute(
                sa_update(KnowledgeIngestJob)
                .where(*claimable)
                .values(
                    status="failed",
                    stage=job.stage or "validate",
                    error_code="KNOWLEDGE_REVISION_DEADLINE_EXCEEDED",
                    error="正文修订超过持久 deadline，未发布候选。",
                    lease_owner=None,
                    lease_until=None,
                    finished_at=current_time,
                    updated_at=current_time,
                )
            )
            db.commit()
            continue
        if previous_attempt_count >= REVISION_MAX_ATTEMPTS:
            db.execute(
                sa_update(KnowledgeIngestJob)
                .where(*claimable)
                .values(
                    status="failed",
                    stage=job.stage or "validate",
                    error_code="KNOWLEDGE_REVISION_RETRY_EXHAUSTED",
                    error="正文修订已达到最大接管次数，未发布候选。",
                    lease_owner=None,
                    lease_until=None,
                    finished_at=current_time,
                    updated_at=current_time,
                )
            )
            db.commit()
            continue
        claim_result = db.execute(
            sa_update(KnowledgeIngestJob)
            .where(*claimable)
            .values(
                status="running",
                stage="clone" if job.stage in {"queued", "prepare"} else job.stage,
                lease_owner=normalized_worker,
                lease_until=current_time + timedelta(seconds=lease_seconds),
                attempt_count=previous_attempt_count + 1,
                fence=previous_fence + 1,
                started_at=job.started_at or current_time,
                updated_at=current_time,
            )
        )
        if claim_result.rowcount != 1:
            db.rollback()
            continue
        db.commit()
        db.refresh(job)
        return job
    db.commit()
    return None


def recover_revision_jobs(
    db: Session,
    *,
    now: datetime | None = None,
) -> list[str]:
    """扫描正文任务的 deadline 与过期租约，返回可被队列重新唤醒的同一 job ID。"""

    current_time = now or utc_now()
    rows = db.exec(
        select(KnowledgeIngestJob).where(
            KnowledgeIngestJob.job_kind == "revision",
            KnowledgeIngestJob.status.in_(
                ("queued", "running", "ready_to_publish", "publishing")
            ),
        )
        .order_by(KnowledgeIngestJob.created_at.asc(), KnowledgeIngestJob.id.asc())
        .limit(REVISION_RECOVERY_BATCH_SIZE)
    ).all()
    wake_ids: list[str] = []
    for job in rows:
        if job.deadline_at and job.deadline_at <= current_time:
            _mark_revision_failed(
                db,
                job,
                "KNOWLEDGE_REVISION_DEADLINE_EXCEEDED",
                "正文修订超过持久 deadline，未发布候选。",
                current_time,
            )
            continue
        if job.status in {"queued", "running"} and int(job.attempt_count or 0) >= REVISION_MAX_ATTEMPTS:
            _mark_revision_failed(
                db,
                job,
                "KNOWLEDGE_REVISION_RETRY_EXHAUSTED",
                "正文修订已达到最大接管次数，未发布候选。",
                current_time,
            )
            continue
        if job.status == "publishing":
            # 活跃 publisher 持有独立的发布租约时，恢复扫描只能观察，不能撤销其发布权。
            # 租约失效后用旧 fence 条件接管并递增 fence，旧 publisher 的最终提交会失败。
            publication_lease_expired = bool(
                job.lease_until and job.lease_until <= current_time
            )
            if not publication_lease_expired and job.lease_until is not None:
                continue
            if job.lease_until is None:
                legacy_lease_deadline = job.updated_at + timedelta(
                    seconds=REVISION_PUBLICATION_LEASE_SECONDS
                )
                if legacy_lease_deadline > current_time:
                    continue
            previous_fence = int(job.fence or 0)
            reclaim_result = db.execute(
                sa_update(KnowledgeIngestJob)
                .where(
                    KnowledgeIngestJob.id == job.id,
                    KnowledgeIngestJob.job_kind == "revision",
                    KnowledgeIngestJob.status == "publishing",
                    KnowledgeIngestJob.fence == previous_fence,
                    or_(
                        and_(
                            KnowledgeIngestJob.lease_until.is_not(None),
                            KnowledgeIngestJob.lease_until <= current_time,
                        ),
                        and_(
                            KnowledgeIngestJob.lease_until.is_(None),
                            KnowledgeIngestJob.updated_at
                            <= current_time - timedelta(seconds=REVISION_PUBLICATION_LEASE_SECONDS),
                        ),
                    ),
                )
                .values(
                    status="ready_to_publish",
                    stage="validate",
                    lease_owner=None,
                    lease_until=None,
                    fence=previous_fence + 1,
                    updated_at=current_time,
                )
            )
            if reclaim_result.rowcount != 1:
                # 其他连接可能已完成取消或新 fence 接管。不要回滚同轮前面
                # 已成功的 deadline/attempt 结算，也不能为失权任务返回唤醒 ID。
                continue
            db.commit()
            wake_ids.append(job.id)
            continue
        if job.status == "running":
            if not job.lease_until or job.lease_until > current_time:
                continue
            observed_fence = job.fence
            observed_owner = job.lease_owner
            observed_lease_until = job.lease_until
            requeue_filters = [
                KnowledgeIngestJob.id == job.id,
                KnowledgeIngestJob.job_kind == "revision",
                KnowledgeIngestJob.status == "running",
                KnowledgeIngestJob.lease_until == observed_lease_until,
                KnowledgeIngestJob.lease_until <= current_time,
            ]
            if observed_fence is None:
                requeue_filters.append(KnowledgeIngestJob.fence.is_(None))
            else:
                requeue_filters.append(KnowledgeIngestJob.fence == observed_fence)
            if observed_owner is None:
                requeue_filters.append(KnowledgeIngestJob.lease_owner.is_(None))
            else:
                requeue_filters.append(KnowledgeIngestJob.lease_owner == observed_owner)
            requeue_result = db.execute(
                sa_update(KnowledgeIngestJob)
                .where(*requeue_filters)
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_until=None,
                    updated_at=current_time,
                )
            )
            if requeue_result.rowcount != 1:
                # 其他连接可能已取消或接管该任务。不要回滚同轮前面已经成功的
                # deadline/attempt 结算；只有提交时条件成立才允许唤醒原 job。
                continue
            db.commit()
            wake_ids.append(job.id)
            continue
        if job.status in {"queued", "ready_to_publish"}:
            wake_ids.append(job.id)
    db.commit()
    return wake_ids


def _assert_revision_lease(
    db: Session,
    job: KnowledgeIngestJob,
    worker_id: str,
    fence: int,
) -> None:
    """在候选资产写入前确认当前 worker 仍拥有未过期 fence。"""

    db.expire(job)
    current = db.get(KnowledgeIngestJob, job.id)
    now = utc_now()
    if (
        not current
        or current.status != "running"
        or current.lease_owner != worker_id
        or current.fence != fence
        or (current.deadline_at and current.deadline_at <= now)
        or (current.lease_until and current.lease_until <= now)
    ):
        raise KnowledgeRevisionConflictError("正文修订 worker 租约或 fence 已失效。")


def build_revision(db: Session, job_id: str) -> KnowledgeIngestJob:
    """以持久 claim 执行候选构建，失败时把任务收口而不是留下永久 running。"""

    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.job_kind != "revision":
        raise KnowledgeRevisionInputError("正文修订任务不存在。")
    if job.status in {"ready_to_publish", "completed"}:
        return job
    if job.status in {"failed", "conflict", "cancelled"}:
        raise KnowledgeRevisionConflictError(f"正文修订任务已终止：{job.status}。")
    worker_id = f"revision-builder:{job_id}"
    claimed = claim_revision_job(db, worker_id=worker_id, job_id=job_id)
    if claimed is None:
        current = db.get(KnowledgeIngestJob, job_id)
        if current and current.status in {"ready_to_publish", "completed"}:
            return current
        raise KnowledgeRevisionConflictError("正文修订任务正在由其他 worker 构建。")
    try:
        return _build_revision_claimed(db, job_id, worker_id, claimed.fence)
    except Exception as exc:
        current = db.get(KnowledgeIngestJob, job_id)
        if current and current.status == "running" and current.fence == claimed.fence:
            _mark_revision_failed(
                db,
                current,
                "KNOWLEDGE_REVISION_BUILD_FAILED",
                str(exc)[:500],
                utc_now(),
            )
            db.commit()
        raise


def _build_revision_claimed(
    db: Session,
    job_id: str,
    worker_id: str,
    fence: int,
) -> KnowledgeIngestJob:
    """依据 prepare 冻结的正文和源版本重建候选整库，完成后仍不推进 head。"""

    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.job_kind != "revision":
        raise KnowledgeRevisionInputError("正文修订任务不存在。")
    if job.status in {"ready_to_publish", "completed"}:
        return job
    if job.status in {"failed", "conflict", "cancelled"}:
        raise KnowledgeRevisionConflictError(f"正文修订任务已终止：{job.status}。")
    candidate_id = job.candidate_version_id
    source_version_id = job.source_version_id
    if not candidate_id or not source_version_id or not job.document_id:
        raise KnowledgeRevisionInputError("正文修订任务缺少候选、源版本或目标文档。")
    candidate = db.get(KnowledgeBaseVersion, candidate_id)
    source_version = db.get(KnowledgeBaseVersion, source_version_id)
    if not candidate or not source_version:
        raise KnowledgeRevisionInputError("正文修订的候选或源版本不存在。")
    if candidate.publication_state != "candidate" or source_version.publication_state != "published":
        raise KnowledgeRevisionConflictError("正文修订版本状态不允许构建。")
    _assert_revision_lease(db, job, worker_id, fence)

    source_documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == job.tenant_id,
            KnowledgeDocument.knowledge_base_id == job.knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == source_version_id,
        ).order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
    ).all()
    target_source_document = db.get(KnowledgeDocument, job.document_id)
    if not target_source_document or target_source_document not in source_documents:
        operation_kind = str((job.request_payload_json or {}).get("operation_kind") or "")
        if (
            job.document_id == INITIAL_REVISION_DOCUMENT_ID
            and operation_kind in {"upload", "okf_import", "concept_upsert", "version_fields"}
            and not source_documents
        ):
            return _build_initial_revision_claimed(db, job, worker_id, fence, candidate)
        raise KnowledgeRevisionInputError("正文修订目标不属于冻结源版本。")
    target_source_buckets = db.exec(
        select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == job.tenant_id,
            KnowledgeBucket.knowledge_base_id == job.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == source_version_id,
            KnowledgeBucket.document_id == target_source_document.id,
        ).order_by(KnowledgeBucket.created_at.asc(), KnowledgeBucket.id.asc())
    ).all()
    target_source_chunks = db.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == job.tenant_id,
            KnowledgeChunk.knowledge_base_id == job.knowledge_base_id,
            KnowledgeChunk.knowledge_base_version_id == source_version_id,
            KnowledgeChunk.document_id == target_source_document.id,
        ).order_by(KnowledgeChunk.bucket_id.asc(), KnowledgeChunk.chunk_index.asc(), KnowledgeChunk.id.asc())
    ).all()
    target_source_concepts = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == job.tenant_id,
            KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == source_version_id,
            KnowledgeConcept.document_id == target_source_document.id,
        ).order_by(KnowledgeConcept.created_at.asc(), KnowledgeConcept.id.asc())
    ).all()

    _clear_candidate_assets(db, job.tenant_id, job.knowledge_base_id, candidate_id)
    _assert_revision_lease(db, job, worker_id, fence)
    document_id_map: dict[str, str] = {}
    bucket_id_map: dict[str, str] = {}
    chunk_id_map: dict[str, str] = {}
    concept_id_map: dict[str, str | None] = {}
    candidate_documents: dict[str, KnowledgeDocument] = {}
    normalized_source = str((job.request_payload_json or {}).get("content_md") or "")
    for source_document in source_documents:
        cloned_document = KnowledgeDocument(
            tenant_id=source_document.tenant_id,
            knowledge_base_id=source_document.knowledge_base_id,
            knowledge_base_version_id=candidate_id,
            lineage_id=source_document.lineage_id or source_document.id,
            parent_document_id=source_document.id,
            filename=source_document.filename,
            file_type=source_document.file_type,
            title=source_document.title,
            status="processing" if source_document.id == target_source_document.id else source_document.status,
            normalized_source=(
                normalized_source
                if source_document.id == target_source_document.id
                else source_document.normalized_source
            ),
            source_checksum=(
                sha256(normalized_source.encode("utf-8")).hexdigest()
                if source_document.id == target_source_document.id and normalized_source
                else source_document.source_checksum
            ),
            source_kind=(
                "normalized" if source_document.id == target_source_document.id else source_document.source_kind
            ),
            ingest_schema_version=source_document.ingest_schema_version,
            bucket_count=source_document.bucket_count,
            chunk_count=source_document.chunk_count,
            metadata_json=deepcopy(source_document.metadata_json or {}),
            error=None,
            created_at=source_document.created_at,
            updated_at=utc_now(),
        )
        db.add(cloned_document)
        db.flush()
        document_id_map[source_document.id] = cloned_document.id
        candidate_documents[source_document.id] = cloned_document

    source_buckets = db.exec(
        select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == job.tenant_id,
            KnowledgeBucket.knowledge_base_id == job.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == source_version_id,
            KnowledgeBucket.document_id != target_source_document.id,
        ).order_by(KnowledgeBucket.created_at.asc(), KnowledgeBucket.id.asc())
    ).all()
    for source_bucket in source_buckets:
        cloned_bucket = KnowledgeBucket(
            tenant_id=source_bucket.tenant_id,
            knowledge_base_id=source_bucket.knowledge_base_id,
            knowledge_base_version_id=candidate_id,
            document_id=document_id_map[source_bucket.document_id],
            bucket_key=source_bucket.bucket_key,
            title=source_bucket.title,
            summary=source_bucket.summary,
            token_estimate=source_bucket.token_estimate,
            metadata_json=deepcopy(source_bucket.metadata_json or {}),
            created_at=source_bucket.created_at,
            updated_at=utc_now(),
        )
        db.add(cloned_bucket)
        db.flush()
        bucket_id_map[source_bucket.id] = cloned_bucket.id

    source_chunks = db.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == job.tenant_id,
            KnowledgeChunk.knowledge_base_id == job.knowledge_base_id,
            KnowledgeChunk.knowledge_base_version_id == source_version_id,
            KnowledgeChunk.document_id != target_source_document.id,
        ).order_by(KnowledgeChunk.created_at.asc(), KnowledgeChunk.id.asc())
    ).all()
    for source_chunk in source_chunks:
        cloned_chunk = KnowledgeChunk(
            tenant_id=source_chunk.tenant_id,
            knowledge_base_id=source_chunk.knowledge_base_id,
            knowledge_base_version_id=candidate_id,
            document_id=document_id_map[source_chunk.document_id],
            bucket_id=bucket_id_map[source_chunk.bucket_id],
            chunk_index=source_chunk.chunk_index,
            content=source_chunk.content,
            summary=source_chunk.summary,
            source_ref=_remap_reference_text(source_chunk.source_ref, document_id_map, bucket_id_map),
            metadata_json=deepcopy(source_chunk.metadata_json or {}),
            created_at=source_chunk.created_at,
            updated_at=utc_now(),
        )
        db.add(cloned_chunk)
        db.flush()
        chunk_id_map[source_chunk.id] = cloned_chunk.id

    for source_chunk in source_chunks:
        cloned_chunk = db.get(KnowledgeChunk, chunk_id_map[source_chunk.id])
        if cloned_chunk:
            cloned_chunk.metadata_json = _remap_reference_json(
                cloned_chunk.metadata_json or {}, document_id_map, bucket_id_map, chunk_id_map
            )
            db.add(cloned_chunk)
    for source_bucket in source_buckets:
        cloned_bucket = db.get(KnowledgeBucket, bucket_id_map[source_bucket.id])
        if cloned_bucket:
            cloned_bucket.metadata_json = _remap_reference_json(
                cloned_bucket.metadata_json or {}, document_id_map, bucket_id_map, chunk_id_map
            )
            db.add(cloned_bucket)

    source_concepts = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == job.tenant_id,
            KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == source_version_id,
            KnowledgeConcept.document_id != target_source_document.id,
        ).order_by(KnowledgeConcept.created_at.asc(), KnowledgeConcept.id.asc())
    ).all()
    for source_concept in source_concepts:
        cloned_concept = KnowledgeConcept(
            tenant_id=source_concept.tenant_id,
            knowledge_base_id=source_concept.knowledge_base_id,
            knowledge_base_version_id=candidate_id,
            document_id=document_id_map.get(source_concept.document_id or "", source_concept.document_id),
            concept_id=source_concept.concept_id,
            concept_type=source_concept.concept_type,
            title=source_concept.title,
            description=source_concept.description,
            content_md=_remap_reference_text(
                source_concept.content_md, document_id_map, bucket_id_map
            )
            or source_concept.content_md,
            frontmatter_json=deepcopy(source_concept.frontmatter_json or {}),
            links_json=deepcopy(source_concept.links_json or []),
            citations_json=deepcopy(source_concept.citations_json or []),
            source_refs_json=_remap_reference_json(
                source_concept.source_refs_json or {}, document_id_map, bucket_id_map, chunk_id_map
            ),
            status=source_concept.status,
            created_at=source_concept.created_at,
            updated_at=utc_now(),
        )
        db.add(cloned_concept)
        db.flush()
        concept_id_map[source_concept.id] = cloned_concept.id

    source_target_manual_concepts = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == job.tenant_id,
            KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == source_version_id,
            KnowledgeConcept.document_id == target_source_document.id,
        ).order_by(KnowledgeConcept.created_at.asc(), KnowledgeConcept.id.asc())
    ).all()
    for source_concept in source_target_manual_concepts:
        origin = (source_concept.frontmatter_json or {}).get("origin")
        if origin not in {"manual", "imported"}:
            continue
        cloned_concept = KnowledgeConcept(
            tenant_id=source_concept.tenant_id,
            knowledge_base_id=source_concept.knowledge_base_id,
            knowledge_base_version_id=candidate_id,
            document_id=document_id_map[target_source_document.id],
            concept_id=source_concept.concept_id,
            concept_type=source_concept.concept_type,
            title=source_concept.title,
            description=source_concept.description,
            content_md=source_concept.content_md,
            frontmatter_json=deepcopy(source_concept.frontmatter_json or {}),
            links_json=deepcopy(source_concept.links_json or []),
            citations_json=deepcopy(source_concept.citations_json or []),
            source_refs_json=_remap_reference_json(
                source_concept.source_refs_json or {},
                document_id_map,
                bucket_id_map,
                chunk_id_map,
            ),
            status="source_stale",
            created_at=source_concept.created_at,
            updated_at=utc_now(),
        )
        db.add(cloned_concept)
        db.flush()
        concept_id_map[source_concept.id] = cloned_concept.id

    source_suggestions = db.exec(
        select(KnowledgeDiscoverySuggestion).where(
            KnowledgeDiscoverySuggestion.tenant_id == job.tenant_id,
            KnowledgeDiscoverySuggestion.knowledge_base_id == job.knowledge_base_id,
            KnowledgeDiscoverySuggestion.knowledge_base_version_id == source_version_id,
            KnowledgeDiscoverySuggestion.document_id != target_source_document.id,
        )
    ).all()
    for source_suggestion in source_suggestions:
        db.add(
            KnowledgeDiscoverySuggestion(
                tenant_id=source_suggestion.tenant_id,
                knowledge_base_id=source_suggestion.knowledge_base_id,
                knowledge_base_version_id=candidate_id,
                document_id=document_id_map[source_suggestion.document_id],
                bucket_id=bucket_id_map.get(source_suggestion.bucket_id or ""),
                suggestion_type=source_suggestion.suggestion_type,
                title=source_suggestion.title,
                status=source_suggestion.status,
                payload_json=deepcopy(source_suggestion.payload_json or {}),
                source_refs_json=_remap_reference_json(
                    source_suggestion.source_refs_json or {},
                    document_id_map,
                    bucket_id_map,
                    chunk_id_map,
                ),
                reason=source_suggestion.reason,
                created_at=source_suggestion.created_at,
                updated_at=utc_now(),
            )
        )

    db.commit()
    target_document = candidate_documents[target_source_document.id]
    from app.knowledge.okf import build_okf_for_document, upsert_concepts
    from app.knowledge.service import KnowledgeService, _build_document_card, _build_section_nodes

    section_nodes = _build_section_nodes(normalized_source)
    title = str((job.request_payload_json or {}).get("title") or target_document.title or Path(target_document.filename).stem)
    document_card = _build_document_card(
        title=title,
        filename=target_document.filename,
        file_type=target_document.file_type,
        text=normalized_source,
        section_nodes=section_nodes,
    )
    target_metadata = dict(target_document.metadata_json or {})
    target_metadata.update(
        {
            "ingest_schema_version": target_document.ingest_schema_version,
            "char_count": len(normalized_source),
            "document_card": document_card,
            "section_tree": section_nodes,
            "section_stats": {
                "section_count": len(section_nodes),
                "paragraph_count": len([line for line in normalized_source.splitlines() if line.strip()]),
            },
        }
    )
    target_document.title = title
    target_document.normalized_source = normalized_source
    target_document.source_checksum = sha256(normalized_source.encode("utf-8")).hexdigest()
    target_document.metadata_json = target_metadata
    db.add(target_document)
    db.commit()
    service = KnowledgeService(db)
    target_buckets = service._build_buckets(
        job.tenant_id,
        job.knowledge_base_id,
        target_document,
        normalized_source,
        section_nodes,
        document_card,
        job,
        use_llm=False,
    )
    target_chunk_count = service._build_chunks(
        job.tenant_id,
        job.knowledge_base_id,
        target_document,
        target_buckets,
        section_nodes,
        job,
    )
    candidate_target_buckets = db.exec(
        select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == job.tenant_id,
            KnowledgeBucket.knowledge_base_id == job.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == candidate_id,
            KnowledgeBucket.document_id == target_document.id,
        ).order_by(KnowledgeBucket.created_at.asc(), KnowledgeBucket.id.asc())
    ).all()
    bucket_id_map.update(
        _pair_asset_ids(
            target_source_buckets,
            candidate_target_buckets,
            key=lambda row: (row.bucket_key, row.title),
        )
    )
    candidate_target_chunks = db.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == job.tenant_id,
            KnowledgeChunk.knowledge_base_id == job.knowledge_base_id,
            KnowledgeChunk.knowledge_base_version_id == candidate_id,
            KnowledgeChunk.document_id == target_document.id,
        ).order_by(KnowledgeChunk.bucket_id.asc(), KnowledgeChunk.chunk_index.asc(), KnowledgeChunk.id.asc())
    ).all()
    chunk_id_map.update(
        _pair_chunk_ids(
            target_source_chunks,
            candidate_target_chunks,
            bucket_id_map,
        )
    )
    upsert_concepts(
        db,
        job.tenant_id,
        job.knowledge_base_id,
        candidate_id,
        build_okf_for_document(target_document, section_nodes, target_buckets),
    )
    candidate_concepts = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == job.tenant_id,
            KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == candidate_id,
        )
    ).all()
    candidate_concepts_by_path = {row.concept_id: row for row in candidate_concepts}
    for source_concept in target_source_concepts:
        # 目标正文重建可能合法删除旧的模型概念；用 null 记录已确认的删除，
        # 让发布清单覆盖全部源概念，而不是把“未匹配”误判为清单损坏。
        concept_id_map[source_concept.id] = None
        candidate_concept = candidate_concepts_by_path.get(source_concept.concept_id)
        if candidate_concept:
            concept_id_map[source_concept.id] = candidate_concept.id
    _remap_candidate_references(
        db,
        candidate_id,
        document_id_map,
        bucket_id_map,
        chunk_id_map,
    )
    target_document.bucket_count = len(target_buckets)
    target_document.chunk_count = target_chunk_count
    mutation_status = _apply_revision_mutation(
        db,
        job,
        target_document,
        document_id_map,
        bucket_id_map,
        chunk_id_map,
    )
    if mutation_status is None:
        target_document.status = "ready"
    target_document.updated_at = utc_now()
    db.add(target_document)
    manifest = {
        **(candidate.content_manifest_json or {}),
        "document_id_map": document_id_map,
        "bucket_id_map": bucket_id_map,
        "chunk_id_map": chunk_id_map,
        "concept_id_map": concept_id_map,
        "document_count": len(
            db.exec(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_version_id == candidate_id
                )
            ).all()
        ),
        "bucket_count": len(db.exec(select(KnowledgeBucket).where(KnowledgeBucket.knowledge_base_version_id == candidate_id)).all()),
        "chunk_count": len(db.exec(select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_version_id == candidate_id)).all()),
        "concept_count": len(
            db.exec(
                select(KnowledgeConcept).where(
                    KnowledgeConcept.knowledge_base_version_id == candidate_id
                )
            ).all()
        ),
    }
    candidate.content_manifest_json = manifest
    candidate.content_manifest_hash = sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _assert_revision_lease(db, job, worker_id, fence)
    db.add(candidate)
    job.status = "ready_to_publish"
    job.stage = "validate"
    job.progress = 0.95
    job.updated_at = utc_now()
    job.lease_owner = None
    job.lease_until = None
    job.metadata_json = {
        **(job.metadata_json or {}),
        "stage_label": "待发布校验",
        "document_count": manifest["document_count"],
        "bucket_count": manifest["bucket_count"],
        "chunk_count": manifest["chunk_count"],
        "concept_count": manifest["concept_count"],
    }
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _build_initial_revision_claimed(
    db: Session,
    job: KnowledgeIngestJob,
    worker_id: str,
    fence: int,
    candidate: KnowledgeBaseVersion,
) -> KnowledgeIngestJob:
    """为没有正文资产的 head 构建首次上传候选，不制造已发布版本内的占位文档。"""

    _clear_candidate_assets(db, job.tenant_id, job.knowledge_base_id, candidate.id)
    _assert_revision_lease(db, job, worker_id, fence)
    document_id_map: dict[str, str | None] = {}
    bucket_id_map: dict[str, str | None] = {}
    chunk_id_map: dict[str, str | None] = {}
    _apply_revision_mutation(
        db,
        job,
        None,
        document_id_map,
        bucket_id_map,
        chunk_id_map,
    )
    manifest = {
        **(candidate.content_manifest_json or {}),
        "document_id_map": document_id_map,
        "bucket_id_map": bucket_id_map,
        "chunk_id_map": chunk_id_map,
        "concept_id_map": {},
        "document_count": len(
            db.exec(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.tenant_id == job.tenant_id,
                    KnowledgeDocument.knowledge_base_id == job.knowledge_base_id,
                    KnowledgeDocument.knowledge_base_version_id == candidate.id,
                )
            ).all()
        ),
        "bucket_count": len(
            db.exec(
                select(KnowledgeBucket).where(
                    KnowledgeBucket.tenant_id == job.tenant_id,
                    KnowledgeBucket.knowledge_base_id == job.knowledge_base_id,
                    KnowledgeBucket.knowledge_base_version_id == candidate.id,
                )
            ).all()
        ),
        "chunk_count": len(
            db.exec(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.tenant_id == job.tenant_id,
                    KnowledgeChunk.knowledge_base_id == job.knowledge_base_id,
                    KnowledgeChunk.knowledge_base_version_id == candidate.id,
                )
            ).all()
        ),
        "concept_count": len(
            db.exec(
                select(KnowledgeConcept).where(
                    KnowledgeConcept.tenant_id == job.tenant_id,
                    KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
                    KnowledgeConcept.knowledge_base_version_id == candidate.id,
                )
            ).all()
        ),
    }
    candidate.content_manifest_json = manifest
    candidate.content_manifest_hash = sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    _assert_revision_lease(db, job, worker_id, fence)
    db.add(candidate)
    job.status = "ready_to_publish"
    job.stage = "validate"
    job.progress = 0.95
    job.lease_owner = None
    job.lease_until = None
    job.updated_at = utc_now()
    job.metadata_json = {
        **(job.metadata_json or {}),
        "stage_label": "待发布校验",
        "document_count": manifest["document_count"],
        "bucket_count": manifest["bucket_count"],
        "chunk_count": manifest["chunk_count"],
        "concept_count": manifest["concept_count"],
    }
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _apply_revision_mutation(
    db: Session,
    job: KnowledgeIngestJob,
    target_document: KnowledgeDocument | None,
    document_id_map: dict[str, str | None],
    bucket_id_map: dict[str, str | None],
    chunk_id_map: dict[str, str | None],
) -> str | None:
    """在候选快照内应用兼容写入口的字段或派生层变更，并返回显式文档状态。"""

    payload = job.request_payload_json or {}
    operation_kind = str(payload.get("operation_kind") or "content")
    mutation = payload.get("mutation_payload")
    if not isinstance(mutation, dict):
        mutation = {}
    if operation_kind == "content":
        return None
    if operation_kind == "version_fields":
        candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        if not candidate:
            raise KnowledgeRevisionInputError("候选中找不到知识库版本字段。")
        name = mutation.get("name")
        if isinstance(name, str):
            normalized_name = name.strip()
            if not normalized_name:
                raise KnowledgeRevisionInputError("知识库名称不能为空。")
            candidate.name = normalized_name
        description = mutation.get("description")
        if description is not None and not isinstance(description, str):
            raise KnowledgeRevisionInputError("知识库描述必须是文本。")
        if isinstance(description, str):
            candidate.description = description
        metadata = mutation.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise KnowledgeRevisionInputError("知识库版本元数据必须是对象。")
            candidate.metadata_json = {
                **(candidate.metadata_json or {}),
                **metadata,
            }
        status = mutation.get("status")
        if isinstance(status, str) and status:
            candidate.status = status
        manifest = dict(candidate.content_manifest_json or {})
        manifest["version_fields"] = {
            key: value
            for key, value in {
                "name": candidate.name,
                "description": candidate.description,
                "metadata": candidate.metadata_json,
                "status": candidate.status,
                "knowledge_base_metadata": mutation.get("knowledge_base_metadata"),
            }.items()
            if value is not None
        }
        candidate.content_manifest_json = manifest
        candidate.updated_at = utc_now()
        db.add(candidate)
        return None
    if operation_kind == "document_fields":
        metadata = mutation.get("metadata")
        if isinstance(metadata, dict):
            protected_keys = {
                "section_tree",
                "section_stats",
                "document_card",
                "char_count",
                "ingest_schema_version",
            }
            target_metadata = dict(target_document.metadata_json or {})
            target_metadata.update(
                {
                    key: value
                    for key, value in metadata.items()
                    if key not in protected_keys
                }
            )
            target_document.metadata_json = target_metadata
            db.add(target_document)
        title = mutation.get("title")
        if isinstance(title, str) and title.strip():
            target_document.title = title.strip()
            metadata = dict(target_document.metadata_json or {})
            card = metadata.get("document_card")
            document_card = dict(card) if isinstance(card, dict) else {}
            document_card["title"] = target_document.title
            metadata["document_card"] = document_card
            target_document.metadata_json = metadata
        status = mutation.get("status")
        if isinstance(status, str) and status:
            target_document.status = status
            return status
        return None
    if operation_kind == "bucket_fields":
        source_bucket_id = str(mutation.get("bucket_id") or "")
        candidate_bucket_id = bucket_id_map.get(source_bucket_id)
        bucket = db.get(KnowledgeBucket, candidate_bucket_id) if candidate_bucket_id else None
        if not bucket:
            raise KnowledgeRevisionInputError("候选中找不到待更新分桶。")
        if isinstance(mutation.get("title"), str) and mutation["title"].strip():
            bucket.title = mutation["title"].strip()
        if isinstance(mutation.get("summary"), str):
            bucket.summary = mutation["summary"]
        if isinstance(mutation.get("metadata"), dict):
            bucket.metadata_json = {
                **(bucket.metadata_json or {}),
                **mutation["metadata"],
            }
        bucket.updated_at = utc_now()
        db.add(bucket)
        _refresh_candidate_okf(db, target_document)
        return None
    if operation_kind == "chunk_fields":
        source_chunk_id = str(mutation.get("chunk_id") or "")
        candidate_chunk_id = chunk_id_map.get(source_chunk_id)
        chunk = db.get(KnowledgeChunk, candidate_chunk_id) if candidate_chunk_id else None
        if not chunk:
            raise KnowledgeRevisionInputError("候选中找不到待更新分块。")
        if isinstance(mutation.get("content"), str):
            chunk.content = mutation["content"]
        if isinstance(mutation.get("summary"), str):
            chunk.summary = mutation["summary"]
        if isinstance(mutation.get("metadata"), dict):
            chunk.metadata_json = {
                **(chunk.metadata_json or {}),
                **mutation["metadata"],
            }
        chunk.updated_at = utc_now()
        db.add(chunk)
        bucket = db.get(KnowledgeBucket, chunk.bucket_id)
        if bucket:
            chunks = db.exec(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.knowledge_base_version_id == job.candidate_version_id)
                .where(KnowledgeChunk.bucket_id == bucket.id)
                .order_by(KnowledgeChunk.chunk_index.asc(), KnowledgeChunk.id.asc())
            ).all()
            metadata = dict(bucket.metadata_json or {})
            metadata["content"] = "\n\n".join(
                row.content for row in chunks if row.content and row.content.strip()
            )[:6000]
            metadata["representative_chunk_ids"] = [row.id for row in chunks[:3]]
            metadata["chunk_count"] = len(chunks)
            bucket.metadata_json = metadata
            bucket.updated_at = utc_now()
            db.add(bucket)
        _refresh_candidate_okf(db, target_document)
        return None
    if operation_kind == "concept_upsert":
        content_md = mutation.get("content_md")
        concept_id = mutation.get("concept_id")
        if not isinstance(content_md, str) or not content_md.strip() or not isinstance(concept_id, str):
            raise KnowledgeRevisionInputError("候选 OKF 写入缺少 concept_id 或 content_md。")
        from app.knowledge.okf import parse_okf_markdown, upsert_concepts

        parsed = parse_okf_markdown(concept_id, content_md)
        source_document_id = str(mutation.get("document_id") or "")
        candidate_document_id = document_id_map.get(source_document_id)
        if candidate_document_id is None and target_document is not None:
            candidate_document_id = target_document.id
        upsert_concepts(
            db,
            job.tenant_id,
            job.knowledge_base_id,
            job.candidate_version_id,
            [
                {
                    "concept_id": parsed.concept_id,
                    "content_md": parsed.content_md,
                    "document_id": candidate_document_id,
                    "status": mutation.get("status") or "active",
                }
            ],
        )
        return None
    if operation_kind in {"upload", "okf_import"}:
        filename = str(mutation.get("filename") or "").strip()
        if not filename:
            raise KnowledgeRevisionInputError("候选导入缺少 filename。")
        from app.knowledge.service import KnowledgeService, _build_document_card, _build_section_nodes, _normalize_text
        from app.knowledge.okf import build_okf_for_document, upsert_concepts

        file_type = "okf" if operation_kind == "okf_import" else "md"
        if operation_kind == "okf_import":
            parsed_docs = mutation.get("parsed_docs")
            if not isinstance(parsed_docs, list) or not parsed_docs:
                raise KnowledgeRevisionInputError("候选 OKF 导入缺少概念页。")
            source_parts = [
                str(item.get("content_md") or "").strip()
                for item in parsed_docs
                if isinstance(item, dict) and str(item.get("content_md") or "").strip()
            ]
            if not source_parts:
                raise KnowledgeRevisionInputError("候选 OKF 导入没有可保存正文。")
            text = "\n\n".join(source_parts)
        else:
            content_base64 = mutation.get("content_base64")
            if not isinstance(content_base64, str):
                raise KnowledgeRevisionInputError("候选上传缺少正文载荷。")
            from app.knowledge.parser import extract_text

            try:
                raw_content = base64.b64decode(content_base64)
                text, file_type = extract_text(filename, raw_content)
            except Exception as exc:  # noqa: BLE001 - convert parser detail to stable input error.
                raise KnowledgeRevisionInputError("候选上传正文无法解析。") from exc
        normalized = normalize_revision_source(_normalize_text(text))
        section_nodes = _build_section_nodes(normalized)
        metadata = mutation.get("metadata") if isinstance(mutation.get("metadata"), dict) else {}
        title = str(mutation.get("title") or Path(filename).stem or filename).strip()
        document = KnowledgeDocument(
            tenant_id=job.tenant_id,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_base_version_id=job.candidate_version_id,
            lineage_id=new_id("lineage"),
            parent_document_id=None,
            filename=filename,
            file_type=file_type,
            title=title,
            status="processing",
            normalized_source=normalized,
            source_checksum=sha256(normalized.encode("utf-8")).hexdigest(),
            source_kind="normalized",
            metadata_json={**metadata, "section_tree": section_nodes},
        )
        db.add(document)
        db.flush()
        document_card = _build_document_card(
            title=title,
            filename=filename,
            file_type=file_type,
            text=normalized,
            section_nodes=section_nodes,
        )
        document.metadata_json = {
            **(document.metadata_json or {}),
            "document_card": document_card,
            "char_count": len(normalized),
        }
        service = KnowledgeService(db)
        buckets = service._build_buckets(
            job.tenant_id,
            job.knowledge_base_id,
            document,
            normalized,
            section_nodes,
            document_card,
            job,
            use_llm=False,
        )
        chunk_count = service._build_chunks(
            job.tenant_id,
            job.knowledge_base_id,
            document,
            buckets,
            section_nodes,
            job,
        )
        if operation_kind == "okf_import":
            from app.knowledge.okf import create_concept_evidence_rows

            parsed_docs = mutation.get("parsed_docs")
            concept_rows = upsert_concepts(
                db,
                job.tenant_id,
                job.knowledge_base_id,
                job.candidate_version_id,
                [
                    {
                        "concept_id": item.get("concept_id"),
                        "content_md": item.get("content_md"),
                        "document_id": document.id,
                        "source_refs": [{"document_id": document.id}],
                    }
                    for item in parsed_docs
                    if isinstance(item, dict)
                ],
            )
            create_concept_evidence_rows(
                db,
                job.tenant_id,
                job.knowledge_base_id,
                job.candidate_version_id,
                document,
                concept_rows,
            )
        else:
            upsert_concepts(
                db,
                job.tenant_id,
                job.knowledge_base_id,
                job.candidate_version_id,
                build_okf_for_document(document, section_nodes, buckets),
            )
        document.bucket_count = len(buckets)
        document.chunk_count = chunk_count
        document.status = "ready"
        document.updated_at = utc_now()
        db.add(document)
        candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
        if candidate:
            manifest = dict(candidate.content_manifest_json or {})
            manifest["created_document_id"] = document.id
            manifest["created_document_lineage_id"] = document.lineage_id
            candidate.content_manifest_json = manifest
            db.add(candidate)
        db.commit()
        return None
    raise KnowledgeRevisionInputError(f"未知的正文兼容写入类型：{operation_kind}。")


def _refresh_candidate_okf(db: Session, document: KnowledgeDocument) -> None:
    """按候选文档最新桶投影来源 OKF，不触碰其他版本概念。"""

    from app.knowledge.okf import build_okf_for_document, upsert_concepts

    metadata = document.metadata_json or {}
    section_nodes = metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
    buckets = db.exec(
        select(KnowledgeBucket)
        .where(
            KnowledgeBucket.knowledge_base_version_id == document.knowledge_base_version_id,
            KnowledgeBucket.document_id == document.id,
        )
        .order_by(KnowledgeBucket.created_at.asc(), KnowledgeBucket.id.asc())
    ).all()
    upsert_concepts(
        db,
        document.tenant_id,
        document.knowledge_base_id,
        document.knowledge_base_version_id,
        build_okf_for_document(document, section_nodes, buckets),
    )


def publish_revision(db: Session, job_id: str) -> KnowledgeIngestJob:
    """在当前 scope 的 CAS 仍成立时一次性公开候选并推进唯一 head。"""

    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.job_kind != "revision":
        raise KnowledgeRevisionInputError("正文修订任务不存在。")
    if job.status == "completed":
        return job
    if job.status != "ready_to_publish":
        raise KnowledgeRevisionConflictError(f"正文修订尚未具备发布条件：{job.status}。")
    if job.deadline_at and job.deadline_at <= utc_now():
        _mark_revision_failed(
            db,
            job,
            "KNOWLEDGE_REVISION_DEADLINE_EXCEEDED",
            "正文修订超过持久 deadline，未发布候选。",
            utc_now(),
        )
        db.commit()
        raise KnowledgeRevisionConflictError("正文修订超过持久 deadline。")
    if not job.candidate_version_id or not job.source_version_id or job.expected_revision is None:
        raise KnowledgeRevisionInputError("正文修订任务缺少发布 CAS 依据。")
    candidate = db.get(KnowledgeBaseVersion, job.candidate_version_id)
    source_version = db.get(KnowledgeBaseVersion, job.source_version_id)
    knowledge_base = db.get(KnowledgeBase, job.knowledge_base_id)
    if not candidate or not source_version or not knowledge_base:
        raise KnowledgeRevisionInputError("正文修订发布对象不存在。")
    if candidate.publication_state != "candidate" or source_version.publication_state != "published":
        raise KnowledgeRevisionConflictError("正文修订候选或源版本状态已改变。")
    if not _revision_manifest_is_publishable(db, job, candidate):
        _mark_revision_conflict(db, job, "KNOWLEDGE_REVISION_MANIFEST_INVALID", "候选清单校验失败。")
        raise KnowledgeRevisionConflictError("正文修订候选清单校验失败。")
    if not _revision_actor_still_authorized(db, job):
        _mark_revision_conflict(
            db,
            job,
            "KNOWLEDGE_REVISION_AUTH_CHANGED",
            "正文修订发布前操作者的租户成员或 Agent 管理授权已变化。",
        )
        raise KnowledgeRevisionConflictError("正文修订发布前授权已变化。")

    # cancel_revision 与这里必须竞争同一个持久 job 状态。先把 ready 转为
    # publishing 并提交，取消方看到 publishing 后只能保留已选中的发布者，
    # 不能再删除候选资产或覆盖发布结果。
    if not _claim_revision_publication(db, job):
        winner = _read_completed_publication_winner(db, job)
        if winner:
            return winner
        raise KnowledgeRevisionConflictError("正文修订发布权已被其他终态操作取得。")
    publication_owner = job.lease_owner
    publication_fence = int(job.fence or 0)
    if not publication_owner:
        raise KnowledgeRevisionConflictError("正文修订发布权缺少持久租约。")
    if job.deadline_at and job.deadline_at <= utc_now():
        _mark_revision_failed(
            db,
            job,
            "KNOWLEDGE_REVISION_DEADLINE_EXCEEDED",
            "正文修订超过持久 deadline，未发布候选。",
            utc_now(),
        )
        db.commit()
        raise KnowledgeRevisionConflictError("正文修订超过持久 deadline。")

    if job.scope == "overall":
        current_version = str((knowledge_base.metadata_json or {}).get("current_version") or "1.0.0")
        current_source = db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.tenant_id == job.tenant_id,
                KnowledgeBaseVersion.knowledge_base_id == knowledge_base.id,
                KnowledgeBaseVersion.version == current_version,
            )
        ).first()
        if (
            knowledge_base.content_revision != job.expected_revision
            or not current_source
            or current_source.id != source_version.id
        ):
            _mark_revision_conflict(
                db,
                job,
                "KNOWLEDGE_REVISION_CAS_CONFLICT",
                "知识库内容 head 已变化，候选未发布。",
            )
            raise KnowledgeRevisionConflictError("知识库内容 head 已变化，候选未发布。")
        next_revision = job.expected_revision + 1
    else:
        agent_id = job.scope.removeprefix("agent:").strip()
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == job.tenant_id,
                AgentKnowledgeBranch.agent_id == agent_id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base.id,
                AgentKnowledgeBranch.status == "active",
            )
        ).first()
        if not branch or branch.content_revision != job.expected_revision or branch.head_version != source_version.version:
            _mark_revision_conflict(
                db,
                job,
                "KNOWLEDGE_REVISION_CAS_CONFLICT",
                "Agent 知识 head 已变化，候选未发布。",
            )
            raise KnowledgeRevisionConflictError("Agent 知识 head 已变化，候选未发布。")
        next_revision = job.expected_revision + 1

    if not _lock_revision_publication(
        db,
        job,
        publication_owner,
        publication_fence,
    ):
        raise KnowledgeRevisionConflictError("正文修订发布租约或 fence 已失效。")
    published_version = _next_published_version(db, job, source_version)
    publish_time = utc_now()
    if job.scope == "overall":
        next_metadata = {
            **(knowledge_base.metadata_json or {}),
            "current_version": published_version,
        }
        cas_result = db.execute(
            sa_update(KnowledgeBase)
            .where(
                KnowledgeBase.id == knowledge_base.id,
                KnowledgeBase.tenant_id == job.tenant_id,
                KnowledgeBase.content_revision == job.expected_revision,
            )
            .values(
                metadata_json=next_metadata,
                content_revision=next_revision,
                updated_at=publish_time,
            )
        )
        if cas_result.rowcount != 1:
            _mark_revision_conflict(
                db,
                job,
                "KNOWLEDGE_REVISION_CAS_CONFLICT",
                "知识库内容 head 在发布提交时已变化，候选未发布。",
            )
            raise KnowledgeRevisionConflictError("知识库内容 head 在发布提交时已变化。")
        db.expire(knowledge_base)
        db.refresh(knowledge_base)
    else:
        branch_id = None
        if branch:
            branch_id = branch.id
        cas_result = db.execute(
            sa_update(AgentKnowledgeBranch)
            .where(
                AgentKnowledgeBranch.id == branch_id,
                AgentKnowledgeBranch.tenant_id == job.tenant_id,
                AgentKnowledgeBranch.content_revision == job.expected_revision,
                AgentKnowledgeBranch.head_version == source_version.version,
                AgentKnowledgeBranch.status == "active",
            )
            .values(
                head_version=published_version,
                content_revision=next_revision,
                sync_state="diverged",
                updated_at=publish_time,
            )
        )
        if cas_result.rowcount != 1:
            _mark_revision_conflict(
                db,
                job,
                "KNOWLEDGE_REVISION_CAS_CONFLICT",
                "Agent 知识 head 在发布提交时已变化，候选未发布。",
            )
            raise KnowledgeRevisionConflictError("Agent 知识 head 在发布提交时已变化。")
        if branch:
            db.expire(branch)
            db.refresh(branch)
    candidate.version = published_version
    candidate.publication_state = "published"
    candidate.updated_at = utc_now()
    db.add(candidate)
    if job.scope == "overall":
        db.add(knowledge_base)
    elif branch:
        db.add(branch)

    manifest = dict(candidate.content_manifest_json or {})
    manifest["published_version"] = published_version
    candidate.content_manifest_json = manifest
    candidate.content_manifest_hash = sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    version_fields = manifest.get("version_fields")
    if job.scope == "overall" and isinstance(version_fields, dict):
        name = version_fields.get("name")
        if isinstance(name, str) and name.strip():
            knowledge_base.name = name.strip()
        description = version_fields.get("description")
        if description is None or isinstance(description, str):
            knowledge_base.description = description
        metadata = version_fields.get("knowledge_base_metadata")
        if isinstance(metadata, dict):
            knowledge_base.metadata_json = {
                **(knowledge_base.metadata_json or {}),
                **metadata,
                "current_version": published_version,
            }
        knowledge_base.updated_at = utc_now()
        db.add(knowledge_base)
    job.status = "completed"
    job.stage = "publish"
    job.progress = 1.0
    job.published_version_id = candidate.id
    if job.request_payload_json and job.request_payload_json.get("operation_kind") in {
        "upload",
        "okf_import",
    }:
        job.published_document_id = manifest.get("created_document_id")
    else:
        job.published_document_id = (
            (manifest.get("document_id_map") or {}).get(job.document_id or "")
            if isinstance(manifest.get("document_id_map"), dict)
            else None
        )
    job.error = None
    job.error_code = None
    job.lease_owner = None
    job.lease_until = None
    job.finished_at = utc_now()
    job.updated_at = utc_now()
    job.metadata_json = {
        **(job.metadata_json or {}),
        "stage_label": "已发布",
        "published_content_revision": next_revision,
        "manifest_hash": candidate.content_manifest_hash,
    }
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _read_completed_publication_winner(
    db: Session,
    expected_job: KnowledgeIngestJob,
) -> KnowledgeIngestJob | None:
    """发布 CAS 失利后短暂对账同一任务，只接受已完成的同一发布结果。"""

    db.rollback()
    deadline = time.monotonic() + REVISION_PUBLICATION_RECONCILE_SECONDS
    while True:
        db.expire_all()
        current = db.get(KnowledgeIngestJob, expected_job.id)
        if (
            current is None
            or current.job_kind != "revision"
            or current.tenant_id != expected_job.tenant_id
            or current.knowledge_base_id != expected_job.knowledge_base_id
            or current.candidate_version_id != expected_job.candidate_version_id
        ):
            return None
        if current.status == "completed":
            return current
        if current.status != "publishing" or time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def cancel_revision(db: Session, job_id: str) -> KnowledgeIngestJob:
    """取消正文修订并清理不可见候选，已发布结果不会被回滚。"""

    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.job_kind != "revision":
        raise KnowledgeRevisionInputError("正文修订任务不存在。")
    if job.status in {"completed", "failed", "conflict", "cancelled", "publishing"}:
        return job

    cancelled_at = utc_now()
    cancel_result = db.execute(
        sa_update(KnowledgeIngestJob)
        .where(
            KnowledgeIngestJob.id == job.id,
            KnowledgeIngestJob.job_kind == "revision",
            KnowledgeIngestJob.status.in_(
                ("queued", "running", "ready_to_publish")
            ),
        )
        .values(
            status="cancelled",
            stage="cancelled",
            error_code="KNOWLEDGE_REVISION_CANCELLED",
            error="正文修订已取消。",
            lease_owner=None,
            lease_until=None,
            finished_at=cancelled_at,
            updated_at=cancelled_at,
        )
    )
    if cancel_result.rowcount != 1:
        db.rollback()
        current = db.get(KnowledgeIngestJob, job.id)
        if current and current.status in {
            "completed",
            "failed",
            "conflict",
            "cancelled",
            "publishing",
        }:
            return current
        raise KnowledgeRevisionConflictError("正文修订取消权已被其他状态转换取得。")
    db.commit()

    current = db.get(KnowledgeIngestJob, job.id)
    if current is None:
        raise KnowledgeRevisionInputError("正文修订任务在取消提交后不存在。")
    candidate = db.get(KnowledgeBaseVersion, current.candidate_version_id)
    if candidate and candidate.publication_state == "candidate":
        _clear_candidate_assets(
            db,
            current.tenant_id,
            current.knowledge_base_id,
            candidate.id,
            commit=False,
        )
        candidate.publication_state = "failed"
        candidate.status = "archived"
        candidate.updated_at = cancelled_at
        db.add(candidate)
    db.commit()
    db.refresh(current)
    return current


def run_revision_job(job_id: str) -> None:
    """在独立持久会话中执行正文构建，避免请求会话成为异步任务所有者。"""

    with Session(engine) as db:
        job = build_revision(db, job_id)
        if job.status == "ready_to_publish":
            publish_revision(db, job_id)


def run_compatibility_revision(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    scope: str,
    actor_user_id: str,
    document_id: str,
    operation_kind: str,
    mutation_payload: dict[str, object],
) -> KnowledgeIngestJob:
    """把旧同步写入口适配为同一套候选构建与发布，并同步返回新任务事实。"""

    if document_id == INITIAL_REVISION_DOCUMENT_ID:
        job = prepare_initial_revision(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            scope=scope,
            actor_user_id=actor_user_id,
            operation_kind=operation_kind,
            mutation_payload=mutation_payload,
        )
    else:
        job = prepare_compatibility_revision(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            scope=scope,
            actor_user_id=actor_user_id,
            document_id=document_id,
            operation_kind=operation_kind,
            mutation_payload=mutation_payload,
        )
    built = build_revision(db, job.id)
    if built.status == "ready_to_publish":
        return publish_revision(db, job.id)
    return built


def prepare_compatibility_revision(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    scope: str,
    actor_user_id: str,
    document_id: str,
    operation_kind: str,
    mutation_payload: dict[str, object],
) -> KnowledgeIngestJob:
    """只完成旧写入口的持久 prepare，供异步上传先返回任务再由 worker 构建。"""

    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if not knowledge_base or knowledge_base.tenant_id != tenant_id:
        raise KnowledgeRevisionInputError("兼容写入知识库不存在或租户不匹配。")
    current_revision, source_version = _current_scope_revision_and_version(
        db,
        knowledge_base,
        scope,
    )
    document = db.get(KnowledgeDocument, document_id)
    if (
        not document
        or document.tenant_id != tenant_id
        or document.knowledge_base_id != knowledge_base_id
        or document.knowledge_base_version_id != source_version.id
    ):
        raise KnowledgeRevisionInputError("兼容写入目标不是当前作用域的已发布文档。")
    source = document.normalized_source or _reconstruct_revision_source(document)
    if not source:
        raise KnowledgeRevisionInputError(
            "历史文档没有可验证的完整正文，不能用兼容字段写入口创建新版本。"
        )
    client_request_id = _compatibility_client_request_id(
        tenant_id,
        actor_user_id,
        knowledge_base_id,
        scope,
        document_id,
        operation_kind,
        mutation_payload,
    )
    return prepare_revision(
        db,
        RevisionPrepareInput(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            scope=scope,
            actor_user_id=actor_user_id,
            document_id=document_id,
            content_md=source,
            expected_revision=current_revision,
            client_request_id=client_request_id,
            source_version_id=source_version.id,
            operation_kind=operation_kind,
            mutation_payload=mutation_payload,
        ),
    )


def prepare_initial_revision(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    scope: str,
    actor_user_id: str,
    operation_kind: str,
    mutation_payload: dict[str, object],
) -> KnowledgeIngestJob:
    """为没有正文文档的已发布 head 受理首次上传、OKF 导入或字段兼容候选。"""

    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if not knowledge_base or knowledge_base.tenant_id != tenant_id:
        raise KnowledgeRevisionInputError("首次写入知识库不存在或租户不匹配。")
    current_revision, source_version = _current_scope_revision_and_version(
        db,
        knowledge_base,
        scope,
    )
    client_request_id = _compatibility_client_request_id(
        tenant_id,
        actor_user_id,
        knowledge_base_id,
        scope,
        INITIAL_REVISION_DOCUMENT_ID,
        operation_kind,
        mutation_payload,
    )
    initial_content = str(mutation_payload.get("content_md") or "initial revision input")
    return prepare_revision(
        db,
        RevisionPrepareInput(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            scope=scope,
            actor_user_id=actor_user_id,
            document_id=INITIAL_REVISION_DOCUMENT_ID,
            content_md=initial_content,
            expected_revision=current_revision,
            client_request_id=client_request_id,
            source_version_id=source_version.id,
            operation_kind=operation_kind,
            mutation_payload=mutation_payload,
        ),
    )


def _reconstruct_revision_source(document: KnowledgeDocument) -> str | None:
    """从历史完整 section_tree 重组兼容写入口所需的正文，而不使用摘要伪造全文。"""

    metadata = document.metadata_json or {}
    sections = metadata.get("section_tree") if isinstance(metadata, dict) else None
    if not isinstance(sections, list) or not sections:
        return None
    parts: list[str] = []
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("content"), str):
            return None
        content = section["content"].strip()
        if not content:
            return None
        title = str(section.get("title") or section.get("path") or "").strip()
        parts.append(f"# {title}\n\n{content}" if title else content)
    return "\n\n".join(parts).strip() or None


def _compatibility_client_request_id(
    tenant_id: str,
    actor_user_id: str,
    knowledge_base_id: str,
    scope: str,
    document_id: str,
    operation_kind: str,
    mutation_payload: dict[str, object],
) -> str:
    """为无显式 client ID 的旧入口派生可重试的稳定受理身份。"""

    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "knowledge_base_id": knowledge_base_id,
            "scope": scope,
            "document_id": document_id,
            "operation_kind": operation_kind,
            "mutation_payload": mutation_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"compat-{sha256(payload.encode('utf-8')).hexdigest()[:56]}"


def _revision_manifest_is_publishable(
    db: Session,
    job: KnowledgeIngestJob,
    candidate: KnowledgeBaseVersion,
) -> bool:
    """核对候选清单中的物理映射、目标文档和候选资产闭包。"""

    manifest = candidate.content_manifest_json or {}
    document_map = manifest.get("document_id_map")
    bucket_map = manifest.get("bucket_id_map")
    chunk_map = manifest.get("chunk_id_map")
    concept_map = manifest.get("concept_id_map")
    if not all(
        isinstance(item, dict)
        for item in (document_map, bucket_map, chunk_map, concept_map)
    ):
        return False
    initial_revision = job.document_id == INITIAL_REVISION_DOCUMENT_ID
    if not initial_revision and (not job.document_id or job.document_id not in document_map):
        return False
    if manifest.get("source_version_id") != job.source_version_id:
        return False
    candidate_documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == job.tenant_id,
            KnowledgeDocument.knowledge_base_id == job.knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == candidate.id,
        )
    ).all()
    candidate_buckets = db.exec(
        select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == job.tenant_id,
            KnowledgeBucket.knowledge_base_id == job.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == candidate.id,
        )
    ).all()
    candidate_chunks = db.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == job.tenant_id,
            KnowledgeChunk.knowledge_base_id == job.knowledge_base_id,
            KnowledgeChunk.knowledge_base_version_id == candidate.id,
        )
    ).all()
    candidate_concepts = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == job.tenant_id,
            KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == candidate.id,
        )
    ).all()
    for key, actual in (
        ("document_count", len(candidate_documents)),
        ("bucket_count", len(candidate_buckets)),
        ("chunk_count", len(candidate_chunks)),
        ("concept_count", len(candidate_concepts)),
    ):
        expected = manifest.get(key)
        if not isinstance(expected, int) or expected != actual:
            return False
    if len({row.lineage_id for row in candidate_documents}) != len(candidate_documents):
        return False
    candidate_document_ids = {row.id for row in candidate_documents}
    candidate_bucket_ids = {row.id for row in candidate_buckets}
    candidate_chunk_ids = {row.id for row in candidate_chunks}
    candidate_concept_ids = {row.id for row in candidate_concepts}
    if not initial_revision:
        target_id = document_map.get(job.document_id)
        if not isinstance(target_id, str) or target_id not in candidate_document_ids:
            return False

    source_documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == job.tenant_id,
            KnowledgeDocument.knowledge_base_id == job.knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == job.source_version_id,
        )
    ).all()
    source_buckets = db.exec(
        select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == job.tenant_id,
            KnowledgeBucket.knowledge_base_id == job.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == job.source_version_id,
        )
    ).all()
    source_chunks = db.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == job.tenant_id,
            KnowledgeChunk.knowledge_base_id == job.knowledge_base_id,
            KnowledgeChunk.knowledge_base_version_id == job.source_version_id,
        )
    ).all()
    source_concepts = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == job.tenant_id,
            KnowledgeConcept.knowledge_base_id == job.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == job.source_version_id,
            KnowledgeConcept.status != "deleted",
        )
    ).all()
    if set(document_map) != {row.id for row in source_documents}:
        return False
    if set(bucket_map) != {row.id for row in source_buckets}:
        return False
    if set(chunk_map) != {row.id for row in source_chunks}:
        return False
    if set(concept_map) != {row.id for row in source_concepts}:
        return False
    for mapping, candidate_ids in (
        (document_map, candidate_document_ids),
        (bucket_map, candidate_bucket_ids),
        (chunk_map, candidate_chunk_ids),
        (concept_map, candidate_concept_ids),
    ):
        mapped = [value for value in mapping.values() if value is not None]
        if any(not isinstance(value, str) or value not in candidate_ids for value in mapped):
            return False
        if len(mapped) != len(set(mapped)):
            return False

    for bucket in candidate_buckets:
        if bucket.document_id not in candidate_document_ids:
            return False
        if not _candidate_reference_json_is_closed(
            bucket.metadata_json or {},
            candidate_document_ids,
            candidate_bucket_ids,
            candidate_chunk_ids,
        ):
            return False
    for chunk in candidate_chunks:
        if (
            chunk.document_id not in candidate_document_ids
            or chunk.bucket_id not in candidate_bucket_ids
            or not _candidate_reference_json_is_closed(
                chunk.metadata_json or {},
                candidate_document_ids,
                candidate_bucket_ids,
                candidate_chunk_ids,
            )
        ):
            return False
    for concept in candidate_concepts:
        if concept.document_id and concept.document_id not in candidate_document_ids:
            return False
        if not all(
            _candidate_reference_json_is_closed(
                value,
                candidate_document_ids,
                candidate_bucket_ids,
                candidate_chunk_ids,
            )
            for value in (
                concept.frontmatter_json or {},
                concept.links_json or [],
                concept.citations_json or [],
                concept.source_refs_json or [],
            )
        ):
            return False
    for document in candidate_documents:
        if document.parent_document_id and document.parent_document_id not in candidate_document_ids:
            parent = db.get(KnowledgeDocument, document.parent_document_id)
            if (
                not parent
                or parent.tenant_id != job.tenant_id
                or parent.knowledge_base_id != job.knowledge_base_id
            ):
                return False
        if not _candidate_reference_json_is_closed(
            document.metadata_json or {},
            candidate_document_ids,
            candidate_bucket_ids,
            candidate_chunk_ids,
        ):
            return False
    for old_id in (
        {row.id for row in source_documents}
        | {row.id for row in source_buckets}
        | {row.id for row in source_chunks}
    ):
        for row in (*candidate_documents, *candidate_chunks, *candidate_concepts):
            text_value = getattr(row, "source_ref", None) or getattr(row, "content_md", None)
            if isinstance(text_value, str) and old_id in text_value:
                return False
    return True


def _candidate_reference_json_is_closed(
    value: object,
    document_ids: set[str],
    bucket_ids: set[str],
    chunk_ids: set[str],
) -> bool:
    """确认候选 JSON 中显式的文档、桶和 chunk 引用全部落在同一物理快照。"""

    if isinstance(value, list):
        return all(
            _candidate_reference_json_is_closed(item, document_ids, bucket_ids, chunk_ids)
            for item in value
        )
    if not isinstance(value, dict):
        return True
    for key, item in value.items():
        if key == "document_id" and isinstance(item, str) and item not in document_ids:
            return False
        if key == "bucket_id" and isinstance(item, str) and item not in bucket_ids:
            return False
        if key in {"chunk_id", "previous_chunk_id", "next_chunk_id", "related_previous_chunk_id", "related_next_chunk_id"} and isinstance(item, str) and item not in chunk_ids:
            return False
        if key == "document_ids" and isinstance(item, list) and any(row not in document_ids for row in item):
            return False
        if key == "bucket_ids" and isinstance(item, list) and any(row not in bucket_ids for row in item):
            return False
        if key in {"chunk_ids", "related_chunk_ids", "representative_chunk_ids"} and isinstance(item, list) and any(row not in chunk_ids for row in item):
            return False
        if not _candidate_reference_json_is_closed(item, document_ids, bucket_ids, chunk_ids):
            return False
    return True


def _revision_actor_still_authorized(db: Session, job: KnowledgeIngestJob) -> bool:
    """在异步发布前复核仍存在的操作者，避免撤权后继续推进 head。"""

    if not job.actor_user_id:
        return True
    actor = db.get(User, job.actor_user_id)
    if not actor or actor.tenant_id != job.tenant_id or actor.membership_status != "active":
        return False
    if job.scope == "overall":
        # 受理 API 已经要求管理员；这里仅在持久用户存在时重复确认，内部迁移任务可无用户。
        return actor.role == "admin"
    agent_id = job.scope.removeprefix("agent:").strip()
    if actor.role == "admin":
        return True
    from app.agents.branching import get_agent
    from app.security.permissions import agent_owned_by_user

    agent = get_agent(db, job.tenant_id, agent_id)
    return bool(agent and agent.status == "active" and agent_owned_by_user(agent, actor))


def _mark_revision_conflict(
    db: Session,
    job: KnowledgeIngestJob,
    error_code: str,
    message: str,
) -> bool:
    """按调用者读到的状态、owner 和 fence 条件收口 conflict，保护已提交终态。"""

    current_time = utc_now()
    expected_fence = job.fence
    ownership_filters = [
        KnowledgeIngestJob.id == job.id,
        KnowledgeIngestJob.job_kind == "revision",
        KnowledgeIngestJob.status.in_(
            ("queued", "running", "ready_to_publish", "publishing")
        ),
        KnowledgeIngestJob.status == job.status,
    ]
    if expected_fence is None:
        ownership_filters.append(KnowledgeIngestJob.fence.is_(None))
    else:
        ownership_filters.append(KnowledgeIngestJob.fence == expected_fence)
    if job.lease_owner is None:
        ownership_filters.append(KnowledgeIngestJob.lease_owner.is_(None))
    else:
        ownership_filters.append(KnowledgeIngestJob.lease_owner == job.lease_owner)
    result = db.execute(
        sa_update(KnowledgeIngestJob)
        .where(*ownership_filters)
        .values(
            status="conflict",
            stage="publish",
            error_code=error_code,
            error=message,
            lease_owner=None,
            lease_until=None,
            finished_at=current_time,
            updated_at=current_time,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def _mark_revision_failed(
    db: Session,
    job: KnowledgeIngestJob,
    error_code: str,
    message: str,
    current_time: datetime,
) -> bool:
    """按旧状态、owner 和 fence 条件收口 failed，避免扫描覆盖已提交终态。"""

    expected_fence = job.fence
    failure_filters = [
        KnowledgeIngestJob.id == job.id,
        KnowledgeIngestJob.job_kind == "revision",
        KnowledgeIngestJob.status.in_(
            ("queued", "running", "ready_to_publish", "publishing")
        ),
        KnowledgeIngestJob.status == job.status,
    ]
    if expected_fence is None:
        failure_filters.append(KnowledgeIngestJob.fence.is_(None))
    else:
        failure_filters.append(KnowledgeIngestJob.fence == expected_fence)
    if job.lease_owner is None:
        failure_filters.append(KnowledgeIngestJob.lease_owner.is_(None))
    else:
        failure_filters.append(KnowledgeIngestJob.lease_owner == job.lease_owner)
    result = db.execute(
        sa_update(KnowledgeIngestJob)
        .where(*failure_filters)
        .values(
            status="failed",
            stage=job.stage or "validate",
            error_code=error_code,
            error=message,
            lease_owner=None,
            lease_until=None,
            finished_at=current_time,
            updated_at=current_time,
        )
    )
    return result.rowcount == 1


def _claim_revision_publication(db: Session, job: KnowledgeIngestJob) -> bool:
    """用持久 job 状态、发布租约和递增 fence 原子取得发布权。"""

    current_time = utc_now()
    previous_fence = int(job.fence or 0)
    publication_owner = f"revision-publisher:{new_id('publisher')}"
    result = db.execute(
        sa_update(KnowledgeIngestJob)
        .where(
            KnowledgeIngestJob.id == job.id,
            KnowledgeIngestJob.job_kind == "revision",
            KnowledgeIngestJob.status == "ready_to_publish",
            KnowledgeIngestJob.fence == previous_fence,
        )
        .values(
            status="publishing",
            lease_owner=publication_owner,
            lease_until=current_time + timedelta(seconds=REVISION_PUBLICATION_LEASE_SECONDS),
            fence=previous_fence + 1,
            updated_at=current_time,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    db.refresh(job)
    return True


def _lock_revision_publication(
    db: Session,
    job: KnowledgeIngestJob,
    publication_owner: str,
    publication_fence: int,
) -> bool:
    """在同一事务内锁住发布租约，保证恢复接管与 head/Candidate 提交二选一。"""

    current_time = utc_now()
    result = db.execute(
        sa_update(KnowledgeIngestJob)
        .where(
            KnowledgeIngestJob.id == job.id,
            KnowledgeIngestJob.job_kind == "revision",
            KnowledgeIngestJob.status == "publishing",
            KnowledgeIngestJob.lease_owner == publication_owner,
            KnowledgeIngestJob.fence == publication_fence,
            KnowledgeIngestJob.lease_until.is_not(None),
            KnowledgeIngestJob.lease_until > current_time,
            or_(
                KnowledgeIngestJob.deadline_at.is_(None),
                KnowledgeIngestJob.deadline_at > current_time,
            ),
        )
        .values(updated_at=current_time)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    return True


def _next_published_version(
    db: Session,
    job: KnowledgeIngestJob,
    source_version: KnowledgeBaseVersion,
) -> str:
    """在整个知识库已用版本集合中分配含 scope 命名空间的有限长度标签。"""

    base_parts = source_version.version.partition("-branch.")[0].split(".")
    if len(base_parts) >= 3 and all(part.isdigit() for part in base_parts[:3]):
        base = f"{base_parts[0]}.{int(base_parts[1]) + 1}.0"
    else:
        base = f"{source_version.version}.1"
    scope_hash = sha256(job.scope.encode("utf-8")).hexdigest()[:10]
    prefix = f"{base}-sd5-{scope_hash}"
    existing = {
        row.version
        for row in db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.tenant_id == job.tenant_id,
                KnowledgeBaseVersion.knowledge_base_id == job.knowledge_base_id,
            )
        ).all()
    }
    candidate = prefix
    suffix = 1
    while candidate in existing:
        suffix += 1
        candidate = f"{prefix}-{suffix}"
    return candidate[:64]


def _clear_candidate_assets(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    version_id: str,
    *,
    commit: bool = True,
) -> None:
    """清理未发布候选的可重建资产，不触碰源版本或已发布版本。"""

    for model in (
        KnowledgeDiscoverySuggestion,
        KnowledgeConcept,
        KnowledgeChunk,
        KnowledgeBucket,
        KnowledgeDocument,
    ):
        db.exec(
            delete(model).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == knowledge_base_id,
                model.knowledge_base_version_id == version_id,
            )
        )
    if commit:
        db.commit()


def _remap_reference_json(
    value: object,
    document_id_map: dict[str, str | None],
    bucket_id_map: dict[str, str | None],
    chunk_id_map: dict[str, str | None] | None = None,
) -> object:
    """递归重写派生 JSON 中的 document/bucket/chunk 物理引用。"""

    chunk_id_map = chunk_id_map or {}
    if isinstance(value, list):
        remapped_items = [
            _remap_reference_json(item, document_id_map, bucket_id_map, chunk_id_map)
            for item in value
        ]
        return [item for item in remapped_items if item is not None]
    if not isinstance(value, dict):
        return value
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, str):
            if key == "document_id":
                item = _mapped_reference(item, document_id_map)
            elif key == "bucket_id":
                item = _mapped_reference(item, bucket_id_map)
            elif key in {"chunk_id", "previous_chunk_id", "next_chunk_id", "related_previous_chunk_id", "related_next_chunk_id"}:
                item = _mapped_reference(item, chunk_id_map)
        elif key in {"document_ids", "bucket_ids", "chunk_ids", "related_chunk_ids", "representative_chunk_ids"} and isinstance(item, list):
            mapping = document_id_map if key == "document_ids" else bucket_id_map if key == "bucket_ids" else chunk_id_map
            item = [
                mapped
                for row in item
                if (mapped := _mapped_reference(str(row), mapping)) is not None
            ]
        result[key] = _remap_reference_json(item, document_id_map, bucket_id_map, chunk_id_map)
    return {key: item for key, item in result.items() if item is not None}


def _mapped_reference(value: str, mapping: dict[str, str | None] | None) -> str | None:
    """将已知旧物理 ID 映射到候选 ID；明确删除的资产不会原样泄漏。"""

    if mapping is None or value not in mapping:
        return value
    return mapping[value]


def _pair_asset_ids(
    source_rows: list[KnowledgeBucket],
    candidate_rows: list[KnowledgeBucket],
    *,
    key,
) -> dict[str, str | None]:
    """按冻结业务键和稳定顺序建立同一文档桶的物理 ID 映射。"""

    remaining = list(candidate_rows)
    result: dict[str, str | None] = {}
    for source_row in source_rows:
        match_index = next(
            (index for index, candidate_row in enumerate(remaining) if key(candidate_row) == key(source_row)),
            None,
        )
        if match_index is None and remaining:
            # 正文重建可能重新计算 bucket_key/title；此时沿源/候选稳定创建顺序配对，
            # 不按 filename/title 猜跨文档身份。
            match_index = 0
        if match_index is None:
            result[source_row.id] = None
            continue
        result[source_row.id] = remaining.pop(match_index).id
    return result


def _pair_chunk_ids(
    source_rows: list[KnowledgeChunk],
    candidate_rows: list[KnowledgeChunk],
    bucket_id_map: dict[str, str | None],
) -> dict[str, str | None]:
    """按映射后的桶和 chunk_index 建立目标分块物理 ID 映射。"""

    candidate_by_bucket: dict[str, dict[int, KnowledgeChunk]] = {}
    for row in candidate_rows:
        candidate_by_bucket.setdefault(row.bucket_id, {})[row.chunk_index] = row
    result: dict[str, str | None] = {}
    for source_row in source_rows:
        candidate_bucket_id = bucket_id_map.get(source_row.bucket_id)
        candidate = (
            candidate_by_bucket.get(candidate_bucket_id or "", {}).get(source_row.chunk_index)
            if candidate_bucket_id
            else None
        )
        result[source_row.id] = candidate.id if candidate else None
    return result


def _remap_candidate_references(
    db: Session,
    candidate_id: str,
    document_id_map: dict[str, str | None],
    bucket_id_map: dict[str, str | None],
    chunk_id_map: dict[str, str | None],
) -> None:
    """对候选全部派生 JSON 做一次最终物理引用重写，避免目标旧 ID 残留。"""

    documents = db.exec(
        select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_version_id == candidate_id)
    ).all()
    for row in documents:
        row.metadata_json = _remap_reference_json(
            row.metadata_json or {}, document_id_map, bucket_id_map, chunk_id_map
        )
        db.add(row)
    buckets = db.exec(
        select(KnowledgeBucket).where(KnowledgeBucket.knowledge_base_version_id == candidate_id)
    ).all()
    for row in buckets:
        row.metadata_json = _remap_reference_json(
            row.metadata_json or {}, document_id_map, bucket_id_map, chunk_id_map
        )
        db.add(row)
    chunks = db.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_version_id == candidate_id)
    ).all()
    for row in chunks:
        row.metadata_json = _remap_reference_json(
            row.metadata_json or {}, document_id_map, bucket_id_map, chunk_id_map
        )
        row.source_ref = _remap_reference_text(row.source_ref, document_id_map, bucket_id_map)
        db.add(row)
    concepts = db.exec(
        select(KnowledgeConcept).where(KnowledgeConcept.knowledge_base_version_id == candidate_id)
    ).all()
    for row in concepts:
        row.links_json = _remap_reference_json(
            row.links_json or [], document_id_map, bucket_id_map, chunk_id_map
        )
        row.citations_json = _remap_reference_json(
            row.citations_json or [], document_id_map, bucket_id_map, chunk_id_map
        )
        row.source_refs_json = _remap_reference_json(
            row.source_refs_json or [], document_id_map, bucket_id_map, chunk_id_map
        )
        row.content_md = _remap_reference_text(row.content_md, document_id_map, bucket_id_map) or row.content_md
        db.add(row)
    db.commit()


def _remap_reference_text(
    value: str | None,
    document_id_map: dict[str, str | None],
    bucket_id_map: dict[str, str | None],
) -> str | None:
    """替换 OKF/来源文本中明确的 document 或 bucket URI 身份。"""

    if value is None:
        return None
    result = value
    for old_id, new_ref in {**document_id_map, **bucket_id_map}.items():
        if new_ref is not None:
            result = result.replace(old_id, new_ref)
    return result


def _revision_dedupe_key(request: RevisionPrepareInput) -> str:
    """从租户、操作者、知识库、作用域和客户端身份派生持久幂等键。"""

    identity = "\x1f".join(
        (
            request.tenant_id,
            request.actor_user_id,
            request.knowledge_base_id,
            request.scope,
            request.client_request_id,
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def _revision_job_by_dedupe(db: Session, tenant_id: str, dedupe_key: str) -> KnowledgeIngestJob | None:
    """按租户和持久 dedupe key 查找原修订任务。"""

    return db.exec(
        select(KnowledgeIngestJob).where(
            KnowledgeIngestJob.tenant_id == tenant_id,
            KnowledgeIngestJob.dedupe_key == dedupe_key,
        )
    ).first()


def _current_scope_revision_and_version(
    db: Session,
    knowledge_base: KnowledgeBase,
    scope: str,
) -> tuple[int, KnowledgeBaseVersion]:
    """读取 overall 或指定 Agent 分支的当前 CAS 序号和已发布版本。"""

    if scope == "overall":
        current_version = str((knowledge_base.metadata_json or {}).get("current_version") or "1.0.0")
        source_version = ensure_knowledge_base_version(db, knowledge_base, current_version)
        if source_version.publication_state != "published":
            raise KnowledgeRevisionInputError("当前知识 head 不是已发布版本。")
        return knowledge_base.content_revision, source_version

    agent_id = scope.removeprefix("agent:").strip()
    if not agent_id:
        raise KnowledgeRevisionInputError("Agent 作用域缺少 agent_id。")
    branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == knowledge_base.tenant_id,
            AgentKnowledgeBranch.agent_id == agent_id,
            AgentKnowledgeBranch.knowledge_base_id == knowledge_base.id,
            AgentKnowledgeBranch.status == "active",
        )
    ).first()
    if not branch:
        raise KnowledgeRevisionInputError("Agent 知识分支不存在或已停用。")
    source_version = db.exec(
        select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.tenant_id == knowledge_base.tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == knowledge_base.id,
            KnowledgeBaseVersion.version == branch.head_version,
        )
    ).first()
    if not source_version or source_version.publication_state != "published":
        raise KnowledgeRevisionInputError("Agent 当前 head 不是已发布版本。")
    return branch.content_revision, source_version
