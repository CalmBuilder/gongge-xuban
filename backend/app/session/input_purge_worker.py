"""
@Time       : 2026/08/14 16:58
@Author     : zhanglp8181
@File       : input_purge_worker.py
@CallChain  : FastAPI lifespan/独立worker → 上传失败/InputResource清理作业 → 受管存储
@Description: 扫描待处理或租约过期的附件清理作业并以lease/fencing幂等恢复。
"""

from __future__ import annotations

import logging
import threading

from sqlmodel import Session, or_, select

from app.config import get_settings
from app.db import engine
from app.db.models import (
    AttachmentUploadCleanupJob,
    InputResourcePurgeJob,
    ManagedInputResource,
    utc_now,
)
from app.session.managed_resources import ManagedInputResourceService


LOGGER = logging.getLogger(__name__)
WORKER_POLL_SECONDS = 2.0
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def run_purge_maintenance_once() -> int:
    """优先恢复失败上传清理，再接管一个会话资源销毁作业并收敛到持久终态。"""

    now = utc_now()
    with Session(engine, expire_on_commit=False) as db:
        upload_job = db.exec(
            select(AttachmentUploadCleanupJob)
            .where(
                or_(
                    AttachmentUploadCleanupJob.status.in_(("pending", "failed")),
                    (
                        AttachmentUploadCleanupJob.status.in_(("claimed", "purging"))
                        & AttachmentUploadCleanupJob.lease_expires_at.is_not(None)
                        & (AttachmentUploadCleanupJob.lease_expires_at <= now)
                    ),
                )
            )
            .order_by(AttachmentUploadCleanupJob.created_at, AttachmentUploadCleanupJob.id)
        ).first()
        if upload_job is not None:
            try:
                ManagedInputResourceService(db).run_upload_failure_cleanup(
                    upload_job.id,
                    worker_id=f"upload-cleanup:{threading.get_ident()}",
                )
            except Exception:  # noqa: BLE001 - 作业内部已持久化失败，循环必须继续。
                LOGGER.exception(
                    "attachment upload cleanup job failed",
                    extra={"cleanup_job_id": upload_job.id},
                )
            return 1
        job = db.exec(
            select(InputResourcePurgeJob)
            .where(
                or_(
                    InputResourcePurgeJob.status.in_(("pending", "failed")),
                    (
                        InputResourcePurgeJob.status.in_(("claimed", "purging"))
                        & InputResourcePurgeJob.lease_expires_at.is_not(None)
                        & (InputResourcePurgeJob.lease_expires_at <= now)
                    ),
                )
            )
            .order_by(InputResourcePurgeJob.created_at, InputResourcePurgeJob.id)
        ).first()
        if job is None:
            return 0
        resource = db.get(ManagedInputResource, job.resource_id)
        if resource is None or resource.tenant_id != job.tenant_id:
            job.status = "dead_letter"
            job.error_code = "ATTACHMENT_PURGE_RESOURCE_MISSING"
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            db.add(job)
            db.commit()
            return 1
        try:
            ManagedInputResourceService(db).purge_session_resource(
                resource,
                session_id=job.session_id,
                actor_user_id=job.requested_by_user_id,
            )
        except Exception:  # noqa: BLE001 - 作业内部已持久化失败，循环必须继续。
            LOGGER.exception("attachment purge job failed", extra={"purge_job_id": job.id})
        return 1


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_POLL_SECONDS) -> None:
    """持续处理附件销毁作业，单作业失败不会终止维护线程。"""

    while not _stop_event.is_set():
        run_purge_maintenance_once()
        if once:
            return
        _stop_event.wait(max(0.5, poll_seconds))


def start_background_worker() -> None:
    """在附件分析开关启用时幂等启动销毁恢复线程。"""

    global _worker_thread
    if not get_settings().attachment_analysis_enabled:
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=run_worker,
        name="gongge-xuban-attachment-purge-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_background_worker() -> None:
    """停止附件销毁恢复线程并释放进程内句柄。"""

    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
    _worker_thread = None
