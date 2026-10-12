"""Asynchronous, fail-open dashboard session touch worker."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from src.auth.sessions import touch_session
from src.db.connection import scoped_observability_write

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TouchItem:
    session_id: str
    enqueued_monotonic: float


class SessionTouchWorker:
    """Deduplicate and persist session keep-alives away from request threads."""

    def __init__(self, *, queue_size: int = 2048, write_timeout: float = 0.05) -> None:
        self._queue: queue.Queue[_TouchItem] = queue.Queue(maxsize=max(1, queue_size))
        self._write_timeout = max(0.001, float(write_timeout))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, float] = {}
        self._enqueued = 0
        self._processed = 0
        self._dropped = 0
        self._errors = 0
        self._last_error: str | None = None
        self._last_processed_at: float | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="session-touch-worker",
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, session_id: str | None) -> bool:
        """Queue one touch without blocking; duplicate pending IDs are coalesced."""
        normalized = str(session_id or "").strip()
        if not normalized:
            return False
        with self._lock:
            if self._thread is None or not self._thread.is_alive() or self._stop_event.is_set():
                self._dropped += 1
                return False
            if normalized in self._pending:
                return True
            enqueued_at = time.monotonic()
            self._pending[normalized] = enqueued_at
            self._enqueued += 1
        try:
            self._queue.put_nowait(_TouchItem(normalized, enqueued_at))
            return True
        except queue.Full:
            with self._lock:
                self._pending.pop(normalized, None)
                self._dropped += 1
            return False

    def stop(self, timeout: float = 2.0) -> bool:
        """Request shutdown and wait only for the supplied bounded timeout."""
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def health(self) -> dict[str, Any]:
        with self._lock:
            oldest = min(self._pending.values(), default=None)
            thread = self._thread
            return {
                "running": bool(thread is not None and thread.is_alive()),
                "queue_depth": self._queue.qsize(),
                "pending_sessions": len(self._pending),
                "queue_lag_ms": (
                    round((time.monotonic() - oldest) * 1000, 3)
                    if oldest is not None
                    else 0.0
                ),
                "enqueued": self._enqueued,
                "processed": self._processed,
                "dropped": self._dropped,
                "errors": self._errors,
                "last_error": self._last_error,
                "last_processed_monotonic": self._last_processed_at,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with scoped_observability_write(timeout=self._write_timeout) as conn:
                    touch_session(conn, item.session_id)
                with self._lock:
                    self._processed += 1
                    self._last_processed_at = time.monotonic()
            except Exception as exc:
                with self._lock:
                    self._errors += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"[:1000]
                logger.debug("Session touch failed for %s", item.session_id, exc_info=True)
            finally:
                with self._lock:
                    self._pending.pop(item.session_id, None)
                self._queue.task_done()


_worker: SessionTouchWorker | None = None
_worker_lock = threading.Lock()


def start_session_touch_worker(*, queue_size: int = 2048) -> SessionTouchWorker:
    """Start the process-global session touch worker once."""
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.health()["running"]:
            _worker = SessionTouchWorker(queue_size=queue_size)
            _worker.start()
        return _worker


def enqueue_session_touch(session_id: str | None) -> bool:
    """Fail-open request-thread producer entry point."""
    worker = _worker
    return worker.enqueue(session_id) if worker is not None else False


def stop_session_touch_worker(timeout: float = 2.0) -> bool:
    """Stop the process-global worker within a bounded timeout."""
    global _worker
    with _worker_lock:
        worker = _worker
    if worker is None:
        return True
    stopped = worker.stop(timeout=timeout)
    if stopped:
        with _worker_lock:
            if _worker is worker:
                _worker = None
    return stopped


def session_touch_health() -> dict[str, Any]:
    worker = _worker
    if worker is None:
        return {
            "running": False,
            "queue_depth": 0,
            "pending_sessions": 0,
            "queue_lag_ms": 0.0,
            "enqueued": 0,
            "processed": 0,
            "dropped": 0,
            "errors": 0,
            "last_error": None,
            "last_processed_monotonic": None,
        }
    return worker.health()
