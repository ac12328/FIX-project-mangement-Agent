"""Retention cleanup for observability data."""
from __future__ import annotations

import sqlite3


def cleanup_traces(
    conn: sqlite3.Connection,
    payload_days: int = 30,
    detail_days: int = 180,
) -> dict[str, int]:
    payload = conn.execute(
        """UPDATE trace_events
           SET payload_ciphertext=NULL,payload_nonce=NULL,payload_key_version=NULL,payload_hash=NULL
           WHERE payload_ciphertext IS NOT NULL AND created_at < datetime('now', ?)""",
        (f"-{payload_days} days",),
    ).rowcount
    diagnostics = conn.execute(
        "DELETE FROM trace_diagnostics WHERE created_at < datetime('now', ?)",
        (f"-{detail_days} days",),
    ).rowcount
    events = conn.execute(
        "DELETE FROM trace_events WHERE created_at < datetime('now', ?)",
        (f"-{detail_days} days",),
    ).rowcount
    runs = conn.execute(
        "DELETE FROM trace_runs WHERE started_at < datetime('now', ?)",
        (f"-{detail_days} days",),
    ).rowcount
    llm = conn.execute(
        "DELETE FROM llm_traces WHERE created_at < datetime('now', ?)",
        (f"-{detail_days} days",),
    ).rowcount
    try:
        from src.observability.pipeline import compact_trace_spool

        compact_trace_spool()
    except Exception:
        pass
    return {
        "payloads_cleared": payload,
        "diagnostics_deleted": diagnostics,
        "events_deleted": events,
        "runs_deleted": runs,
        "llm_traces_deleted": llm,
    }
