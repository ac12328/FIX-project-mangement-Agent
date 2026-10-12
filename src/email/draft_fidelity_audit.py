"""Redacted trace and write-scope helpers for instruction-fidelity drafts."""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, ContextManager, Iterator

logger = logging.getLogger(__name__)
WriteConnectionFactory = Callable[[], ContextManager[sqlite3.Connection]]


def _connection_is_query_only(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA query_only").fetchone()
        return bool(int(row[0])) if row is not None else False
    except (AttributeError, IndexError, TypeError, ValueError, sqlite3.Error):
        return False


@contextmanager
def draft_write_connection(
    conn: sqlite3.Connection,
    execution_context: dict[str, Any],
) -> Iterator[sqlite3.Connection]:
    """Yield a writer only for a short draft persistence or trace phase."""
    if not _connection_is_query_only(conn):
        yield conn
        return
    writer_factory: WriteConnectionFactory | None = execution_context.get(
        "write_connection_factory"
    )
    if writer_factory is None:
        from src.db.connection import scoped_write

        writer_factory = scoped_write
    with writer_factory() as wconn:
        yield wconn


def _email_domain(email: str | None) -> str | None:
    value = (email or "").strip().casefold()
    return value.rsplit("@", 1)[-1] if "@" in value else None


def _issue_code(issue: str) -> str:
    lowered = (issue or "").casefold()
    prefixes = {
        "missing required disable": "missing_disable_action",
        "missing required enable": "missing_enable_action",
        "missing named target": "missing_named_target",
        "missing ip target": "missing_ip_target",
        "missing port target": "missing_port_target",
        "missing endpoint target": "missing_endpoint_target",
        "missing session target": "missing_session_target",
    }
    for prefix, code in prefixes.items():
        if lowered.startswith(prefix):
            return code
    normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return normalized[:64] or "validation_issue"


def _redacted_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for attempt in attempts:
        fidelity_issues = list(attempt.get("fidelity_issues") or ())
        content_issues = list(attempt.get("content_issues") or ())
        redacted.append({
            "attempt": attempt.get("attempt"),
            "fidelity_issue_codes": sorted({_issue_code(value) for value in fidelity_issues}),
            "fidelity_issue_count": len(fidelity_issues),
            "content_issue_codes": sorted({_issue_code(value) for value in content_issues}),
            "content_issue_count": len(content_issues),
        })
    return redacted


def record_draft_fidelity_event(
    conn: sqlite3.Connection,
    *,
    status: str,
    spec: Any,
    attempts: list[dict[str, Any]],
    thread_mode: str | None,
    to_email: str | None,
    execution_context: dict[str, Any],
    draft_id: str | None = None,
) -> None:
    """Record a best-effort trace without raw instructions, targets, or addresses."""
    try:
        from src.observability.run_tracing import current_run_id, record_event

        if not current_run_id():
            return
        metadata = {
            **spec.audit_metadata(),
            "attempts": _redacted_attempts(attempts),
            "resolved_thread_mode": thread_mode,
            "recipient_domains": [
                domain for domain in (_email_domain(to_email),) if domain
            ],
            "tool_call_id": execution_context.get("tool_call_id"),
            "draft_id": draft_id,
        }
        with draft_write_connection(conn, execution_context) as wconn:
            record_event(
                wconn,
                event_type="draft_instruction_fidelity",
                status=status,
                tool_name="draft_reply",
                metadata=metadata,
            )
    except Exception:
        logger.exception("Failed to record draft instruction fidelity event")


def fidelity_failure_result(
    conn: sqlite3.Connection,
    *,
    spec: Any,
    attempts: list[dict[str, Any]],
    thread_mode: str,
    to_email: str | None,
    execution_context: dict[str, Any],
    issues: list[str],
) -> dict[str, Any]:
    """Record and return a no-write result after deterministic retry failure."""
    record_draft_fidelity_event(
        conn,
        status="instruction_fidelity_failed",
        spec=spec,
        attempts=attempts,
        thread_mode=thread_mode,
        to_email=to_email,
        execution_context=execution_context,
    )
    return {
        "status": "instruction_fidelity_failed",
        "error": "instruction_fidelity_failed",
        "issues": issues,
        "message": (
            "The draft still weakened or omitted the operator instruction after one retry. "
            "No draft was created."
        ),
    }


def clarification_result(
    conn: sqlite3.Connection,
    *,
    spec: Any,
    attempts: list[dict[str, Any]],
    thread_mode: str,
    to_email: str | None,
    execution_context: dict[str, Any],
) -> dict[str, Any]:
    """Record and return a no-write result for unresolved required targets."""
    record_draft_fidelity_event(
        conn,
        status="clarification_required",
        spec=spec,
        attempts=attempts,
        thread_mode=thread_mode,
        to_email=to_email,
        execution_context=execution_context,
    )
    return {
        "status": "clarification_required",
        "error": "clarification_required",
        "unresolved_targets": list(spec.unresolved_targets),
        "message": (
            "The requested access-control action is clear, but required target details "
            "could not be resolved safely. No draft was created."
        ),
    }
