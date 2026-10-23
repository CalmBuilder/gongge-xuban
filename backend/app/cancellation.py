"""
@Time       : 2026/08/28 11:15
@Author     : zhanglp8181
@File       : cancellation.py
@CallChain  : Chat cancel API/Agent Loop → 同步阻塞阶段 → 取消异常与资源关闭
@Description: 提供不依赖 app.core 包初始化的 Turn 取消原语，供底层工具、MCP 和 LLM 客户端共享。
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from math import isfinite
from queue import Empty, Full, Queue
from threading import Lock, Thread
from time import monotonic
from typing import TypeVar

_lock = Lock()
_cancelled_turns: set[tuple[str, str]] = set()
_unbound_cancelled_turns: dict[str, float] = {}
_UNBOUND_CANCEL_TTL_SECONDS = 15 * 60
_T = TypeVar("_T")


class TurnCancellationRequested(RuntimeError):
    """表示同步 Agent 阶段在返回结果前观察到当前 Turn 已取消。"""


class CancellableStageTimeout(TimeoutError):
    """表示同步阶段自身的绝对墙钟期限已到，而非外部操作报告超时。"""


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


def cancel_unbound_chat_turn(turn_id: str) -> None:
    """登记尚未创建会话的流式 Turn 取消，等待 worker 建立会话后接管。"""

    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return
    now = monotonic()
    with _lock:
        expired = [
            key
            for key, created_at in _unbound_cancelled_turns.items()
            if now - created_at > _UNBOUND_CANCEL_TTL_SECONDS
        ]
        for key in expired:
            _unbound_cancelled_turns.pop(key, None)
        _unbound_cancelled_turns[normalized_turn_id] = now


def clear_unbound_chat_turn_cancelled(turn_id: str) -> None:
    """清理已被 worker 接管或已结束的无会话 Turn 取消登记。"""

    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return
    with _lock:
        _unbound_cancelled_turns.pop(normalized_turn_id, None)


def is_unbound_chat_turn_cancelled(turn_id: str) -> bool:
    """查询尚未绑定会话的 Turn 是否已收到取消，并清理过期登记。"""

    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return False
    now = monotonic()
    with _lock:
        created_at = _unbound_cancelled_turns.get(normalized_turn_id)
        if created_at is None:
            return False
        if now - created_at > _UNBOUND_CANCEL_TTL_SECONDS:
            _unbound_cancelled_turns.pop(normalized_turn_id, None)
            return False
        return True


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
        if self.client_turn_id and is_unbound_chat_turn_cancelled(self.client_turn_id):
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
    timeout_seconds: float | None = None,
    on_timeout: Callable[[], None] | None = None,
    poll_seconds: float = 0.05,
) -> _T:
    """在可轮询线程中运行同步外呼，并执行撤销或绝对截止对应的关闭钩子。

    到达期限后即拒绝接收迟到结果；底层 SDK 若不响应 close，会留在 daemon worker
    中收敛，不能把同一个 SQLAlchemy Session 传入 worker。
    """

    raise_if_cancelled(is_cancelled)
    deadline = None
    if timeout_seconds is not None:
        timeout_budget = float(timeout_seconds)
        if not isfinite(timeout_budget):
            raise ValueError("同步阶段期限必须是有限秒数。")
        if timeout_budget <= 0:
            raise CancellableStageTimeout("同步阶段超过冻结墙钟期限。")
        deadline = monotonic() + timeout_budget
    if is_cancelled is None and deadline is None:
        return operation()

    result_queue: Queue[tuple[bool, object, float]] = Queue(maxsize=1)

    def invoke_hook(hook: Callable[[], None] | None) -> None:
        """执行退出钩子并隔离资源关闭回调中的异常。"""

        if hook is None:
            return
        try:
            hook()
        except BaseException:
            return

    def resolve_result(result: tuple[bool, object, float]) -> _T:
        """仅接受墙钟截止前完成的调用结果，并传播原始异常。"""

        succeeded, value, finished_at = result
        if deadline is not None and finished_at > deadline:
            invoke_hook(on_timeout)
            raise CancellableStageTimeout("同步阶段超过冻结墙钟期限。")
        if succeeded:
            return value  # type: ignore[return-value]
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("可取消阶段返回了未知结果。")

    def worker() -> None:
        """执行不可安全强杀的同步调用，并把结果或异常投递给消费者。"""

        if deadline is not None and monotonic() >= deadline:
            return
        try:
            result_queue.put_nowait((True, operation(), monotonic()))
        except Full:
            return
        except BaseException as exc:
            try:
                result_queue.put_nowait((False, exc, monotonic()))
            except Full:
                return

    worker_context = copy_context()
    Thread(
        target=worker_context.run,
        args=(worker,),
        name="turn-cancellable-stage",
        daemon=True,
    ).start()
    poll_timeout = max(0.01, float(poll_seconds))
    while True:
        if is_cancelled is not None and is_cancelled():
            invoke_hook(on_cancel)
            raise TurnCancellationRequested("当前 Turn 已取消。")
        try:
            result = result_queue.get_nowait()
        except Empty:
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                invoke_hook(on_timeout)
                raise CancellableStageTimeout("同步阶段超过冻结墙钟期限。")
            wait_seconds = (
                poll_timeout if remaining is None else min(poll_timeout, remaining)
            )
            try:
                result = result_queue.get(timeout=wait_seconds)
            except Empty:
                continue
        return resolve_result(result)
