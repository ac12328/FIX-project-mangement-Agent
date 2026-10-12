"""Bounded background worker for Email Project Truth B shadow observations."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from src.email.project_truth.shadow import (
    EmailProjectTruthShadowJob,
    EmailProjectTruthShadowObservation,
    observe_email_project_truth_shadow,
)


Observer = Callable[..., EmailProjectTruthShadowObservation]


class EmailProjectTruthShadowWorker:
    """Process immutable B-path jobs without blocking inbound email work."""

    def __init__(
        self,
        *,
        queue_size: int = 128,
        worker_count: int = 1,
        timeout_seconds: int = 20,
        model_override: str | None = None,
        observer: Observer | None = None,
    ) -> None:
        self._queue: queue.Queue[EmailProjectTruthShadowJob] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._worker_count = max(1, int(worker_count))
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._model_override = model_override or None
        self._observer = observer or observe_email_project_truth_shadow
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._enqueued = 0
        self._processed = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0
        self._errors = 0
        self._persistence_errors = 0
        self._last_error_class: str | None = None
        self._oldest_enqueued: float | None = None
        self._max_queue_lag_ms = 0.0

    def start(self) -> "EmailProjectTruthShadowWorker":
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return self
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._run,
                    name=f"email-project-truth-shadow-{index + 1}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            ]
            for thread in self._threads:
                thread.start()
        return self

    def enqueue(self, job: EmailProjectTruthShadowJob) -> bool:
        """Enqueue without blocking; stopped or saturated workers fail open."""
        with self._lock:
            if self._stop_event.is_set() or not any(
                thread.is_alive() for thread in self._threads
            ):
                self._dropped += 1
                return False
        queued = replace(
            job,
            project_snapshot=job.project_snapshot.model_copy(deep=True),
            attachments=tuple(job.attachments),
            baseline_fields=tuple(job.baseline_fields),
            enqueued_monotonic=job.enqueued_monotonic or time.monotonic(),
        )
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

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout: float = 5.0) -> bool:
        """Drain queued jobs within a bounded shutdown deadline."""
        self._stop_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def health(self) -> dict[str, Any]:
        with self._lock:
            oldest = self._oldest_enqueued
            return {
                "mode": "shadow",
                "enabled": True,
                "serving_enabled": False,
                "running": any(thread.is_alive() for thread in self._threads),
                "worker_count": len(
                    [thread for thread in self._threads if thread.is_alive()]
                ),
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "queue_lag_ms": round(
                    (time.monotonic() - oldest) * 1000, 3
                ) if oldest else 0.0,
                "max_queue_lag_ms": round(self._max_queue_lag_ms, 3),
                "enqueued": self._enqueued,
                "processed": self._processed,
                "completed": self._completed,
                "failed": self._failed,
                "dropped": self._dropped,
                "errors": self._errors,
                "persistence_errors": self._persistence_errors,
                "last_error_class": self._last_error_class,
                "timeout_seconds": self._timeout_seconds,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            queue_lag_ms = max(
                0.0,
                (time.monotonic() - job.enqueued_monotonic) * 1000,
            )
            try:
                observation = self._observer(
                    job,
                    model_override=self._model_override,
                    timeout_seconds=self._timeout_seconds,
                    queue_lag_ms=queue_lag_ms,
                )
                with self._lock:
                    self._processed += 1
                    if observation.execution_status == "completed":
                        self._completed += 1
                    else:
                        self._failed += 1
                    if not observation.persisted:
                        self._persistence_errors += 1
                    if observation.error_class:
                        self._last_error_class = observation.error_class
            except Exception as exc:
                with self._lock:
                    self._processed += 1
                    self._failed += 1
                    self._errors += 1
                    self._last_error_class = type(exc).__name__
            finally:
                with self._lock:
                    self._max_queue_lag_ms = max(
                        self._max_queue_lag_ms,
                        queue_lag_ms,
                    )
                    if self._queue.empty():
                        self._oldest_enqueued = None
                self._queue.task_done()


_worker: EmailProjectTruthShadowWorker | None = None
_worker_lock = threading.Lock()


def email_project_truth_mode() -> str:
    from config import defaults

    return str(getattr(defaults, "EMAIL_PROJECT_TRUTH_MODE", "off") or "off")


def email_project_truth_shadow_enabled() -> bool:
    return email_project_truth_mode() == "shadow"


def email_project_truth_serving_enabled() -> bool:
    return email_project_truth_mode() == "serve"


def email_project_truth_capture_enabled() -> bool:
    return email_project_truth_mode() in {"shadow", "serve"}


def start_email_project_truth_shadow_worker(
    *,
    queue_size: int | None = None,
    worker_count: int = 1,
    timeout_seconds: int | None = None,
    model_override: str | None = None,
) -> EmailProjectTruthShadowWorker | None:
    """Start the process-global worker only in explicit shadow mode."""
    from config import defaults

    if not email_project_truth_shadow_enabled():
        return None
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.health()["running"]:
            configured_model = getattr(
                defaults,
                "EMAIL_PROJECT_TRUTH_SHADOW_MODEL",
                "",
            )
            _worker = EmailProjectTruthShadowWorker(
                queue_size=queue_size
                or getattr(
                    defaults,
                    "EMAIL_PROJECT_TRUTH_SHADOW_QUEUE_CAPACITY",
                    128,
                ),
                worker_count=worker_count,
                timeout_seconds=timeout_seconds
                or getattr(
                    defaults,
                    "EMAIL_PROJECT_TRUTH_SHADOW_TIMEOUT_SECONDS",
                    20,
                ),
                model_override=(
                    model_override
                    if model_override is not None
                    else configured_model or None
                ),
            ).start()
        return _worker


def submit_email_project_truth_shadow(job: EmailProjectTruthShadowJob) -> bool:
    worker = _worker
    return False if worker is None else worker.enqueue(job)


def stop_email_project_truth_shadow_worker(timeout: float = 5.0) -> bool:
    global _worker
    with _worker_lock:
        worker = _worker
        if worker is None:
            return True
        stopped = worker.stop(timeout)
        if stopped:
            _worker = None
        return stopped


def email_project_truth_shadow_health() -> dict[str, Any]:
    from config import defaults

    worker = _worker
    if worker is not None:
        return worker.health()
    return {
        "mode": email_project_truth_mode(),
        "enabled": email_project_truth_shadow_enabled(),
        "serving_enabled": email_project_truth_serving_enabled(),
        "running": False,
        "worker_count": 0,
        "queue_depth": 0,
        "queue_capacity": int(
            getattr(defaults, "EMAIL_PROJECT_TRUTH_SHADOW_QUEUE_CAPACITY", 128)
        ),
        "queue_lag_ms": 0.0,
        "max_queue_lag_ms": 0.0,
        "enqueued": 0,
        "processed": 0,
        "completed": 0,
        "failed": 0,
        "dropped": 0,
        "errors": 0,
        "persistence_errors": 0,
        "last_error_class": None,
        "timeout_seconds": int(
            getattr(defaults, "EMAIL_PROJECT_TRUTH_SHADOW_TIMEOUT_SECONDS", 20)
        ),
    }


__all__ = [
    "EmailProjectTruthShadowWorker",
    "email_project_truth_capture_enabled",
    "email_project_truth_mode",
    "email_project_truth_serving_enabled",
    "email_project_truth_shadow_enabled",
    "email_project_truth_shadow_health",
    "start_email_project_truth_shadow_worker",
    "stop_email_project_truth_shadow_worker",
    "submit_email_project_truth_shadow",
]
