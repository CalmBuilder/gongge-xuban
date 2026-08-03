from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4


SpanSink = Callable[[str, dict[str, Any]], None]


_span_sink: ContextVar[SpanSink | None] = ContextVar("span_sink", default=None)
_span_operation: ContextVar[str] = ContextVar("span_operation", default="llm.request")
_span_attributes: ContextVar[dict[str, Any]] = ContextVar("span_attributes", default={})
_parent_span_id: ContextVar[str | None] = ContextVar("parent_span_id", default=None)


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _span_id() -> str:
    return f"span_{uuid4().hex[:16]}"


def emit_span_event(event_type: str, payload: dict[str, Any]) -> None:
    sink = _span_sink.get()
    if sink is None:
        return
    try:
        sink(event_type, payload)
    except Exception:
        # Observability must never turn a successful business request into a failure.
        return


@contextmanager
def bind_span_sink(sink: SpanSink) -> Iterator[None]:
    token = _span_sink.set(sink)
    try:
        yield
    finally:
        _span_sink.reset(token)


def set_span_sink(sink: SpanSink):  # noqa: ANN201 - ContextVar token type is implementation-specific.
    return _span_sink.set(sink)


def reset_span_sink(token: Any) -> None:
    _span_sink.reset(token)


@contextmanager
def llm_operation(operation: str, **attributes: Any) -> Iterator[None]:
    operation_token = _span_operation.set(operation)
    attributes_token = _span_attributes.set({**_span_attributes.get(), **attributes})
    try:
        yield
    finally:
        _span_attributes.reset(attributes_token)
        _span_operation.reset(operation_token)


@contextmanager
def llm_span_attributes(**attributes: Any) -> Iterator[None]:
    token = _span_attributes.set({**_span_attributes.get(), **attributes})
    try:
        yield
    finally:
        _span_attributes.reset(token)


def current_llm_operation() -> str:
    return _span_operation.get()


@dataclass
class ManualSpan:
    event_prefix: str
    operation: str
    attributes: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=_span_id)
    parent_span_id: str | None = field(default_factory=lambda: _parent_span_id.get())
    started_at: str = field(default_factory=_utc_iso)
    _started_perf: float = field(default_factory=perf_counter)
    _finished: bool = False

    def __post_init__(self) -> None:
        emit_span_event(f"{self.event_prefix}_started", self._payload())

    def elapsed_ms(self) -> float:
        return round((perf_counter() - self._started_perf) * 1000, 3)

    def finish(self, *, status: str = "success", **attributes: Any) -> None:
        if self._finished:
            return
        self._finished = True
        emit_span_event(
            f"{self.event_prefix}_finished",
            self._payload(
                finished_at=_utc_iso(),
                duration_ms=self.elapsed_ms(),
                status=status,
                **attributes,
            ),
        )

    def fail(self, error: BaseException, **attributes: Any) -> None:
        if self._finished:
            return
        self._finished = True
        emit_span_event(
            f"{self.event_prefix}_failed",
            self._payload(
                finished_at=_utc_iso(),
                duration_ms=self.elapsed_ms(),
                status="failed",
                error_type=error.__class__.__name__,
                error=str(error)[:500],
                **attributes,
            ),
        )

    def _payload(self, **attributes: Any) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "started_at": self.started_at,
            **self.attributes,
            **attributes,
        }


@contextmanager
def observed_span(
    event_prefix: str,
    operation: str,
    **attributes: Any,
) -> Iterator[ManualSpan]:
    span = ManualSpan(event_prefix, operation, attributes)
    parent_token = _parent_span_id.set(span.span_id)
    try:
        yield span
    except BaseException as exc:
        span.fail(exc)
        raise
    else:
        span.finish()
    finally:
        _parent_span_id.reset(parent_token)


def start_llm_call(**attributes: Any) -> ManualSpan:
    return ManualSpan(
        "llm_call",
        _span_operation.get(),
        {**_span_attributes.get(), **attributes},
    )
