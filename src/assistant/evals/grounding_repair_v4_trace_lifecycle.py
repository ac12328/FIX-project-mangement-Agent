"""Trace-lifecycle validation for Policy-v4 holdout collection."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_ERROR_STATUSES = frozenset({"error", "failed", "failure"})
_ERROR_EXIT_MARKERS = ("error", "fail", "exception")


def _connect(database: Path) -> sqlite3.Connection:
    resolved = Path(database).resolve()
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _read_lifecycle(database: Path, run_id: str) -> dict[str, Any]:
    with _connect(database) as conn:
        events = conn.execute(
            "SELECT event_type, status FROM trace_events "
            "WHERE run_id=? ORDER BY sequence, created_at, event_id",
            (run_id,),
        ).fetchall()
        run = conn.execute(
            "SELECT status, exit_reason, ended_at FROM trace_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()

    event_types = [str(row["event_type"] or "") for row in events]
    llm_response_count = event_types.count("llm.response")
    llm_error_count = event_types.count("llm.error")
    round_end_count = event_types.count("round.end")
    round_error_count = event_types.count("round.error")
    run_finish_rows = [
        row for row in events if str(row["event_type"] or "") == "run.finish"
    ]
    run_status = str(run["status"] or "") if run is not None else ""
    exit_reason = str(run["exit_reason"] or "") if run is not None else ""
    run_event_error = any(
        str(row["event_type"] or "").startswith("run.")
        and str(row["status"] or "").casefold() in _ERROR_STATUSES
        for row in events
    )
    run_error = (
        run_event_error
        or run_status.casefold() in _ERROR_STATUSES
        or any(marker in exit_reason.casefold() for marker in _ERROR_EXIT_MARKERS)
    )
    terminal = bool(run_finish_rows) and bool(run is not None and run["ended_at"])
    complete = (
        terminal
        and llm_response_count >= 1
        and llm_error_count == 0
        and round_end_count >= 1
        and round_error_count == 0
        and not run_error
    )
    return {
        "complete": complete,
        "terminal": terminal,
        "llm_response_count": llm_response_count,
        "llm_error_count": llm_error_count,
        "round_end_count": round_end_count,
        "round_error_count": round_error_count,
        "run_finish_count": len(run_finish_rows),
        "run_error": run_error,
        "run_status": run_status,
        "exit_reason": exit_reason,
    }


def trace_lifecycle(
    database: Path, *, run_id: str, timeout_seconds: float
) -> dict[str, Any]:
    """Wait for a projected terminal run and return aggregate lifecycle evidence."""
    deadline = time.perf_counter() + max(0.0, float(timeout_seconds))
    lifecycle = _read_lifecycle(database, run_id)
    while not lifecycle["terminal"] and time.perf_counter() < deadline:
        time.sleep(0.1)
        lifecycle = _read_lifecycle(database, run_id)
    return lifecycle


def lifecycle_payload_valid(value: Any) -> bool:
    """Validate encrypted aggregate lifecycle evidence for a completed case."""
    if not isinstance(value, dict):
        return False
    return (
        value.get("complete") is True
        and value.get("terminal") is True
        and int(value.get("llm_response_count") or 0) >= 1
        and int(value.get("llm_error_count") or 0) == 0
        and int(value.get("round_end_count") or 0) >= 1
        and int(value.get("round_error_count") or 0) == 0
        and int(value.get("run_finish_count") or 0) >= 1
        and value.get("run_error") is False
    )


__all__ = ["lifecycle_payload_valid", "trace_lifecycle"]
