"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : jobs.py
@CallChain  : 持久 MemoryCaptureJob → durable worker → MemoryService/MemoryRecord
@Description: 保留旧任务入口的兼容壳，但所有生产记忆写入必须经过持久 worker 屏障。
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.db import engine
from app.db.models import Message
from app.memory.worker import process_memory_capture_job


def enqueue_memory_capture(*_args: Any, **_kwargs: Any) -> None:
    """拒绝旧内存队列写入旁路，调用方必须在完成事务中登记持久任务。"""

    raise RuntimeError("memory capture requires a durable MemoryCaptureJob registration")


def run_memory_capture_job(payload: dict[str, Any]) -> None:
    """仅执行包含持久 job_id 的兼容唤醒，不再从 payload 猜测会话最新消息。"""

    job_id = str(payload.get("job_id") or "").strip()
    worker_id = str(payload.get("worker_id") or "legacy-memory-wakeup").strip()
    if not job_id:
        return
    with Session(engine) as db:
        process_memory_capture_job(db, job_id=job_id, worker_id=worker_id)


def _conversation_messages_for_turn(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_id: str,
) -> list[dict[str, str]]:
    """按显式 Turn 截止点读取历史，仅供兼容查询和迁移测试，不做最新回退。"""

    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return []
    rows = list(
        db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        ).all()
    )
    target_index = next(
        (
            index
            for index, row in enumerate(rows)
            if row.role == "assistant"
            and (
                row.id == normalized_turn_id
                or str(
                    (row.metadata_json or {}).get("turn_id")
                    or (row.metadata_json or {}).get("user_message_id")
                    or ""
                )
                == normalized_turn_id
            )
        ),
        None,
    )
    if target_index is None:
        return []
    return [
        {"role": row.role, "content": row.content}
        for row in rows[: target_index + 1]
        if row.role in {"user", "assistant"} and row.content.strip()
    ]
