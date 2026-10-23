"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : worker.py
@CallChain  : 唤醒/扫描 → MemoryCaptureJob lease → 模型外呼 → MemoryKeyState 原子结算
@Description: 提供有界领取、租约 fence、实际模型派发计数和迟到写入屏障。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import wraps
import logging
import os
import unicodedata
from threading import Event, Lock, Thread, current_thread
from typing import Any
from time import sleep

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.db.time import database_utc_now
from app.memory.keys import normalize_memory_key_identity
from app.db.models import (
    AgentProfile,
    ChatSession,
    MemoryCaptureJob,
    MemoryCaptureScope,
    MemoryKeyState,
    MemoryRecord,
    Message,
    ModelConfig,
    User,
    new_id,
    utc_now,
)
from app.memory.capture import (
    AGENT_SCOPE_KIND,
    USER_SCOPE_KEY,
    _message_hash,
    configure_memory_capture_wakeup,
    memory_model_contract_hash,
)
from app.memory.service import (
    MemoryAgentUnavailable, MemoryService, memory_effective_agent_id,
)
from app.memory import service as memory_service
from app.memory.extraction_contract import MemoryExtractionContract, MemoryExtractionVersionInvalid
from app.observability.event_log import EventLog
from app.security.permissions import can_use_agent_in_chat
from app.session.session_schema import ChatTurnRequest, StepAgentResult


LEASE_SECONDS = 90
DEFAULT_DEADLINE_SECONDS = 600
RETRY_DELAYS_SECONDS = (5, 15)
MAX_CLAIM_BATCH = 50
MAX_MODEL_DISPATCHES_PER_JOB = 6
MAX_MODEL_DISPATCHES_PER_CLAIM = 2
PROVIDER_TIMEOUT_SECONDS = 60
HEARTBEAT_SECONDS = 20
_worker_lock = Lock()
_worker_executor: ThreadPoolExecutor | None = None
_worker_scan_thread: Thread | None = None
_worker_stop_event = Event()
WORKER_POLL_SECONDS = 2.0
LOGGER = logging.getLogger(__name__)
_MEMORY_PLACEHOLDER_PUNCTUATION = " \t\r\n。.!！?？:：;；,，、\"'“”‘’`()[]{}"
_UNKNOWN_MEMORY_PLACEHOLDERS = frozenset({
    "未知",
    "不知道",
    "不清楚",
    "不确定",
    "未提及",
    "未提供",
    "无相关信息",
    "无法确定",
    "暂无信息",
    "没有答案",
    "unknown",
    "not known",
    "not specified",
    "not mentioned",
    "not provided",
    "not sure",
    "unsure",
    "n/a",
    "none",
})


class MemoryCaptureFenceLost(RuntimeError):
    """表示当前 worker 已失去任务租约或 fencing token。"""


class MemoryCaptureDispatchLimit(MemoryCaptureFenceLost):
    """表示本次 claim 已用尽实际发送预算，需按原任务有限重试。"""


class MemoryCaptureSourceInvalid(RuntimeError):
    """表示来源消息缺失、被改写或无法通过固定 manifest 校验。"""


def _retry_short_write(function):
    """仅重试 SQLite busy/MySQL deadlock 的短结算，不重复执行模型。"""

    @wraps(function)
    def retry(db, *args, **kwargs):
        """每次冲突回滚并重新取得当前写权，最多执行三个短事务。"""

        for attempt in range(3):
            try:
                return function(db, *args, **kwargs)
            except OperationalError as exc:
                db.rollback()
                code = exc.orig.args[0] if exc.orig.args else None
                if attempt == 2 or not (
                    code in {1205, 1213} or "locked" in str(exc.orig).lower()
                ):
                    raise
                sleep(0.03 * (attempt + 1))
            except Exception:
                db.rollback()
                raise
        return None
    return retry


def _lock_job_for_write(
    db: Session, job_id: str, worker_id: str, fencing_token: int
) -> MemoryCaptureJob:
    """先沿前台 User/Agent 生命周期锁序，再按 user scope→agent scope→job 保留写权。"""

    identity = db.exec(select(
        MemoryCaptureJob.tenant_id, MemoryCaptureJob.user_id, MemoryCaptureJob.scope_id,
        MemoryCaptureJob.agent_id,
    ).where(MemoryCaptureJob.id == job_id)).first()
    db.rollback()
    if identity is None:
        raise MemoryCaptureFenceLost("memory capture job missing")
    tenant_id, user_id, scope_id, agent_id = identity
    # 前台完成持有 Agent 后才写 session，成员变更持有 User 后才推进 scope。
    # 先取得这些资源行，避免后台持有 scope/session 后反向等待前台资源。
    _current_row(db, User, user_id)
    if agent_id:
        _current_row(db, AgentProfile, agent_id)
    for conditions in (
        (MemoryCaptureScope.tenant_id == tenant_id, MemoryCaptureScope.user_id == user_id,
         MemoryCaptureScope.scope_kind == "user", MemoryCaptureScope.agent_scope_key == USER_SCOPE_KEY),
        (MemoryCaptureScope.id == scope_id,),
    ):
        db.exec(update(MemoryCaptureScope).where(*conditions).values(
            row_version=MemoryCaptureScope.row_version + 1
        ).execution_options(synchronize_session=False))
    job = db.exec(select(MemoryCaptureJob).where(MemoryCaptureJob.id == job_id)
        .with_for_update().execution_options(populate_existing=True)).first()
    if job is None or not _job_fence_matches(job, worker_id, fencing_token, database_utc_now(db)):
        db.rollback()
        raise MemoryCaptureFenceLost("memory capture write fence lost")
    result = db.exec(update(MemoryCaptureJob).where(
        *_write_conditions(db, job, worker_id, fencing_token, job.row_version)
    ).values(row_version=MemoryCaptureJob.row_version + 1)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise MemoryCaptureFenceLost("memory capture write reservation lost")
    db.refresh(job)
    return MemoryCaptureJob.model_validate(job.model_dump())


def _write_conditions(db, job, worker_id, fencing_token, version):
    """最终写入仍要求同一 owner、fence、版本、有效租约和总截止。"""

    now = database_utc_now(db)
    return (
        MemoryCaptureJob.id == job.id, MemoryCaptureJob.status == "running",
        MemoryCaptureJob.lease_owner == worker_id, MemoryCaptureJob.fencing_token == fencing_token,
        MemoryCaptureJob.row_version == version, MemoryCaptureJob.lease_expires_at > now,
        or_(MemoryCaptureJob.deadline_at.is_(None), MemoryCaptureJob.deadline_at > now),
    )


def _commit_fenced_job(db, job, worker_id, fencing_token, version):
    """正文、key、事件与条件终态一并提交；任何失权都回滚全部派生变化。"""

    conditions = _write_conditions(db, job, worker_id, fencing_token, version)
    values = {name: getattr(job, name) for name in (
        "status", "finished_at", "terminal_reason", "error_code", "next_attempt_at",
        "lease_owner", "lease_expires_at", "row_version", "applied_count", "discarded_count",
    )}
    if job in db:
        db.expunge(job)
    with db.no_autoflush:
        result = db.exec(update(MemoryCaptureJob).where(*conditions).values(**values)
            .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise MemoryCaptureFenceLost("memory capture terminal CAS lost")
    db.commit()
    db.expire_all()


def _claimable_statement(job_id: str | None, now) -> Any:
    """构造仅命中待领取或过期 running 任务的有界查询。"""

    statement = select(MemoryCaptureJob)
    claimable_deadline = or_(
        MemoryCaptureJob.deadline_at.is_(None),
        MemoryCaptureJob.deadline_at > now,
    )
    retry_ready = or_(
        MemoryCaptureJob.next_attempt_at.is_(None),
        MemoryCaptureJob.next_attempt_at <= now,
    )
    statement = statement.where(
        and_(
            MemoryCaptureJob.id == job_id if job_id else True,
            or_(
                and_(
                    MemoryCaptureJob.status.in_(("pending", "retry")),
                    retry_ready,
                    claimable_deadline,
                ),
                and_(
                    MemoryCaptureJob.status == "running",
                    MemoryCaptureJob.lease_expires_at <= now,
                    claimable_deadline,
                ),
            ),
        )
    )
    return statement.order_by(
        MemoryCaptureJob.source_sequence.asc(),
        MemoryCaptureJob.created_at.asc(),
        MemoryCaptureJob.id.asc(),
    ).limit(1).with_for_update(skip_locked=True)


@_retry_short_write
def reap_memory_capture_jobs(db: Session, *, job_id: str | None = None, limit: int = 50) -> int:
    """有界条件结算截止或耗尽的持久任务，不抢活动租约，也不触发模型。"""

    now = database_utc_now(db)
    inactive = or_(MemoryCaptureJob.status.in_(("pending", "retry")),
        and_(MemoryCaptureJob.status == "running", MemoryCaptureJob.lease_expires_at <= now))
    exhausted = or_(MemoryCaptureJob.attempt_count >= 3, MemoryCaptureJob.attempt_count >= MemoryCaptureJob.max_attempts,
        MemoryCaptureJob.model_dispatch_count >= MAX_MODEL_DISPATCHES_PER_JOB)
    deadline = MemoryCaptureJob.deadline_at <= now
    rows = db.exec(select(MemoryCaptureJob).where(
        MemoryCaptureJob.id == job_id if job_id else True,
        MemoryCaptureJob.status.in_(("pending", "retry", "running")),
        or_(deadline, and_(inactive, exhausted)),
    ).order_by(MemoryCaptureJob.created_at, MemoryCaptureJob.id)
        .limit(max(0, min(limit, MAX_CLAIM_BATCH)))
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)).all()
    count = 0
    for row in rows:
        expired = row.deadline_at is not None and row.deadline_at <= now
        result = db.exec(update(MemoryCaptureJob).where(
            MemoryCaptureJob.id == row.id, MemoryCaptureJob.row_version == row.row_version,
            MemoryCaptureJob.status == row.status,
            or_(deadline, and_(inactive, exhausted)),
        ).values(status="failed", finished_at=now, lease_owner=None, lease_expires_at=None,
            row_version=MemoryCaptureJob.row_version + 1,
            terminal_reason="deadline_exceeded" if expired else "max_attempts_exceeded",
            error_code="MEMORY_CAPTURE_DEADLINE" if expired else "MEMORY_CAPTURE_MAX_ATTEMPTS",
        ).execution_options(synchronize_session=False))
        if result.rowcount == 1:
            count += 1
            _record_memory_event(db, job=row, source_message=db.get(Message, row.source_message_id),
                event_type="memory_error", payload={"status": "failed", "error_code":
                    "MEMORY_CAPTURE_DEADLINE" if expired else "MEMORY_CAPTURE_MAX_ATTEMPTS"})
    db.commit()
    db.expire_all()
    return count


@_retry_short_write
def claim_memory_capture_job(
    db: Session, *, worker_id: str, job_id: str | None = None,
    lease_seconds: int = LEASE_SECONDS, now=None,
) -> MemoryCaptureJob | None:
    """以数据库 UTC 和状态/版本 CAS 领取，三次 claim 包含过期接管。"""

    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    db.rollback()
    reap_memory_capture_jobs(db, job_id=job_id, limit=1 if job_id else MAX_CLAIM_BATCH)
    now = now or database_utc_now(db)
    job = db.exec(_claimable_statement(job_id, now)
        .execution_options(populate_existing=True)).first()
    if job is None:
        db.rollback()
        return None
    version, status = job.row_version, job.status
    deadline = job.deadline_at or job.created_at + timedelta(seconds=DEFAULT_DEADLINE_SECONDS)
    ready = (MemoryCaptureJob.lease_expires_at <= now if status == "running" else or_(
        MemoryCaptureJob.next_attempt_at.is_(None), MemoryCaptureJob.next_attempt_at <= now))
    result = db.exec(update(MemoryCaptureJob).where(
        MemoryCaptureJob.id == job.id, MemoryCaptureJob.row_version == version,
        MemoryCaptureJob.status == status, ready,
        MemoryCaptureJob.attempt_count < MemoryCaptureJob.max_attempts,
        MemoryCaptureJob.attempt_count < 3,
        MemoryCaptureJob.model_dispatch_count < MAX_MODEL_DISPATCHES_PER_JOB,
        or_(MemoryCaptureJob.deadline_at.is_(None), MemoryCaptureJob.deadline_at > now),
    ).values(status="running", lease_owner=worker_id,
        lease_expires_at=min(now + timedelta(seconds=max(1, lease_seconds)), deadline),
        fencing_token=MemoryCaptureJob.fencing_token + 1,
        attempt_count=MemoryCaptureJob.attempt_count + 1, claim_dispatch_count=0,
        next_attempt_at=None, deadline_at=deadline, row_version=MemoryCaptureJob.row_version + 1,
    ).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    db.refresh(job)
    return job


def renew_memory_capture_lease(
    db: Session,
    *,
    job_id: str,
    worker_id: str,
    fencing_token: int,
    lease_seconds: int = LEASE_SECONDS,
) -> None:
    """在实际模型派发前以当前 fence 短事务续租，但不延长总截止时间。"""

    now = database_utc_now(db)
    job = db.get(MemoryCaptureJob, job_id, populate_existing=True)
    if job is None or (job.deadline_at is not None and job.deadline_at <= now):
        db.rollback()
        raise MemoryCaptureFenceLost(f"memory capture lease deadline lost: {job_id}")
    lease_until = min(now + timedelta(seconds=max(1, lease_seconds)),
        job.deadline_at or now + timedelta(seconds=DEFAULT_DEADLINE_SECONDS))
    result = db.exec(
        update(MemoryCaptureJob)
        .where(
            MemoryCaptureJob.id == job_id,
            MemoryCaptureJob.status == "running",
            MemoryCaptureJob.lease_owner == worker_id,
            MemoryCaptureJob.fencing_token == fencing_token,
            MemoryCaptureJob.lease_expires_at > now,
            MemoryCaptureJob.row_version == job.row_version,
            or_(MemoryCaptureJob.deadline_at.is_(None), MemoryCaptureJob.deadline_at > now),
        )
        .values(
            lease_expires_at=lease_until,
            row_version=MemoryCaptureJob.row_version + 1,
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        raise MemoryCaptureFenceLost(f"memory capture lease fence lost: {job_id}")
    db.commit()


@_retry_short_write
def increment_model_dispatch_count(
    db: Session, *, job_id: str, worker_id: str, fencing_token: int,
) -> int:
    """每次真实发送原子预留，每 claim 两次、全任务六次，提交后不留下读事务。"""

    now = database_utc_now(db)
    job = db.get(MemoryCaptureJob, job_id, populate_existing=True)
    if job is None:
        db.rollback()
        raise MemoryCaptureFenceLost("memory capture job missing")
    if job.claim_dispatch_count >= MAX_MODEL_DISPATCHES_PER_CLAIM:
        db.rollback()
        raise MemoryCaptureDispatchLimit("memory capture claim dispatch limit")
    result = db.exec(update(MemoryCaptureJob).where(
        *_write_conditions(db, job, worker_id, fencing_token, job.row_version),
        MemoryCaptureJob.model_dispatch_count < MAX_MODEL_DISPATCHES_PER_JOB,
        MemoryCaptureJob.claim_dispatch_count < MAX_MODEL_DISPATCHES_PER_CLAIM,
        MemoryCaptureJob.deadline_at > now,
    ).values(model_dispatch_count=MemoryCaptureJob.model_dispatch_count + 1,
        claim_dispatch_count=MemoryCaptureJob.claim_dispatch_count + 1,
        row_version=MemoryCaptureJob.row_version + 1,
    ).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise MemoryCaptureFenceLost("memory capture dispatch fence lost")
    count = job.model_dispatch_count + 1
    db.commit()
    return count


def _scope_for_job(db: Session, job: MemoryCaptureJob) -> tuple[MemoryCaptureScope | None, MemoryCaptureScope | None]:
    """读取用户 scope 与任务绑定的 Agent scope，供结算前代数屏障校验。"""

    user_scope = db.exec(
        select(MemoryCaptureScope)
        .where(
            MemoryCaptureScope.tenant_id == job.tenant_id,
            MemoryCaptureScope.user_id == job.user_id,
            MemoryCaptureScope.scope_kind == "user",
            MemoryCaptureScope.agent_scope_key == USER_SCOPE_KEY,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    agent_scope = db.exec(
        select(MemoryCaptureScope)
        .where(MemoryCaptureScope.id == job.scope_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if agent_scope is None and job.agent_id:
        agent_scope = db.exec(
            select(MemoryCaptureScope)
            .where(
                MemoryCaptureScope.tenant_id == job.tenant_id,
                MemoryCaptureScope.user_id == job.user_id,
                MemoryCaptureScope.scope_kind == AGENT_SCOPE_KIND,
                MemoryCaptureScope.agent_scope_key == job.agent_scope_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
    return user_scope, agent_scope


def _normalize_key(value: object) -> str:
    """沿统一持久规范键适配原worker接口，不改变结算键语义。"""

    return normalize_memory_key_identity(value)


def _validated_delta(delta: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """验证整批模型 delta；任何一个坏项都拒绝整批写入。"""

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in delta:
        if not isinstance(raw, dict):
            raise ValueError("memory delta item must be an object")
        operation = str(raw.get("operation") or "upsert").strip().lower()
        kind = str(raw.get("kind") or "").strip().lower()
        key = _normalize_key(raw.get("key"))
        if not isinstance(raw.get("key"), str) or len(key) > 128:
            raise ValueError("memory key must be 1..128 characters")
        if operation not in {"upsert", "delete"} or kind not in {"profile", "preference", "fact"} or not key:
            raise ValueError("invalid memory delta identity")
        identity = (kind, key)
        if identity in seen:
            raise ValueError("duplicate memory delta identity")
        seen.add(identity)
        item: dict[str, Any] = {"operation": operation, "kind": kind, "key": key}
        if operation == "upsert":
            content = str(raw.get("content") or "").strip()
            if not content or len(content) > 1200:
                raise ValueError("memory content must be 1..1200 characters")
            try:
                importance = float(raw.get("importance", 0.5))
            except (TypeError, ValueError) as exc:
                raise ValueError("memory importance must be numeric") from exc
            if not 0 <= importance <= 1:
                raise ValueError("memory importance must be between 0 and 1")
            item.update({"content": content, "importance": importance, "reason": raw.get("reason")})
        normalized.append(item)
    return normalized


def _job_fence_matches(job: MemoryCaptureJob, worker_id: str, fencing_token: int, now) -> bool:
    """检查结算者仍持有当前任务 owner/fence。"""

    return (
        job.status == "running"
        and job.lease_owner == worker_id
        and job.fencing_token == fencing_token
        and job.lease_expires_at is not None
        and job.lease_expires_at > now
        and (job.deadline_at is None or job.deadline_at > now)
    )


def _manifest_conversation(db: Session, job: MemoryCaptureJob) -> list[dict[str, str]]:
    """按持久 manifest 精确加载模型上下文，禁止回退到会话最新消息。"""

    manifest_rows = (job.source_manifest_json or {}).get("messages")
    if not isinstance(manifest_rows, list) or not manifest_rows:
        raise MemoryCaptureSourceInvalid("memory capture manifest has no message identities")
    conversation: list[dict[str, str]] = []
    for item in manifest_rows:
        if not isinstance(item, dict):
            raise MemoryCaptureSourceInvalid("memory capture manifest item is invalid")
        message_id = str(item.get("id") or "").strip()
        expected_role = str(item.get("role") or "").strip()
        expected_hash = str(item.get("hash") or "").strip()
        message = db.get(Message, message_id)
        if (
            message is None
            or message.tenant_id != job.tenant_id
            or message.session_id != db.get(Message, job.source_message_id).session_id
            or message.role != expected_role
            or not expected_hash
            or _message_hash(message) != expected_hash
            or message.role not in {"user", "assistant"}
        ):
            raise MemoryCaptureSourceInvalid("memory capture manifest message changed")
        conversation.append({"role": message.role, "content": message.content})
    if not any(
        isinstance(item, dict) and item.get("id") == job.source_message_id
        for item in manifest_rows
    ):
        raise MemoryCaptureSourceInvalid("memory capture manifest source turn is missing")
    if not any(
        isinstance(item, dict) and item.get("id") == job.source_assistant_message_id
        for item in manifest_rows
    ):
        raise MemoryCaptureSourceInvalid("memory capture manifest assistant cutoff is missing")
    return conversation


def _discard_job(job: MemoryCaptureJob, *, reason: str, count: int = 0) -> None:
    """在同一事务中把已失效任务收敛为 discarded。"""

    job.status = "discarded"
    job.finished_at = utc_now()
    job.terminal_reason = reason
    job.discarded_count += max(0, count)
    job.lease_owner = None
    job.lease_expires_at = None
    job.row_version += 1


def _current_row(db: Session, model, identity):
    """使用当前锁定读取获取资源，避免 MySQL 快照和 ORM 缓存批准失效来源。"""

    return db.exec(select(model).where(model.id == identity).with_for_update()
        .execution_options(populate_existing=True)).first()


def _current_contract_matches(db: Session, job: MemoryCaptureJob) -> bool:
    """验证 Agent、模型和冻结代数未发生撤权/变更/ABA。"""

    user = _current_row(db, User, job.user_id)
    if user is None or user.tenant_id != job.tenant_id or user.membership_status != "active":
        return False
    agent = _current_row(db, AgentProfile, job.agent_id) if job.agent_id else None
    source = _current_row(db, Message, job.source_message_id)
    if source is None or source.tenant_id != job.tenant_id:
        return False
    session = _current_row(db, ChatSession, source.session_id)
    if (
        session is None
        or session.tenant_id != job.tenant_id
        or session.user_id != job.user_id
        or session.agent_id != job.agent_id
    ):
        return False
    if job.agent_id and (
        agent is None
        or agent.tenant_id != job.tenant_id
        or agent.status != "active"
        or int(getattr(agent, "memory_capture_generation", 1) or 1) != job.agent_capture_generation
        or not can_use_agent_in_chat(db, agent, user)
    ):
        return False
    model = _current_row(db, ModelConfig, job.model_config_id)
    if (
        model is None
        or model.tenant_id != job.tenant_id
        or not model.enabled
        or int(getattr(model, "memory_capture_generation", 1) or 1) != job.model_capture_generation
        or memory_model_contract_hash(model) != job.model_contract_hash
    ):
        return False
    user_scope, agent_scope = _scope_for_job(db, job)
    if user_scope is None or agent_scope is None:
        return False
    if (
        user_scope.source_sequence < job.source_sequence
        or user_scope.clear_generation != job.user_generation
        or user_scope.permission_generation != job.user_permission_generation
        or agent_scope.id != job.scope_id
        or agent_scope.agent_scope_key != job.agent_scope_key
        or agent_scope.clear_generation != job.agent_generation
        or agent_scope.permission_generation != job.agent_permission_generation
    ):
        return False
    return True


def _key_state(
    db: Session,
    *,
    job: MemoryCaptureJob,
    kind: str,
    key: str,
) -> MemoryKeyState | None:
    """锁定单个结构化记忆键的最新结算事实。"""

    return db.exec(
        select(MemoryKeyState)
        .where(
            MemoryKeyState.tenant_id == job.tenant_id,
            MemoryKeyState.user_id == job.user_id,
            MemoryKeyState.agent_scope_key == job.agent_scope_key,
            MemoryKeyState.kind == kind,
            MemoryKeyState.normalized_key == key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _is_unknown_memory_placeholder(content: str) -> bool:
    """识别纯未知值及字段标签未知，防止它覆写已有事实正文。"""

    normalized = unicodedata.normalize("NFKC", content).casefold().strip(
        _MEMORY_PLACEHOLDER_PUNCTUATION
    )
    if normalized in _UNKNOWN_MEMORY_PLACEHOLDERS:
        return True
    for separator in (":", "="):
        if separator not in normalized:
            continue
        label, value = normalized.rsplit(separator, 1)
        placeholder = value.strip(_MEMORY_PLACEHOLDER_PUNCTUATION)
        if label.strip(_MEMORY_PLACEHOLDER_PUNCTUATION) and placeholder in _UNKNOWN_MEMORY_PLACEHOLDERS:
            return True
    return False


def _advance_memory_key_state(job: MemoryCaptureJob, state: MemoryKeyState) -> None:
    """推进键的来源序号及清空/授权代数，不改变正文或删除状态。"""

    state.last_source_sequence = job.source_sequence
    state.last_job_id = job.id
    state.clear_generation = job.user_generation
    state.permission_generation = job.user_permission_generation
    state.user_generation = job.user_generation
    state.agent_generation = job.agent_generation
    state.user_permission_generation = job.user_permission_generation
    state.agent_permission_generation = job.agent_permission_generation
    state.row_version += 1
    state.updated_at = utc_now()


def _record_memory_event(
    db: Session,
    *,
    job: MemoryCaptureJob,
    source_message: Message | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """把派生记忆结果事件放入与正文和任务终态相同的结算事务。"""

    if source_message is None:
        return
    EventLog(db).record(
        job.tenant_id,
        source_message.session_id,
        event_type,
        {
            "feature": "memory",
            "job_name": "memory.capture_turn",
            "job_id": job.id,
            "source_message_id": job.source_message_id,
            **payload,
        },
    )


@_retry_short_write
def settle_memory_capture_job(
    db: Session,
    *,
    job_id: str,
    worker_id: str,
    fencing_token: int,
    delta: list[dict[str, Any]],
    discard_reason: str | None = None,
    extraction_contract: MemoryExtractionContract | None = None,
) -> dict[str, int | str]:
    """确认实际快照与冻结版本后，在代数屏障、键CAS和任务终态同一事务结算。"""

    job = _lock_job_for_write(db, job_id, worker_id, fencing_token)
    version = job.row_version
    if not discard_reason:
        try:
            contract = extraction_contract or MemoryExtractionContract.read(memory_service.PROMPT_PATH)
            if not contract.matches(job.extractor_schema_version, job.prompt_hash):
                discard_reason = "capture_contract_changed"
        except MemoryExtractionVersionInvalid:
            discard_reason = "capture_contract_changed"
    try:
        normalized = _validated_delta(delta)
    except ValueError as exc:
        job.status = "failed"
        job.finished_at = utc_now()
        job.terminal_reason = "invalid_delta"
        job.error_code = "MEMORY_CAPTURE_DELTA_INVALID"
        job.lease_owner = None
        job.lease_expires_at = None
        job.row_version += 1
        _record_memory_event(
            db,
            job=job,
            source_message=db.get(Message, job.source_message_id),
            event_type="memory_error",
            payload={"status": "failed", "error_code": job.error_code},
        )
        _commit_fenced_job(db, job, worker_id, fencing_token, version)
        raise ValueError(str(exc)) from exc

    if discard_reason or not _current_contract_matches(db, job):
        source_message = db.get(Message, job.source_message_id)
        _discard_job(job, reason=discard_reason or "capture_contract_changed", count=len(normalized))
        _record_memory_event(
            db,
            job=job,
            source_message=source_message,
            event_type="memory_error",
            payload={"status": "discarded", "error_code": "MEMORY_CAPTURE_CONTRACT_CHANGED"},
        )
        _commit_fenced_job(db, job, worker_id, fencing_token, version)
        return {"status": job.status, "applied": 0, "discarded": job.discarded_count}

    source_message = db.get(Message, job.source_message_id)
    assistant_message = db.get(Message, job.source_assistant_message_id)
    if (
        source_message is None
        or assistant_message is None
        or source_message.role != "user"
        or assistant_message.role != "assistant"
        or not str(job.source_manifest_json.get("source_message_hash") or "")
        or _message_hash(source_message)
        != str(job.source_manifest_json.get("source_message_hash") or "")
        or _message_hash(assistant_message)
        != str(job.source_manifest_json.get("source_assistant_message_hash") or "")
    ):
        _discard_job(job, reason="source_changed", count=len(normalized))
        _record_memory_event(
            db,
            job=job,
            source_message=source_message,
            event_type="memory_error",
            payload={"status": "discarded", "error_code": "MEMORY_CAPTURE_SOURCE_CHANGED"},
        )
        _commit_fenced_job(db, job, worker_id, fencing_token, version)
        return {"status": job.status, "applied": 0, "discarded": job.discarded_count}

    user = db.get(User, job.user_id)
    session_id = source_message.session_id
    applied = 0
    discarded = 0
    service = MemoryService(db)
    baseline = service._list_user_memories(
        job.tenant_id, job.user_id, limit=None, normalize=False, agent_id=job.agent_id
    )
    session_agents = service._session_agent_map(job.tenant_id, baseline)
    for item in sorted(normalized, key=lambda value: (value["kind"], value["key"])):
        state = _key_state(db, job=job, kind=item["kind"], key=item["key"])
        if state is not None and state.last_source_sequence >= job.source_sequence:
            discarded += 1
            continue
        matching = [row for row in baseline
            if row.kind == item["kind"] and _normalize_key((row.metadata_json or {}).get("key")) == item["key"]
            and (memory_effective_agent_id(row, session_agents) or USER_SCOPE_KEY)
            == job.agent_scope_key]
        if state is None:
            state = MemoryKeyState(
                id=new_id("memkey"),
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                agent_scope_key=job.agent_scope_key,
                kind=item["kind"],
                normalized_key=item["key"],
            )
            db.add(state)
            db.flush()
        if state.record_id is None and not state.tombstoned and matching:
            state.record_id = matching[0].id
        if item["operation"] == "upsert" and _is_unknown_memory_placeholder(item["content"]):
            # 未知占位不是事实更新：保留正文与墓碑，只推进键序号以屏障更旧来源。
            _advance_memory_key_state(job, state)
            db.add(state)
            discarded += 1
            continue
        # 沿 SD-2 有效归属承接序号零旧行，清理同 scope/key 的重复基线。
        for duplicate in matching:
            if item["operation"] == "delete" or duplicate.id != state.record_id:
                db.delete(duplicate)
        if item["operation"] == "delete":
            if state.record_id:
                record = db.get(MemoryRecord, state.record_id)
                if record is not None and record not in db.deleted:
                    db.delete(record)
            state.record_id = None
            state.tombstoned = True
        else:
            record = db.get(MemoryRecord, state.record_id) if state.record_id else None
            if record is None:
                record = MemoryRecord(
                    id=new_id("mem"),
                    tenant_id=job.tenant_id,
                    agent_id=job.agent_id,
                    agent_scope_key=job.agent_scope_key,
                    user_id=job.user_id,
                    username=user.username if user else job.user_id,
                    session_id=session_id,
                    kind=item["kind"],
                    content=item["content"],
                    importance=item["importance"],
                )
                db.add(record)
                db.flush()
            else:
                record.agent_id = job.agent_id
                record.agent_scope_key = job.agent_scope_key
                record.session_id = session_id
                record.content = item["content"]
                record.importance = item["importance"]
                record.updated_at = utc_now()
            record.metadata_json = {
                **(record.metadata_json or {}),
                "source": "model_memory_extractor",
                "key": item["key"],
                "reason": item.get("reason"),
                "agent_id": job.agent_id,
            }
            record.source_sequence = job.source_sequence
            record.capture_job_id = job.id
            record.source_hash = job.source_hash
            record.user_generation = job.user_generation
            record.agent_generation = job.agent_generation
            record.user_permission_generation = job.user_permission_generation
            record.agent_permission_generation = job.agent_permission_generation
            state.record_id = record.id
            state.tombstoned = False
        _advance_memory_key_state(job, state)
        db.add(state)
        applied += 1

    job.status = "completed"
    job.finished_at = utc_now()
    job.terminal_reason = "applied"
    job.applied_count += applied
    job.discarded_count += discarded
    job.lease_owner = None
    job.lease_expires_at = None
    job.row_version += 1
    if applied:
        _record_memory_event(
            db,
            job=job,
            source_message=source_message,
            event_type="memory_saved",
            payload={
                "status": "completed",
                "applied_count": applied,
                "discarded_count": discarded,
            },
        )
    elif discarded:
        _record_memory_event(
            db,
            job=job,
            source_message=source_message,
            event_type="memory_error",
            payload={
                "status": "completed",
                "error_code": "MEMORY_CAPTURE_SUPERSEDED",
                "discarded_count": discarded,
            },
        )
    _commit_fenced_job(db, job, worker_id, fencing_token, version)
    return {"status": job.status, "applied": applied, "discarded": discarded}


@_retry_short_write
def fail_memory_capture_job(
    db: Session,
    *,
    job_id: str,
    worker_id: str,
    fencing_token: int,
    error_code: str,
) -> str:
    """按当前 fence 将 worker 异常收敛为有界 retry 或 failed。"""

    job = _lock_job_for_write(db, job_id, worker_id, fencing_token)
    version = job.row_version
    now = database_utc_now(db)
    if (job.attempt_count >= min(3, job.max_attempts)
        or job.model_dispatch_count >= MAX_MODEL_DISPATCHES_PER_JOB
        or now >= (job.deadline_at or now)):
        job.status = "failed"
        job.finished_at = utc_now()
        job.terminal_reason = "worker_failed"
    else:
        retry_index = min(job.attempt_count - 1, len(RETRY_DELAYS_SECONDS) - 1)
        job.status = "retry"
        job.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS_SECONDS[max(0, retry_index)])
        job.terminal_reason = "retryable_failure"
    job.error_code = error_code[:64]
    job.lease_owner = None
    job.lease_expires_at = None
    job.row_version += 1
    if job.status == "failed":
        _record_memory_event(
            db,
            job=job,
            source_message=db.get(Message, job.source_message_id),
            event_type="memory_error",
            payload={"status": "failed", "error_code": job.error_code},
        )
    _commit_fenced_job(db, job, worker_id, fencing_token, version)
    return job.status


def _prepare_disclosure(
    db: Session, job_id: str, worker_id: str, fence: int, *,
    extraction_contract: MemoryExtractionContract, reserve_dispatch: bool = False
):
    """每次实际发送承接同一提示词/schema快照，并校验失效向量后提交短事务。"""

    job = _lock_job_for_write(db, job_id, worker_id, fence)
    if not extraction_contract.matches(job.extractor_schema_version, job.prompt_hash):
        db.rollback()
        raise MemoryExtractionVersionInvalid("capture_contract_changed")
    if not _current_contract_matches(db, job):
        db.rollback()
        raise MemoryCaptureSourceInvalid("capture_contract_changed")
    source = _current_row(db, Message, job.source_message_id)
    assistant = _current_row(db, Message, job.source_assistant_message_id)
    manifest = job.source_manifest_json or {}
    if (source is None or assistant is None
        or _message_hash(source) != manifest.get("source_message_hash")
        or _message_hash(assistant) != manifest.get("source_assistant_message_hash")):
        db.rollback()
        raise MemoryCaptureSourceInvalid("source_changed")
    conversation = _manifest_conversation(db, job)
    model = ModelConfig.model_validate(db.get(ModelConfig, job.model_config_id).model_dump())
    session = ChatSession.model_validate(db.get(ChatSession, source.session_id).model_dump())
    request = ChatTurnRequest(tenant_id=job.tenant_id, user_id=job.user_id, agent_id=job.agent_id,
        session_id=session.id, message=source.content)
    remaining = (job.deadline_at - database_utc_now(db)).total_seconds()
    reserved = (
        increment_model_dispatch_count(db, job_id=job_id, worker_id=worker_id, fencing_token=fence)
        if reserve_dispatch else None
    )
    db.commit()
    return request, session, model, conversation, min(PROVIDER_TIMEOUT_SECONDS, remaining), reserved


def _heartbeat(engine, job_id, worker_id, fence, stop: Event) -> None:
    """独立短 Session 每二十秒续租；失权即停止，总截止不被延长。"""

    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with Session(engine) as db:
                renew_memory_capture_lease(db, job_id=job_id, worker_id=worker_id,
                    fencing_token=fence)
        except Exception:
            return


def process_memory_capture_job(
    db: Session, *, job_id: str, worker_id: str,
) -> dict[str, int | str] | None:
    """一次读取可信提取版本，逐次复核发送并在唯一T3核对执行与持久合同。"""

    job = db.get(MemoryCaptureJob, job_id, populate_existing=True)
    if job is None:
        db.rollback()
        return None
    if job.status != "running" or job.lease_owner != worker_id:
        job = claim_memory_capture_job(db, worker_id=worker_id, job_id=job_id)
    if job is None:
        return None
    fence = job.fencing_token
    engine = db.get_bind()
    dispatch_failures: list[Exception] = []
    stop = Event()
    heartbeat = None
    try:
        extraction_contract = MemoryExtractionContract.read(memory_service.PROMPT_PATH)
        request, session, model, conversation, timeout, _ = _prepare_disclosure(
            db, job_id, worker_id, fence, extraction_contract=extraction_contract)
        db.rollback()
        def counter() -> int:
            """紧邻真实发送复核当前授权、来源、模型和原提取快照，提交有界预留。"""

            try:
                with Session(engine) as dispatch_db:
                    *_, reserved = _prepare_disclosure(
                        dispatch_db, job_id, worker_id, fence,
                        extraction_contract=extraction_contract, reserve_dispatch=True
                    )
                    return int(reserved)
            except Exception as exc:
                dispatch_failures.append(exc)
                raise
        heartbeat = Thread(target=_heartbeat, args=(engine, job_id, worker_id, fence, stop),
            name="memory-capture-heartbeat", daemon=True)
        heartbeat.start()
        updates = MemoryService(db).extract_turn_delta(request, session, StepAgentResult(), None,
            model, conversation, dispatch_observer=counter, timeout_seconds=timeout,
            extraction_contract=extraction_contract)
        return settle_memory_capture_job(db, job_id=job_id, worker_id=worker_id,
            fencing_token=fence, delta=updates, extraction_contract=extraction_contract)
    except Exception as exc:
        error = dispatch_failures[-1] if dispatch_failures else exc
        if isinstance(error, ValueError):
            db.rollback()
            current = db.get(MemoryCaptureJob, job_id, populate_existing=True)
            invalid_settled = (current is not None and current.status == "failed"
                and current.error_code == "MEMORY_CAPTURE_DELTA_INVALID")
            db.rollback()
            if invalid_settled:
                # T3 已原子拒绝整批；不能把已终态任务再次转入普通错误结算。
                return {"status": "failed", "applied": 0, "discarded": 0}
        if isinstance(error, (
            MemoryCaptureSourceInvalid, MemoryAgentUnavailable, MemoryExtractionVersionInvalid,
        )):
            return settle_memory_capture_job(db, job_id=job_id, worker_id=worker_id,
                fencing_token=fence, delta=[], discard_reason=(
                    str(error) if str(error) in {"capture_contract_changed", "source_changed"}
                    else "source_changed"))
        if isinstance(error, MemoryCaptureFenceLost) and not isinstance(error, MemoryCaptureDispatchLimit):
            raise error
        fail_memory_capture_job(db, job_id=job_id, worker_id=worker_id, fencing_token=fence,
            error_code="MEMORY_CAPTURE_DISPATCH_LIMIT" if isinstance(error, MemoryCaptureDispatchLimit)
            else "MEMORY_CAPTURE_WORKER_ERROR")
        return None
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=2)


def run_memory_capture_worker_once(
    db: Session,
    *,
    worker_id: str,
    limit: int = MAX_CLAIM_BATCH,
) -> int:
    """按固定上限扫描并处理到期任务，单次运行不把队列变成无界线程。"""

    bounded = max(0, min(limit, MAX_CLAIM_BATCH))
    processed = reap_memory_capture_jobs(db, limit=bounded)
    for _ in range(bounded - processed):
        job = claim_memory_capture_job(db, worker_id=worker_id)
        if job is None:
            break
        process_memory_capture_job(db, job_id=job.id, worker_id=worker_id)
        processed += 1
    return processed


def scan_memory_capture_jobs_once(
    *,
    worker_id: str,
    limit: int = MAX_CLAIM_BATCH,
    database_engine: Any | None = None,
) -> int:
    """用每任务独立 Session 扫描有限批次，供新进程启动和周期维护恢复。"""

    if database_engine is None:
        from app.db import engine as database_engine

    bounded = max(0, min(limit, MAX_CLAIM_BATCH))
    with Session(database_engine) as maintenance_db:
        processed = reap_memory_capture_jobs(maintenance_db, limit=bounded)
    for _ in range(bounded - processed):
        with Session(database_engine) as claim_db:
            job = claim_memory_capture_job(claim_db, worker_id=worker_id)
            job_id = job.id if job is not None else None
        if job_id is None:
            break
        with Session(database_engine) as process_db:
            try:
                process_memory_capture_job(
                    process_db,
                    job_id=job_id,
                    worker_id=worker_id,
                )
            except MemoryCaptureFenceLost:
                LOGGER.info("memory capture lease changed during recovery", extra={"job_id": job_id})
            except Exception:  # noqa: BLE001 - one bad job cannot stop the scanner.
                LOGGER.exception(
                    "memory capture recovery failed",
                    extra={"job_id": job_id},
                )
        processed += 1
    return processed


def _run_woken_memory_capture(job_id: str) -> None:
    """在独立短 worker 中处理唤醒任务，失败由持久 retry/scanner 接管。"""

    from app.db import engine

    worker_id = f"memory-capture:{current_thread().name}"
    with Session(engine) as db:
        try:
            process_memory_capture_job(db, job_id=job_id, worker_id=worker_id)
        except MemoryCaptureFenceLost:
            LOGGER.info("memory capture wakeup lost lease", extra={"job_id": job_id})
        except Exception:  # noqa: BLE001 - scanner remains the durable fallback.
            LOGGER.exception("memory capture wakeup failed", extra={"job_id": job_id})


def _run_memory_capture_scanner() -> None:
    """启动后及周期性接管丢失通知、过期租约和 retry 任务。"""

    worker_id = f"memory-capture-scanner:{os.getpid()}"
    while not _worker_stop_event.is_set():
        try:
            scan_memory_capture_jobs_once(worker_id=worker_id)
        except Exception:  # noqa: BLE001 - a maintenance round must not end recovery.
            LOGGER.exception("memory capture scanner failed")
        _worker_stop_event.wait(WORKER_POLL_SECONDS)


def enable_memory_capture_worker() -> None:
    """在 SD-6B 写入屏障就绪后启用有界新 worker。"""

    global _worker_executor, _worker_scan_thread
    with _worker_lock:
        if _worker_executor is not None:
            return
        _worker_stop_event.clear()
        _worker_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-capture")
        configure_memory_capture_wakeup(_submit_woken_memory_capture)
        _worker_scan_thread = Thread(
            target=_run_memory_capture_scanner,
            name="gongge-xuban-memory-capture-scanner",
            daemon=True,
        )
        _worker_scan_thread.start()


def _submit_woken_memory_capture(job_id: str) -> None:
    """提交一个持久 job 唤醒，未启用时仅等待 scanner，不走旧内存写入器。"""

    with _worker_lock:
        executor = _worker_executor
    if executor is not None:
        executor.submit(_run_woken_memory_capture, job_id)


def disable_memory_capture_worker() -> None:
    """关闭唤醒投递器，已提交任务仍由下次进程 scanner 按租约恢复。"""

    global _worker_executor, _worker_scan_thread
    _worker_stop_event.set()
    with _worker_lock:
        executor = _worker_executor
        scan_thread = _worker_scan_thread
        _worker_executor = None
        _worker_scan_thread = None
        configure_memory_capture_wakeup(None)
    if scan_thread is not None and scan_thread is not current_thread():
        scan_thread.join(timeout=5)
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=False)
