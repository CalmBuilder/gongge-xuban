"""
@Time       : 2026/08/28 11:15
@Author     : zhanglp8181
@File       : cancellation.py
@CallChain  : Chat cancel API/Agent Loop → 同步阻塞阶段 → 取消异常与资源关闭
@Description: 提供不依赖 app.core 包初始化的 Turn 取消原语，供底层工具、MCP 和 LLM 客户端共享。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Lock, Thread
from typing import TypeVar

_lock = Lock()
_cancelled_turns: set[tuple[str, str]] = set()
_T = TypeVar("_T")


class TurnCancellationRequested(RuntimeError):
    """表示同步 Agent 阶段在返回结果前观察到当前 Turn 已取消。"""


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


def raise_if_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    """在阶段边界检查取消回调，避免取消后的结果继续进入下一个阶段。"""

    if is_cancelled is not None and is_cancelled():
        raise TurnCancellationRequested("当前 Turn 已取消。")


def run_cancellable(
    operation: Callable[[], _T],
    is_cancelled: Callable[[], bool] | None,
    *,
    on_cancel: Callable[[], None] | None = None,
    poll_seconds: float = 0.05,
) -> _T:
    """在可轮询线程中运行同步外呼，并在取消时执行非阻塞的资源关闭钩子。

    该函数只负责让调用方及时离开当前阶段；底层 SDK 若不响应 close，仍由其
    自身 timeout 和 daemon worker 收敛，不能把同一个 SQLAlchemy Session 传入 worker。
    """

    raise_if_cancelled(is_cancelled)
    if is_cancelled is None:
        return operation()

    result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def worker() -> None:
        """执行不可安全强杀的同步调用，并把结果或异常投递给消费者。"""

        try:
            result_queue.put_nowait((True, operation()))
        except Full:
            return
        except BaseException as exc:
            try:
                result_queue.put_nowait((False, exc))
            except Full:
                return

    Thread(target=worker, name="turn-cancellable-stage", daemon=True).start()
    timeout = max(0.01, float(poll_seconds))
    while True:
        if is_cancelled():
            if on_cancel is not None:
                try:
                    on_cancel()
                except BaseException:
                    pass
            raise TurnCancellationRequested("当前 Turn 已取消。")
        try:
            succeeded, value = result_queue.get(timeout=timeout)
        except Empty:
            continue
        if succeeded:
            return value  # type: ignore[return-value]
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("可取消阶段返回了未知结果。")
