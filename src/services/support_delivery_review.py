"""Admin attestation for support cards whose Telegram outcome is unknown.

No operation here sends or requeues a card. A local receipt alone cannot prove
remote delivery; the operator must independently inspect the bound chat.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


class SupportDeliveryReviewError(ValueError):
    """A support card cannot safely be marked delivered."""


def _details(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["notification_json"] or "{}")
    return {
        "email_id": row["email_id"], "ticket_id": row["ticket_id"],
        "project_id": payload.get("project_id"), "title": payload.get("title", ""),
        "body": payload.get("body", ""), "is_followup": bool(row["is_followup"]),
        "chat_id": row["chat_id"], "bot_fingerprint": row["bot_fingerprint"],
        "state": row["card_state"], "attempts": row["attempts"],
        "telegram_message_id": row["telegram_message_id"],
        "last_error": row["last_error"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def uncertain_support_deliveries(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Show the original recipient and content to authenticated review staff."""
    rows = conn.execute(
        """SELECT * FROM support_email_effects WHERE card_state='uncertain'
           ORDER BY updated_at,email_id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [_details(row) for row in rows]


def _matches_local_receipt(message: sqlite3.Row, reviewed: dict[str, Any]) -> bool:
    try:
        metadata = json.loads(message["metadata_json"] or "{}")
    except (TypeError, ValueError):
        return False
    return (
        message["direction"] == "outbound" and message["message_kind"] == "card"
        and message["project_id"] == reviewed["project_id"]
        and metadata.get("ticket_id") == reviewed["ticket_id"]
        and metadata.get("email_summary") == reviewed["body"]
        and metadata.get("is_followup") is reviewed["is_followup"]
        and metadata.get("email_id", reviewed["email_id"]) == reviewed["email_id"]
    )


def _check_local_receipts(
    conn: sqlite3.Connection, reviewed: dict[str, Any], message_id: int,
) -> None:
    """Reject conflicting local evidence, including an ID used by another card."""
    rows = conn.execute(
        """SELECT chat_id,telegram_message_id,direction,message_kind,project_id,metadata_json
           FROM telegram_messages WHERE chat_id=? AND direction='outbound' AND message_kind='card'
             AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json
                                   ELSE '{}' END, '$.ticket_id')=?""",
        (reviewed["chat_id"], reviewed["ticket_id"]),
    ).fetchall()
    matches = {int(row["telegram_message_id"]) for row in rows
               if _matches_local_receipt(row, reviewed)}
    if matches and matches != {message_id}:
        raise SupportDeliveryReviewError("Telegram receipt conflicts with persisted card")
    existing = conn.execute(
        """SELECT chat_id,telegram_message_id,direction,message_kind,project_id,metadata_json
           FROM telegram_messages WHERE chat_id=? AND telegram_message_id=?""",
        (reviewed["chat_id"], message_id),
    ).fetchone()
    if existing is not None and not _matches_local_receipt(existing, reviewed):
        raise SupportDeliveryReviewError("Telegram message ID belongs to another message")
    claimed = conn.execute(
        """SELECT 1 FROM contact_identity_card_outbox
           WHERE chat_id=? AND CAST(telegram_message_id AS TEXT)=? LIMIT 1""",
        (reviewed["chat_id"], str(message_id)),
    ).fetchone()
    if claimed is not None:
        raise SupportDeliveryReviewError("Telegram message ID claimed by another delivery")


def confirm_uncertain_support_sent(
    conn: sqlite3.Connection, *, email_id: str, actor: str,
    expected_updated_at: str, expected_attempts: int, expected_chat_id: int,
    expected_bot_fingerprint: str, telegram_message_id: int, evidence_ref: str,
) -> dict[str, Any]:
    """Record a positive, independently verified receipt and audit atomically."""
    from src.repositories.activity_log_repo import insert_activity

    if (not actor.strip() or not evidence_ref.strip() or not expected_updated_at.strip()
            or len(expected_bot_fingerprint) != 64):
        raise SupportDeliveryReviewError("actor, evidence reference and version required")
    if (not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool)
            or telegram_message_id <= 0):
        raise SupportDeliveryReviewError("positive Telegram message ID required")
    if (not isinstance(expected_chat_id, int) or isinstance(expected_chat_id, bool)
            or not isinstance(expected_attempts, int) or isinstance(expected_attempts, bool)
            or expected_attempts < 1):
        raise SupportDeliveryReviewError("valid destination and attempt version required")
    row = conn.execute(
        "SELECT * FROM support_email_effects WHERE email_id=?", (email_id,),
    ).fetchone()
    if row is None:
        raise KeyError(email_id)
    if (row["card_state"] != "uncertain" or row["updated_at"] != expected_updated_at
            or row["attempts"] != expected_attempts or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint
            or (row["telegram_message_id"] is not None
                and str(row["telegram_message_id"]) != str(telegram_message_id))):
        raise SupportDeliveryReviewError("stale or non-reviewable support delivery")
    reviewed = _details(row)
    _check_local_receipts(conn, reviewed, telegram_message_id)
    now = datetime.now(timezone.utc).isoformat()
    changed = conn.execute(
        """UPDATE support_email_effects SET card_state='sent',telegram_message_id=?,
           last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=?
           WHERE email_id=? AND card_state='uncertain' AND updated_at=?
             AND attempts=? AND chat_id=? AND bot_fingerprint=?""",
        (str(telegram_message_id), now, email_id, expected_updated_at,
         expected_attempts, expected_chat_id, expected_bot_fingerprint),
    )
    if changed.rowcount != 1:
        raise SupportDeliveryReviewError("stale support delivery review")
    insert_activity(
        conn, actor=actor.strip(), action_type="support_delivery_reconciled",
        project_id=reviewed["project_id"], surface="dashboard", object_ref=email_id,
        summary="Operator attested support Telegram card delivery", touch_project=False,
        metadata_json=json.dumps({
            "decision": "confirm_sent", "ticket_id": reviewed["ticket_id"],
            "evidence_ref": evidence_ref.strip(), "chat_id": expected_chat_id,
            "telegram_message_id": telegram_message_id, "attempts": expected_attempts,
            "previous_error": row["last_error"],
        }, ensure_ascii=False),
    )
    reviewed.update(state="sent", telegram_message_id=str(telegram_message_id),
                    last_error=None, updated_at=now)
    return reviewed
