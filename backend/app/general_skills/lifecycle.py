"""
@Time       : 2026/08/11 23:18
@Author     : zhanglp8181
@File       : lifecycle.py
@CallChain  : GeneralSkill import API/worker → lifecycle transition → SQLModel transaction
@Description: 集中定义 Skill 导入作业与不可变修订的合法状态迁移和并发版本门禁。
"""

from __future__ import annotations

from enum import StrEnum

from app.db.models import GeneralSkillImportJob, GeneralSkillRevision, utc_now


class GeneralSkillLifecycleError(ValueError):
    """表示状态迁移或乐观锁违反已冻结的 Skill 生命周期契约。"""

    error_code = "GENERAL_SKILL_STATE_CONFLICT"


class ImportJobStatus(StrEnum):
    """列举导入作业可持久化的完整状态。"""

    CREATED = "created"
    FETCHING = "fetching"
    FETCHED = "fetched"
    NORMALIZING = "normalizing"
    NORMALIZED = "normalized"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    CONFIRMING = "confirming"
    INSTALLED = "installed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class RevisionStatus(StrEnum):
    """列举不可变修订可持久化的审核与发布状态。"""

    DRAFT = "draft"
    REVIEWING = "reviewing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


IMPORT_JOB_TRANSITIONS: dict[ImportJobStatus, frozenset[ImportJobStatus]] = {
    ImportJobStatus.CREATED: frozenset({ImportJobStatus.FETCHING}),
    ImportJobStatus.FETCHING: frozenset({ImportJobStatus.FETCHED}),
    ImportJobStatus.FETCHED: frozenset({ImportJobStatus.NORMALIZING}),
    ImportJobStatus.NORMALIZING: frozenset({ImportJobStatus.NORMALIZED}),
    ImportJobStatus.NORMALIZED: frozenset({ImportJobStatus.ANALYZING}),
    ImportJobStatus.ANALYZING: frozenset({ImportJobStatus.AWAITING_APPROVAL}),
    ImportJobStatus.AWAITING_APPROVAL: frozenset({ImportJobStatus.CONFIRMING}),
    ImportJobStatus.CONFIRMING: frozenset({ImportJobStatus.INSTALLED}),
    ImportJobStatus.INSTALLED: frozenset(),
    ImportJobStatus.FAILED: frozenset(),
    ImportJobStatus.CANCELLED: frozenset(),
    ImportJobStatus.EXPIRED: frozenset(),
}

REVISION_TRANSITIONS: dict[RevisionStatus, frozenset[RevisionStatus]] = {
    RevisionStatus.DRAFT: frozenset({RevisionStatus.REVIEWING, RevisionStatus.REJECTED}),
    RevisionStatus.REVIEWING: frozenset(
        {RevisionStatus.PUBLISHED, RevisionStatus.REJECTED}
    ),
    RevisionStatus.PUBLISHED: frozenset(
        {RevisionStatus.SUPERSEDED, RevisionStatus.REVOKED}
    ),
    RevisionStatus.SUPERSEDED: frozenset({RevisionStatus.REVOKED}),
    RevisionStatus.REJECTED: frozenset(),
    RevisionStatus.REVOKED: frozenset(),
}

IMPORT_JOB_TERMINAL_STATUSES = frozenset(
    {
        ImportJobStatus.INSTALLED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.EXPIRED,
    }
)


def transition_import_job(
    job: GeneralSkillImportJob,
    target: ImportJobStatus,
    *,
    expected_row_version: int,
    error_code: str | None = None,
    error_detail_redacted: str | None = None,
) -> None:
    """校验单步或终止迁移并原地更新作业时间戳与乐观锁版本。"""

    current = ImportJobStatus(job.status)
    if job.row_version != expected_row_version:
        raise GeneralSkillLifecycleError("import job row version does not match")
    allowed = IMPORT_JOB_TRANSITIONS[current]
    can_terminate = target in {
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.EXPIRED,
    } and current not in IMPORT_JOB_TERMINAL_STATUSES
    if target not in allowed and not can_terminate:
        raise GeneralSkillLifecycleError(f"illegal import job transition: {current} -> {target}")
    now = utc_now()
    job.status = target.value
    job.row_version += 1
    job.updated_at = now
    if target == ImportJobStatus.FETCHED:
        job.fetched_at = now
    elif target == ImportJobStatus.NORMALIZED:
        job.normalized_at = now
    elif target == ImportJobStatus.AWAITING_APPROVAL:
        job.analyzed_at = now
    elif target == ImportJobStatus.INSTALLED:
        job.confirmed_at = now
    if target in IMPORT_JOB_TERMINAL_STATUSES:
        job.terminal_at = now
    job.error_code = error_code
    job.error_detail_redacted = error_detail_redacted


def transition_revision(
    revision: GeneralSkillRevision,
    target: RevisionStatus,
    *,
    expected_row_version: int,
) -> None:
    """只允许 ADR 定义的修订状态边并同步发布、撤销时间和行版本。"""

    current = RevisionStatus(revision.status)
    if revision.row_version != expected_row_version:
        raise GeneralSkillLifecycleError("revision row version does not match")
    if target not in REVISION_TRANSITIONS[current]:
        raise GeneralSkillLifecycleError(f"illegal revision transition: {current} -> {target}")
    now = utc_now()
    revision.status = target.value
    revision.row_version += 1
    if target == RevisionStatus.PUBLISHED:
        revision.published_at = now
    elif target == RevisionStatus.REVOKED:
        revision.revoked_at = now
