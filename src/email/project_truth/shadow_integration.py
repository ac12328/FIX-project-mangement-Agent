"""Fail-open integration between inbound email A and Project Truth B shadow."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from src.email.project_truth.contracts import ProjectSnapshot
from src.email.project_truth.shadow import (
    EmailProjectTruthShadowAttachment,
    EmailProjectTruthShadowJob,
    normalize_shadow_fields,
)
from src.email.project_truth.shadow_worker import (
    email_project_truth_capture_enabled,
    submit_email_project_truth_shadow,
)


def capture_email_project_truth_shadow_job(
    conn: Any,
    *,
    event: Any,
    body: str,
    fetched: Any = None,
) -> EmailProjectTruthShadowJob | None:
    """Capture the immutable pre-A input snapshot without affecting inbound flow."""
    if not email_project_truth_capture_enabled():
        return None
    if conn is None:
        from src.db.connection import scoped_read
        with scoped_read() as read_conn:
            return capture_email_project_truth_shadow_job(read_conn, event=event, body=body, fetched=fetched)
    try:
        project_id = str(getattr(event, "project_id", "") or "").strip()
        if not project_id:
            return None
        snapshot = _load_project_snapshot(conn, project_id)
        if snapshot is None:
            return None
        source_ref = str(getattr(event, "email_id", "") or "").strip()
        if not source_ref:
            digest = hashlib.sha256(
                f"{project_id}\0{body}".encode("utf-8")
            ).hexdigest()[:24]
            source_ref = f"inbound:{digest}"
        received_at = getattr(event, "received_at", None) or getattr(fetched, "received_at", None)
        from src.email.source_authority import resolve_source_authority
        authority = resolve_source_authority(conn, project_id=project_id,
            sender_email=getattr(event, "sender_email", None))
        return EmailProjectTruthShadowJob(
            source_ref=source_ref,
            project_id=project_id,
            subject=str(getattr(event, "subject", "") or ""),
            sender_email=str(getattr(event, "sender_email", "") or ""),
            received_at=str(received_at) if received_at else None,
            body=str(body or ""),
            project_snapshot=snapshot,
            source_authority=authority,
            sent_at=_optional_text(getattr(fetched, "sent_at", None)),
            attachments=_capture_attachments(fetched),
            baseline_status="pending",
            thread_id=_optional_text(
                getattr(event, "thread_id", None)
                or getattr(fetched, "in_reply_to", None)
            ),
        )
    except Exception:
        return None


def submit_email_project_truth_shadow_fail_open(
    captured: EmailProjectTruthShadowJob | None,
    *,
    baseline_status: str,
    baseline_fields: Mapping[str, Any] | None = None,
    baseline_summary: str | None = None,
    baseline_suggested_action: str | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
) -> bool:
    """Finalize the A result and enqueue B without surfacing any failure."""
    if captured is None:
        return False
    try:
        job = replace(
            captured,
            baseline_status=str(baseline_status or "failed"),
            baseline_fields=normalize_shadow_fields(baseline_fields),
            baseline_summary=str(baseline_summary or ""),
            baseline_suggested_action=str(baseline_suggested_action or ""),
            parent_trace_id=_optional_text(trace_id),
            parent_run_id=_optional_text(run_id),
            turn_id=_optional_text(turn_id) or captured.turn_id,
        )
        return bool(submit_email_project_truth_shadow(job))
    except Exception:
        return False


def _load_project_snapshot(conn: Any, project_id: str) -> ProjectSnapshot | None:
    if conn is not None:
        return _query_project_snapshot(conn, project_id)
    from src.db.connection import scoped_read

    with scoped_read() as read_conn:
        return _query_project_snapshot(read_conn, project_id)


def _query_project_snapshot(conn: Any, project_id: str) -> ProjectSnapshot | None:
    row = conn.execute(
        """SELECT project_id,current_milestone,project_status,ball_with,
                  next_action,client_name
           FROM projects WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    values = dict(row) if hasattr(row, "keys") else {
        "project_id": row[0],
        "current_milestone": row[1],
        "project_status": row[2],
        "ball_with": row[3],
        "next_action": row[4],
        "client_name": row[5],
    }
    from src.services.followup_plan import plan_snapshot
    head = conn.execute("SELECT revision FROM followup_plan_heads WHERE project_id=?", (project_id,)).fetchone()
    if head is not None:
        plan = plan_snapshot(conn, project_id)
        if len(plan["actions"]) <= 100:
            values["action_plan"] = plan
        else:
            values["action_plan_truncated"] = True
    values["snapshot_as_of"] = datetime.now(timezone.utc)
    values["reconstructed"] = False
    return ProjectSnapshot.model_validate(values)


def _capture_attachments(fetched: Any) -> tuple[EmailProjectTruthShadowAttachment, ...]:
    attachments: list[EmailProjectTruthShadowAttachment] = []
    for attachment in tuple(getattr(fetched, "attachments", None) or ()):
        try:
            data = getattr(attachment, "data", None) or b""
            if not isinstance(data, bytes):
                data = bytes(data)
            attachments.append(
                EmailProjectTruthShadowAttachment(
                    filename=str(getattr(attachment, "filename", "") or "unknown"),
                    content_type=str(
                        getattr(attachment, "content_type", "")
                        or "application/octet-stream"
                    ),
                    data=data,
                    size_bytes=max(
                        0,
                        int(getattr(attachment, "size_bytes", len(data)) or len(data)),
                    ),
                )
            )
        except Exception:
            continue
    return tuple(attachments)


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "capture_email_project_truth_shadow_job",
    "submit_email_project_truth_shadow_fail_open",
]
