"""Internal helpers for draft workspace attachments, auditing, and delivery context."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from config.settings import CONFIG
from src.email.attachments import list_attachments
from src.email.recipients import canonical_email
def prepare_attachment_mutation(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    expected_revision: int | None,
    actor_user_id: str,
    lease_token: str | None = None,
    attachment_data_dir: str | None = None,
    initialize: bool = True,
) -> dict[str, Any]:
    """Validate revision and editability before mutating attachment files."""
    from src.email.draft_workspace import WorkspaceConflictError, build_workspace_snapshot

    snapshot = build_workspace_snapshot(
        conn, draft_id, initialize=initialize, commit_initialization=False,
        attachment_data_dir=attachment_data_dir,
    )
    if not snapshot["updated_at"]:
        raise ValueError("Draft Workspace not initialized")
    if expected_revision is not None and snapshot["revision"]["number"] != expected_revision:
        raise WorkspaceConflictError(snapshot)
    _assert_editable(conn, snapshot, actor_user_id, lease_token)
    return snapshot


def attachment_manifest(
    draft_id: str, *, data_dir: str | None = None,
) -> list[dict[str, Any]]:
    from src.email.attachment_recovery import attachment_visibility_lock

    with attachment_visibility_lock(data_dir if data_dir is not None else CONFIG.data_dir):
        rows = list_attachments(data_dir if data_dir is not None else CONFIG.data_dir, draft_id)
        manifest: list[dict[str, Any]] = []
        for row in rows:
            path = row.get("path")
            digest = hashlib.sha256()
            try:
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                sha256 = digest.hexdigest()
            except OSError:
                sha256 = "missing"
            manifest.append({**row, "sha256": sha256})
        return manifest


def compute_attachment_hash(
    draft_id: str, *, manifest: list[dict[str, Any]] | None = None
) -> str:
    """Hash one observed manifest rather than re-reading its files when supplied."""
    payload = [
        {"filename": row["filename"], "size": row["size"], "sha256": row["sha256"]}
        for row in (manifest if manifest is not None else attachment_manifest(draft_id))
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def refresh_attachment_revision(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    expected_revision: int,
    actor_user_id: str,
    lease_token: str | None = None,
    commit: bool = True,
    attachment_data_dir: str | None = None,
    return_workspace: bool = True,
) -> dict[str, Any]:
    """Advance the working revision after an attachment filesystem mutation."""
    from src.email.draft_workspace import (
        WorkspaceConflictError, build_workspace_snapshot,
        compute_send_affecting_hash, utcnow_iso,
    )

    snapshot = build_workspace_snapshot(
        conn, draft_id, initialize=return_workspace, commit_initialization=False,
        attachment_data_dir=attachment_data_dir,
    )
    if snapshot["revision"]["number"] != expected_revision:
        raise WorkspaceConflictError(snapshot)
    _assert_editable(conn, snapshot, actor_user_id, lease_token)
    next_revision = expected_revision + 1
    attachment_hash = snapshot["revision"]["attachment_hash"]
    now = utcnow_iso()
    review_state = snapshot["collaboration"].get("review_state") or "not_requested"
    next_review_state = "stale" if review_state == "approved" else review_state
    conn.execute(
        """UPDATE drafts SET workspace_revision=?, attachment_hash=?, review_state=?,
                  approval_hash=CASE WHEN ?='stale' THEN NULL ELSE approval_hash END,
                  approved_at=CASE WHEN ?='stale' THEN NULL ELSE approved_at END,
                  workspace_updated_at=? WHERE draft_id=?""",
        (next_revision, attachment_hash, next_review_state, next_review_state, next_review_state, now, draft_id),
    )
    fresh = build_workspace_snapshot(
        conn, draft_id, initialize=False, attachment_data_dir=attachment_data_dir,
    )
    send_hash = compute_send_affecting_hash(fresh)
    conn.execute(
        "UPDATE drafts SET send_affecting_hash=? WHERE draft_id=?",
        (send_hash, draft_id),
    )
    audit_event(
        conn,
        draft_id,
        "attachment_manifest_changed",
        actor_user_id,
        {"revision": next_revision, "attachment_hash": attachment_hash},
    )
    if commit:
        conn.commit()
    if not return_workspace:
        return build_workspace_snapshot(
            conn, draft_id, initialize=False, attachment_data_dir=attachment_data_dir,
        )
    from src.email.workspace_view import get_workspace

    return get_workspace(conn, draft_id)


def audit_event(
    conn: sqlite3.Connection,
    draft_id: str,
    event_type: str,
    actor_user_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    from src.email.draft_workspace import utcnow_iso

    conn.execute(
        """INSERT INTO draft_audit_events
           (event_id, draft_id, event_type, actor_user_id, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            f"dae_{uuid.uuid4().hex[:16]}",
            draft_id,
            event_type,
            actor_user_id,
            json.dumps(metadata or {}, ensure_ascii=False),
            utcnow_iso(),
        ),
    )


def _contact_lookup(conn: sqlite3.Connection, project_id: str | None) -> dict[str, str]:
    if not project_id:
        return {}
    rows = conn.execute(
        """SELECT c.contact_id, c.primary_email, c.email_addresses
           FROM contact_project_links l JOIN contacts c ON c.contact_id=l.contact_id
           WHERE l.project_id=?""",
        (project_id,),
    ).fetchall()
    lookup: dict[str, str] = {}
    for row in rows:
        emails = [row["primary_email"]]
        try:
            emails.extend(json.loads(row["email_addresses"] or "[]"))
        except json.JSONDecodeError:
            pass
        for email in emails:
            canonical = canonical_email(email)
            if canonical:
                lookup[canonical] = row["contact_id"]
    return lookup


def _infer_delivery_context(
    conn: sqlite3.Connection,
    draft: dict[str, Any],
    recipients: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    context_ref = draft.get("context_packet_ref")
    if not context_ref:
        return {"delivery_mode": "new", "reply_to_email_id": None, "suggested_reply_to_email_id": None}
    row = conn.execute(
        """SELECT email_id, project_id, direction, sender_email
           FROM processed_emails WHERE email_id=?""",
        (context_ref,),
    ).fetchone()
    to_addresses = {canonical_email(item.get("email")) for item in recipients["to"]}
    confirmed = bool(
        row
        and (not draft.get("project_id") or row["project_id"] == draft.get("project_id"))
        and (row["direction"] or "inbound") == "inbound"
        and canonical_email(row["sender_email"]) in to_addresses
    )
    if confirmed:
        return {"delivery_mode": "reply", "reply_to_email_id": context_ref, "suggested_reply_to_email_id": None}
    suggested = _suggest_reply_email(conn, draft, to_addresses) or context_ref
    return {"delivery_mode": "unresolved", "reply_to_email_id": None, "suggested_reply_to_email_id": suggested}


def _suggest_reply_email(
    conn: sqlite3.Connection,
    draft: dict[str, Any],
    to_addresses: set[str],
) -> str | None:
    project_id = draft.get("project_id")
    if not project_id:
        return None
    rows = conn.execute(
        """SELECT email_id, sender_email FROM processed_emails
           WHERE project_id=? AND direction='inbound'
           ORDER BY COALESCE(received_at, processed_at) DESC""",
        (project_id,),
    ).fetchall()
    for row in rows:
        if canonical_email(row["sender_email"]) in to_addresses:
            return row["email_id"]
    return rows[0]["email_id"] if rows else None


def _assert_editable(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    actor_user_id: str,
    lease_token: str | None,
) -> None:
    from src.email.draft_workspace import WorkspaceLockedError, utcnow_iso

    if snapshot["delivery_status"] in {"scheduled", "undoable", "sending", "sent"} or snapshot["legacy_status"] in {"sent", "discarded"}:
        raise WorkspaceLockedError("Cancel or undo delivery before editing this draft")
    if snapshot["collaboration"]["review_state"] == "requested":
        raise WorkspaceLockedError("Draft is read-only while review is requested")
    lease = conn.execute(
        """SELECT holder_user_id, lease_token, expires_at FROM draft_edit_leases
           WHERE draft_id=? AND expires_at > ?""",
        (snapshot["draft_id"], utcnow_iso()),
    ).fetchone()
    if lease and (
        lease["holder_user_id"] != actor_user_id
        or not lease_token
        or lease["lease_token"] != lease_token
    ):
        raise WorkspaceLockedError("A valid edit lease is required to modify this draft")


def _migrate_legacy_schedules(conn: sqlite3.Connection) -> None:
    from src.email.outbox import queue_legacy_scheduled_draft

    rows = conn.execute(
        """SELECT draft_id, scheduled_send_at FROM drafts
           WHERE scheduled_send_at IS NOT NULL AND trim(scheduled_send_at) != ''
             AND status NOT IN ('sent','discarded')"""
    ).fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            scheduled = datetime.fromisoformat(str(row["scheduled_send_at"]).replace("Z", "+00:00"))
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
        except ValueError:
            scheduled = now
        queue_legacy_scheduled_draft(
            conn,
            row["draft_id"],
            scheduled_for=scheduled,
            needs_attention=scheduled <= now,
        )
