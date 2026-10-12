"""Internal claim, serialization, and immutable snapshot helpers for the draft outbox."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.settings import CONFIG
from src.email.draft_workspace import audit_event, utcnow_iso
from src.email.sending import SendResult

CLAIM_SECONDS = 120


def _claim_next(conn: sqlite3.Connection, worker_id: str) -> sqlite3.Row | None:
    now = utcnow_iso()
    row = conn.execute(
        """SELECT * FROM draft_outbox AS candidate
           WHERE (
              status='pending'
              OR (status='undoable' AND datetime(undo_until) <= datetime('now'))
              OR (status='scheduled' AND datetime(scheduled_for) <= datetime('now'))
           )
           AND (claim_expires_at IS NULL OR datetime(claim_expires_at) <= datetime('now'))
           AND NOT EXISTS (
               SELECT 1 FROM draft_outbox AS prior
               WHERE prior.draft_id=candidate.draft_id AND prior.status='uncertain'
                 AND prior.outbox_id<>candidate.outbox_id
           )
           ORDER BY COALESCE(scheduled_for, undo_until, created_at), created_at
           LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    claim_expires = (datetime.now(timezone.utc) + timedelta(seconds=CLAIM_SECONDS)).isoformat()
    cursor = conn.execute(
        """UPDATE draft_outbox SET status='sending', claimed_by=?, claim_expires_at=?,
                  attempts=attempts+1, updated_at=?
           WHERE outbox_id=? AND status=?""",
        (worker_id, claim_expires, now, row["outbox_id"], row["status"]),
    )
    if not cursor.rowcount:
        return None
    conn.execute(
        "UPDATE drafts SET delivery_status='sending', status='sending', workspace_updated_at=? WHERE draft_id=?",
        (now, row["draft_id"]),
    )
    conn.commit()
    return conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (row["outbox_id"],)).fetchone()


def _claim_specific(conn: sqlite3.Connection, row: sqlite3.Row, worker_id: str) -> sqlite3.Row | None:
    status = row["status"]
    now = datetime.now(timezone.utc)
    due = status == "pending"
    due = due or (status == "undoable" and row["undo_until"] and _as_utc(row["undo_until"]) <= now)
    due = due or (status == "scheduled" and row["scheduled_for"] and _as_utc(row["scheduled_for"]) <= now)
    if not due:
        return None
    expires = (now + timedelta(seconds=CLAIM_SECONDS)).isoformat()
    cursor = conn.execute(
        """UPDATE draft_outbox SET status='sending', claimed_by=?, claim_expires_at=?,
                  attempts=attempts+1, updated_at=? WHERE outbox_id=? AND status=?""",
        (worker_id, expires, utcnow_iso(), row["outbox_id"], status),
    )
    if not cursor.rowcount:
        return None
    conn.execute(
        "UPDATE drafts SET delivery_status='sending', status='sending', workspace_updated_at=? WHERE draft_id=?",
        (utcnow_iso(), row["draft_id"]),
    )
    conn.commit()
    return conn.execute("SELECT * FROM draft_outbox WHERE outbox_id=?", (row["outbox_id"],)).fetchone()


def _mark_needs_attention(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> SendResult:
    now = utcnow_iso()
    conn.execute(
        """UPDATE draft_outbox SET status='needs_attention', last_error=?, updated_at=?,
                  claimed_by=NULL, claim_expires_at=NULL WHERE outbox_id=?""",
        (reason, now, row["outbox_id"]),
    )
    conn.execute(
        "UPDATE drafts SET delivery_status='needs_attention', status='send_failed', scheduled_send_at=NULL, workspace_updated_at=? WHERE draft_id=?",
        (now, row["draft_id"]),
    )
    audit_event(conn, row["draft_id"], "send_needs_attention", row["created_by"], {"outbox_id": row["outbox_id"], "reason": reason})
    conn.commit()
    return SendResult(success=False, error=f"needs_attention: {reason}")


def _quarantine_expired_claims(conn: sqlite3.Connection) -> int:
    """Never replay an expired in-flight send: provider acceptance is unknown."""
    rows = conn.execute(
        """SELECT * FROM draft_outbox WHERE status='sending'
           AND (claim_expires_at IS NULL OR datetime(claim_expires_at) <= datetime('now'))"""
    ).fetchall()
    now = utcnow_iso()
    quarantined = 0
    for row in rows:
        result = conn.execute(
            """UPDATE draft_outbox SET status='uncertain',
                      last_error='Delivery claim expired; provider acceptance unconfirmed',
                      provider_result_json=?, claimed_by=NULL, claim_expires_at=NULL,
                      updated_at=? WHERE outbox_id=? AND status='sending'""",
            (json.dumps({"success": False, "outcome": "uncertain",
                         "reason": "delivery_claim_expired"}), now, row["outbox_id"]),
        )
        if not result.rowcount:
            continue
        conn.execute(
            """UPDATE drafts SET delivery_status='uncertain', status='send_failed',
                      scheduled_send_at=NULL, workspace_updated_at=? WHERE draft_id=?""",
            (now, row["draft_id"]),
        )
        audit_event(
            conn, row["draft_id"], "send_uncertain", row["created_by"],
            {"outbox_id": row["outbox_id"], "reason": "delivery_claim_expired"},
        )
        quarantined += 1
    if quarantined:
        conn.commit()
    return quarantined


def _copy_immutable_snapshot(snapshot: dict[str, Any], outbox_id: str) -> dict[str, Any]:
    immutable = json.loads(json.dumps(snapshot, ensure_ascii=False))
    root = Path(CONFIG.data_dir) / "outbox" / outbox_id / "attachments"
    copied: list[dict[str, Any]] = []
    if immutable.get("attachments"):
        root.mkdir(parents=True, exist_ok=True)
    from src.email.attachment_recovery import attachment_visibility_lock, require_reconciled

    with attachment_visibility_lock(CONFIG.data_dir):
        require_reconciled()
        for attachment in immutable.get("attachments") or []:
            source = Path(str(attachment.get("path") or ""))
            if not source.is_file():
                raise FileNotFoundError(f"Attachment missing: {attachment.get('filename')}")
            target = root / Path(str(attachment.get("filename") or source.name)).name
            shutil.copy2(source, target)
            copied.append({**attachment, "path": str(target)})
    immutable["attachments"] = copied
    return immutable


def _deserialize_outbox(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["snapshot"] = json.loads(data.pop("snapshot_json"))
    data["provider_result"] = json.loads(data.pop("provider_result_json") or "null")
    return data


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
