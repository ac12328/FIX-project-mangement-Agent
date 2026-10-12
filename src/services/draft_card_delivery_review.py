"""Positive, auditable reconciliation of uncertain operational draft cards."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from src.repositories.activity_log_repo import insert_activity


class DraftCardDeliveryReviewError(ValueError):
    """An unsafe or stale review cannot change a delivery intent."""


def uncertain_draft_card_deliveries(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT o.draft_id,d.project_id,o.to_email,o.subject,o.chat_id,"
        "o.bot_fingerprint,o.state,o.attempts,o.last_error,o.created_at,o.updated_at "
        "FROM draft_card_outbox o JOIN drafts d ON d.draft_id=o.draft_id "
        "WHERE o.state='uncertain' ORDER BY o.updated_at,o.draft_id LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def confirm_uncertain_draft_card_sent(
    conn: sqlite3.Connection, *, draft_id: str, actor: str,
    expected_updated_at: str, expected_attempts: int, expected_chat_id: int,
    expected_bot_fingerprint: str, telegram_message_ids: list[int],
    evidence_ref: str,
) -> dict[str, Any]:
    """Attest independently verified remote messages, never trigger a replay."""
    if (not actor.strip() or not evidence_ref.strip() or not expected_updated_at.strip()
            or len(expected_bot_fingerprint) != 64):
        raise DraftCardDeliveryReviewError("actor, evidence and version required")
    if (type(expected_attempts) is not int or expected_attempts < 1
            or type(expected_chat_id) is not int or expected_chat_id <= 0):
        raise DraftCardDeliveryReviewError("valid destination and attempt required")
    if (not isinstance(telegram_message_ids, list) or not 1 <= len(telegram_message_ids) <= 20
            or any(type(mid) is not int or mid <= 0 for mid in telegram_message_ids)
            or len(set(telegram_message_ids)) != len(telegram_message_ids)):
        raise DraftCardDeliveryReviewError("positive unique Telegram message IDs required")
    row = conn.execute(
        "SELECT o.*,d.project_id FROM draft_card_outbox o "
        "JOIN drafts d ON d.draft_id=o.draft_id WHERE o.draft_id=?", (draft_id,),
    ).fetchone()
    if row is None:
        raise KeyError(draft_id)
    if (row["state"] != "uncertain" or row["updated_at"] != expected_updated_at
            or row["attempts"] != expected_attempts or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint):
        raise DraftCardDeliveryReviewError("stale or non-reviewable delivery")
    # A partial local audit must agree with the complete remotely verified set.
    local_ids = set()
    for message in conn.execute(
        "SELECT telegram_message_id,metadata_json FROM telegram_messages "
        "WHERE chat_id=? AND direction='outbound' AND message_kind='card'",
        (expected_chat_id,),
    ):
        try:
            metadata = json.loads(message["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("draft_id") == draft_id:
            local_ids.add(int(message["telegram_message_id"]))
    if not local_ids.issubset(set(telegram_message_ids)):
        raise DraftCardDeliveryReviewError("receipt conflicts with local draft message audit")
    for mid in telegram_message_ids:
        other = conn.execute(
            "SELECT metadata_json FROM telegram_messages "
            "WHERE chat_id=? AND telegram_message_id=?", (expected_chat_id, mid),
        ).fetchone()
        if other is not None:
            try:
                metadata = json.loads(other[0] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if metadata.get("draft_id") != draft_id:
                raise DraftCardDeliveryReviewError("Telegram message ID belongs to another operation")
    now = datetime.now(timezone.utc).isoformat()
    changed = conn.execute(
        "UPDATE draft_card_outbox SET state='sent',telegram_message_ids_json=?,"
        "last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=? "
        "WHERE draft_id=? AND state='uncertain' AND updated_at=? AND attempts=? "
        "AND chat_id=? AND bot_fingerprint=?",
        (json.dumps(telegram_message_ids), now, draft_id, expected_updated_at,
         expected_attempts, expected_chat_id, expected_bot_fingerprint),
    ).rowcount
    if changed != 1:
        raise DraftCardDeliveryReviewError("stale delivery review")
    insert_activity(
        conn, actor=actor.strip(), action_type="draft_card_delivery_reconciled",
        project_id=row["project_id"], surface="dashboard", object_ref=draft_id,
        summary="Operator attested operational draft card delivery",
        touch_project=False, metadata_json=json.dumps({
            "decision": "confirm_sent", "evidence_ref": evidence_ref.strip(),
            "chat_id": expected_chat_id, "telegram_message_ids": telegram_message_ids,
            "attempts": expected_attempts, "previous_error": row["last_error"],
        }, ensure_ascii=False),
    )
    return {
        "draft_id": draft_id, "project_id": row["project_id"],
        "chat_id": expected_chat_id, "state": "sent", "attempts": expected_attempts,
        "telegram_message_ids": telegram_message_ids, "last_error": None,
        "created_at": row["created_at"], "updated_at": now,
    }


def attachment_preparation_reviews(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """List quarantined, never-attempted card intents for privileged review."""
    rows = conn.execute(
        "SELECT o.draft_id,d.project_id,d.status AS draft_status,o.to_email,"
        "o.subject,o.chat_id,o.bot_fingerprint,o.state,o.attachment_state,"
        "o.attempts,o.last_error,o.created_at,o.updated_at "
        "FROM draft_card_outbox o JOIN drafts d ON d.draft_id=o.draft_id "
        "WHERE o.state='pending' AND o.attachment_state='needs_review' "
        "ORDER BY o.updated_at,o.draft_id LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def cancel_unverified_draft_card(
    conn: sqlite3.Connection, *, draft_id: str, actor: str,
    expected_updated_at: str, expected_to_email: str, expected_subject: str,
    expected_chat_id: int | None, expected_bot_fingerprint: str | None,
    evidence_ref: str, acknowledge_no_card: bool,
) -> dict[str, Any]:
    """Suppress only a quarantined card, preserving the draft for manual review."""
    if (not actor.strip() or not evidence_ref.strip() or not expected_updated_at.strip()
            or not expected_to_email.strip() or not expected_subject.strip()
            or acknowledge_no_card is not True):
        raise DraftCardDeliveryReviewError("review evidence and explicit no-card acknowledgement required")
    if (expected_chat_id is not None and (type(expected_chat_id) is not int or expected_chat_id <= 0)):
        raise DraftCardDeliveryReviewError("invalid expected chat")
    if (expected_bot_fingerprint is not None and
            (len(expected_bot_fingerprint) != 64 or
             any(c not in "0123456789abcdef" for c in expected_bot_fingerprint))):
        raise DraftCardDeliveryReviewError("invalid expected bot fingerprint")
    row = conn.execute(
        "SELECT o.*,d.project_id,d.status AS draft_status FROM draft_card_outbox o "
        "JOIN drafts d ON d.draft_id=o.draft_id WHERE o.draft_id=?", (draft_id,),
    ).fetchone()
    if row is None:
        raise KeyError(draft_id)
    if (row["state"] != "pending" or row["attachment_state"] != "needs_review"
            or row["attempts"] != 0 or row["lease_token"] is not None
            or row["lease_until"] is not None
            or row["telegram_message_ids_json"] is not None
            or row["updated_at"] != expected_updated_at
            or row["to_email"] != expected_to_email or row["subject"] != expected_subject
            or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint):
        raise DraftCardDeliveryReviewError("stale or non-reviewable attachment preparation")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("SAVEPOINT cancel_unverified_draft_card")
    try:
        changed = conn.execute(
            "UPDATE draft_card_outbox SET state='cancelled',"
            "last_error='attachment_preparation_cancelled_by_operator',updated_at=? "
            "WHERE draft_id=? AND state='pending' AND attachment_state='needs_review' "
            "AND attempts=0 AND lease_token IS NULL AND lease_until IS NULL "
            "AND telegram_message_ids_json IS NULL AND updated_at=? "
            "AND to_email=? AND subject=? AND chat_id IS ? AND bot_fingerprint IS ?",
            (now, draft_id, expected_updated_at, expected_to_email, expected_subject,
             expected_chat_id, expected_bot_fingerprint),
        ).rowcount
        if changed != 1:
            raise DraftCardDeliveryReviewError("stale attachment preparation review")
        insert_activity(
            conn, actor=actor.strip(), action_type="draft_card_preparation_reconciled",
            project_id=row["project_id"], surface="dashboard", object_ref=draft_id,
            summary="Operator suppressed unverified draft card; draft retained",
            touch_project=False, metadata_json=json.dumps({
                "decision": "cancel_card_only", "evidence_ref": evidence_ref.strip(),
                "draft_status": row["draft_status"], "to_email": expected_to_email,
                "subject": expected_subject, "chat_id": expected_chat_id,
                "bot_fingerprint": expected_bot_fingerprint,
                "previous_error": row["last_error"],
            }, ensure_ascii=False),
        )
        conn.execute("RELEASE SAVEPOINT cancel_unverified_draft_card")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT cancel_unverified_draft_card")
        conn.execute("RELEASE SAVEPOINT cancel_unverified_draft_card")
        raise
    return {
        "draft_id": draft_id, "project_id": row["project_id"],
        "draft_status": row["draft_status"], "state": "cancelled",
        "attachment_state": "needs_review", "updated_at": now,
    }
