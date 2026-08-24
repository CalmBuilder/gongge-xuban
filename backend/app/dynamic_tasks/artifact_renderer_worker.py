"""
@Time       : 2026/08/14 12:05
@Author     : zhanglp8181
@File       : artifact_renderer_worker.py
@CallChain  : FastAPI lifespan/独立worker → ArtifactRendererJob → ArtifactService原子发布
@Description: 恢复过期渲染租约并重放已验证ExecutionResult，避免崩溃留下永久交付卡点。
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import or_
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import ArtifactRendererJob, utc_now
from app.dynamic_tasks.artifact_renderer import ArtifactRenderError, ArtifactRendererService


LOGGER = logging.getLogger(__name__)
WORKER_POLL_SECONDS = 2.0
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def run_renderer_maintenance_once(*, worker_id: str = "artifact-renderer-worker") -> int:
    """回收全部过期租约并处理一个到期任务，供线程、独立进程和故障测试复用。"""

    with Session(engine, expire_on_commit=False) as db:
        service = ArtifactRendererService(db)
        progressed = service.requeue_expired()
        if progressed:
            db.commit()
        job = db.exec(
            select(ArtifactRendererJob)
            .where(
                ArtifactRendererJob.status.in_(("pending", "retry_wait")),
                or_(
                    ArtifactRendererJob.retry_at.is_(None),
                    ArtifactRendererJob.retry_at <= utc_now(),
                ),
            )
            .order_by(ArtifactRendererJob.created_at, ArtifactRendererJob.id)
        ).first()
        if job is None:
            return progressed
        try:
            service.claim(job, worker_id=worker_id)
            db.commit()
            service.resume_job(job, worker_id=worker_id)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - worker必须持久化任意渲染故障并继续轮询。
            db.rollback()
            current = db.get(ArtifactRendererJob, job.id)
            if current is not None and current.lease_owner == worker_id:
                try:
                    service.fail_or_retry(
                        current,
                        worker_id=worker_id,
                        fencing_token=current.fencing_token,
                        error_code=(
                            str(exc)
                            if isinstance(exc, ArtifactRenderError)
                            else "ARTIFACT_RENDER_FAILED"
                        ),
                    )
                    db.commit()
                except ArtifactRenderError:
                    db.rollback()
            LOGGER.exception("artifact renderer maintenance failed", extra={"job_id": job.id})
        return progressed + 1


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_POLL_SECONDS) -> None:
    """持续处理Artifact渲染任务，单个坏任务不会终止恢复循环。"""

    while not _stop_event.is_set():
        try:
            run_renderer_maintenance_once()
        except Exception:  # noqa: BLE001 - 后台维护必须跨数据库瞬态故障继续运行。
            LOGGER.exception("artifact renderer worker failed")
        if once:
            return
        _stop_event.wait(max(0.5, poll_seconds))


def start_background_worker() -> None:
    """在附件能力启用时幂等启动Renderer恢复线程。"""

    global _worker_thread
    if not get_settings().attachment_analysis_enabled:
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=run_worker,
        name="gongge-xuban-artifact-renderer-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_background_worker() -> None:
    """停止Renderer恢复线程并释放进程内句柄。"""

    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
    _worker_thread = None
