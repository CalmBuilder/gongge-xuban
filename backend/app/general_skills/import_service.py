"""
@Time       : 2026/08/12 01:00
@Author     : zhanglp8181
@File       : import_service.py
@CallChain  : GeneralSkill ImportJob API → ImportJobService → normalizer/object store/SQLModel
@Description: 编排上传暂存、脱敏预览、checksum 确认、不可变修订和默认 pinned 绑定。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import PurePath
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillDependency,
    GeneralSkillImportJob,
    GeneralSkillImportQuota,
    GeneralSkillRevision,
    User,
    utc_now,
)
from app.general_skills.import_schema import (
    GeneralSkillDependencyDecision,
    GeneralSkillImportCandidateRead,
    GeneralSkillImportConfirm,
    GeneralSkillImportJobCreate,
    GeneralSkillImportJobRead,
    GeneralSkillUploadFile,
)
from app.general_skills.lifecycle import (
    GeneralSkillLifecycleError,
    IMPORT_JOB_TERMINAL_STATUSES,
    ImportJobStatus,
    RevisionStatus,
    transition_import_job,
    transition_revision,
)
from app.general_skills.object_store import FileSystemSkillObjectStore, SkillObjectStoreError
from app.general_skills.package_security import (
    GeneralSkillPackageError,
    SkillCandidate,
    normalize_zip_package,
)
from app.general_skills.remote_source import (
    GITHUB_ARCHIVE_HOSTS,
    SKILLHUB_DOWNLOAD_HOSTS,
    GeneralSkillRemoteSourceError,
    RemoteFetcher,
    SecureHttpsFetcher,
    github_archive_url,
    skillhub_archive_url,
    validated_remote_reference,
)
from app.security.permissions import ensure_agent_scope_manager
from app.security.tenant import ensure_tenant


SLUG_INVALID = re.compile(r"[^a-z0-9]+")
TENANT_ACTIVE_IMPORT_LIMIT = 4
USER_ACTIVE_IMPORT_LIMIT = 2
TENANT_STAGED_BYTE_LIMIT = 500 * 1024 * 1024
USER_STAGED_BYTE_LIMIT = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _InstalledCandidate:
    """保存一次 confirm 中候选到新建稳定 Skill/Revision 的映射。"""

    skill_id: str
    revision_id: str
    invocation_policy: str


class GeneralSkillImportError(RuntimeError):
    """向 API 暴露稳定错误码、HTTP 状态和脱敏摘要的领域错误。"""

    def __init__(self, error_code: str, detail: str, status_code: int) -> None:
        """保存调用方可安全显示的结构化导入失败。"""

        super().__init__(detail)
        self.error_code = error_code
        self.status_code = status_code


class GeneralSkillImportService:
    """在单一事务边界内管理用户私有 Skill 的预览和确认。"""

    def __init__(
        self,
        db: Session,
        object_store: FileSystemSkillObjectStore,
        *,
        https_allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        """绑定请求 Session、对象存储和可选的公开 HTTPS 来源白名单。"""

        self.db = db
        self.object_store = object_store
        self.https_allowed_hosts = https_allowed_hosts

    def create_upload_job(
        self,
        request: GeneralSkillImportJobCreate,
        *,
        idempotency_key: str,
        current_user: User,
    ) -> GeneralSkillImportJobRead:
        """鉴权、严格解码并同步形成可跨刷新恢复的 awaiting-approval 作业。"""

        ensure_tenant(self.db, request.tenant_id)
        ensure_agent_scope_manager(
            self.db,
            request.tenant_id,
            request.target_agent_id,
            current_user,
        )
        key = _validated_idempotency_key(idempotency_key)
        if request.filename is None:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PACKAGE_INVALID", "upload source is incomplete", 400
            )
        payload = _prepare_upload_payload(request)
        raw_checksum = hashlib.sha256(payload).hexdigest()
        existing = self.db.exec(
            select(GeneralSkillImportJob).where(
                GeneralSkillImportJob.tenant_id == request.tenant_id,
                GeneralSkillImportJob.owner_user_id == current_user.id,
                GeneralSkillImportJob.idempotency_key == key,
                GeneralSkillImportJob.attempt == 1,
            )
        ).first()
        if existing:
            if existing.raw_checksum and existing.raw_checksum != raw_checksum:
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_STATE_CONFLICT",
                    "idempotency key was already used for different content",
                    409,
                )
            return import_job_read(existing)
        self._reserve_import_quota(request.tenant_id, current_user.id)
        now = utc_now()
        job = GeneralSkillImportJob(
            tenant_id=request.tenant_id,
            owner_user_id=current_user.id,
            target_agent_id=request.target_agent_id,
            source_kind=request.source_kind,
            source_reference_redacted=_redacted_filename(request.filename),
            raw_checksum=raw_checksum,
            idempotency_key=key,
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.db.add(job)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise GeneralSkillImportError(
                "GENERAL_SKILL_STATE_CONFLICT",
                "an import with this idempotency key already exists",
                409,
            ) from exc
        try:
            self._checkpoint_raw_payload(job, payload)
            return self._normalize_fetched(job, payload)
        except GeneralSkillPackageError as exc:
            self._fail_job(job, exc.error_code, str(exc))
            return import_job_read(job)
        except SkillObjectStoreError as exc:
            self._fail_job(job, "GENERAL_SKILL_STORAGE_UNAVAILABLE", str(exc))
            return import_job_read(job)
        except GeneralSkillImportError as exc:
            if exc.error_code != "GENERAL_SKILL_QUOTA_EXCEEDED":
                raise
            self._fail_job(job, exc.error_code, str(exc))
            return import_job_read(job)

    def create_job(
        self,
        request: GeneralSkillImportJobCreate,
        *,
        idempotency_key: str,
        current_user: User,
        fetcher: RemoteFetcher | None = None,
    ) -> GeneralSkillImportJobRead:
        """按来源类型进入同一作业状态机，远程来源固定请求后再复用规范化链。"""

        if request.source_kind == "upload":
            return self.create_upload_job(
                request,
                idempotency_key=idempotency_key,
                current_user=current_user,
            )
        return self._create_remote_job(
            request,
            idempotency_key=idempotency_key,
            current_user=current_user,
            fetcher=fetcher or SecureHttpsFetcher(),
        )
    def get_job(self, job_id: str, *, current_user: User) -> GeneralSkillImportJobRead:
        """按 tenant/user 双边界读取作业，管理员也不能跨 tenant 枚举。"""

        job = self._owned_job(job_id, current_user)
        return import_job_read(job)

    def cancel_job(
        self,
        job_id: str,
        *,
        expected_row_version: int,
        current_user: User,
    ) -> GeneralSkillImportJobRead:
        """幂等取消未终态作业并同步释放其暂存对象和配额。"""

        job = self._owned_job(job_id, current_user)
        if job.status == ImportJobStatus.CANCELLED:
            return import_job_read(job)
        try:
            transition_import_job(
                job,
                ImportJobStatus.CANCELLED,
                expected_row_version=expected_row_version,
            )
        except GeneralSkillLifecycleError as exc:
            raise _state_conflict(exc) from exc
        self.object_store.release_staging(job.id)
        self._release_import_quota(job)
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return import_job_read(job)

    def confirm_job(
        self,
        job_id: str,
        request: GeneralSkillImportConfirm,
        *,
        current_user: User,
    ) -> GeneralSkillImportJobRead:
        """重新鉴权并原子发布所选候选、不可变修订和默认 pinned 绑定。"""

        job = self._owned_job(job_id, current_user)
        ensure_agent_scope_manager(
            self.db,
            job.tenant_id,
            job.target_agent_id,
            current_user,
        )
        confirmation_checksum = _confirmation_request_checksum(request)
        if job.status == ImportJobStatus.INSTALLED:
            if job.preview_json.get("confirmation_request_checksum") == confirmation_checksum:
                return import_job_read(job)
            raise GeneralSkillImportError(
                "GENERAL_SKILL_STATE_CONFLICT",
                "installed import cannot be replayed with a different confirmation",
                409,
            )
        if job.status != ImportJobStatus.AWAITING_APPROVAL:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_STATE_CONFLICT", "import job is not awaiting approval", 409
            )
        if job.preview_checksum != request.preview_checksum:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PREVIEW_MISMATCH",
                "preview checksum no longer matches the reviewed package",
                422,
            )
        candidate_ids = list(dict.fromkeys(request.candidate_ids))
        candidates = {
            str(candidate["candidate_id"]): candidate
            for candidate in job.preview_json.get("candidates", [])
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        }
        if len(candidate_ids) != len(request.candidate_ids) or any(
            candidate_id not in candidates for candidate_id in candidate_ids
        ):
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PACKAGE_INVALID", "candidate selection is invalid", 400
            )
        dependency_decisions = _validated_dependency_decisions(
            candidates,
            candidate_ids,
            request.dependency_decisions,
        )
        try:
            self._claim_confirmation(job, expected_row_version=request.expected_row_version)
            job.preview_json = {
                **job.preview_json,
                "confirmation_request_checksum": confirmation_checksum,
            }
            installed = {
                candidate_id: self._install_candidate(
                    job,
                    candidates[candidate_id],
                    current_user,
                )
                for candidate_id in candidate_ids
            }
            self._install_dependencies(
                job,
                dependency_decisions,
                installed,
                current_user,
            )
            revision_ids = [installed[candidate_id].revision_id for candidate_id in candidate_ids]
            transition_import_job(
                job,
                ImportJobStatus.INSTALLED,
                expected_row_version=job.row_version,
            )
            job.installed_revision_ids_json = revision_ids
            self.object_store.release_staging(job.id)
            self._release_import_quota(job)
            self.db.add(job)
            self.db.commit()
        except GeneralSkillLifecycleError as exc:
            self.db.rollback()
            raise _state_conflict(exc) from exc
        except IntegrityError as exc:
            self.db.rollback()
            raise GeneralSkillImportError(
                "GENERAL_SKILL_STATE_CONFLICT",
                "confirmation conflicted with another committed write",
                409,
            ) from exc
        except SkillObjectStoreError as exc:
            self.db.rollback()
            raise GeneralSkillImportError(
                "GENERAL_SKILL_STORAGE_UNAVAILABLE",
                "confirmed package could not be committed atomically",
                503,
            ) from exc
        self.db.refresh(job)
        return import_job_read(job)

    def _claim_confirmation(
        self,
        job: GeneralSkillImportJob,
        *,
        expected_row_version: int,
    ) -> None:
        """用数据库条件更新抢占 confirm，确保多进程中只有一个写者进入安装事务。"""

        result = self.db.exec(
            update(GeneralSkillImportJob)
            .where(
                GeneralSkillImportJob.id == job.id,
                GeneralSkillImportJob.status == ImportJobStatus.AWAITING_APPROVAL.value,
                GeneralSkillImportJob.row_version == expected_row_version,
            )
            .values(
                status=ImportJobStatus.CONFIRMING.value,
                row_version=GeneralSkillImportJob.row_version + 1,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise GeneralSkillLifecycleError("import confirmation was already claimed")
        self.db.expire(job)
        self.db.refresh(job)

    def _checkpoint_raw_payload(
        self,
        job: GeneralSkillImportJob,
        payload: bytes,
    ) -> None:
        """把原始包与 fetched 状态提交为可恢复检查点，再开始后续分析。"""

        if job.status == ImportJobStatus.CREATED:
            transition_import_job(job, ImportJobStatus.FETCHING, expected_row_version=job.row_version)
        raw_checksum = hashlib.sha256(payload).hexdigest()
        self.object_store.stage_payload(job.id, payload, raw_checksum)
        job.raw_checksum = raw_checksum
        job.staging_manifest_json = [
            {
                "kind": "raw_package",
                "checksum": raw_checksum,
                "size": len(payload),
                "media_type": "application/zip",
            }
        ]
        self._adjust_import_quota(job, len(payload))
        transition_import_job(job, ImportJobStatus.FETCHED, expected_row_version=job.row_version)
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

    def _normalize_fetched(
        self,
        job: GeneralSkillImportJob,
        payload: bytes,
        *,
        source_subpath: str | None = None,
    ) -> GeneralSkillImportJobRead:
        """把已完整抓取的 ZIP 复用到统一规范化、分析、暂存和预览阶段。"""

        transition_import_job(job, ImportJobStatus.NORMALIZING, expected_row_version=job.row_version)
        package = normalize_zip_package(payload, source_subpath=source_subpath)
        transition_import_job(job, ImportJobStatus.NORMALIZED, expected_row_version=job.row_version)
        unique_resources = {
            resource.content_checksum: resource
            for candidate in package.candidates
            for resource in candidate.resources
        }
        self.object_store.stage_resources(job.id, tuple(unique_resources.values()))
        transition_import_job(job, ImportJobStatus.ANALYZING, expected_row_version=job.row_version)
        candidates = [_candidate_preview(candidate) for candidate in package.candidates]
        preview_payload = {
            "schema_version": 1,
            "normalized_checksum": package.normalized_checksum,
            "candidates": candidates,
        }
        source_request_checksum = job.preview_json.get("source_request_checksum")
        if isinstance(source_request_checksum, str) and source_request_checksum:
            preview_payload["source_request_checksum"] = source_request_checksum
        job.normalized_checksum = package.normalized_checksum
        job.preview_json = preview_payload
        job.preview_checksum = _canonical_checksum(preview_payload)
        raw_entries = [
            item
            for item in job.staging_manifest_json
            if isinstance(item, dict) and item.get("kind") == "raw_package"
        ]
        job.staging_manifest_json = raw_entries + [
            {
                "kind": "normalized_resource",
                "checksum": resource.content_checksum,
                "size": resource.size,
                "media_type": resource.media_type,
            }
            for resource in sorted(unique_resources.values(), key=lambda item: item.content_checksum)
        ]
        self._adjust_import_quota(
            job,
            sum(int(item.get("size", 0)) for item in raw_entries) + package.expanded_bytes,
        )
        transition_import_job(
            job,
            ImportJobStatus.AWAITING_APPROVAL,
            expected_row_version=job.row_version,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return import_job_read(job)

    def _create_remote_job(
        self,
        request: GeneralSkillImportJobCreate,
        *,
        idempotency_key: str,
        current_user: User,
        fetcher: RemoteFetcher,
    ) -> GeneralSkillImportJobRead:
        """创建固定远程来源作业，逐跳安全下载后进入与上传相同的预览链。"""

        if request.source_url is None:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PACKAGE_INVALID", "remote source URL is required", 400
            )
        ensure_tenant(self.db, request.tenant_id)
        ensure_agent_scope_manager(
            self.db,
            request.tenant_id,
            request.target_agent_id,
            current_user,
        )
        key = _validated_idempotency_key(idempotency_key)
        source_url = request.source_url.strip()
        allowed_hosts: frozenset[str] | None = None
        try:
            if request.source_kind == "github":
                source_url = github_archive_url(source_url, request.revision or "")
                allowed_hosts = GITHUB_ARCHIVE_HOSTS
                validated_reference = validated_remote_reference(
                    request.source_url,
                    allowed_hosts=frozenset({"github.com"}),
                )
            elif request.source_kind == "skillhub":
                source_url, validated_reference = skillhub_archive_url(source_url)
                allowed_hosts = SKILLHUB_DOWNLOAD_HOSTS
            else:
                if self.https_allowed_hosts is not None:
                    if not self.https_allowed_hosts:
                        raise GeneralSkillImportError(
                            "GENERAL_SKILL_SOURCE_NOT_CONFIGURED",
                            "public HTTPS skill sources are not configured for this deployment",
                            403,
                        )
                    allowed_hosts = self.https_allowed_hosts
                validated_reference = validated_remote_reference(
                    request.source_url,
                    allowed_hosts=allowed_hosts,
                )
        except GeneralSkillRemoteSourceError as exc:
            raise GeneralSkillImportError(exc.error_code, str(exc), 400) from exc
        source_reference = _remote_reference(
            validated_reference,
            request.revision,
            request.source_subpath,
        )
        request_checksum = _canonical_checksum(
            {
                "source_kind": request.source_kind,
                "source_reference": source_reference,
            }
        )
        existing = self.db.exec(
            select(GeneralSkillImportJob).where(
                GeneralSkillImportJob.tenant_id == request.tenant_id,
                GeneralSkillImportJob.owner_user_id == current_user.id,
                GeneralSkillImportJob.idempotency_key == key,
                GeneralSkillImportJob.attempt == 1,
            )
        ).first()
        if existing:
            existing_request_checksum = str(
                existing.preview_json.get("source_request_checksum", "")
            )
            if existing_request_checksum and existing_request_checksum != request_checksum:
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_STATE_CONFLICT",
                    "idempotency key was already used for a different remote source",
                    409,
                )
            return import_job_read(existing)
        self._reserve_import_quota(request.tenant_id, current_user.id)
        now = utc_now()
        job = GeneralSkillImportJob(
            tenant_id=request.tenant_id,
            owner_user_id=current_user.id,
            target_agent_id=request.target_agent_id,
            source_kind=request.source_kind,
            source_reference_redacted=source_reference,
            idempotency_key=key,
            preview_json={
                "source_request_checksum": request_checksum,
                "source_subpath": request.source_subpath,
            },
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.db.add(job)
        try:
            self.db.commit()
            transition_import_job(job, ImportJobStatus.FETCHING, expected_row_version=job.row_version)
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
            result = fetcher.fetch(source_url, allowed_hosts=allowed_hosts)
            self._checkpoint_raw_payload(job, result.payload)
            return self._normalize_fetched(
                job,
                result.payload,
                source_subpath=request.source_subpath,
            )
        except GeneralSkillRemoteSourceError as exc:
            self._fail_job(job, exc.error_code, str(exc))
            return import_job_read(job)
        except GeneralSkillPackageError as exc:
            self._fail_job(job, exc.error_code, str(exc))
            return import_job_read(job)
        except SkillObjectStoreError as exc:
            self._fail_job(job, "GENERAL_SKILL_STORAGE_UNAVAILABLE", str(exc))
            return import_job_read(job)
        except GeneralSkillImportError as exc:
            if exc.error_code != "GENERAL_SKILL_QUOTA_EXCEEDED":
                raise
            self._fail_job(job, exc.error_code, str(exc))
            return import_job_read(job)

    def recover_stale_jobs(
        self,
        *,
        stale_before: datetime,
        limit: int = 100,
    ) -> list[GeneralSkillImportJobRead]:
        """恢复已落 raw 检查点的中断作业，其余陈旧中间态安全失败并回收配额。"""

        active_statuses = [
            status.value
            for status in ImportJobStatus
            if status not in IMPORT_JOB_TERMINAL_STATUSES
            and status != ImportJobStatus.AWAITING_APPROVAL
        ]
        jobs = list(
            self.db.exec(
                select(GeneralSkillImportJob)
                .where(
                    GeneralSkillImportJob.status.in_(active_statuses),
                    GeneralSkillImportJob.updated_at <= stale_before,
                )
                .order_by(GeneralSkillImportJob.updated_at, GeneralSkillImportJob.id)
                .limit(limit)
            ).all()
        )
        recovered: list[GeneralSkillImportJobRead] = []
        for job in jobs:
            if job.status == ImportJobStatus.FETCHED and job.raw_checksum:
                try:
                    payload = self.object_store.read_staged(job.id, job.raw_checksum)
                    source_subpath = job.preview_json.get("source_subpath")
                    recovered.append(
                        self._normalize_fetched(
                            job,
                            payload,
                            source_subpath=(
                                str(source_subpath) if isinstance(source_subpath, str) else None
                            ),
                        )
                    )
                    continue
                except (GeneralSkillPackageError, SkillObjectStoreError) as exc:
                    code = getattr(exc, "error_code", "GENERAL_SKILL_STORAGE_UNAVAILABLE")
                    self._fail_job(job, str(code), str(exc))
            else:
                self._fail_job(
                    job,
                    "GENERAL_SKILL_RECOVERY_REQUIRED",
                    "interrupted import had no safe replay checkpoint",
                )
            recovered.append(import_job_read(job))
        return recovered

    def expire_jobs(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[GeneralSkillImportJobRead]:
        """幂等终止到期非终态作业，并同步清除 raw/规范资源与配额事实。"""

        jobs = list(
            self.db.exec(
                select(GeneralSkillImportJob)
                .where(
                    GeneralSkillImportJob.status.not_in(
                        [status.value for status in IMPORT_JOB_TERMINAL_STATUSES]
                    ),
                    GeneralSkillImportJob.expires_at <= now,
                )
                .order_by(GeneralSkillImportJob.expires_at, GeneralSkillImportJob.id)
                .limit(limit)
            ).all()
        )
        expired: list[GeneralSkillImportJobRead] = []
        for job in jobs:
            transition_import_job(
                job,
                ImportJobStatus.EXPIRED,
                expected_row_version=job.row_version,
                error_code="GENERAL_SKILL_IMPORT_EXPIRED",
                error_detail_redacted="import approval window expired",
            )
            self.object_store.release_staging(job.id)
            self._release_import_quota(job)
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
            expired.append(import_job_read(job))
        return expired

    def _fail_job(self, job: GeneralSkillImportJob, error_code: str, detail: str) -> None:
        """将非终态作业落为失败并清理可能已产生的暂存对象。"""

        transition_import_job(
            job,
            ImportJobStatus.FAILED,
            expected_row_version=job.row_version,
            error_code=error_code,
            error_detail_redacted=detail,
        )
        self.object_store.release_staging(job.id)
        self._release_import_quota(job)
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

    def _reserve_import_quota(self, tenant_id: str, owner_user_id: str) -> None:
        """以条件更新同时预留 tenant/user 活动作业名额，任一级失败则整体回滚。"""

        scopes = (
            ("tenant", tenant_id, TENANT_ACTIVE_IMPORT_LIMIT),
            ("user", owner_user_id, USER_ACTIVE_IMPORT_LIMIT),
        )
        for scope_kind, scope_id, _ in scopes:
            self._ensure_quota_row(tenant_id, scope_kind, scope_id)
        for scope_kind, scope_id, active_limit in scopes:
            result = self.db.exec(
                update(GeneralSkillImportQuota)
                .where(
                    GeneralSkillImportQuota.tenant_id == tenant_id,
                    GeneralSkillImportQuota.scope_kind == scope_kind,
                    GeneralSkillImportQuota.scope_id == scope_id,
                    GeneralSkillImportQuota.active_jobs < active_limit,
                )
                .values(
                    active_jobs=GeneralSkillImportQuota.active_jobs + 1,
                    row_version=GeneralSkillImportQuota.row_version + 1,
                    updated_at=utc_now(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                self.db.rollback()
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_QUOTA_EXCEEDED",
                    f"{scope_kind} concurrent import limit exceeded",
                    429,
                )

    def _ensure_quota_row(self, tenant_id: str, scope_kind: str, scope_id: str) -> None:
        """幂等创建配额计数行；并发唯一冲突回滚后重新读取既有行。"""

        existing = self.db.exec(
            select(GeneralSkillImportQuota).where(
                GeneralSkillImportQuota.tenant_id == tenant_id,
                GeneralSkillImportQuota.scope_kind == scope_kind,
                GeneralSkillImportQuota.scope_id == scope_id,
            )
        ).first()
        if existing:
            return
        self.db.add(
            GeneralSkillImportQuota(
                tenant_id=tenant_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
            )
        )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if not self.db.exec(
                select(GeneralSkillImportQuota.id).where(
                    GeneralSkillImportQuota.tenant_id == tenant_id,
                    GeneralSkillImportQuota.scope_kind == scope_kind,
                    GeneralSkillImportQuota.scope_id == scope_id,
                )
            ).first():
                raise

    def _adjust_import_quota(self, job: GeneralSkillImportJob, new_bytes: int) -> None:
        """原子调整两级暂存字节，正增量受硬上限约束且任一级失败全部回滚。"""

        delta = new_bytes - job.quota_bytes
        if delta == 0:
            return
        scopes = (
            ("tenant", job.tenant_id, TENANT_STAGED_BYTE_LIMIT),
            ("user", job.owner_user_id, USER_STAGED_BYTE_LIMIT),
        )
        for scope_kind, scope_id, byte_limit in scopes:
            statement = update(GeneralSkillImportQuota).where(
                GeneralSkillImportQuota.tenant_id == job.tenant_id,
                GeneralSkillImportQuota.scope_kind == scope_kind,
                GeneralSkillImportQuota.scope_id == scope_id,
                GeneralSkillImportQuota.staged_bytes + delta >= 0,
            )
            if delta > 0:
                statement = statement.where(
                    GeneralSkillImportQuota.staged_bytes + delta <= byte_limit
                )
            result = self.db.exec(
                statement.values(
                    staged_bytes=GeneralSkillImportQuota.staged_bytes + delta,
                    row_version=GeneralSkillImportQuota.row_version + 1,
                    updated_at=utc_now(),
                ).execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                self.db.rollback()
                self.db.refresh(job)
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_QUOTA_EXCEEDED",
                    f"{scope_kind} staged byte limit exceeded",
                    429,
                )
        job.quota_bytes = new_bytes

    def _release_import_quota(self, job: GeneralSkillImportJob) -> None:
        """在作业终止事务中同时归还两级活跃名额和其当前暂存字节。"""

        scopes = (("tenant", job.tenant_id), ("user", job.owner_user_id))
        for scope_kind, scope_id in scopes:
            result = self.db.exec(
                update(GeneralSkillImportQuota)
                .where(
                    GeneralSkillImportQuota.tenant_id == job.tenant_id,
                    GeneralSkillImportQuota.scope_kind == scope_kind,
                    GeneralSkillImportQuota.scope_id == scope_id,
                    GeneralSkillImportQuota.active_jobs > 0,
                    GeneralSkillImportQuota.staged_bytes >= job.quota_bytes,
                )
                .values(
                    active_jobs=GeneralSkillImportQuota.active_jobs - 1,
                    staged_bytes=GeneralSkillImportQuota.staged_bytes - job.quota_bytes,
                    row_version=GeneralSkillImportQuota.row_version + 1,
                    updated_at=utc_now(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise SkillObjectStoreError("import quota reservation is inconsistent")
        job.quota_bytes = 0

    def _owned_job(self, job_id: str, current_user: User) -> GeneralSkillImportJob:
        """按当前用户 tenant 和 owner 同时定位作业，越权统一表现为不可用。"""

        job = self.db.exec(
            select(GeneralSkillImportJob).where(
                GeneralSkillImportJob.id == job_id,
                GeneralSkillImportJob.tenant_id == current_user.tenant_id,
                GeneralSkillImportJob.owner_user_id == current_user.id,
            )
        ).first()
        if not job:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_NOT_AVAILABLE", "import job is not available", 404
            )
        return job

    def _install_candidate(
        self,
        job: GeneralSkillImportJob,
        candidate: dict[str, Any],
        current_user: User,
    ) -> _InstalledCandidate:
        """创建独立 Skill 根、published revision 和当前 Agent 的 pinned 绑定。"""

        resources = candidate.get("resources")
        if not isinstance(resources, list):
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PACKAGE_INVALID", "candidate resources are invalid", 400
            )
        promoted_manifest: list[dict[str, object]] = []
        legacy_files: list[dict[str, object]] = []
        markdown = ""
        for resource in resources:
            if not isinstance(resource, dict):
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_PACKAGE_INVALID", "candidate resource is invalid", 400
                )
            checksum = str(resource["content_checksum"])
            content = self.object_store.read_staged_or_object(job.id, checksum)
            object_key = self.object_store.promote(job.id, checksum)
            relative_path = str(resource["relative_path"])
            is_text = bool(resource["is_text"])
            promoted_manifest.append({**resource, "object_key": object_key})
            if is_text:
                text_content = content.decode("utf-8", errors="strict")
                legacy_files.append(
                    {
                        "path": relative_path,
                        "content": text_content,
                        "size": len(content),
                        "mime_type": str(resource["media_type"]),
                    }
                )
                if relative_path == "SKILL.md":
                    markdown = text_content
        if not markdown:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PACKAGE_INVALID", "selected candidate has no SKILL.md", 400
            )
        skill = GeneralSkill(
            tenant_id=job.tenant_id,
            slug=self._unique_slug(job.tenant_id, str(candidate["name"])),
            name=str(candidate["name"]),
            description=str(candidate["description"]),
            skill_markdown=markdown,
            skill_files_json=legacy_files,
            metadata_json={
                "created_by_user_id": current_user.id,
                "import_job_id": job.id,
                "source_kind": job.source_kind,
            },
            status="published",
            permissions_json={"requested_tools": list(candidate.get("allowed_tools", []))},
            runtime_config_json={},
            usage_mode="planning_guidance",
            owner_user_id=current_user.id,
            visibility_scope="agent_private",
        )
        self.db.add(skill)
        self.db.flush()
        revision = GeneralSkillRevision(
            tenant_id=job.tenant_id,
            skill_id=skill.id,
            revision_number=1,
            content_checksum=str(candidate["content_checksum"]),
            manifest_checksum=str(candidate["manifest_checksum"]),
            normalized_skill_markdown=markdown,
            parsed_metadata_json=dict(candidate.get("metadata", {})),
            resource_manifest_json=promoted_manifest,
            requested_capabilities_json={
                "allowed_tools": list(candidate.get("allowed_tools", [])),
                "invocation_policy": str(candidate.get("invocation_policy", "model_allowed")),
                "argument_hint": candidate.get("argument_hint"),
            },
            source_snapshot_json={
                "source_kind": job.source_kind,
                "source_reference_redacted": job.source_reference_redacted,
                "raw_checksum": job.raw_checksum,
                "normalized_checksum": job.normalized_checksum,
                "import_job_id": job.id,
            },
            created_by=current_user.id,
        )
        transition_revision(revision, RevisionStatus.REVIEWING, expected_row_version=1)
        transition_revision(revision, RevisionStatus.PUBLISHED, expected_row_version=2)
        self.db.add(revision)
        self.db.flush()
        skill.current_published_revision_id = revision.id
        skill.row_version += 1
        self.db.add(skill)
        binding = AgentResourceBinding(
            tenant_id=job.tenant_id,
            agent_id=job.target_agent_id,
            resource_type="general_skill",
            resource_id=skill.id,
            status="active",
            metadata_json={
                "schema_version": 1,
                "revision_policy": "pinned",
                "pinned_revision_id": revision.id,
                "invocation_policy": str(candidate.get("invocation_policy", "model_allowed")),
                "atomic_execution_allowed": False,
                "created_by_user_id": current_user.id,
            },
        )
        self.db.add(binding)
        return _InstalledCandidate(
            skill_id=skill.id,
            revision_id=revision.id,
            invocation_policy=str(candidate.get("invocation_policy", "model_allowed")),
        )

    def _install_dependencies(
        self,
        job: GeneralSkillImportJob,
        decisions: list[tuple[str, str, str]],
        installed: dict[str, _InstalledCandidate],
        current_user: User,
    ) -> None:
        """把已审核且两端均安装的候选边固定到稳定 Skill/Revision 标识。"""

        for parent_candidate_id, child_candidate_id, dependency_kind in decisions:
            parent = installed[parent_candidate_id]
            child = installed[child_candidate_id]
            edge_payload = {
                "tenant_id": job.tenant_id,
                "parent_revision_id": parent.revision_id,
                "child_revision_id": child.revision_id,
                "dependency_kind": dependency_kind,
                "source": "human_confirmed",
                "allow_user_only": child.invocation_policy == "user_only",
            }
            self.db.add(
                GeneralSkillDependency(
                    tenant_id=job.tenant_id,
                    parent_skill_id=parent.skill_id,
                    parent_revision_id=parent.revision_id,
                    child_skill_id=child.skill_id,
                    child_revision_id=child.revision_id,
                    dependency_kind=dependency_kind,
                    source="human_confirmed",
                    allow_user_only=child.invocation_policy == "user_only",
                    edge_checksum=_canonical_checksum(edge_payload),
                    created_by=current_user.id,
                )
            )

    def _unique_slug(self, tenant_id: str, name: str) -> str:
        """生成租户内不覆盖既有 Skill 的稳定可读 slug。"""

        base = SLUG_INVALID.sub("-", name.lower()).strip("-") or "imported-skill"
        base = base[:160]
        candidate = base
        suffix = 2
        while self.db.exec(
            select(GeneralSkill.id).where(
                GeneralSkill.tenant_id == tenant_id,
                GeneralSkill.slug == candidate,
            )
        ).first():
            candidate = f"{base[:150]}-{suffix}"
            suffix += 1
        return candidate


def import_job_read(job: GeneralSkillImportJob) -> GeneralSkillImportJobRead:
    """把持久化作业转换为不暴露 owner、credential 或正文的 API 投影。"""

    candidates = [
        GeneralSkillImportCandidateRead.model_validate(candidate)
        for candidate in job.preview_json.get("candidates", [])
        if isinstance(candidate, dict)
    ]
    return GeneralSkillImportJobRead(
        id=job.id,
        tenant_id=job.tenant_id,
        target_agent_id=job.target_agent_id,
        source_kind=job.source_kind,
        source_reference_redacted=job.source_reference_redacted,
        status=job.status,
        attempt=job.attempt,
        raw_checksum=job.raw_checksum,
        normalized_checksum=job.normalized_checksum,
        preview_checksum=job.preview_checksum,
        quota_bytes=job.quota_bytes,
        error_code=job.error_code,
        error_detail_redacted=job.error_detail_redacted,
        candidates=candidates,
        expires_at=job.expires_at.isoformat(),
        row_version=job.row_version,
        installed_revision_ids=list(job.installed_revision_ids_json or []),
    )


def _candidate_preview(candidate: SkillCandidate) -> dict[str, object]:
    """生成不含正文、但足以核验内容树和权限候选的预览。"""

    resources = [
        {
            "relative_path": _relative_candidate_path(resource.path, candidate.root),
            "content_checksum": resource.content_checksum,
            "size": resource.size,
            "media_type": resource.media_type,
            "is_text": resource.is_text,
        }
        for resource in candidate.resources
    ]
    return {
        "candidate_id": candidate.candidate_id,
        "manifest_path": candidate.manifest_path,
        "name": candidate.name,
        "description": candidate.description,
        "content_checksum": candidate.content_checksum,
        "manifest_checksum": candidate.manifest_checksum,
        "metadata": candidate.metadata,
        "allowed_tools": list(candidate.allowed_tools),
        "invocation_policy": candidate.invocation_policy,
        "argument_hint": candidate.argument_hint,
        "dependency_candidates": [
            {
                "dependency_candidate_id": dependency.dependency_candidate_id,
                "referenced_name": dependency.referenced_name,
                "referenced_candidate_id": dependency.referenced_candidate_id,
                "reference_count": dependency.reference_count,
            }
            for dependency in candidate.dependency_candidates
        ],
        "platform_commands": list(candidate.platform_commands),
        "resources": resources,
    }


def _decode_base64(value: str, *, allow_empty: bool = False) -> bytes:
    """严格解码上传正文，拒绝非 base64 字符和空包。"""

    try:
        payload = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GeneralSkillImportError(
            "GENERAL_SKILL_PACKAGE_INVALID", "upload content is not valid base64", 400
        ) from exc
    if not payload and not allow_empty:
        raise GeneralSkillImportError(
            "GENERAL_SKILL_PACKAGE_INVALID", "upload package is empty", 400
        )
    return payload


def _build_folder_archive(files: list[GeneralSkillUploadFile]) -> bytes:
    """把文件夹相对路径清单重建为内存 ZIP，再交给唯一安全规范化入口。"""

    if not files or len(files) > 240:
        raise GeneralSkillImportError(
            "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED", "folder file count is invalid", 413
        )
    decoded: list[tuple[str, bytes]] = []
    expanded_bytes = 0
    for item in files:
        path = str(item.path)
        content = _decode_base64(str(item.content_base64), allow_empty=True)
        expanded_bytes += len(content)
        if expanded_bytes > 80 * 1024 * 1024:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED",
                "folder expanded size exceeds configured byte limit",
                413,
            )
        decoded.append((path, content))
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for path, content in sorted(decoded, key=lambda item: item[0]):
            entry = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content)
    return buffer.getvalue()


def _validated_idempotency_key(value: str) -> str:
    """限制幂等键长度和字符，避免把凭据或正文误写入索引。"""

    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", normalized):
        raise GeneralSkillImportError(
            "GENERAL_SKILL_PACKAGE_INVALID", "Idempotency-Key is invalid", 400
        )
    return normalized


def _prepare_upload_payload(request: GeneralSkillImportJobCreate) -> bytes:
    """把单个 SKILL.md 或浏览器文件夹封装为 ZIP，统一复用安全规范化边界。"""

    if request.content_base64 is None:
        return _build_folder_archive(request.files or [])
    payload = _decode_base64(request.content_base64)
    filename = (request.filename or "").lower()
    if filename.endswith(".zip"):
        return payload
    if filename.endswith(".md") and PurePath(filename).name == "skill.md":
        return _build_folder_archive(
            [GeneralSkillUploadFile(path="SKILL.md", content_base64=request.content_base64)]
        )
    raise GeneralSkillImportError(
        "GENERAL_SKILL_PACKAGE_INVALID",
        "upload must be a ZIP package or a file named SKILL.md",
        400,
    )


def _redacted_filename(value: str) -> str:
    """只保留无路径的上传文件名作为来源展示。"""

    normalized = value.replace("\\", "/")
    return PurePath(normalized.rsplit("/", 1)[-1]).name[:255]


def _remote_reference(
    validated_source_url: str,
    revision: str | None,
    source_subpath: str | None,
) -> str:
    """为已校验、已脱敏的远程 URL 附加固定 revision 和候选子路径证据。"""

    redacted = validated_source_url
    if revision:
        redacted = f"{redacted}@{revision.lower()}"
    return f"{redacted}#{source_subpath}" if source_subpath else redacted


def _relative_candidate_path(path: str, root: str) -> str:
    """从规范包路径生成候选根内相对路径。"""

    return path[len(root) + 1 :] if root else path


def _canonical_checksum(value: object) -> str:
    """计算 preview 等结构化契约的规范 JSON checksum。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _confirmation_request_checksum(request: GeneralSkillImportConfirm) -> str:
    """规范化 confirm 的语义集合，使同内容重放成功而不同裁决稳定冲突。"""

    return _canonical_checksum(
        {
            "preview_checksum": request.preview_checksum,
            "candidate_ids": sorted(request.candidate_ids),
            "dependency_decisions": sorted(
                (
                    {
                        "dependency_candidate_id": decision.dependency_candidate_id,
                        "dependency_kind": decision.dependency_kind,
                    }
                    for decision in request.dependency_decisions
                ),
                key=lambda item: str(item["dependency_candidate_id"]),
            ),
        }
    )


def _state_conflict(exc: Exception) -> GeneralSkillImportError:
    """把内部状态机异常转换为稳定的 409 API 错误。"""

    return GeneralSkillImportError("GENERAL_SKILL_STATE_CONFLICT", str(exc), 409)


def _validated_dependency_decisions(
    candidates: dict[str, dict[str, Any]],
    selected_candidate_ids: list[str],
    decisions: list[GeneralSkillDependencyDecision],
) -> list[tuple[str, str, str]]:
    """要求逐边裁决完整且无环，并返回需持久化的必需/可选稳定候选边。"""

    selected = set(selected_candidate_ids)
    available: dict[str, tuple[str, str]] = {}
    for parent_id in selected_candidate_ids:
        dependency_candidates = candidates[parent_id].get("dependency_candidates", [])
        if not isinstance(dependency_candidates, list):
            raise GeneralSkillImportError(
                "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency preview is invalid", 400
            )
        for dependency in dependency_candidates:
            if not isinstance(dependency, dict):
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency preview is invalid", 400
                )
            decision_id = str(dependency.get("dependency_candidate_id", ""))
            child_id = str(dependency.get("referenced_candidate_id", ""))
            if not decision_id or not child_id or decision_id in available:
                raise GeneralSkillImportError(
                    "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency identity is ambiguous", 400
                )
            available[decision_id] = (parent_id, child_id)
    by_id = {decision.dependency_candidate_id: decision.dependency_kind for decision in decisions}
    if len(by_id) != len(decisions) or set(by_id) != set(available):
        raise GeneralSkillImportError(
            "GENERAL_SKILL_DEPENDENCY_INVALID",
            "every dependency candidate must be explicitly classified",
            400,
        )
    accepted: list[tuple[str, str, str]] = []
    for decision_id, (parent_id, child_id) in available.items():
        dependency_kind = by_id[decision_id]
        if dependency_kind == "ignored":
            continue
        if child_id not in selected:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_DEPENDENCY_INVALID",
                "confirmed dependency must be selected for installation",
                400,
            )
        accepted.append((parent_id, child_id, dependency_kind))
    _validate_dependency_graph(accepted)
    return accepted


def _validate_dependency_graph(edges: list[tuple[str, str, str]]) -> None:
    """在写库前拒绝依赖环、超深和超量展开，避免把故障推迟到运行时。"""

    if len(edges) > 32:
        raise GeneralSkillImportError(
            "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency graph exceeds edge limit", 400
        )
    graph: dict[str, set[str]] = {}
    for parent_id, child_id, _ in edges:
        graph.setdefault(parent_id, set()).add(child_id)

    def visit(node: str, path: tuple[str, ...]) -> None:
        """深度优先验证当前候选路径没有回边且深度不超过八层。"""

        if node in path:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency graph contains a cycle", 400
            )
        if len(path) >= 8:
            raise GeneralSkillImportError(
                "GENERAL_SKILL_DEPENDENCY_INVALID", "dependency graph exceeds depth limit", 400
            )
        next_path = (*path, node)
        for child_id in sorted(graph.get(node, set())):
            visit(child_id, next_path)

    for root in sorted(graph):
        visit(root, ())
