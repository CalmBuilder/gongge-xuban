from __future__ import annotations

from threading import Lock

_lock = Lock()
_cancelled_turns: set[tuple[str, str]] = set()


def cancel_chat_turn(session_id: str, turn_id: str) -> None:
    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.add((session_id, turn_id))


def clear_chat_turn_cancelled(session_id: str, turn_id: str) -> None:
    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.discard((session_id, turn_id))


def is_chat_turn_cancelled(session_id: str, turn_id: str) -> bool:
    if not session_id or not turn_id:
        return False
    with _lock:
        return (session_id, turn_id) in _cancelled_turns
