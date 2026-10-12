"""
Agent Observability — LLM trace records and error classification (Plan Step 2.16).
Every LLM call gets a structured trace record in llm_traces table.
Error classification: infra (retry), schema (escalate), guard (log), timeout (watchdog).
"""

import hashlib
import json
import sqlite3
import time
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def hash_prompt(content: str) -> str:
    """Compute a short SHA256 hex digest of a prompt string for trace fingerprinting."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def encode_guard_results(results: list[dict]) -> str:
    """Serialize guard results list to JSON string for storage."""
    return json.dumps(results, ensure_ascii=False)


@dataclass
class TraceRecord:
    """Structured trace for a single LLM call."""
    trace_id: str
    task: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    output_valid: bool = True
    error_class: str | None = None  # none | infra | schema | guard | timeout
    error_message: str | None = None
    decision_outcome: str | None = None
    prompt_hash: str | None = None  # SHA256 of input prompt
    per_guard_results: str | None = None  # JSON array of guard outcomes
    trace_metadata: str | None = None  # JSON metadata (e.g. active rule IDs)
    run_id: str | None = None
    created_at: str = ""


def record_trace(conn: sqlite3.Connection, trace: TraceRecord) -> str:
    """Insert a trace record into the llm_traces table. Returns trace_id."""
    if not trace.created_at:
        trace.created_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO llm_traces
           (trace_id, task, model, tokens_in, tokens_out, latency_ms,
            output_valid, error_class, error_message, decision_outcome,
            prompt_hash, per_guard_results, trace_metadata, created_at, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trace.trace_id, trace.task, trace.model,
            trace.tokens_in, trace.tokens_out, trace.latency_ms,
            1 if trace.output_valid else 0,
            trace.error_class, trace.error_message,
            trace.decision_outcome,
            trace.prompt_hash, trace.per_guard_results,
            trace.trace_metadata,
            trace.created_at, trace.run_id,
        ),
    )
    conn.commit()
    return trace.trace_id


def get_recent_traces(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict]:
    """Get recent trace records."""
    rows = conn.execute(
        "SELECT * FROM llm_traces ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_error_traces(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict]:
    """Get recent error traces."""
    rows = conn.execute(
        """SELECT * FROM llm_traces
           WHERE error_class IS NOT NULL AND error_class != 'none'
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict | None:
    """Fetch one trace by ID."""
    row = conn.execute(
        "SELECT * FROM llm_traces WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()
    return dict(row) if row else None


def get_trace_linked_messages(
    conn: sqlite3.Connection,
    trace_id: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """Return Telegram messages linked to one LLM trace."""
    rows = conn.execute(
        """SELECT DISTINCT m.*
           FROM telegram_messages m
           JOIN message_object_map map
             ON map.chat_id = m.chat_id
            AND map.telegram_message_id = m.telegram_message_id
           WHERE map.object_type = 'llm_trace'
             AND map.object_ref = ?
           ORDER BY m.created_at DESC
           LIMIT ?""",
        (trace_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_traces_for_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return LLM traces linked to a project (audit P2-6).

    Joins llm_traces → message_object_map(object_type='llm_trace') →
    telegram_messages.project_id, so an operator/engineer can query
    "what did the bot do on project X" without a denormalized project_id
    column on llm_traces.
    """
    rows = conn.execute(
        """SELECT DISTINCT t.*
           FROM llm_traces t
           JOIN message_object_map map
             ON map.object_type = 'llm_trace' AND map.object_ref = t.trace_id
           JOIN telegram_messages m
             ON m.chat_id = map.chat_id
            AND m.telegram_message_id = map.telegram_message_id
           WHERE m.project_id = ?
           ORDER BY t.created_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def classify_error(error: Exception) -> str:
    """
    Classify an error into one of the standard categories.
    L1 infra: network/API errors → retry
    L2 schema: invalid output format → escalate
    L3 guard: guard rejection → log
    L4 timeout: call took too long → watchdog
    """
    error_str = str(type(error).__name__).lower()
    error_msg = str(error).lower()

    if "timeout" in error_str or "timeout" in error_msg:
        return "timeout"
    if "json" in error_str or "validation" in error_str or "schema" in error_msg:
        return "schema"
    if "guard" in error_str or "guard" in error_msg or "rejected" in error_msg:
        return "guard"
    # Default: infrastructure (network, auth, rate limit, etc.)
    return "infra"


class TracedLLMCall:
    """
    Context manager that wraps an LLM call with tracing.

    Usage:
        with TracedLLMCall(conn, task="draft", model="gpt-4o") as t:
            result = litellm.completion(...)
            t.tokens_in = result.usage.prompt_tokens
            t.tokens_out = result.usage.completion_tokens
    """

    def __init__(self, conn: sqlite3.Connection, task: str, model: str):
        self.conn = conn
        self.trace = TraceRecord(
            trace_id=f"tr_{uuid.uuid4().hex[:12]}",
            task=task,
            model=model,
        )
        self._start_time: float = 0.0

    def __enter__(self):
        self._start_time = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.monotonic() - self._start_time
        self.trace.latency_ms = int(elapsed * 1000)

        if exc_type is not None:
            self.trace.error_class = classify_error(exc_val)
            self.trace.error_message = str(exc_val)[:500]
            self.trace.output_valid = False

        try:
            record_trace(self.conn, self.trace)
        except Exception:
            logger.exception("Failed to record LLM trace")

        return False  # Don't suppress exceptions

    @property
    def tokens_in(self) -> int:
        return self.trace.tokens_in

    @tokens_in.setter
    def tokens_in(self, value: int):
        self.trace.tokens_in = value

    @property
    def tokens_out(self) -> int:
        return self.trace.tokens_out

    @tokens_out.setter
    def tokens_out(self, value: int):
        self.trace.tokens_out = value

    @property
    def output_valid(self) -> bool:
        return self.trace.output_valid

    @output_valid.setter
    def output_valid(self, value: bool):
        self.trace.output_valid = value
