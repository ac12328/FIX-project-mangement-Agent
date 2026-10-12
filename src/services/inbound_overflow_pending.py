"""Admin rebind of never-claimed IMAP overflow items after recipient rotation.

An in-flight batch is never eligible: its external outcome may be uncertain.
The caller owns commit and no Telegram I/O occurs in this service.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from src.repositories.activity_log_repo import insert_activity


class PendingOverflowError(ValueError):
    """A pending source cannot safely be rebound with these expectations."""


def list_other_recipient_items(
    conn: sqlite3.Connection, *, current_chat_id: int, limit: int = 50,
) -> list[dict[str, Any]]:
    """Show only unclaimed items stranded under a different stored recipient."""
    rows = conn.execute("""SELECT i.email_id,i.chat_id,i.summary,i.created_at,w.route_state
        FROM inbound_overflow_items i
        JOIN inbound_email_work w ON w.email_id=i.email_id
        WHERE i.batch_id IS NULL AND i.chat_id!=?
        ORDER BY i.created_at,i.email_id LIMIT ?""",
        (current_chat_id, max(1, min(limit, 100))),
    ).fetchall()
    return [{**dict(row), "summary_sha256": hashlib.sha256(
        row["summary"].encode("utf-8"),
    ).hexdigest()} for row in rows]


def rebind_pending_item(
    conn: sqlite3.Connection, *, email_id: str, actor: str,
    expected_chat_id: int, expected_summary_sha256: str,
    target_chat_id: int, current_chat_id: int, evidence_ref: str,
    recipient_verified: bool,
) -> dict[str, Any]:
    """Rebind one completed, unsent source to the configured primary chat.

    The attestation must cover the destination's authorization to receive this
    source. Changing the stored recipient does not claim or send the item.
    """
    if (not email_id.strip() or not actor.strip() or not evidence_ref.strip()
            or type(expected_chat_id) is not int or expected_chat_id == 0
            or type(target_chat_id) is not int or target_chat_id == 0
            or target_chat_id != current_chat_id or target_chat_id == expected_chat_id
            or recipient_verified is not True or len(expected_summary_sha256) != 64):
        raise PendingOverflowError("valid source, configured recipient and independent attestation required")
    row = conn.execute("""SELECT i.chat_id,i.summary,i.batch_id,w.route_state
        FROM inbound_overflow_items i JOIN inbound_email_work w ON w.email_id=i.email_id
        WHERE i.email_id=?""", (email_id,)).fetchone()
    if row is None:
        raise KeyError(email_id)
    summary_sha256 = hashlib.sha256(row["summary"].encode("utf-8")).hexdigest()
    if (row["chat_id"] != expected_chat_id or row["batch_id"] is not None
            or row["route_state"] != "completed"
            or summary_sha256 != expected_summary_sha256):
        raise PendingOverflowError("stale, claimed or incomplete overflow source")

    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT rebind_pending_overflow")
    try:
        updated = conn.execute("""UPDATE inbound_overflow_items SET chat_id=?
            WHERE email_id=? AND chat_id=? AND summary=? AND batch_id IS NULL
              AND EXISTS (SELECT 1 FROM inbound_email_work w
                          WHERE w.email_id=inbound_overflow_items.email_id
                            AND w.route_state='completed')""",
            (target_chat_id, email_id, expected_chat_id, row["summary"]),
        ).rowcount
        if updated != 1:
            raise PendingOverflowError("overflow source changed before rebind")
        insert_activity(
            conn, actor=actor.strip(), surface="dashboard",
            action_type="inbound_overflow_pending_rebound", object_ref=email_id,
            summary="Admin rebound unsent overflow source to configured recipient",
            touch_project=False, metadata_json=json.dumps({
                "old_chat_id": expected_chat_id, "new_chat_id": target_chat_id,
                "summary_sha256": summary_sha256,
                "evidence_ref": evidence_ref.strip(), "recipient_verified": True,
            }, ensure_ascii=False),
        )
        conn.execute("RELEASE SAVEPOINT rebind_pending_overflow")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT rebind_pending_overflow")
        conn.execute("RELEASE SAVEPOINT rebind_pending_overflow")
        if started_transaction:
            conn.rollback()
        raise
    return {"email_id": email_id, "old_chat_id": expected_chat_id,
            "chat_id": target_chat_id, "summary_sha256": summary_sha256}
