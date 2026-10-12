"""Sanitized engineering and LLM trace inspection endpoints."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_conn
from src.auth.repository import record_security_event
from src.observability.trace_api_support import (
    SESSION_HEALTH_FIELDS,
    TOOL_CENTRIC_SHADOW_HEALTH_FIELDS,
    EMAIL_PROJECT_TRUTH_SHADOW_HEALTH_FIELDS,
    TRACE_HEALTH_FIELDS,
    event_payload_envelope,
    sanitize_classic_trace,
    sanitize_diagnostic,
    sanitize_event,
    sanitize_export_run,
    sanitize_feedback,
    sanitize_llm_export,
    sanitize_run,
    select_fields,
)
from src.observability.traces import (
    get_error_traces,
    get_recent_traces,
    get_trace,
    get_traces_for_project,
)

router = APIRouter(tags=["traces"])


def _audit_reveal(
    conn: sqlite3.Connection,
    *,
    request: Request,
    actor: CurrentActor,
    event_type: str,
    action_type: str,
    object_ref: str,
    summary: str,
    metadata: dict[str, Any],
) -> None:
    client_host = request.client.host if request.client else None
    record_security_event(
        conn,
        event_type,
        severity="warning",
        actor_user_id=actor.user_id,
        session_id=actor.session_id,
        ip_address=client_host,
        user_agent=request.headers.get("user-agent"),
        metadata=metadata,
    )
    conn.execute(
        """
        INSERT INTO activity_log(
            project_id,actor,surface,action_type,object_ref,summary,metadata_json,created_at
        ) VALUES (?, ?, 'dashboard', ?, ?, ?, ?, datetime('now'))
        """,
        (
            None,
            actor.user_id,
            action_type,
            object_ref,
            summary,
            json.dumps({"audited": True, **metadata}),
        ),
    )


def _decrypt_event_payload(row: Mapping[str, Any]) -> Any:
    from src.observability.payload_crypto import decrypt_payload

    try:
        return decrypt_payload(
            str(row["payload_ciphertext"]),
            str(row["payload_nonce"]),
            str(row.get("payload_key_version") or "v1"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Trace payload decryption is unavailable",
        ) from exc


@router.get("/traces")
def list_traces(
    limit: int = Query(50, ge=1, le=200),
    errors_only: bool = Query(False),
    task: str | None = Query(None),
    project_id: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    if project_id:
        rows = get_traces_for_project(conn, project_id, limit=limit)
        if task:
            rows = [row for row in rows if row.get("task") == task]
        if errors_only:
            rows = [
                row
                for row in rows
                if row.get("error_class") and row["error_class"] != "none"
            ]
    else:
        rows = (
            get_error_traces(conn, limit=limit)
            if errors_only
            else get_recent_traces(conn, limit=limit)
        )
        if task:
            rows = [row for row in rows if row.get("task") == task]
    return [sanitize_classic_trace(row) for row in rows]


@router.get("/traces/runs")
def list_trace_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    surface: str | None = None,
    task: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
    model: str | None = None,
    exit_reason: str | None = None,
    q: str | None = Query(None, max_length=200),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    errors_only: bool = False,
    severity: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("r.surface", surface),
        ("r.task", task),
        ("r.status", status),
        ("r.exit_reason", exit_reason),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if project_id:
        clauses.append(
            "(r.project_id = ? OR EXISTS (SELECT 1 FROM json_each("
            "CASE WHEN json_valid(r.metadata_json) THEN r.metadata_json ELSE '{}' END, "
            "'$.effective_project_ids') WHERE value = ?))"
        )
        params.extend([project_id, project_id])
    if model:
        clauses.append(
            "EXISTS (SELECT 1 FROM llm_traces l WHERE l.run_id=r.run_id AND l.model=?)"
        )
        params.append(model)
    if q:
        clauses.append(
            "(" + " OR ".join(
                f"COALESCE({column}, '') LIKE ? COLLATE NOCASE"
                for column in (
                    "r.run_id", "r.trace_id", "r.turn_id", "r.thread_id",
                    "r.surface", "r.task", "r.project_id", "r.status",
                    "r.exit_reason", "r.component",
                )
            ) + ")"
        )
        params.extend([f"%{q}%"] * 10)
    if from_date is not None:
        clauses.append("datetime(r.started_at) >= datetime(?)")
        params.append(from_date.astimezone(timezone.utc).isoformat())
    if to_date is not None:
        clauses.append("datetime(r.started_at) <= datetime(?)")
        params.append(to_date.astimezone(timezone.utc).isoformat())
    if errors_only:
        clauses.append("(r.status = 'error' OR r.anomaly_flags_json NOT IN ('[]',''))")
    if severity:
        clauses.append(
            "EXISTS (SELECT 1 FROM trace_diagnostics d "
            "WHERE d.run_id=r.run_id AND d.severity=?)"
        )
        params.append(severity)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT r.* FROM trace_runs r{where} "
        "ORDER BY r.started_at DESC, r.run_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [sanitize_run(dict(row)) for row in rows]


def _get_run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    run = conn.execute("SELECT * FROM trace_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Trace run not found")
    return run


def _get_event_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trace_events WHERE run_id=? ORDER BY sequence, created_at, event_id",
        (run_id,),
    ).fetchall()


def _get_feedback_rows(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
) -> list[sqlite3.Row]:
    """Return feedback linked by any durable run identity."""
    clauses = ["run_id=?"]
    params: list[str] = [str(run["run_id"])]
    for column in ("trace_id", "turn_id"):
        value = run[column]
        if value:
            clauses.append(f"{column}=?")
            params.append(str(value))
    return conn.execute(
        "SELECT * FROM ai_feedback WHERE "
        + " OR ".join(clauses)
        + " ORDER BY created_at, feedback_id",
        params,
    ).fetchall()


@router.get("/traces/runs/{run_id}")
def get_trace_run(
    run_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    run = _get_run_row(conn, run_id)
    event_rows = _get_event_rows(conn, run_id)
    diagnostic_rows = conn.execute(
        "SELECT * FROM trace_diagnostics WHERE run_id=? ORDER BY created_at DESC",
        (run_id,),
    ).fetchall()
    llm_trace_rows = conn.execute(
        "SELECT * FROM llm_traces WHERE run_id=? ORDER BY created_at, trace_id",
        (run_id,),
    ).fetchall()
    feedback_rows = _get_feedback_rows(conn, run)
    return {
        **sanitize_run(dict(run)),
        "events": [sanitize_event(dict(row)) for row in event_rows],
        "diagnostics": [
            sanitize_diagnostic(dict(row)) for row in diagnostic_rows
        ],
        "llm_traces": [
            sanitize_classic_trace(dict(row)) for row in llm_trace_rows
        ],
        "feedback": [sanitize_feedback(dict(row)) for row in feedback_rows],
    }


@router.get("/traces/runs/{run_id}/export")
def export_trace_run(
    run_id: str,
    response: Response,
    include_encrypted_payloads: bool = Query(True),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Export a reconstructible bundle without decrypting any payload."""
    run = _get_run_row(conn, run_id)
    event_rows = _get_event_rows(conn, run_id)
    diagnostics = conn.execute(
        "SELECT * FROM trace_diagnostics WHERE run_id=? ORDER BY created_at, diagnostic_id",
        (run_id,),
    ).fetchall()
    llm_traces = conn.execute(
        "SELECT * FROM llm_traces WHERE run_id=? ORDER BY created_at, trace_id",
        (run_id,),
    ).fetchall()
    feedback_rows = _get_feedback_rows(conn, run)
    events: list[dict[str, Any]] = []
    for row in event_rows:
        raw = dict(row)
        event = sanitize_event(raw)
        if include_encrypted_payloads:
            event["payload_envelope"] = event_payload_envelope(raw)
        events.append(event)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = f'attachment; filename="trace-{run_id}.json"'
    return {
        "bundle_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "payload_mode": "encrypted" if include_encrypted_payloads else "omitted",
        "run": sanitize_export_run(dict(run)),
        "events": events,
        "llm_traces": [sanitize_llm_export(dict(row)) for row in llm_traces],
        "diagnostics": [sanitize_diagnostic(dict(row)) for row in diagnostics],
        "feedback": [sanitize_feedback(dict(row)) for row in feedback_rows],
    }


@router.post("/traces/runs/{run_id}/diagnose")
def diagnose_trace_run(
    run_id: str,
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, Any]:
    from src.observability.diagnostics import diagnose_run

    try:
        return sanitize_diagnostic(diagnose_run(conn, run_id, "manual"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Trace run not found") from exc


@router.post("/traces/runs/{run_id}/reveal")
def reveal_trace_run(
    run_id: str,
    request: Request,
    response: Response,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, Any]:
    run = _get_run_row(conn, run_id)
    event_rows = _get_event_rows(conn, run_id)
    available = [dict(row) for row in event_rows if row["payload_ciphertext"]]
    if not available:
        raise HTTPException(status_code=410, detail="Run payloads unavailable or expired")
    events = [
        {**sanitize_event(row), "payload": _decrypt_event_payload(row)}
        for row in available
    ]
    _audit_reveal(
        conn,
        request=request,
        actor=actor,
        event_type="trace.run_payloads_revealed",
        action_type="trace_run_payload_reveal",
        object_ref=run_id,
        summary="Revealed encrypted trace run payloads",
        metadata={"run_id": run_id, "event_count": len(events)},
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "run": sanitize_export_run(dict(run)),
        "events": events,
    }


@router.post("/traces/events/{event_id}/reveal")
def reveal_trace_event(
    event_id: str,
    request: Request,
    response: Response,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM trace_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trace event not found")
    raw = dict(row)
    if not raw["payload_ciphertext"]:
        raise HTTPException(status_code=410, detail="Payload unavailable or expired")
    payload = _decrypt_event_payload(raw)
    _audit_reveal(
        conn,
        request=request,
        actor=actor,
        event_type="trace.payload_revealed",
        action_type="trace_payload_reveal",
        object_ref=event_id,
        summary="Revealed encrypted trace event payload",
        metadata={"event_id": event_id, "run_id": raw["run_id"]},
    )
    response.headers["Cache-Control"] = "no-store"
    return {"event_id": event_id, "payload": payload}


@router.get("/traces/health")
def get_trace_health(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    from src.auth.session_touch import session_touch_health
    from src.assistant.tool_centric_shadow_worker import tool_centric_shadow_health
    from src.email.project_truth.shadow_worker import email_project_truth_shadow_health
    from src.observability.pipeline import trace_pipeline_health

    trace_health = trace_pipeline_health()
    session_health = session_touch_health()
    shadow_health = tool_centric_shadow_health()
    email_shadow_health = email_project_truth_shadow_health()
    trace_view = select_fields(trace_health, TRACE_HEALTH_FIELDS)
    session_view = select_fields(session_health, SESSION_HEALTH_FIELDS)
    shadow_view = select_fields(shadow_health, TOOL_CENTRIC_SHADOW_HEALTH_FIELDS)
    email_shadow_view = select_fields(
        email_shadow_health,
        EMAIL_PROJECT_TRUTH_SHADOW_HEALTH_FIELDS,
    )
    from src.email.project_truth.shadow_repository import (
        summarize_email_project_truth_shadow,
    )

    email_shadow_view["observations"] = summarize_email_project_truth_shadow(conn)
    trace_failed = bool(
        trace_view.get("enabled", True)
        and trace_view.get("started")
        and not trace_view.get("worker_alive")
    )
    projection_failed = bool(trace_view.get("projection_failure_active"))
    exporter_failed = any(
        status != "ok"
        for status in (trace_view.get("exporters") or {}).values()
    )
    shadow_failed = bool(shadow_view.get("enabled") and not shadow_view.get("running"))
    email_shadow_failed = bool(
        email_shadow_view.get("enabled") and not email_shadow_view.get("running")
    )
    unrecoverable_errors = (
        sum(
            int(trace_view.get(name) or 0)
            for name in (
                "queue_drops", "spool_errors", "encryption_errors",
            )
        )
        + int(session_view.get("dropped") or 0)
        + int(session_view.get("errors") or 0)
        + int(shadow_view.get("dropped") or 0)
        + int(shadow_view.get("errors") or 0)
        + int(email_shadow_view.get("dropped") or 0)
        + int(email_shadow_view.get("errors") or 0)
        + int(email_shadow_view.get("persistence_errors") or 0)
    )
    return {
        "status": "degraded" if (
            trace_failed
            or projection_failed
            or exporter_failed
            or shadow_failed
            or email_shadow_failed
            or unrecoverable_errors
        ) else "ok",
        "trace_pipeline": trace_view,
        "session_touch": session_view,
        "tool_centric_shadow": shadow_view,
        "email_project_truth_shadow": email_shadow_view,
    }


@router.get("/traces/{trace_id}")
def get_trace_detail(
    trace_id: str,
    message_limit: int = Query(20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    del message_limit  # Retained for API compatibility; message payloads are never exposed here.
    trace = get_trace(conn, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return sanitize_classic_trace(trace)
