"""Vendor-neutral engineering trace contracts and context propagation."""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

TRACE_SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_trace_id() -> str:
    """Return a W3C-compatible 32-hex trace identifier."""
    return uuid.uuid4().hex


def new_identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class EngineeringTraceEvent(BaseModel):
    """One ordered, reconstructible engineering lifecycle event.

    ``payload`` is request-thread memory only. The background pipeline removes
    secrets and hidden reasoning before encrypting it; plaintext is never
    written to the spool or observability tables.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    schema_version: int = TRACE_SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: new_identifier("evt"))
    sequence: int
    event_type: str
    trace_id: str
    run_id: str
    turn_id: str | None = None
    thread_id: str | None = None
    round_index: int | None = None
    llm_call_id: str | None = None
    tool_call_id: str | None = None
    timestamp: str = Field(default_factory=utcnow)
    duration_ms: float | None = None
    status: str = "ok"
    component: str = "agentfixbpro"
    component_version: str = "0.1.0"
    parent_event_id: str | None = None
    parent_span_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    payload: Any = None


@dataclass
class TraceContext:
    """Stable run identity shared by surfaces, loops, tools, and LLM calls."""

    trace_id: str
    run_id: str
    turn_id: str | None = None
    thread_id: str | None = None
    surface: str = "background"
    task: str = "background"
    parent_event_id: str | None = None
    component: str = "agentfixbpro"
    component_version: str = "0.1.0"
    implicit: bool = False
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(
        cls,
        *,
        surface: str,
        task: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
        implicit: bool = False,
        parent_event_id: str | None = None,
    ) -> "TraceContext":
        return cls(
            trace_id=trace_id or new_trace_id(),
            run_id=run_id or new_identifier("run"),
            turn_id=turn_id or new_identifier("turn"),
            thread_id=thread_id,
            surface=surface,
            task=task,
            implicit=implicit,
            parent_event_id=parent_event_id,
        )

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def mark_started(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._started = True
            return True

    def mark_finished(self) -> bool:
        with self._lock:
            if self._finished:
                return False
            self._finished = True
            return True

    def make_event(self, event_type: str, **kwargs: Any) -> EngineeringTraceEvent:
        return EngineeringTraceEvent(
            sequence=self.next_sequence(),
            event_type=event_type,
            trace_id=self.trace_id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            thread_id=self.thread_id,
            component=kwargs.pop("component", self.component),
            component_version=kwargs.pop("component_version", self.component_version),
            parent_event_id=kwargs.pop("parent_event_id", self.parent_event_id),
            **kwargs,
        )


@runtime_checkable
class TraceSink(Protocol):
    """Non-blocking producer contract for engineering trace events."""

    def emit(self, event: EngineeringTraceEvent) -> bool: ...

    def flush(self, timeout: float = 5.0) -> bool: ...

    def health(self) -> dict[str, Any]: ...


@runtime_checkable
class TraceExporter(Protocol):
    """Optional background exporter, for example a future LangSmith adapter."""

    name: str

    def export(self, record: dict[str, Any]) -> None: ...


_current_trace_context: ContextVar[TraceContext | None] = ContextVar(
    "engineering_trace_context", default=None
)
_current_round_index: ContextVar[int | None] = ContextVar(
    "engineering_trace_round_index", default=None
)


def current_trace_context() -> TraceContext | None:
    return _current_trace_context.get()


def bind_trace_context(context: TraceContext) -> Token:
    return _current_trace_context.set(context)


def unbind_trace_context(token: Token) -> None:
    _current_trace_context.reset(token)


@contextmanager
def trace_context_scope(context: TraceContext) -> Iterator[TraceContext]:
    token = bind_trace_context(context)
    try:
        yield context
    finally:
        unbind_trace_context(token)


def current_round_index() -> int | None:
    return _current_round_index.get()


@contextmanager
def round_index_scope(round_index: int) -> Iterator[int]:
    token = _current_round_index.set(round_index)
    try:
        yield round_index
    finally:
        _current_round_index.reset(token)
