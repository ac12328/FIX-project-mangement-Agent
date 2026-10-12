"""Admin-only positive attestation of quarantined contact identity card sends.

This module never contacts Telegram. An operator must independently verify the
original bot, chat, exact frozen card and positive remote message ID first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.repositories.activity_log_repo import insert_activity


class ContactIdentityCardReviewError(ValueError):
    """The requested attestation is stale, contradictory or unsafe."""


def _details(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in (
        "suggestion_id", "source_email_id", "project_id", "chat_id",
        "bot_fingerprint", "card_text", "state", "attempts",
        "telegram_message_id", "last_error", "created_at", "updated_at",
    )}


def list_unresolved_contact_identity_cards(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Blocked rows are diagnostic only; they cannot be attested as sent."""
    rows = conn.execute(
        "SELECT * FROM contact_identity_card_outbox "
        "WHERE state IN ('uncertain','blocked') "
        "ORDER BY updated_at,suggestion_id LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [_details(row) for row in rows]


def _check_local_receipts(
    conn: sqlite3.Connection, row: sqlite3.Row, message_id: int,
) -> None:
    chat_id, suggestion_id = row["chat_id"], row["suggestion_id"]
    # A persisted receipt cannot be silently repurposed. The recorded source
    # text, not a reconstructed card, is compared against the frozen intent.
    existing = conn.execute(
        "SELECT * FROM telegram_messages WHERE chat_id=? AND telegram_message_id=?",
        (chat_id, message_id),
    ).fetchone()
    if existing is not None:
        try:
            metadata = json.loads(existing["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict) or (
            existing["direction"] != "outbound"
            or existing["message_kind"] != "card"
            or existing["text"] != row["card_text"]
            or existing["project_id"] != row["project_id"]
            or metadata.get("card_profile") != "E"
            or metadata.get("contact_suggestion_id") != suggestion_id
            or metadata.get("source_email_id") != row["source_email_id"]
            or metadata.get("bot_fingerprint") != row["bot_fingerprint"]
        ):
            raise ContactIdentityCardReviewError(
                "Telegram receipt conflicts with frozen contact identity card"
            )
    # A different locally recorded message for this same intent is ambiguous,
    # including a partial multi-chunk send. Do not claim one ID as the whole card.
    other = conn.execute(
        """SELECT 1 FROM telegram_messages
           WHERE (chat_id<>? OR telegram_message_id<>?)
             AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json
                                   ELSE '{}' END, '$.contact_suggestion_id')=?
           LIMIT 1""",
        (chat_id, message_id, suggestion_id),
    ).fetchone()
    if other is not None:
        raise ContactIdentityCardReviewError("Another local receipt exists for this card")

    # Other durable intents must not already claim this chat/message pair. Text
    # ID columns are compared as decimal strings to avoid SQLite affinity traps.
    claims = (
        ("contact_identity_card_outbox", "suggestion_id", suggestion_id),
        ("project_advisory_outbox", "email_id", None),
        ("inbound_presentation_outbox", "email_id", None),
        ("support_email_effects", "email_id", None),
        ("go_live_telegram_outbox", "project_id", None),
        ("email_commitment_card_outbox", "reply_id", None),
        ("staged_update_card_outbox", "operation_id", None),
    )
    for table, key, own_key in claims:
        found = conn.execute(
            f"SELECT {key} FROM {table} WHERE chat_id=? "
            "AND CAST(telegram_message_id AS TEXT)=? LIMIT 1",
            (chat_id, str(message_id)),
        ).fetchone()
        if found is not None and (own_key is None or found[0] != own_key):
            raise ContactIdentityCardReviewError(
                "Telegram message ID claimed by another delivery"
            )
    # Draft-card receipts are a JSON array; update notifications have no bound
    # chat, so any equal ID is contradictory and must fail closed.
    if conn.execute(
        """SELECT 1 FROM draft_card_outbox
           WHERE chat_id=? AND json_valid(telegram_message_ids_json)
             AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(telegram_message_ids_json)
                                             THEN telegram_message_ids_json ELSE '[]' END)
                         WHERE CAST(value AS TEXT)=?) LIMIT 1""",
        (chat_id, str(message_id)),
    ).fetchone() or conn.execute(
        """SELECT 1 FROM email_update_notification_outbox
           WHERE CAST(telegram_message_id AS TEXT)=? LIMIT 1""",
        (str(message_id),),
    ).fetchone():
        raise ContactIdentityCardReviewError(
            "Telegram message ID claimed by another delivery"
        )


def confirm_uncertain_contact_identity_card_sent(
    conn: sqlite3.Connection, *, suggestion_id: str, actor: str,
    expected_updated_at: str, expected_attempts: int, expected_chat_id: int,
    expected_bot_fingerprint: str, expected_card_text: str,
    telegram_message_id: int, evidence_ref: str,
) -> dict[str, Any]:
    """Atomically record the original send's receipt and an append-only audit."""
    if (not isinstance(actor, str) or not actor.strip()
            or not isinstance(evidence_ref, str) or not evidence_ref.strip()
            or not isinstance(expected_updated_at, str) or not expected_updated_at.strip()
            or not isinstance(expected_card_text, str) or not expected_card_text.strip()
            or not isinstance(expected_bot_fingerprint, str)
            or len(expected_bot_fingerprint) != 64):
        raise ContactIdentityCardReviewError("actor, evidence and frozen version required")
    if (type(expected_attempts) is not int or expected_attempts < 1
            or type(expected_chat_id) is not int or expected_chat_id == 0
            or type(telegram_message_id) is not int or telegram_message_id <= 0):
        raise ContactIdentityCardReviewError("positive ID, attempt and original chat required")
    row = conn.execute(
        "SELECT * FROM contact_identity_card_outbox WHERE suggestion_id=?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise KeyError(suggestion_id)
    if (row["state"] != "uncertain" or row["attempts"] != expected_attempts
            or row["updated_at"] != expected_updated_at
            or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint
            or row["card_text"] != expected_card_text
            or row["lease_token"] is not None or row["lease_until"] is not None
            or (row["telegram_message_id"] is not None
                and row["telegram_message_id"] != telegram_message_id)):
        raise ContactIdentityCardReviewError("stale or non-reviewable contact identity card")
    suggestion = conn.execute(
        "SELECT status,telegram_message_id,last_pushed_at FROM contact_suggestions "
        "WHERE suggestion_id=?", (suggestion_id,),
    ).fetchone()
    if (suggestion is None or suggestion["status"] != "pending"
            or suggestion["telegram_message_id"] is not None
            or suggestion["last_pushed_at"] is not None):
        raise ContactIdentityCardReviewError("suggestion has another delivery or was resolved")
    _check_local_receipts(conn, row, telegram_message_id)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("SAVEPOINT contact_identity_card_review")
    try:
        changed = conn.execute(
            """UPDATE contact_identity_card_outbox
               SET state='sent',telegram_message_id=?,last_error=NULL,
                   lease_token=NULL,lease_until=NULL,updated_at=?
               WHERE suggestion_id=? AND state='uncertain' AND updated_at=?
                 AND attempts=? AND chat_id=? AND bot_fingerprint=?
                 AND card_text=? AND lease_token IS NULL AND lease_until IS NULL
                 AND (telegram_message_id IS NULL OR telegram_message_id=?)""",
            (telegram_message_id, now, suggestion_id, expected_updated_at,
             expected_attempts, expected_chat_id, expected_bot_fingerprint,
             expected_card_text, telegram_message_id),
        ).rowcount
        if changed != 1:
            raise ContactIdentityCardReviewError("stale contact identity card review")
        changed = conn.execute(
            """UPDATE contact_suggestions
               SET telegram_message_id=?,last_pushed_at=?,updated_at=?
               WHERE suggestion_id=? AND status='pending'
                 AND telegram_message_id IS NULL AND last_pushed_at IS NULL""",
            (str(telegram_message_id), now, now, suggestion_id),
        ).rowcount
        if changed != 1:
            raise ContactIdentityCardReviewError("suggestion receipt changed")
        insert_activity(
            conn, actor=actor.strip(), action_type="contact_identity_card_reconciled",
            project_id=row["project_id"], surface="dashboard", object_ref=suggestion_id,
            summary="Operator attested contact identity card delivery",
            touch_project=False, metadata_json=json.dumps({
                "decision": "confirm_sent", "evidence_ref": evidence_ref.strip(),
                "chat_id": expected_chat_id,
                "bot_fingerprint": expected_bot_fingerprint,
                "telegram_message_id": telegram_message_id,
                "attempts": expected_attempts,
                "card_text_sha256": hashlib.sha256(expected_card_text.encode("utf-8")).hexdigest(),
                "previous_error": row["last_error"],
            }, ensure_ascii=False),
        )
        conn.execute("RELEASE SAVEPOINT contact_identity_card_review")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT contact_identity_card_review")
        conn.execute("RELEASE SAVEPOINT contact_identity_card_review")
        raise
    result = _details(row)
    result.update(state="sent", telegram_message_id=telegram_message_id,
                  last_error=None, updated_at=now)
    return result



