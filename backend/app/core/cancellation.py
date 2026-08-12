"""
@Time       : 2026/08/12 10:30
@Author     : zhanglp8181
@File       : cancellation.py
@CallChain  : Chat cancel API → AgentLoop → ResponseGenerator → LLMClient
@Description: 维护 Turn 级取消加速信号，并将持久事件探针组合为跨 worker 取消令牌。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

_lock = Lock()
_cancelled_turns: set[tuple[str, str]] = set()


def cancel_chat_turn(session_id: str, turn_id: str) -> None:
    """记录会话 Turn 的进程内取消信号，仅用于加速已持久的取消。"""

    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.add((session_id, turn_id))


def clear_chat_turn_cancelled(session_id: str, turn_id: str) -> None:
    """清理已收敛 Turn 的进程内取消信号。"""

    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.discard((session_id, turn_id))


def is_chat_turn_cancelled(session_id: str, turn_id: str) -> bool:
    """返回当前进程是否收到指定 Turn 的取消加速信号。"""

    if not session_id or not turn_id:
        return False
    with _lock:
        return (session_id, turn_id) in _cancelled_turns


@dataclass(frozen=True, slots=True)
class TurnCancellationToken:
    """合并 server/client Turn 别名与持久事件探针，提供稳定取消边界。"""

    session_id: str
    server_turn_id: str
    client_turn_id: str = ""
    persistent_probe: Callable[[], bool] | None = None

    def is_cancelled(self) -> bool:
        """先读进程内快速信号，再读跨 worker 持久取消事实。"""

        aliases = (self.server_turn_id, self.client_turn_id)
        if any(
            turn_id and is_chat_turn_cancelled(self.session_id, turn_id)
            for turn_id in aliases
        ):
            return True
        return bool(self.persistent_probe and self.persistent_probe())
