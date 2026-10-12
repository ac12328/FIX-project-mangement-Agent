"""Aggregated and public-safe views for the secure draft workspace."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.email.attachments import ALLOWED_EXTENSIONS, MAX_FILE_SIZE, MAX_TOTAL_SIZE
from src.email.draft_workspace import build_workspace_snapshot, sender_identities


def get_workspace(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(conn, draft_id)
    from src.email.send_gate import SendGateService
    from src.email.workspace_collaboration import get_active_lease, list_comments

    checkpoints = [dict(row) for row in conn.execute(
        """SELECT checkpoint_id, version, revision, snapshot_hash, reason, created_by, created_at
           FROM draft_checkpoints WHERE draft_id=? ORDER BY version DESC""",
        (draft_id,),
    ).fetchall()]
    audit = [dict(row) for row in conn.execute(
        """SELECT event_id, event_type, actor_user_id, metadata_json, created_at
           FROM draft_audit_events WHERE draft_id=? ORDER BY created_at DESC LIMIT 100""",
        (draft_id,),
    ).fetchall()]
    for row in audit:
        row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
    outbox_rows = conn.execute(
        "SELECT * FROM draft_outbox WHERE draft_id=? ORDER BY created_at DESC, rowid DESC",
        (draft_id,),
    ).fetchall()
    outbox_history = [public_outbox(row) for row in outbox_rows]
    from src.email.ai_proposals import list_ai_proposals

    preflight = SendGateService(conn).preflight(snapshot)
    public_snapshot = public_workspace_snapshot(snapshot)
    public_attachments = public_snapshot["attachments"]
    return {
        "draft": public_snapshot,
        "delivery_candidates": delivery_candidates(conn, snapshot.get("project_id"), snapshot["delivery_context"]),
        "contacts": project_contacts(conn, snapshot.get("project_id")),
        "sender_identities": sender_identities(),
        "attachments": public_attachments,
        "review": snapshot["collaboration"],
        "comments": list_comments(conn, draft_id),
        "ai_proposals": list_ai_proposals(conn, draft_id),
        "history": {"checkpoints": checkpoints, "audit": audit, "outbox": outbox_history},
        "lease": public_lease(get_active_lease(conn, draft_id)),
        "outbox": outbox_history[0] if outbox_history else None,
        "capabilities": {
            "attachment": {
                "max_file_size": MAX_FILE_SIZE,
                "max_total_size": MAX_TOTAL_SIZE,
                "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
            },
            "lease_seconds": 120,
            "lease_renew_seconds": 45,
            "undo_send_seconds": 30,
            "rich_text_tags": ["p", "br", "strong", "em", "ul", "ol", "li", "a"],
        },
        "preflight": public_preflight(preflight),
    }


def public_workspace_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a draft snapshot without local attachment paths."""
    return {
        **snapshot,
        "attachments": [public_attachment(item) for item in snapshot.get("attachments") or []],
    }


def public_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Return a final preview without local attachment paths."""
    return {
        **preview,
        "attachments": [public_attachment(item) for item in preview.get("attachments") or []],
    }


def public_preflight(result: Any) -> dict[str, Any]:
    """Return a preflight result safe for API responses."""
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    return {**data, "preview": public_preview(data.get("preview") or {})}


def public_lease(lease: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return lease holder state without exposing the bearer token."""
    if not lease:
        return None
    return {
        "draft_id": lease.get("draft_id"),
        "holder_user_id": lease.get("holder_user_id"),
        "holder_display_name": lease.get("holder_display_name"),
        "expires_at": lease.get("expires_at"),
        "updated_at": lease.get("updated_at"),
    }


def public_attachment(attachment: dict[str, Any]) -> dict[str, Any]:
    """Return attachment metadata safe for API responses."""
    return {
        "filename": attachment.get("filename"),
        "size": int(attachment.get("size") or 0),
        "sha256": attachment.get("sha256"),
    }


def public_outbox(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Return delivery state without exposing immutable snapshots or local paths."""
    data = dict(row)
    provider_result = data.get("provider_result") if isinstance(data.get("provider_result"), dict) else None
    raw_provider = data.get("provider_result_json")
    if raw_provider:
        try:
            parsed = json.loads(raw_provider)
            if isinstance(parsed, dict):
                provider_result = {
                    key: parsed.get(key)
                    for key in ("message_id", "provider", "status")
                    if parsed.get(key) is not None
                }
        except (TypeError, json.JSONDecodeError):
            provider_result = None
    return {
        "outbox_id": data.get("outbox_id"),
        "draft_id": data.get("draft_id"),
        "status": data.get("status"),
        "scheduled_for": data.get("scheduled_for"),
        "undo_until": data.get("undo_until"),
        "created_by": data.get("created_by"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "sent_at": data.get("sent_at"),
        "cancelled_at": data.get("cancelled_at"),
        "provider_message_id": data.get("provider_message_id"),
        "provider_result": provider_result,
        "last_error": data.get("last_error"),
        "attempt_count": int(data.get("attempts") or data.get("attempt_count") or 0),
    }


def delivery_candidates(
    conn: sqlite3.Connection,
    project_id: str | None,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not project_id:
        return []
    rows = conn.execute(
        """SELECT email_id, subject, sender_email, direction, received_at, processed_at,
                  cc, to_recipients_json, cc_recipients_json, body_snippet
           FROM processed_emails WHERE project_id=?
           ORDER BY COALESCE(received_at, processed_at) DESC LIMIT 40""",
        (project_id,),
    ).fetchall()
    return [
        {
            **dict(row),
            "confirmed": row["email_id"] == context.get("reply_to_email_id"),
            "suggested": row["email_id"] == context.get("suggested_reply_to_email_id"),
            "is_evidence_source": row["email_id"] == context.get("context_packet_ref"),
        }
        for row in rows
    ]


def project_contacts(conn: sqlite3.Connection, project_id: str | None) -> list[dict[str, Any]]:
    if not project_id:
        return []
    rows = conn.execute(
        """SELECT c.contact_id, c.display_name, c.primary_email, c.role, c.department,
                  l.role_on_project, l.is_primary
           FROM contact_project_links l JOIN contacts c ON c.contact_id=l.contact_id
           WHERE l.project_id=? ORDER BY l.is_primary DESC, c.display_name""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]
