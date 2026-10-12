"""Run-oriented observability for every LLM workload."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

_current_run: ContextVar[str | None] = ContextVar("trace_run_id", default=None)
_event_sequence: ContextVar[int] = ContextVar("trace_event_sequence", default=0)
_diagnosis_suppressed: ContextVar[bool] = ContextVar("trace_diagnosis_suppressed", default=False)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str | None:
    return _current_run.get()


def diagnosis_suppressed() -> bool:
    return _diagnosis_suppressed.get()


def bind_run(run_id: str):
    """Bind an existing run to the current execution context."""
    return _current_run.set(run_id), _event_sequence.set(0)


def unbind_run(tokens) -> None:
    run_token, seq_token = tokens
    _current_run.reset(run_token)
    _event_sequence.reset(seq_token)


def start_run(conn: sqlite3.Connection, *, surface: str, task: str, project_id: str | None = None,
              session_id: str | None = None, chat_id: int | None = None, operator_id: str | None = None,
              metadata: dict[str, Any] | None = None, implicit: bool = False) -> str:
    run_id = f"run_{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO trace_runs
        (run_id,surface,task,project_id,session_id,chat_id,operator_id,status,started_at,metadata_json,implicit)
        VALUES (?,?,?,?,?,?,?,'running',?,?,?)""",
        (run_id, surface, task, project_id, session_id, chat_id, operator_id, utcnow(),
         json.dumps(metadata or {}, ensure_ascii=False, default=str), int(implicit)))
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, *, status: str = "completed",
               exit_reason: str | None = None, anomaly_flags: list[str] | None = None) -> None:
    conn.execute("""UPDATE trace_runs SET status=?, exit_reason=?, anomaly_flags_json=?, ended_at=?,
        duration_ms=CAST((julianday(?) - julianday(started_at))*86400000 AS INTEGER) WHERE run_id=?""",
        (status, exit_reason, json.dumps(anomaly_flags or []), utcnow(), utcnow(), run_id))


def record_event(conn: sqlite3.Connection, *, event_type: str, status: str = "ok", round_index: int | None = None,
                 llm_trace_id: str | None = None, tool_name: str | None = None, duration_ms: int | None = None,
                 metadata: dict[str, Any] | None = None, payload: Any = None, run_id: str | None = None) -> str | None:
    rid = run_id or current_run_id()
    if not rid:
        return None
    seq = _event_sequence.get() + 1
    _event_sequence.set(seq)
    envelope: dict[str, str | None] = {"ciphertext": None, "nonce": None, "key_version": None, "payload_hash": None}
    if payload is not None:
        from config.settings import CONFIG
        if CONFIG.observability.full_payload_capture:
            from src.observability.payload_crypto import encrypt_payload
            envelope.update(encrypt_payload(payload))
    event_id = f"evt_{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO trace_events
        (event_id,run_id,sequence,event_type,round_index,llm_trace_id,tool_name,status,duration_ms,metadata_json,
         payload_ciphertext,payload_nonce,payload_key_version,payload_hash,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id,rid,seq,event_type,round_index,llm_trace_id,tool_name,status,duration_ms,
         json.dumps(metadata or {}, ensure_ascii=False, default=str), envelope["ciphertext"], envelope["nonce"],
         envelope["key_version"], envelope["payload_hash"], utcnow()))
    return event_id


@contextmanager
def run_scope(conn: sqlite3.Connection, **kwargs: Any) -> Iterator[str]:
    run_id = start_run(conn, **kwargs)
    token = _current_run.set(run_id)
    seq_token = _event_sequence.set(0)
    try:
        yield run_id
    except Exception as exc:
        finish_run(conn, run_id, status="error", exit_reason=type(exc).__name__, anomaly_flags=["error"])
        raise
    finally:
        _current_run.reset(token)
        _event_sequence.reset(seq_token)


def ensure_run_for_llm(conn: sqlite3.Connection, task: str) -> tuple[str, bool]:
    rid = current_run_id()
    if rid:
        return rid, False
    return start_run(conn, surface="llm", task=task, implicit=True), True


def suppress_diagnosis():
    @contextmanager
    def _scope():
        token = _diagnosis_suppressed.set(True)
        try:
            yield
        finally:
            _diagnosis_suppressed.reset(token)
    return _scope()
