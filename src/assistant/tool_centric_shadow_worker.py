"""Bounded background worker for tool-centric Workspace shadow observations."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.assistant.tool_centric_shadow_router import (
    ToolCentricShadowObservation,
    observe_tool_centric_shadow,
)
from src.db.connection import open_connection


@dataclass(frozen=True)
class ToolCentricShadowJob:
    database_path: str
    question: str
    served_answer: str
    scope_mode: str
    project_ids: tuple[str, ...]
    as_of: datetime
    parent_trace_id: str | None
    parent_run_id: str | None
    thread_id: str | None
    turn_id: str | None
    previous_query: dict[str, Any] | None = None
    enqueued_monotonic: float = 0.0


Observer = Callable[..., ToolCentricShadowObservation]


class ToolCentricShadowWorker:
    """Process immutable jobs off-request with owned read connections."""

    def __init__(
        self,
        *,
        queue_size: int = 256,
        worker_count: int = 1,
        planner_timeout_seconds: int = 20,
        model_override: str | None = None,
        observer: Observer | None = None,
    ) -> None:
        self._queue: queue.Queue[ToolCentricShadowJob] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._worker_count = max(1, int(worker_count))
        self._planner_timeout_seconds = max(1, int(planner_timeout_seconds))
        self._model_override = model_override
        self._observer = observer or observe_tool_centric_shadow
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._enqueued = 0
        self._processed = 0
        self._completed = 0
        self._fallback = 0
        self._failed = 0
        self._dropped = 0
        self._errors = 0
        self._last_error_class: str | None = None
        self._oldest_enqueued: float | None = None
        self._max_queue_lag_ms = 0.0
        self._previous_queries: dict[
            str, tuple[str, tuple[str, ...], dict[str, Any], float]
        ] = {}

    def start(self) -> "ToolCentricShadowWorker":
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return self
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._run,
                    name=f"tool-centric-shadow-{index + 1}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            ]
            for thread in self._threads:
                thread.start()
        return self

    def enqueue(self, job: ToolCentricShadowJob) -> bool:
        """Enqueue without blocking; queue saturation fails open."""
        with self._lock:
            if not any(thread.is_alive() for thread in self._threads) or self._stop_event.is_set():
                self._dropped += 1
                return False
        queued = replace(job, enqueued_monotonic=job.enqueued_monotonic or time.monotonic())
        try:
            self._queue.put_nowait(queued)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        with self._lock:
            self._enqueued += 1
            if self._oldest_enqueued is None:
                self._oldest_enqueued = queued.enqueued_monotonic
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Request drain and wait no longer than the supplied timeout."""
        self._stop_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def health(self) -> dict[str, Any]:
        with self._lock:
            oldest = self._oldest_enqueued
            return {
                "enabled": True,
                "running": any(thread.is_alive() for thread in self._threads),
                "worker_count": len([thread for thread in self._threads if thread.is_alive()]),
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "queue_lag_ms": round((time.monotonic() - oldest) * 1000, 3) if oldest else 0.0,
                "max_queue_lag_ms": round(self._max_queue_lag_ms, 3),
                "enqueued": self._enqueued,
                "processed": self._processed,
                "completed": self._completed,
                "fallback": self._fallback,
                "failed": self._failed,
                "dropped": self._dropped,
                "errors": self._errors,
                "last_error_class": self._last_error_class,
                "planner_timeout_seconds": self._planner_timeout_seconds,
                "planner_model": self._model_override,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            lag_ms = max(0.0, (time.monotonic() - job.enqueued_monotonic) * 1000)
            try:
                conn = open_connection(job.database_path)
                try:
                    previous_query = job.previous_query or self._trusted_previous_query(job)
                    observation = self._observer(
                        conn,
                        question=job.question,
                        served_answer=job.served_answer,
                        scope_mode=job.scope_mode,
                        project_ids=list(job.project_ids),
                        as_of=job.as_of,
                        previous_query=previous_query,
                        model_override=self._model_override,
                        planner_timeout_seconds=self._planner_timeout_seconds,
                        parent_trace_id=job.parent_trace_id,
                        parent_run_id=job.parent_run_id,
                        thread_id=job.thread_id,
                        turn_id=job.turn_id,
                    )
                finally:
                    conn.close()
                if observation.execution_status == "completed" and observation.executed_query:
                    self._remember_query(job, observation.executed_query)
                with self._lock:
                    self._processed += 1
                    if observation.execution_status == "completed":
                        self._completed += 1
                    elif observation.execution_status == "fallback":
                        self._fallback += 1
                    else:
                        self._failed += 1
            except Exception as exc:
                with self._lock:
                    self._processed += 1
                    self._errors += 1
                    self._last_error_class = type(exc).__name__
            finally:
                with self._lock:
                    self._max_queue_lag_ms = max(self._max_queue_lag_ms, lag_ms)
                    if self._queue.empty():
                        self._oldest_enqueued = None
                self._queue.task_done()


    def _trusted_previous_query(self, job: ToolCentricShadowJob) -> dict[str, Any] | None:
        if not job.thread_id:
            return None
        with self._lock:
            record = self._previous_queries.get(job.thread_id)
            if record is None:
                return None
            scope_mode, project_ids, query, recorded_at = record
            if time.monotonic() - recorded_at > 3600:
                self._previous_queries.pop(job.thread_id, None)
                return None
            if scope_mode != job.scope_mode or project_ids != job.project_ids:
                return None
            return dict(query)

    def _remember_query(self, job: ToolCentricShadowJob, query: dict[str, Any]) -> None:
        if not job.thread_id:
            return
        with self._lock:
            if len(self._previous_queries) >= 1024 and job.thread_id not in self._previous_queries:
                oldest_thread = min(
                    self._previous_queries,
                    key=lambda key: self._previous_queries[key][3],
                )
                self._previous_queries.pop(oldest_thread, None)
            self._previous_queries[job.thread_id] = (
                job.scope_mode,
                job.project_ids,
                dict(query),
                time.monotonic(),
            )


_worker: ToolCentricShadowWorker | None = None
_worker_lock = threading.Lock()


def database_path_for_connection(conn: sqlite3.Connection) -> str | None:
    """Return the file backing the main database, or None for in-memory DBs."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:
        return None
    for row in rows:
        name = str(row[1] if not isinstance(row, sqlite3.Row) else row["name"])
        path = str(row[2] if not isinstance(row, sqlite3.Row) else row["file"])
        if name == "main" and path:
            return str(Path(path).resolve())
    return None


def start_tool_centric_shadow_worker(
    *,
    queue_size: int | None = None,
    worker_count: int = 1,
    planner_timeout_seconds: int | None = None,
    model_override: str | None = None,
) -> ToolCentricShadowWorker | None:
    """Start the process-global worker only when the rollout flag is enabled."""
    from config import defaults

    from src.assistant.tool_centric_serving import (
        should_run_tool_centric_shadow,
        tool_centric_planner_model,
    )

    if not should_run_tool_centric_shadow("workspace"):
        return None
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.health()["running"]:
            _worker = ToolCentricShadowWorker(
                queue_size=queue_size
                or getattr(defaults, "AI_WORKSPACE_TOOL_CENTRIC_SHADOW_QUEUE_CAPACITY", 256),
                worker_count=worker_count,
                planner_timeout_seconds=planner_timeout_seconds
                or getattr(defaults, "AI_WORKSPACE_TOOL_CENTRIC_SHADOW_TIMEOUT_SECONDS", 20),
                model_override=(
                    str(model_override or "").strip() or tool_centric_planner_model()
                ),
            ).start()
        return _worker


def submit_tool_centric_shadow(job: ToolCentricShadowJob) -> bool:
    worker = _worker
    return False if worker is None else worker.enqueue(job)


def stop_tool_centric_shadow_worker(timeout: float = 5.0) -> bool:
    global _worker
    with _worker_lock:
        worker = _worker
        if worker is None:
            return True
        stopped = worker.stop(timeout)
        if stopped:
            _worker = None
        return stopped



def _shadow_mode_enabled() -> bool:
    from src.assistant.tool_centric_serving import should_run_tool_centric_shadow

    return should_run_tool_centric_shadow("workspace")

def tool_centric_shadow_health() -> dict[str, Any]:
    from config import defaults

    worker = _worker
    if worker is None:
        return {
            "enabled": _shadow_mode_enabled(),
            "running": False,
            "worker_count": 0,
            "queue_depth": 0,
            "queue_capacity": int(
                getattr(defaults, "AI_WORKSPACE_TOOL_CENTRIC_SHADOW_QUEUE_CAPACITY", 256)
            ),
            "queue_lag_ms": 0.0,
            "max_queue_lag_ms": 0.0,
            "enqueued": 0,
            "processed": 0,
            "completed": 0,
            "fallback": 0,
            "failed": 0,
            "dropped": 0,
            "errors": 0,
            "last_error_class": None,
            "planner_timeout_seconds": int(
                getattr(defaults, "AI_WORKSPACE_TOOL_CENTRIC_SHADOW_TIMEOUT_SECONDS", 20)
            ),
            "planner_model": str(
                getattr(defaults, "TOOL_CENTRIC_PLANNER_MODEL", "openai/default-model")
                or "openai/default-model"
            ),
        }
    return worker.health()


__all__ = [
    "ToolCentricShadowJob",
    "ToolCentricShadowWorker",
    "database_path_for_connection",
    "start_tool_centric_shadow_worker",
    "stop_tool_centric_shadow_worker",
    "submit_tool_centric_shadow",
    "tool_centric_shadow_health",
]
