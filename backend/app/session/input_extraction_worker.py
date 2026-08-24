"""
@Time       : 2026/08/14 09:48
@Author     : zhanglp8181
@File       : input_extraction_worker.py
@CallChain  : FastAPI lifespan/独立worker → ExtractionAttempt → 隔离parser → 原子发布Extraction
@Description: 恢复上传请求崩溃遗留的解析租约，并以追加Attempt完成受管附件异步提取。
"""

from __future__ import annotations

import logging
import threading

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import InputResourceExtractionAttempt, ManagedInputResource, utc_now
from app.session.input_extraction import InputExtractionError, InputExtractionService
from app.session.input_parser_process import run_attachment_parser_fd_isolated
from app.session.managed_resources import ManagedInputResourceService
from app.session.provider_input_dispatch import ProviderInputDispatchGateway


LOGGER = logging.getLogger(__name__)
WORKER_POLL_SECONDS = 2.0
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def _attempt_format(attempt: InputResourceExtractionAttempt) -> str:
    """从服务端冻结parser名称恢复白名单格式，不接受数据库自由命令。"""

    name = attempt.parser_name
    if name == "builtin-csv":
        return "csv"
    if name.startswith("builtin-"):
        value = name.removeprefix("builtin-")
        if value in {"text", "pdf", "docx", "pptx", "xlsx", "image"}:
            return value
    raise InputExtractionError("ATTACHMENT_FORMAT_UNSUPPORTED", "解析格式不可恢复。")


def run_extraction_maintenance_once(*, worker_id: str = "attachment-worker") -> int:
    """收敛过期租约并处理一个pending Attempt，供测试、后台线程和独立CLI复用。"""

    settings = get_settings()
    progressed = 0
    with Session(engine, expire_on_commit=False) as db:
        progressed += ProviderInputDispatchGateway(db).expire_dispatching_to_unknown()
        if progressed:
            db.commit()
        service = InputExtractionService(db)
        now = utc_now()
        expired = db.exec(
            select(InputResourceExtractionAttempt).where(
                InputResourceExtractionAttempt.status == "claimed",
                InputResourceExtractionAttempt.lease_expires_at.is_not(None),
                InputResourceExtractionAttempt.lease_expires_at <= now,
            )
        ).all()
        for attempt in expired:
            attempt.status = "failed"
            attempt.error_code = "ATTACHMENT_EXTRACTION_WORKER_LOST"
            attempt.error_detail_json = {"detail": "解析worker租约过期，已安排新Attempt。"}
            attempt.finished_at = now
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            db.add(attempt)
            resource = db.get(ManagedInputResource, attempt.resource_id)
            if resource is not None and resource.tenant_id == attempt.tenant_id:
                service.ensure_attempt(resource, file_format=_attempt_format(attempt))
            progressed += 1
        if expired:
            db.commit()

        attempt = db.exec(
            select(InputResourceExtractionAttempt)
            .where(InputResourceExtractionAttempt.status == "pending")
            .order_by(InputResourceExtractionAttempt.created_at, InputResourceExtractionAttempt.id)
        ).first()
        if attempt is None:
            return progressed
        resource = db.get(ManagedInputResource, attempt.resource_id)
        if (
            resource is None
            or resource.tenant_id != attempt.tenant_id
            or resource.access_status != "active"
        ):
            attempt.status = "cancelled"
            attempt.error_code = "ATTACHMENT_COUNTERMANDED"
            attempt.finished_at = utc_now()
            db.add(attempt)
            db.commit()
            return progressed + 1
        file_format = _attempt_format(attempt)
        service.claim(
            attempt,
            worker_id=worker_id,
            lease_seconds=settings.attachment_parser_timeout_seconds + 5,
        )
        db.commit()
        try:
            resource_service = ManagedInputResourceService(db)
            with resource_service.parser_input_descriptor(resource) as input_fd:
                elements = run_attachment_parser_fd_isolated(
                    input_fd,
                    file_format=file_format,
                    timeout_seconds=settings.attachment_parser_timeout_seconds,
                    memory_mb=settings.attachment_parser_memory_mb,
                )
            service.publish(
                attempt,
                resource,
                elements,
                file_format=file_format,
                worker_id=worker_id,
                fencing_token=attempt.fencing_token,
            )
        except Exception as exc:  # noqa: BLE001 - worker必须把任意parser故障收敛为持久终态。
            error = (
                exc
                if isinstance(exc, InputExtractionError)
                else InputExtractionError("ATTACHMENT_PARSER_FAILED", "附件解析失败。")
            )
            service.fail(
                attempt,
                worker_id=worker_id,
                fencing_token=attempt.fencing_token,
                error=error,
            )
            resource.ingestion_status = "failed"
            db.add(resource)
        db.commit()
        return progressed + 1


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_POLL_SECONDS) -> None:
    """持续执行解析维护；单个坏作业只记录日志，不终止后续恢复。"""

    while not _stop_event.is_set():
        try:
            run_extraction_maintenance_once()
        except Exception:  # noqa: BLE001 - 后台维护必须跨坏作业继续运行。
            LOGGER.exception("attachment extraction maintenance failed")
        if once:
            return
        _stop_event.wait(max(0.5, poll_seconds))


def start_background_worker() -> None:
    """在启用附件分析时幂等启动恢复线程，生产也可替换为独立worker进程。"""

    global _worker_thread
    if not get_settings().attachment_analysis_enabled:
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=run_worker,
        name="gongge-xuban-attachment-extraction-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_background_worker() -> None:
    """停止Web进程内解析维护线程并清理线程句柄。"""

    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
    _worker_thread = None
