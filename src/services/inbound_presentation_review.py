"""Admin attestation for uncertain inbound presentation sends.

This service does not send, rebind, or requeue Telegram messages. The operator
must independently verify the original bot, chat, content, and remote ID.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


class InboundPresentationReviewError(ValueError):
    """The attestation cannot safely be recorded."""


def _details(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "email_id": row["email_id"], "project_id": row["project_id"],
        "kind": row["kind"], "payload": json.loads(row["delivery_payload_json"] or row["payload_json"]),
        "chat_id": row["chat_id"], "bot_fingerprint": row["bot_fingerprint"],
        "state": row["state"], "attempts": row["attempts"],
        "telegram_message_id": row["telegram_message_id"],
        "last_error": row["last_error"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_unresolved_presentations(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Include blocked rows for diagnosis, not for positive confirmation."""
    rows = conn.execute(
        """SELECT * FROM inbound_presentation_outbox
           WHERE state IN ('uncertain','blocked')
           ORDER BY updated_at,email_id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [_details(row) for row in rows]


def _check_local_receipts(
    conn: sqlite3.Connection, reviewed: dict[str, Any], message_id: int,
) -> None:
    chat_id = reviewed["chat_id"]
    # A claimed ID must never point at an unrelated local Telegram message.
    existing = conn.execute(
        """SELECT * FROM telegram_messages
           WHERE chat_id=? AND telegram_message_id=?""",
        (chat_id, message_id),
    ).fetchone()
    if existing is not None:
        if reviewed["kind"] != "info_card":
            raise InboundPresentationReviewError("Telegram message ID belongs to another message")
        try:
            metadata = json.loads(existing["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if (existing["direction"] != "outbound" or existing["message_kind"] != "card"
                or existing["project_id"] != reviewed["project_id"]
                or metadata.get("card_profile") != "I"
                or metadata.get("email_info_only") is not True
                or metadata.get("email_id") != reviewed["email_id"]
                or ("notice" in reviewed["payload"] and
                    metadata.get("analysis_notice") != reviewed["payload"]["notice"])):
            raise InboundPresentationReviewError("Telegram message ID belongs to another message")
    # A receipt for this source email with a different ID is contradictory.
    rows = conn.execute(
        """SELECT telegram_message_id FROM telegram_messages
           WHERE chat_id=? AND direction='outbound' AND message_kind='card'
             AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json
                                   ELSE '{}' END, '$.email_id')=?""",
        (chat_id, reviewed["email_id"]),
    ).fetchall()
    if any(row[0] != message_id for row in rows):
        raise InboundPresentationReviewError("Telegram receipt conflicts with persisted card")
    claimed = conn.execute(
        """SELECT email_id FROM inbound_presentation_outbox
           WHERE chat_id=? AND telegram_message_id=? AND email_id<>?""",
        (chat_id, str(message_id), reviewed["email_id"]),
    ).fetchone()
    if claimed is not None:
        raise InboundPresentationReviewError("Telegram message ID already belongs to another delivery")
    claimed = conn.execute(
        """SELECT 1 FROM contact_identity_card_outbox
           WHERE chat_id=? AND CAST(telegram_message_id AS TEXT)=? LIMIT 1""",
        (chat_id, str(message_id)),
    ).fetchone()
    if claimed is not None:
        raise InboundPresentationReviewError("Telegram message ID claimed by another delivery")


def confirm_uncertain_presentation_sent(
    conn: sqlite3.Connection, *, email_id: str, actor: str,
    expected_updated_at: str, expected_attempts: int, expected_chat_id: int,
    expected_bot_fingerprint: str, telegram_message_id: int, evidence_ref: str,
) -> dict[str, Any]:
    """Atomically attest a positively verified remote send and its activity audit."""
    from src.repositories.activity_log_repo import insert_activity

    if (not actor.strip() or not evidence_ref.strip() or not expected_updated_at.strip()
            or len(expected_bot_fingerprint) != 64):
        raise InboundPresentationReviewError("actor, evidence reference and version required")
    if (not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool)
            or telegram_message_id <= 0):
        raise InboundPresentationReviewError("positive Telegram message ID required")
    if (not isinstance(expected_chat_id, int) or isinstance(expected_chat_id, bool)
            or not isinstance(expected_attempts, int) or isinstance(expected_attempts, bool)
            or expected_attempts < 1):
        raise InboundPresentationReviewError("valid destination and attempt version required")
    row = conn.execute(
        "SELECT * FROM inbound_presentation_outbox WHERE email_id=?", (email_id,),
    ).fetchone()
    if row is None:
        raise KeyError(email_id)
    if (row["state"] != "uncertain" or row["updated_at"] != expected_updated_at
            or row["attempts"] != expected_attempts or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint
            or (row["telegram_message_id"] is not None
                and str(row["telegram_message_id"]) != str(telegram_message_id))):
        raise InboundPresentationReviewError("stale or non-reviewable inbound presentation")
    from src.services.combined_advisory_review import has_combined_delivery, confirm_combined_receipt
    if has_combined_delivery(conn, email_id, presentation=True):
        try:
            confirm_combined_receipt(conn, email_id=email_id, message_id=telegram_message_id,
                                     actor=actor, evidence_ref=evidence_ref)
        except ValueError as exc:
            raise InboundPresentationReviewError(str(exc)) from exc
        return _details(conn.execute("SELECT * FROM inbound_presentation_outbox WHERE email_id=?", (email_id,)).fetchone())
    reviewed = _details(row)
    _check_local_receipts(conn, reviewed, telegram_message_id)
    now = datetime.now(timezone.utc).isoformat()
    changed = conn.execute(
        """UPDATE inbound_presentation_outbox
           SET state='sent',telegram_message_id=?,last_error=NULL,
               lease_token=NULL,lease_until=NULL,updated_at=?
           WHERE email_id=? AND state='uncertain' AND updated_at=?
             AND attempts=? AND chat_id=? AND bot_fingerprint=?""",
        (str(telegram_message_id), now, email_id, expected_updated_at,
         expected_attempts, expected_chat_id, expected_bot_fingerprint),
    )
    if changed.rowcount != 1:
        raise InboundPresentationReviewError("stale inbound presentation review")
    insert_activity(
        conn, actor=actor.strip(), action_type="inbound_presentation_reconciled",
        project_id=reviewed["project_id"], surface="dashboard", object_ref=email_id,
        summary="Operator attested inbound Telegram presentation delivery",
        touch_project=False,
        metadata_json=json.dumps({
            "decision": "confirm_sent", "kind": reviewed["kind"],
            "evidence_ref": evidence_ref.strip(), "chat_id": expected_chat_id,
            "bot_fingerprint": expected_bot_fingerprint,
            "telegram_message_id": telegram_message_id, "attempts": expected_attempts,
            "previous_error": row["last_error"],
        }, ensure_ascii=False),
    )
    reviewed.update(state="sent", telegram_message_id=str(telegram_message_id),
                    last_error=None, updated_at=now)
    return reviewed
