"""
@Time       : 2026/09/06
@Author     : zhanglp8181
@File       : event_log.py
@CallChain  : AgentLoop/后台旁路 → EventLog → Turn 终态栅栏 → AgentEvent
@Description: 统一记录执行事件，并在跨 worker 取消或终态后阻断迟到的 Turn 事件。
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.db.models import AgentEvent, ChatSession
from app.observability.turn_event_query import (
    TURN_EVENT_ID_KEYS,
    event_turn_ids,
    normalize_turn_identity,
    turn_identity_filter,
)


TURN_TERMINAL_EVENT_TYPES = frozenset(
    {
        "complete",
        "error_occurred",
        "stream_cancelled",
        "stream_interrupted",
    }
)
# 取消/中断的 assistant_message_created 是终态闭合动作，不是迟到的模型/工具事件；
# session_state_changed 同样只用于把已完成的业务结果投影到会话状态。
# memory.capture_turn 在同步聊天路径中会在 canonical terminal 提交之后入队，
# 因而 async_job_enqueued 是允许的终态后业务副作用；它不代表新的执行阶段。
TURN_POST_TERMINAL_EVENT_TYPES = frozenset(
    {
        "assistant_message_created",
        "async_job_enqueued",
        "memory_saved",
        "memory_error",
        "session_state_changed",
    }
)


class TurnEventFenceClosed(RuntimeError):
    """表示当前 Turn 已有终态，新的执行事件不得再写入事件账本。"""


def _payload_turn_ids(payload: dict[str, Any]) -> set[str]:
    """提取已归一化的 Turn 身份，拒绝空白身份参与终态判断。"""

    return {
        normalized
        for normalized in (
            normalize_turn_identity(payload.get(key))
            for key in TURN_EVENT_ID_KEYS
        )
        if normalized
    }


def _turn_terminal_event_types(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_ids: set[str],
) -> set[str]:
    """在当前事务中锁定会话并查询目标 Turn 是否已有终态事件。"""

    if not turn_ids:
        return set()
    # MySQL 通过会话行锁串行化取消与 worker 事件追加；SQLite 的写事务仍提供
    # 单写者语义。会话不存在时保留事件日志的通用用法，不凭空创建会话。
    session = db.exec(
        select(ChatSession)
        .where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == tenant_id,
        )
        .with_for_update()
    ).first()
    if session is None:
        return set()
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type.in_(TURN_TERMINAL_EVENT_TYPES),  # type: ignore[attr-defined]
            turn_identity_filter(db, turn_ids),
        )
        .with_for_update()
    ).all()
    return {
        row.event_type
        for row in rows
        if event_turn_ids(row) & turn_ids
    }


def ensure_turn_event_open(
    db: Session,
    tenant_id: str,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """校验 Turn 尚未闭合；仅允许已定义的终态后业务投影或异步入队事件。"""

    turn_ids = _payload_turn_ids(payload)
    if not turn_ids:
        return
    terminal_types = _turn_terminal_event_types(db, tenant_id, session_id, turn_ids)
    if not terminal_types:
        return
    if event_type in TURN_POST_TERMINAL_EVENT_TYPES:
        if event_type == "session_state_changed":
            return
        if event_type == "async_job_enqueued":
            if (
                payload.get("feature") == "memory"
                and payload.get("job_name") == "memory.capture_turn"
            ):
                return
        if event_type in {"memory_saved", "memory_error"}:
            if (
                payload.get("feature") == "memory"
                and payload.get("job_name") == "memory.capture_turn"
            ):
                return
        status = str(payload.get("status") or "").strip().lower()
        if status in {"cancelled", "interrupted"}:
            return
    raise TurnEventFenceClosed(
        f"turn event rejected after terminal state: {event_type} ({session_id})"
    )


class EventLog:
    """为执行链提供带 Turn 身份和终态栅栏的统一事件写入口。"""

    def __init__(self, db: Session):
        """绑定当前数据库会话，并初始化可选的本轮服务端/客户端身份。"""

        self.db = db
        self._turn_id: str | None = None
        self._client_turn_id: str | None = None

    def bind_turn(self, turn_id: str, client_turn_id: str | None = None) -> None:
        """记录本轮身份，使未显式携带身份的运行时事件也能进入终态校验。"""

        self._turn_id = str(turn_id or "").strip() or None
        self._client_turn_id = str(client_turn_id or "").strip() or None

    def terminal_event_types(
        self,
        tenant_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> set[str]:
        """查询当前 Turn 已落账的终态类型，供幂等投影区分成功与失败竞争。"""

        traced_payload = dict(payload)
        if self._turn_id:
            traced_payload.setdefault("turn_id", self._turn_id)
            traced_payload.setdefault("user_message_id", self._turn_id)
        if self._client_turn_id:
            traced_payload.setdefault("client_turn_id", self._client_turn_id)
        return _turn_terminal_event_types(
            self.db,
            tenant_id,
            session_id,
            _payload_turn_ids(traced_payload),
        )

    def record_terminal_once(
        self,
        tenant_id: str,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """在会话生命周期锁内幂等追加一个 Turn 终态，返回本次是否实际写入。"""

        if event_type not in TURN_TERMINAL_EVENT_TYPES:
            raise ValueError(f"event_type is not a Turn terminal event: {event_type}")
        traced_payload = dict(payload)
        if self._turn_id:
            traced_payload.setdefault("turn_id", self._turn_id)
            traced_payload.setdefault("user_message_id", self._turn_id)
        if self._client_turn_id:
            traced_payload.setdefault("client_turn_id", self._client_turn_id)
        if self.terminal_event_types(tenant_id, session_id, traced_payload):
            return False
        self.record(tenant_id, session_id, event_type, traced_payload)
        return True

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict[str, Any]) -> AgentEvent:
        """追加事件并统一执行 Turn 终态栅栏。"""

        traced_payload = dict(payload)
        if self._turn_id:
            traced_payload.setdefault("turn_id", self._turn_id)
            traced_payload.setdefault("user_message_id", self._turn_id)
        if self._client_turn_id:
            traced_payload.setdefault("client_turn_id", self._client_turn_id)
        ensure_turn_event_open(
            self.db,
            tenant_id,
            session_id,
            event_type,
            traced_payload,
        )
        event = AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            payload_json=traced_payload,
        )
        self.db.add(event)
        return event
