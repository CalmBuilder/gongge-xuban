"""
@Time       : 2026/07/22 10:34
@Author     : zhanglp8181
@File       : worker.py
@CallChain  : 应用 lifespan/worker CLI → 到期扫描 → 定时任务执行/人工工作项超时处置
@Description: 轮询并执行到期定时任务，同时消费统一 Runtime 的人工工作项超时事实。
"""

from __future__ import annotations

import argparse
import signal
import threading
from time import sleep

from sqlmodel import Session

from app.agents.deletion import reconcile_pending_agent_deletions
from app.db import engine, init_db
from app.db.models import SopWorkItem
from app.db.seed import seed_demo_data
from app.dynamic_tasks.worker import (
    due_dynamic_task_signals,
    process_dynamic_task_signal,
    reconcile_parallel_read_batches,
    start_dynamic_task_signal_async,
)
from app.general_skills.proposals import GeneralSkillProposalService
from app.scheduled_tasks.service import (
    WORKER_SLEEP_SECONDS,
    due_scheduled_tasks,
    execute_scheduled_task,
    reconcile_scheduled_dynamic_runs,
    start_scheduled_task_async,
)
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.work_items import SopWorkItemService


_stopped = False
_background_thread: threading.Thread | None = None


def _handle_stop(_signum: int, _frame: object) -> None:
    """接收进程退出信号并请求后台轮询循环安全停止。"""

    global _stopped
    _stopped = True


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_SLEEP_SECONDS) -> None:
    """循环执行到期计划任务和人工工作项超时处置，每轮使用独立事务。"""

    init_db()
    with Session(engine) as db:
        seed_demo_data(db)
    while not _stopped:
        with Session(engine) as db:
            due = due_scheduled_tasks(db)
            for task in due:
                if once:
                    execute_scheduled_task(db, task)
                else:
                    start_scheduled_task_async(db, task)
            _process_due_dynamic_signals(db, once=once)
            reconcile_parallel_read_batches(db)
            reconcile_scheduled_dynamic_runs(db)
            reconcile_pending_agent_deletions(db)
            expired_work_items = SopWorkItemService(db).expire_due()
            for work_item in expired_work_items:
                process_expired_work_item(db, work_item)
            db.commit()
        if once:
            return
        sleep(max(1.0, poll_seconds))


def _process_due_dynamic_signals(db: Session, *, once: bool) -> int:
    """单次模式确定性执行，常驻模式有界派发，避免慢 Agent 阻塞调度扫描。"""

    dispatched = 0
    for dynamic_signal in due_dynamic_task_signals(db):
        if once:
            process_dynamic_task_signal(db, dynamic_signal)
            dispatched += 1
        elif start_dynamic_task_signal_async(dynamic_signal.id):
            dispatched += 1
    return dispatched


def process_expired_work_item(db: Session, work_item: SopWorkItem) -> None:
    """先终止 Skill 提案资源，再用统一协调器收敛超时 Execution。"""

    if work_item.attention_kind == "publication":
        GeneralSkillProposalService(db).terminate(
            tenant_id=work_item.tenant_id,
            operation_id=str(work_item.payload_json.get("operation_id") or ""),
            outcome="expired",
            error_code="GENERAL_SKILL_PROPOSAL_EXPIRED",
        )
    DeterministicSopCoordinator(db).timeout_expired_work_item(work_item)


def start_background_worker(*, poll_seconds: float = WORKER_SLEEP_SECONDS) -> None:
    """在 Web 进程内幂等启动后台轮询线程。"""

    global _background_thread, _stopped
    if _background_thread and _background_thread.is_alive():
        return
    _stopped = False
    _background_thread = threading.Thread(
        target=run_worker,
        kwargs={"once": False, "poll_seconds": poll_seconds},
        name="gongge-xuban-scheduled-task-worker",
        daemon=True,
    )
    _background_thread.start()


def stop_background_worker() -> None:
    """通知 Web 进程内后台线程在当前轮次后停止。"""

    global _stopped
    _stopped = True


def main() -> None:
    """解析独立 worker CLI 参数并启动轮询。"""

    parser = argparse.ArgumentParser(description="Run the Gongge Xuban scheduled-task worker")
    parser.add_argument(
        "--once", action="store_true", help="scan and execute due tasks once, then exit"
    )
    parser.add_argument("--poll-seconds", type=float, default=WORKER_SLEEP_SECONDS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    run_worker(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
