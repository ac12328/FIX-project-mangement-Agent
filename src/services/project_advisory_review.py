"""Admin attestation for uncertain inbound project advisory delivery.

This service never sends, rebinds, or retries Telegram. A positive remote
verification of the original bot, chat, content and message ID is required.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.repositories.activity_log_repo import insert_activity


class ProjectAdvisoryReviewError(ValueError):
    """An uncertain send cannot safely be attested."""


def _details(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "email_id": row["email_id"], "project_id": row["project_id"],
        "subject": row["subject"], "event_summary": row["event_summary"],
        "advisory": json.loads(row["advisory_json"]) if row["advisory_json"] else None,
        "chat_id": row["chat_id"], "bot_fingerprint": row["bot_fingerprint"],
        "state": row["state"], "attempts": row["attempts"],
        "delivery_mode": row["delivery_mode"],
        "telegram_message_id": row["telegram_message_id"],
        "last_error": row["last_error"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_unresolved_project_advisories(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Blocked rows are diagnostic only; only uncertain sends can be confirmed."""
    rows = conn.execute(
        """SELECT * FROM project_advisory_outbox
           WHERE state IN ('uncertain','blocked')
           ORDER BY updated_at,email_id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [_details(row) for row in rows]


def _check_local_receipts(
    conn: sqlite3.Connection, row: sqlite3.Row, message_id: int,
) -> None:
    chat_id, email_id = row["chat_id"], row["email_id"]
    existing = conn.execute(
        """SELECT * FROM telegram_messages
           WHERE chat_id=? AND telegram_message_id=?""", (chat_id, message_id),
    ).fetchone()
    if existing is not None:
        try:
            metadata = json.loads(existing["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        fingerprint = hashlib.sha256(row["advisory_json"].encode("utf-8")).hexdigest()
        if (existing["direction"] != "outbound" or existing["message_kind"] != "card"
                or existing["project_id"] != row["project_id"]
                or metadata.get("card_profile") != "O"
                or metadata.get("email_id") != email_id
                or metadata.get("bot_fingerprint") != row["bot_fingerprint"]
                or metadata.get("advisory_fingerprint") != fingerprint):
            raise ProjectAdvisoryReviewError("Telegram message ID belongs to another delivery")
    # A different local receipt for this intent is contradictory unless this ID
    # is also present: long cards may legitimately have multiple chunks.
    other = conn.execute(
        """SELECT telegram_message_id FROM telegram_messages
           WHERE chat_id=? AND direction='outbound' AND message_kind='card'
             AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json
                                   ELSE '{}' END, '$.email_id')=?""",
        (chat_id, email_id),
    ).fetchall()
    if other and existing is None:
        raise ProjectAdvisoryReviewError("Telegram receipt conflicts with advisory")
    # Also prevent claiming a receipt already attributed to another durable send.
    claims = (
        ("project_advisory_outbox", "email_id", email_id),
        ("inbound_presentation_outbox", "email_id", None),
        ("support_email_effects", "email_id", None),
        ("go_live_telegram_outbox", "project_id", None),
        ("email_commitment_card_outbox", "reply_id", None),
        ("staged_update_card_outbox", "operation_id", None),
        ("contact_identity_card_outbox", "suggestion_id", None),
    )
    for table, key, own_key in claims:
        claim = conn.execute(
            f"SELECT {key} FROM {table} WHERE chat_id=? "
            "AND CAST(telegram_message_id AS TEXT)=? LIMIT 1",
            (chat_id, str(message_id)),
        ).fetchone()
        if claim is not None and (own_key is None or claim[0] != own_key):
            raise ProjectAdvisoryReviewError("Telegram message ID claimed by another delivery")


def confirm_uncertain_project_advisory_sent(
    conn: sqlite3.Connection, *, email_id: str, actor: str,
    expected_updated_at: str, expected_attempts: int, expected_chat_id: int,
    expected_bot_fingerprint: str, telegram_message_id: int, evidence_ref: str,
) -> dict[str, Any]:
    """Persist positive attestation and append-only audit in one writer scope."""
    if (not actor.strip() or not evidence_ref.strip() or not expected_updated_at.strip()
            or len(expected_bot_fingerprint) != 64):
        raise ProjectAdvisoryReviewError("actor, evidence reference and version required")
    if (type(telegram_message_id) is not int or telegram_message_id <= 0
            or type(expected_chat_id) is not int
            or type(expected_attempts) is not int or expected_attempts < 1):
        raise ProjectAdvisoryReviewError("positive ID and original destination required")
    row = conn.execute(
        "SELECT * FROM project_advisory_outbox WHERE email_id=?", (email_id,),
    ).fetchone()
    if row is None:
        raise KeyError(email_id)
    if (row["state"] != "uncertain" or not row["advisory_json"]
            or row["updated_at"] != expected_updated_at
            or row["attempts"] != expected_attempts
            or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint
            or (row["telegram_message_id"] is not None
                and row["telegram_message_id"] != telegram_message_id)):
        raise ProjectAdvisoryReviewError("stale or non-reviewable project advisory")
    from src.services.combined_advisory_review import has_combined_delivery, confirm_combined_receipt
    if has_combined_delivery(conn, email_id):
        try:
            confirm_combined_receipt(conn, email_id=email_id, message_id=telegram_message_id,
                                     actor=actor, evidence_ref=evidence_ref)
        except ValueError as exc:
            raise ProjectAdvisoryReviewError(str(exc)) from exc
        return _details(conn.execute("SELECT * FROM project_advisory_outbox WHERE email_id=?", (email_id,)).fetchone())
    _check_local_receipts(conn, row, telegram_message_id)
    now = datetime.now(timezone.utc).isoformat()
    changed = conn.execute(
        """UPDATE project_advisory_outbox
           SET state='sent',telegram_message_id=?,last_error=NULL,
               lease_token=NULL,lease_until=NULL,updated_at=?
           WHERE email_id=? AND state='uncertain' AND updated_at=?
             AND attempts=? AND chat_id=? AND bot_fingerprint=?""",
        (telegram_message_id, now, email_id, expected_updated_at,
         expected_attempts, expected_chat_id, expected_bot_fingerprint),
    )
    if changed.rowcount != 1:
        raise ProjectAdvisoryReviewError("stale project advisory review")
    insert_activity(
        conn, actor=actor.strip(), action_type="project_advisory_reconciled",
        project_id=row["project_id"], surface="dashboard", object_ref=email_id,
        summary="Operator attested inbound project advisory delivery",
        touch_project=False,
        metadata_json=json.dumps({
            "decision": "confirm_sent", "evidence_ref": evidence_ref.strip(),
            "chat_id": expected_chat_id, "bot_fingerprint": expected_bot_fingerprint,
            "telegram_message_id": telegram_message_id,
            "attempts": expected_attempts, "previous_error": row["last_error"],
        }, ensure_ascii=False),
    )
    result = _details(row)
    result.update(state="sent", telegram_message_id=telegram_message_id,
                  last_error=None, updated_at=now)
    return result
