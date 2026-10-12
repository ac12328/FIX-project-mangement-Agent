"""Collaboration, comments, leases, and review workflow for draft workspaces."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.email.draft_workspace import (
    audit_event,
    build_workspace_snapshot,
    compute_send_affecting_hash,
    create_checkpoint,
    utcnow_iso,
)

LEASE_SECONDS = 120
LEASE_RENEW_SECONDS = 45


class LeaseConflictError(RuntimeError):
    def __init__(self, lease: dict[str, Any]):
        super().__init__("Draft is being edited by another user")
        self.lease = lease


class ReviewStateError(RuntimeError):
    pass


def get_active_lease(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT draft_id, holder_user_id, holder_display_name, lease_token,
                  expires_at, updated_at
           FROM draft_edit_leases
           WHERE draft_id=? AND datetime(expires_at) > datetime('now')""",
        (draft_id,),
    ).fetchone()
    return dict(row) if row else None


def acquire_lease(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    actor_display_name: str,
    commit: bool = True,
) -> dict[str, Any]:
    build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    active = get_active_lease(conn, draft_id)
    if active and active["holder_user_id"] != actor_user_id:
        raise LeaseConflictError(active)
    token = active["lease_token"] if active else f"lease_{uuid.uuid4().hex}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    conn.execute(
        """INSERT INTO draft_edit_leases
           (draft_id, holder_user_id, holder_display_name, lease_token, expires_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(draft_id) DO UPDATE SET
             holder_user_id=excluded.holder_user_id,
             holder_display_name=excluded.holder_display_name,
             lease_token=excluded.lease_token,
             expires_at=excluded.expires_at,
             updated_at=excluded.updated_at""",
        (draft_id, actor_user_id, actor_display_name, token, expires_at, utcnow_iso()),
    )
    audit_event(conn, draft_id, "edit_lease_acquired", actor_user_id, {"expires_at": expires_at})
    if commit:
        conn.commit()
    return get_active_lease(conn, draft_id) or {}


def renew_lease(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    lease_token: str,
    commit: bool = True,
) -> dict[str, Any]:
    active = get_active_lease(conn, draft_id)
    if not active or active["holder_user_id"] != actor_user_id or active["lease_token"] != lease_token:
        raise LeaseConflictError(active or {})
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    conn.execute(
        """UPDATE draft_edit_leases SET expires_at=?, updated_at=?
           WHERE draft_id=? AND holder_user_id=? AND lease_token=?""",
        (expires_at, utcnow_iso(), draft_id, actor_user_id, lease_token),
    )
    if commit:
        conn.commit()
    return get_active_lease(conn, draft_id) or {}


def release_lease(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    lease_token: str,
    commit: bool = True,
) -> bool:
    cursor = conn.execute(
        "DELETE FROM draft_edit_leases WHERE draft_id=? AND holder_user_id=? AND lease_token=?",
        (draft_id, actor_user_id, lease_token),
    )
    if cursor.rowcount:
        audit_event(conn, draft_id, "edit_lease_released", actor_user_id)
    if commit:
        conn.commit()
    return bool(cursor.rowcount)


def take_over_lease(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    actor_display_name: str,
    commit: bool = True,
) -> dict[str, Any]:
    previous = get_active_lease(conn, draft_id)
    token = f"lease_{uuid.uuid4().hex}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    conn.execute(
        """INSERT INTO draft_edit_leases
           (draft_id, holder_user_id, holder_display_name, lease_token, expires_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(draft_id) DO UPDATE SET
             holder_user_id=excluded.holder_user_id,
             holder_display_name=excluded.holder_display_name,
             lease_token=excluded.lease_token,
             expires_at=excluded.expires_at,
             updated_at=excluded.updated_at""",
        (draft_id, actor_user_id, actor_display_name, token, expires_at, utcnow_iso()),
    )
    audit_event(
        conn,
        draft_id,
        "edit_lease_taken_over",
        actor_user_id,
        {"previous_holder_user_id": (previous or {}).get("holder_user_id")},
    )
    if commit:
        conn.commit()
    return get_active_lease(conn, draft_id) or {}


def list_comments(conn: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT comment_id, draft_id, parent_comment_id, author_user_id,
                  author_display_name, location_type, location_ref, body,
                  resolved_at, resolved_by, created_at, updated_at
           FROM draft_comments WHERE draft_id=? ORDER BY created_at""",
        (draft_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_comment(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    author_user_id: str,
    author_display_name: str,
    body: str,
    location_type: str = "general",
    location_ref: str | None = None,
    parent_comment_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    text = body.strip()
    if not text:
        raise ValueError("Comment body is required")
    if location_type not in {"general", "subject", "recipient", "body", "attachment"}:
        raise ValueError("Invalid comment location")
    if parent_comment_id:
        parent = conn.execute(
            "SELECT 1 FROM draft_comments WHERE comment_id=? AND draft_id=?",
            (parent_comment_id, draft_id),
        ).fetchone()
        if not parent:
            raise ValueError("Parent comment not found")
    comment_id = f"dc_{uuid.uuid4().hex[:16]}"
    now = utcnow_iso()
    conn.execute(
        """INSERT INTO draft_comments
           (comment_id, draft_id, parent_comment_id, author_user_id,
            author_display_name, location_type, location_ref, body, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            comment_id,
            draft_id,
            parent_comment_id,
            author_user_id,
            author_display_name,
            location_type,
            location_ref,
            text,
            now,
            now,
        ),
    )
    audit_event(conn, draft_id, "comment_created", author_user_id, {"comment_id": comment_id})
    if commit:
        conn.commit()
    return dict(conn.execute("SELECT * FROM draft_comments WHERE comment_id=?", (comment_id,)).fetchone())


def resolve_comment(
    conn: sqlite3.Connection,
    draft_id: str,
    comment_id: str,
    *,
    actor_user_id: str,
    resolved: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM draft_comments WHERE comment_id=? AND draft_id=?",
        (comment_id, draft_id),
    ).fetchone()
    if not row:
        raise ValueError("Comment not found")
    conn.execute(
        """UPDATE draft_comments SET resolved_at=?, resolved_by=?, updated_at=?
           WHERE comment_id=?""",
        (
            utcnow_iso() if resolved else None,
            actor_user_id if resolved else None,
            utcnow_iso(),
            comment_id,
        ),
    )
    audit_event(
        conn,
        draft_id,
        "comment_resolved" if resolved else "comment_reopened",
        actor_user_id,
        {"comment_id": comment_id},
    )
    if commit:
        conn.commit()
    return dict(conn.execute("SELECT * FROM draft_comments WHERE comment_id=?", (comment_id,)).fetchone())


def request_review(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    requester_user_id: str,
    reviewer_user_id: str,
    commit: bool = True,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    if not reviewer_user_id or reviewer_user_id == requester_user_id:
        raise ValueError("Reviewer must be another user")
    if snapshot["delivery_status"] != "editing":
        raise ReviewStateError("Draft must be editable before requesting review")
    create_checkpoint(conn, draft_id, actor_user_id=requester_user_id, reason="review_requested", commit=False)
    now = utcnow_iso()
    conn.execute(
        """UPDATE drafts SET reviewer_user_id=?, review_state='requested',
                  review_requested_by=?, review_requested_at=?, approval_hash=NULL,
                  approved_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
        (reviewer_user_id, requester_user_id, now, now, draft_id),
    )
    audit_event(conn, draft_id, "review_requested", requester_user_id, {"reviewer_user_id": reviewer_user_id})
    if commit:
        conn.commit()
    return build_workspace_snapshot(conn, draft_id, commit_initialization=commit)


def approve_review(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    reviewer_user_id: str,
    commit: bool = True,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    collaboration = snapshot["collaboration"]
    if collaboration["review_state"] != "requested":
        raise ReviewStateError("Review is not awaiting approval")
    if collaboration["reviewer_user_id"] != reviewer_user_id:
        raise PermissionError("Only the assigned reviewer can approve")
    approval_hash = compute_send_affecting_hash(snapshot)
    now = utcnow_iso()
    conn.execute(
        """UPDATE drafts SET review_state='approved', approval_hash=?, approved_at=?,
                  send_affecting_hash=?, workspace_updated_at=? WHERE draft_id=?""",
        (approval_hash, now, approval_hash, now, draft_id),
    )
    audit_event(conn, draft_id, "review_approved", reviewer_user_id, {"approval_hash": approval_hash})
    if commit:
        conn.commit()
    return build_workspace_snapshot(conn, draft_id, commit_initialization=commit)


def request_changes(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    reviewer_user_id: str,
    commit: bool = True,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    collaboration = snapshot["collaboration"]
    if collaboration["review_state"] != "requested":
        raise ReviewStateError("Review is not awaiting a decision")
    if collaboration["reviewer_user_id"] != reviewer_user_id:
        raise PermissionError("Only the assigned reviewer can request changes")
    conn.execute(
        """UPDATE drafts SET review_state='changes_requested', approval_hash=NULL,
                  approved_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
        (utcnow_iso(), draft_id),
    )
    audit_event(conn, draft_id, "review_changes_requested", reviewer_user_id)
    if commit:
        conn.commit()
    return build_workspace_snapshot(conn, draft_id, commit_initialization=commit)


def cancel_review(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    commit: bool = True,
) -> dict[str, Any]:
    snapshot = build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
    collaboration = snapshot["collaboration"]
    allowed = {collaboration.get("owner_user_id"), collaboration.get("review_requested_by")}
    if actor_user_id not in allowed:
        raise PermissionError("Only the owner or requester can cancel review")
    if collaboration["review_state"] not in {"requested", "changes_requested", "approved", "stale"}:
        raise ReviewStateError("Review cannot be cancelled from its current state")
    conn.execute(
        """UPDATE drafts SET review_state='cancelled', approval_hash=NULL,
                  approved_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
        (utcnow_iso(), draft_id),
    )
    audit_event(conn, draft_id, "review_cancelled", actor_user_id)
    if commit:
        conn.commit()
    return build_workspace_snapshot(conn, draft_id, commit_initialization=commit)
