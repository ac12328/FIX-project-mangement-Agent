"""Trace diagnosis and anomaly classification."""
from __future__ import annotations
import json
import sqlite3
import uuid
from typing import Any
from src.observability.run_tracing import utcnow


def anomaly_flags(*, error_class: str | None, output_valid: bool, latency_ms: int) -> list[str]:
    flags: list[str] = []
    if error_class and error_class != "none": flags.append(error_class)
    if not output_valid: flags.append("invalid_output")
    if latency_ms >= 30000: flags.append("abnormal_latency")
    return flags


def diagnose_run(conn: sqlite3.Connection, run_id: str, trigger: str = "manual") -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM trace_diagnostics WHERE run_id=? AND trigger=?", (run_id, trigger)).fetchone()
    if existing: return dict(existing)
    run = conn.execute("SELECT * FROM trace_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run: raise KeyError(run_id)
    events = [dict(r) for r in conn.execute("SELECT * FROM trace_events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()]
    errors = [e for e in events if e.get("status") not in ("ok", "completed", "running")]
    flags = json.loads(run["anomaly_flags_json"] or "[]")
    severity = "high" if errors or any(f in flags for f in ("infra","timeout","loop_exhaustion")) else "medium" if flags else "low"
    root = errors[-1].get("event_type") if errors else (flags[0] if flags else "No anomaly identified")
    summary = f"Run contains {len(events)} events, {len(errors)} failed events, and {len(flags)} anomaly flags."
    recommendations = ["Inspect the failing event payload and linked LLM trace."] if errors else ["No immediate remediation is required."]
    diagnostic_id = f"diag_{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO trace_diagnostics
      (diagnostic_id,run_id,trigger,status,severity,root_cause,summary,evidence_json,recommendations_json,created_at,completed_at)
      VALUES (?,?,?,'completed',?,?,?,?,?,?,?)""",
      (diagnostic_id,run_id,trigger,severity,root,summary,json.dumps(errors[-5:]),json.dumps(recommendations),utcnow(),utcnow()))
    return dict(conn.execute("SELECT * FROM trace_diagnostics WHERE diagnostic_id=?", (diagnostic_id,)).fetchone())
