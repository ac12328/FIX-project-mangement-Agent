"""Asynchronous, encrypted engineering trace pipeline.

Producers only enqueue in-memory events. A background worker sanitizes payloads,
appends an encrypted spool record, projects structured data into SQLite, and
then acknowledges the spool entry. All failures are fail-open for callers.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import deque
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from src.observability.contracts import EngineeringTraceEvent, TraceExporter, TraceSink
from src.observability.payload_crypto import encrypt_payload, filter_secrets, validate_encryption_key
from src.observability.projection import project_record

logger = logging.getLogger(__name__)

WriteScopeFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def _percentile(values: deque[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


class AsyncTracePipeline(TraceSink):
    """Bounded non-blocking producer queue with durable encrypted recovery."""

    def __init__(
        self,
        *,
        spool_path: str | Path,
        queue_capacity: int = 10_000,
        full_payload_capture: bool = False,
        write_scope_factory: WriteScopeFactory | None = None,
        exporters: Iterable[TraceExporter] = (),
        retry_interval: float = 0.2,
        payload_retention_days: int = 30,
        detail_retention_days: int = 180,
    ) -> None:
        if full_payload_capture:
            validate_encryption_key()
        self.spool_path = Path(spool_path)
        self.ack_path = self.spool_path.with_suffix(self.spool_path.suffix + ".ack")
        self.full_payload_capture = full_payload_capture
        self.payload_retention_days = payload_retention_days
        self.detail_retention_days = detail_retention_days
        self._queue: queue.Queue[EngineeringTraceEvent] = queue.Queue(maxsize=max(1, queue_capacity))
        self._pending: deque[dict[str, Any]] = deque()
        self._write_scope_factory = write_scope_factory or self._default_write_scope
        self._exporters = tuple(exporters)
        self._retry_interval = max(0.01, retry_interval)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._condition = threading.Condition()
        self._spool_lock = threading.Lock()
        self._outstanding = 0
        self._started = False
        self._thread: threading.Thread | None = None
        self._emit_samples: deque[float] = deque(maxlen=2048)
        self._lag_samples: deque[float] = deque(maxlen=2048)
        self._metrics: dict[str, int] = {
            "enqueued": 0,
            "projected": 0,
            "queue_drops": 0,
            "spool_errors": 0,
            "encryption_errors": 0,
            "projection_errors": 0,
            "projection_recoveries": 0,
            "exporter_errors": 0,
            "recovered": 0,
            "duplicates": 0,
        }
        self._projection_consecutive_errors = 0
        self._last_projection_error_at: str | None = None
        self._last_projection_error_class: str | None = None
        self._last_projection_success_at: str | None = None
        self._exporter_health = {getattr(item, "name", type(item).__name__): "ok" for item in self._exporters}

    def _default_write_scope(self) -> AbstractContextManager[sqlite3.Connection]:
        from src.db.connection import scoped_observability_write

        return scoped_observability_write(timeout=0.05)

    def start(self) -> "AsyncTracePipeline":
        if self._started:
            return self
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self.compact_spool()
        self._recover()
        self._started = True
        self._thread = threading.Thread(target=self._worker, name="engineering-trace-writer", daemon=True)
        self._thread.start()
        return self

    def emit(self, event: EngineeringTraceEvent) -> bool:
        started = time.perf_counter()
        # Reserve the outstanding count before publishing the event to the
        # worker. Otherwise a fast worker can complete the event between
        # put_nowait() and the counter increment, leaving flush() stuck on a
        # phantom outstanding item. Roll the reservation back if the bounded
        # queue is full.
        with self._condition:
            self._outstanding += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._condition:
                self._outstanding = max(0, self._outstanding - 1)
                self._condition.notify_all()
            self._metrics["queue_drops"] += 1
            self._emit_samples.append((time.perf_counter() - started) * 1000)
            return False
        self._metrics["enqueued"] += 1
        self._emit_samples.append((time.perf_counter() - started) * 1000)
        self._wake.set()
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 0.1))
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop.set()
        self._wake.set()
        flushed = self.flush(max(0.0, deadline - time.monotonic()))
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return flushed and not (thread and thread.is_alive())

    def health(self) -> dict[str, Any]:
        oldest = self._pending[0].get("enqueued_at") if self._pending else None
        spool_lag_ms = max(0.0, (datetime.now(timezone.utc) - _parse_time(oldest)).total_seconds() * 1000) if oldest else 0.0
        return {
            **self._metrics,
            "enabled": True,
            "started": self._started,
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "queue_size": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "pending_events": len(self._pending),
            "outstanding_events": self._outstanding,
            "spool_lag_ms": round(spool_lag_ms, 3),
            "projection_failure_active": self._projection_consecutive_errors > 0,
            "projection_consecutive_errors": self._projection_consecutive_errors,
            "last_projection_error_at": self._last_projection_error_at,
            "last_projection_error_class": self._last_projection_error_class,
            "last_projection_success_at": self._last_projection_success_at,
            "emit_sample_count": len(self._emit_samples),
            "emit_p50_ms": _percentile(self._emit_samples, 0.50),
            "emit_p95_ms": _percentile(self._emit_samples, 0.95),
            "emit_p99_ms": _percentile(self._emit_samples, 0.99),
            "emit_max_ms": round(max(self._emit_samples, default=0.0), 3),
            "queue_lag_sample_count": len(self._lag_samples),
            "queue_lag_p50_ms": _percentile(self._lag_samples, 0.50),
            "queue_lag_p95_ms": _percentile(self._lag_samples, 0.95),
            "queue_lag_p99_ms": _percentile(self._lag_samples, 0.99),
            "queue_lag_max_ms": round(max(self._lag_samples, default=0.0), 3),
            "spool_path": str(self.spool_path),
            "exporters": dict(self._exporter_health),
        }

    def compact_spool(self) -> None:
        """Apply payload/detail retention with an atomic spool rewrite."""
        if not self.spool_path.exists():
            return
        now = datetime.now(timezone.utc)
        payload_cutoff = now - timedelta(days=max(0, self.payload_retention_days))
        detail_cutoff = now - timedelta(days=max(0, self.detail_retention_days))
        acked = self._read_acks()
        kept: list[dict[str, Any]] = []
        try:
            with self._spool_lock:
                for line in self.spool_path.read_text(encoding="utf-8").splitlines():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        self._metrics["spool_errors"] += 1
                        continue
                    created = _parse_time(record.get("created_at"))
                    if created < detail_cutoff and record.get("event_id") in acked:
                        continue
                    if created < payload_cutoff:
                        record["payload"] = None
                    kept.append(record)
                tmp = self.spool_path.with_suffix(self.spool_path.suffix + ".tmp")
                self._write_lines(tmp, kept)
                os.replace(tmp, self.spool_path)
                live_ids = {record.get("event_id") for record in kept}
                self._write_lines(self.ack_path, sorted(event_id for event_id in acked if event_id in live_ids))
        except OSError:
            self._metrics["spool_errors"] += 1
            logger.debug("Trace spool compaction failed", exc_info=True)

    def _worker(self) -> None:
        next_retry = 0.0
        while not self._stop.is_set() or not self._queue.empty() or self._pending:
            consumed = self._consume_one()
            now = time.monotonic()
            if self._pending and now >= next_retry:
                record = self._pending[0]
                if self._project(record):
                    self._pending.popleft()
                    self._ack(record)
                    self._export(record)
                    self._complete_one()
                    next_retry = 0.0
                else:
                    next_retry = now + self._retry_interval
            if not consumed and (not self._pending or now < next_retry):
                wait_for = min(0.1, max(0.01, next_retry - now)) if self._pending else 0.1
                self._wake.wait(wait_for)
                self._wake.clear()

    def _consume_one(self) -> bool:
        try:
            event = self._queue.get_nowait()
        except queue.Empty:
            return False
        try:
            record = self._prepare_record(event)
            self._append_spool(record)
            self._pending.append(record)
        except Exception:
            self._metrics["spool_errors"] += 1
            logger.debug("Engineering trace event could not be spooled", exc_info=True)
            self._complete_one()
        finally:
            self._queue.task_done()
        return True

    def _prepare_record(self, event: EngineeringTraceEvent) -> dict[str, Any]:
        raw = event.model_dump(exclude={"payload"})
        raw["metadata"] = filter_secrets(raw.get("metadata") or {})
        envelope = None
        if self.full_payload_capture and event.payload is not None:
            try:
                envelope = encrypt_payload(event.payload)
            except Exception:
                self._metrics["encryption_errors"] += 1
                raise
        return {
            "record_version": 1,
            "event_id": event.event_id,
            "created_at": event.timestamp,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "event": raw,
            "payload": envelope,
        }

    def _append_spool(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._spool_lock:
            with self.spool_path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _project(self, record: dict[str, Any]) -> bool:
        try:
            with self._write_scope_factory() as conn:
                duplicate = project_record(conn, record)
            self._metrics["duplicates" if duplicate else "projected"] += 1
            if self._projection_consecutive_errors:
                self._metrics["projection_recoveries"] += 1
                self._projection_consecutive_errors = 0
            self._last_projection_success_at = datetime.now(timezone.utc).isoformat()
            lag = (datetime.now(timezone.utc) - _parse_time(record.get("enqueued_at"))).total_seconds() * 1000
            self._lag_samples.append(max(0.0, lag))
            return True
        except Exception as exc:
            self._metrics["projection_errors"] += 1
            self._projection_consecutive_errors += 1
            self._last_projection_error_at = datetime.now(timezone.utc).isoformat()
            self._last_projection_error_class = type(exc).__name__
            logger.debug("Trace projection deferred", exc_info=True)
            return False

    def _ack(self, record: dict[str, Any]) -> None:
        try:
            with self._spool_lock:
                with self.ack_path.open("a", encoding="utf-8") as handle:
                    handle.write(str(record["event_id"]) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError:
            self._metrics["spool_errors"] += 1

    def _export(self, record: dict[str, Any]) -> None:
        for exporter in self._exporters:
            name = getattr(exporter, "name", type(exporter).__name__)
            try:
                exporter.export(record)
                self._exporter_health[name] = "ok"
            except Exception:
                self._metrics["exporter_errors"] += 1
                self._exporter_health[name] = "error"
                logger.debug("Trace exporter %s failed", name, exc_info=True)

    def _complete_one(self) -> None:
        with self._condition:
            self._outstanding = max(0, self._outstanding - 1)
            self._condition.notify_all()

    def _read_acks(self) -> set[str]:
        if not self.ack_path.exists():
            return set()
        try:
            return {line.strip() for line in self.ack_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        except OSError:
            self._metrics["spool_errors"] += 1
            return set()

    def _recover(self) -> None:
        if not self.spool_path.exists():
            return
        acked = self._read_acks()
        try:
            for line in self.spool_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    self._metrics["spool_errors"] += 1
                    continue
                if record.get("event_id") in acked:
                    continue
                self._pending.append(record)
                self._outstanding += 1
                self._metrics["recovered"] += 1
        except OSError:
            self._metrics["spool_errors"] += 1

    @staticmethod
    def _write_lines(path: Path, values: Iterable[Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for value in values:
                if isinstance(value, dict):
                    handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
                else:
                    handle.write(str(value))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())



class _NoopTraceSink(TraceSink):
    def emit(self, event: EngineeringTraceEvent) -> bool:
        return False

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"started": False, "enabled": False, "queue_drops": 0}


_pipeline_lock = threading.Lock()
_pipeline: AsyncTracePipeline | None = None
_noop = _NoopTraceSink()


def start_trace_pipeline(*, exporters: Iterable[TraceExporter] | None = None) -> TraceSink:
    global _pipeline
    from config.settings import CONFIG, DB_PATH

    if not CONFIG.observability.enabled:
        return _noop
    with _pipeline_lock:
        if _pipeline is None:
            spool = getattr(CONFIG.observability, "spool_path", None) or f"{DB_PATH}.engineering-traces.jsonl"
            if exporters is None:
                from src.observability.otel_exporter import OpenTelemetryEventExporter

                exporters = (OpenTelemetryEventExporter(),)
            _pipeline = AsyncTracePipeline(
                spool_path=spool,
                queue_capacity=getattr(CONFIG.observability, "queue_capacity", 10_000),
                full_payload_capture=CONFIG.observability.full_payload_capture,
                exporters=exporters,
                retry_interval=getattr(CONFIG.observability, "retry_interval_seconds", 0.2),
                payload_retention_days=CONFIG.observability.payload_retention_days,
                detail_retention_days=CONFIG.observability.detail_retention_days,
            ).start()
    return _pipeline


def get_trace_sink() -> TraceSink:
    return _pipeline or _noop


def emit_trace_event(event: EngineeringTraceEvent) -> bool:
    try:
        return get_trace_sink().emit(event)
    except Exception:
        logger.debug("Engineering trace emit failed open", exc_info=True)
        return False


def flush_trace_pipeline(timeout: float = 5.0) -> bool:
    return get_trace_sink().flush(timeout)


def stop_trace_pipeline(timeout: float = 5.0) -> bool:
    global _pipeline
    with _pipeline_lock:
        pipeline = _pipeline
        if pipeline is None:
            return True
        stopped = pipeline.stop(timeout)
        if stopped:
            _pipeline = None
        return stopped


def trace_pipeline_health() -> dict[str, Any]:
    return get_trace_sink().health()


def compact_trace_spool() -> None:
    pipeline = _pipeline
    if pipeline is not None:
        pipeline.compact_spool()
