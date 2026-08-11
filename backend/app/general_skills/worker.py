"""
@Time       : 2026/08/12 22:35
@Author     : zhanglp8181
@File       : worker.py
@CallChain  : FastAPI lifespan/worker CLI → ImportJob recovery/expiry → object staging cleanup
@Description: 周期恢复安全检查点并清理到期 Skill 导入作业，避免依赖 Web 请求收尾。
"""

from __future__ import annotations

import argparse
import logging
import threading
from datetime import timedelta
from uuid import uuid4

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import GeneralSkillRevision, utc_now
from app.general_skills.import_service import GeneralSkillImportService, ImportQuotaPolicy
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.remote_source import RemoteFetcher, SecureHttpsFetcher


LOGGER = logging.getLogger(__name__)
WORKER_POLL_SECONDS = 60.0
RECOVERY_STALE_SECONDS = 300
ORPHAN_GRACE_SECONDS = 3600
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
WORKER_ID = f"gsworker_{uuid4().hex[:16]}"


def get_remote_fetcher() -> RemoteFetcher:
    """创建后台作业使用的安全远程抓取器，测试可替换供应商边界。"""

    return SecureHttpsFetcher()


def run_maintenance_once() -> int:
    """在独立数据库会话中恢复陈旧检查点并回收到期作业，返回处理数量。"""

    settings = get_settings()
    if not settings.general_skill_import_v2_enabled:
        return 0
    now = utc_now()
    with Session(engine) as db:
        service = GeneralSkillImportService(
            db,
            FileSystemSkillObjectStore(settings.general_skill_object_store_path),
            https_allowed_hosts=settings.general_skill_https_allowed_host_set,
            quota_policy=ImportQuotaPolicy(
                tenant_active_jobs=settings.general_skill_import_tenant_active_limit,
                user_active_jobs=settings.general_skill_import_user_active_limit,
                tenant_staged_bytes=settings.general_skill_import_tenant_staged_bytes,
                user_staged_bytes=settings.general_skill_import_user_staged_bytes,
            ),
        )
        processed = service.process_pending_jobs(
            worker_id=WORKER_ID,
            fetcher=get_remote_fetcher(),
            now=now,
            lease_seconds=settings.general_skill_import_worker_lease_seconds,
        )
        recovered = service.recover_stale_jobs(
            stale_before=now - timedelta(seconds=RECOVERY_STALE_SECONDS)
        )
        expired = service.expire_jobs(now=now)
        referenced_checksums = {
            str(resource["content_checksum"])
            for revision in db.exec(select(GeneralSkillRevision)).all()
            for resource in revision.resource_manifest_json
            if isinstance(resource, dict) and resource.get("content_checksum")
        }
        removed = service.object_store.sweep_unreferenced_objects(
            referenced_checksums,
            older_than=now - timedelta(seconds=ORPHAN_GRACE_SECONDS),
        )
        return len(processed) + len(recovered) + len(expired) + len(removed)


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_POLL_SECONDS) -> None:
    """运行可独立部署的维护循环，并将单轮异常收敛后继续下一轮。"""

    while not _stop_event.is_set():
        try:
            run_maintenance_once()
        except Exception:  # noqa: BLE001 - 后台维护不能因单个坏作业永久退出。
            LOGGER.exception("general Skill import maintenance failed")
        if once:
            return
        _stop_event.wait(max(poll_seconds, 1.0))


def start_background_worker(*, poll_seconds: float = WORKER_POLL_SECONDS) -> None:
    """在 Web 进程内幂等启动维护线程；生产也可改用独立 CLI worker。"""

    global _worker_thread
    if not get_settings().general_skill_import_v2_enabled:
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    configured_poll_seconds = get_settings().general_skill_import_worker_poll_seconds
    if poll_seconds == WORKER_POLL_SECONDS:
        poll_seconds = configured_poll_seconds
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=run_worker,
        kwargs={"poll_seconds": poll_seconds},
        name="gongge-xuban-general-skill-import-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_background_worker() -> None:
    """请求维护线程停止并进行有限等待，不阻塞应用无限退出。"""

    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
    _worker_thread = None


def main() -> None:
    """解析独立 worker CLI 参数并启动一次或持续维护。"""

    parser = argparse.ArgumentParser(description="Run the general Skill import maintenance worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=WORKER_POLL_SECONDS)
    args = parser.parse_args()
    run_worker(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
