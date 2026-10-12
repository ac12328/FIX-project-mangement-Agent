"""Durable personal reminder dispatch and manual outcome reconciliation.

A `sending` receipt is committed before network I/O. A crashed process leaves
that receipt reviewable and cannot automatically replay an uncertain send.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.db.savepoint import savepoint
from src.repositories.activity_log_repo import insert_activity
from src.repositories.personal_work_item_repo import resolve_personal_work_item
from src.repositories.schedule_repo import complete_run
from src.utils.notification_budget import reserve_notification_attempt
from src.utils.push_guard import URGENCY_NORMAL


class ReminderDeliveryReviewError(ValueError):
    """The receipt is stale, insufficiently verified, or already resolved."""


def prepare_reminder_delivery(
    conn: sqlite3.Connection, *, run_id: str, schedule_id: str,
    chat_id: int, bot_fingerprint: str, text: str,
) -> bool:
    """Reserve budget and store immutable intent in the caller's transaction."""
    if len(bot_fingerprint) != 64:
        raise ValueError("Telegram bot fingerprint is required")
    if not reserve_notification_attempt(conn, URGENCY_NORMAL):
        complete_run(
            conn, run_id, schedule_id, status="failed",
            error="Notification policy suppressed delivery", commit=False,
        )
        return False
    conn.execute(
        """INSERT INTO personal_reminder_deliveries
           (run_id, schedule_id, chat_id, bot_fingerprint, text, state)
           VALUES (?, ?, ?, ?, ?, 'sending')""",
        (run_id, schedule_id, chat_id, bot_fingerprint, text),
    )
    return True


def finish_reminder_delivery(
    conn: sqlite3.Connection, *, run_id: str, schedule_id: str,
    work_item_id: str | None, message_ids: list[int],
) -> None:
    """Apply the receipt, schedule completion and linked resolution atomically."""
    if not message_ids or any(type(item) is not int or item <= 0 for item in message_ids):
        raise ValueError("Telegram send returned no usable persisted message receipt")
    changed = conn.execute(
        """UPDATE personal_reminder_deliveries
           SET state='sent', message_ids_json=?, last_error=NULL,
               review_version=review_version+1, updated_at=datetime('now')
           WHERE run_id=? AND state='sending'""",
        (json.dumps(message_ids), run_id),
    )
    if changed.rowcount != 1:
        raise ReminderDeliveryReviewError("Reminder delivery is not in flight")
    schedule_finalized = complete_run(
        conn, run_id, schedule_id, status="sent", commit=False,
    )
    if schedule_finalized and work_item_id:
        _resolve_linked_item(conn, run_id, work_item_id, "Reminder delivered")


def mark_reminder_delivery_uncertain(
    conn: sqlite3.Connection, *, run_id: str, error: str,
) -> None:
    """Never turn an attempted send into a retryable run."""
    conn.execute(
        """UPDATE personal_reminder_deliveries
           SET state='uncertain', last_error=?, review_version=review_version+1,
               updated_at=datetime('now') WHERE run_id=? AND state='sending'""",
        (error[:2000], run_id),
    )


def _resolve_linked_item(
    conn: sqlite3.Connection, run_id: str, work_item_id: str, note: str,
) -> None:
    """Keep confirmed transport truth even if linked local work cannot resolve."""
    try:
        with savepoint(conn):
            row = conn.execute(
                """SELECT w.status FROM work_items w
                   JOIN personal_work_items p ON p.work_item_id=w.work_item_id
                   WHERE w.work_item_id=?""", (work_item_id,),
            ).fetchone()
            if row is None:
                raise ReminderDeliveryReviewError("Linked personal work item is missing")
            if row[0] != "open":
                return  # A user action already completed/cancelled this item.
            if not resolve_personal_work_item(conn, work_item_id, note, commit=False):
                raise ReminderDeliveryReviewError("Linked personal work item is missing")
    except Exception as exc:
        # The savepoint discards partial item writes. A confirmed Telegram send
        # remains sent, and the sync issue stays visible for manual repair.
        conn.execute(
            """UPDATE personal_reminder_deliveries
               SET last_error=?, updated_at=datetime('now') WHERE run_id=?""",
            (f"Linked personal work item sync failed: {exc}"[:2000], run_id),
        )


def reviewable_reminder_deliveries(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Include uncertain sends and confirmed sends with linked-item repair errors."""
    bounded = max(1, min(limit, 100))
    rows = conn.execute(
        """SELECT d.*, r.scheduled_for, s.title, s.operator_id
           FROM personal_reminder_deliveries d
           JOIN assistant_schedule_runs r ON r.run_id=d.run_id
           JOIN assistant_schedules s ON s.schedule_id=d.schedule_id
           WHERE d.state='uncertain'
              OR (d.state='sending' AND d.created_at<=datetime('now','-5 minutes'))
              OR (d.state='sent' AND d.last_error IS NOT NULL
                  AND d.last_error LIKE 'Linked personal work item sync failed:%'
                  AND d.linked_item_review_outcome IS NULL)
           ORDER BY d.created_at, d.run_id LIMIT ?""",
        (bounded,),
    ).fetchall()
    # Historical claims may predate receipts. Their destination and actual
    # send outcome cannot be reconstructed, so expose them without fabricating
    # an intent or making them eligible for automatic replay.
    unrecorded = conn.execute(
        """SELECT r.run_id, r.schedule_id, NULL AS chat_id,
                  NULL AS bot_fingerprint, NULL AS text,
                  'unrecorded' AS state, 0 AS review_version,
                  'Claim has no durable send intent; investigate historical Telegram audit'
                    AS last_error,
                  r.started_at AS created_at, r.started_at AS updated_at,
                  r.scheduled_for, s.title, s.operator_id
           FROM assistant_schedule_runs r
           JOIN assistant_schedules s ON s.schedule_id=r.schedule_id
           WHERE r.status='claimed' AND NOT EXISTS (
               SELECT 1 FROM personal_reminder_deliveries d WHERE d.run_id=r.run_id
           )
           ORDER BY r.started_at, r.run_id LIMIT ?""",
        (bounded,),
    ).fetchall()
    return sorted(
        (dict(row) for row in (*rows, *unrecorded)),
        key=lambda row: (row["created_at"], row["run_id"]),
    )[:bounded]


def reconcile_reminder_delivery(
    conn: sqlite3.Connection, *, run_id: str, actor: str,
    expected_version: int, expected_chat_id: int,
    expected_bot_fingerprint: str, evidence_ref: str,
    decision: str, telegram_message_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Record a human attestation; no decision automatically resends Telegram."""
    if not actor.strip() or not evidence_ref.strip():
        raise ReminderDeliveryReviewError("Reviewer and independent evidence are required")
    if decision not in {"confirm_sent", "confirm_not_sent"}:
        raise ReminderDeliveryReviewError("Invalid review decision")
    if (type(expected_version) is not int or expected_version < 0
            or type(expected_chat_id) is not int
            or not isinstance(expected_bot_fingerprint, str)
            or len(expected_bot_fingerprint) != 64):
        raise ReminderDeliveryReviewError("Valid receipt version and chat are required")
    if decision == "confirm_sent" and (
        not telegram_message_ids or len(telegram_message_ids) > 100
        or any(type(item) is not int or item <= 0 for item in telegram_message_ids)
        or len(telegram_message_ids) != len(set(telegram_message_ids))
    ):
        raise ReminderDeliveryReviewError("Positive, distinct Telegram message IDs are required")
    if decision == "confirm_not_sent" and telegram_message_ids is not None:
        raise ReminderDeliveryReviewError("Do not attach sent messages to a not-sent decision")
    row = conn.execute(
        """SELECT d.*, s.command_payload_json FROM personal_reminder_deliveries d
           JOIN assistant_schedules s ON s.schedule_id=d.schedule_id
           WHERE d.run_id=?""", (run_id,),
    ).fetchone()
    if row is None:
        legacy = conn.execute(
            "SELECT 1 FROM assistant_schedule_runs WHERE run_id=? AND status='claimed'",
            (run_id,),
        ).fetchone()
        if legacy:
            raise ReminderDeliveryReviewError(
                "Claim has no durable send intent; investigate historical Telegram audit"
            )
        raise KeyError(run_id)
    if row["state"] == "sending" and conn.execute(
        "SELECT created_at>datetime('now','-5 minutes') FROM personal_reminder_deliveries WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]:
        raise ReminderDeliveryReviewError("Reminder send may still be in flight")
    if (row["state"] not in {"sending", "uncertain"}
            or row["review_version"] != expected_version
            or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint):
        raise ReminderDeliveryReviewError("Stale or non-reviewable reminder receipt")
    persisted = conn.execute(
        """SELECT telegram_message_id FROM telegram_messages
           WHERE chat_id=? AND direction='outbound'
             AND json_extract(metadata_json, '$.schedule_run_id')=?""",
        (expected_chat_id, run_id),
    ).fetchall()
    if decision == "confirm_sent" and any(
        int(item[0]) not in telegram_message_ids for item in persisted
    ):
        raise ReminderDeliveryReviewError("Attested message IDs conflict with local receipts")
    if decision == "confirm_not_sent" and persisted:
        raise ReminderDeliveryReviewError("A local Telegram send receipt exists")
    new_state = "sent" if decision == "confirm_sent" else "not_sent"
    changed = conn.execute(
        """UPDATE personal_reminder_deliveries
           SET state=?, message_ids_json=?, reviewer=?, evidence_ref=?,
               last_error=NULL, review_version=review_version+1, updated_at=datetime('now')
           WHERE run_id=? AND state IN ('sending','uncertain')
             AND review_version=? AND chat_id=? AND bot_fingerprint=?""",
        (new_state, json.dumps(telegram_message_ids or []),
         actor.strip(), evidence_ref.strip(), run_id, expected_version, expected_chat_id,
         expected_bot_fingerprint),
    )
    if changed.rowcount != 1:
        raise ReminderDeliveryReviewError("Stale reminder receipt")
    if decision == "confirm_sent":
        schedule_finalized = complete_run(
            conn, run_id, row["schedule_id"], status="sent", commit=False,
        )
        work_item_id = json.loads(row["command_payload_json"] or "{}").get("work_item_id")
        if schedule_finalized and work_item_id:
            _resolve_linked_item(
                conn, run_id, work_item_id, "Reminder delivered (operator verified)",
            )
    else:
        complete_run(conn, run_id, row["schedule_id"], status="missed", commit=False)
    insert_activity(
        conn, actor=actor.strip(), surface="dashboard", touch_project=False,
        action_type="personal_reminder_delivery_reconciled", object_ref=run_id,
        summary="Operator reconciled personal reminder Telegram outcome",
        metadata_json=json.dumps({
            "decision": decision, "evidence_ref": evidence_ref.strip(),
            "chat_id": expected_chat_id, "bot_fingerprint": expected_bot_fingerprint,
            "telegram_message_ids": telegram_message_ids,
        }),
    )
    return {"run_id": run_id, "schedule_id": row["schedule_id"], "state": new_state}


def review_linked_reminder_work_item(
    conn: sqlite3.Connection, *, run_id: str, actor: str,
    expected_version: int, expected_chat_id: int,
    expected_bot_fingerprint: str, decision: str, evidence_ref: str,
    note: str,
) -> dict[str, Any]:
    """Repair local work or acknowledge a documented manual follow-up.

    A sent Telegram receipt and its original send attestation are immutable.
    This operation never sends Telegram, reclaims a run, or changes the schedule.
    """
    if (not actor.strip() or not evidence_ref.strip() or not note.strip()
            or decision not in {"retry_resolution", "acknowledge_manual_followup"}):
        raise ReminderDeliveryReviewError(
            "Reviewer, decision, evidence and manual review note are required"
        )
    if (type(expected_version) is not int or expected_version < 0
            or type(expected_chat_id) is not int
            or not isinstance(expected_bot_fingerprint, str)
            or len(expected_bot_fingerprint) != 64):
        raise ReminderDeliveryReviewError("Valid receipt version and chat are required")
    with savepoint(conn, ensure_transaction=True):
        row = conn.execute(
            """SELECT d.*, r.status AS run_status,
                      r.intent_version AS run_intent_version,
                      s.intent_version AS schedule_intent_version,
                      s.command_payload_json, s.operator_id
               FROM personal_reminder_deliveries d
               JOIN assistant_schedule_runs r ON r.run_id=d.run_id
               JOIN assistant_schedules s ON s.schedule_id=d.schedule_id
               WHERE d.run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if (row["state"] != "sent" or row["run_status"] != "sent"
                or not row["last_error"]
                or not row["last_error"].startswith(
                    "Linked personal work item sync failed:"
                )
                or row["linked_item_review_outcome"] is not None
                or row["review_version"] != expected_version
                or row["chat_id"] != expected_chat_id
                or row["bot_fingerprint"] != expected_bot_fingerprint):
            raise ReminderDeliveryReviewError("Stale or non-reviewable linked-item failure")
        work_item_id = json.loads(row["command_payload_json"] or "{}").get("work_item_id")
        if decision == "retry_resolution":
            if not work_item_id or (
                row["run_intent_version"] != row["schedule_intent_version"]
            ):
                raise ReminderDeliveryReviewError(
                    "Linked item cannot be resolved after later user intent"
                )
            item = conn.execute(
                """SELECT w.status FROM work_items w
                   JOIN personal_work_items p ON p.work_item_id=w.work_item_id
                   WHERE w.work_item_id=? AND p.operator_id=?""",
                (work_item_id, row["operator_id"]),
            ).fetchone()
            if item is None or item[0] != "open":
                raise ReminderDeliveryReviewError(
                    "Linked personal work item is missing or no longer open"
                )
            if not resolve_personal_work_item(
                conn, work_item_id, "Reminder delivered (admin repair)", commit=False,
            ):
                raise ReminderDeliveryReviewError("Linked personal work item repair failed")
            outcome = "resolved"
        else:
            outcome = "acknowledged_manual_followup"
        changed = conn.execute(
            """UPDATE personal_reminder_deliveries
               SET linked_item_review_outcome=?, linked_item_reviewer=?,
                   linked_item_evidence_ref=?, linked_item_review_note=?,
                   linked_item_reviewed_at=datetime('now'),
                   review_version=review_version+1, updated_at=datetime('now')
               WHERE run_id=? AND state='sent' AND review_version=?
                 AND linked_item_review_outcome IS NULL""",
            (outcome, actor.strip(), evidence_ref.strip(), note.strip(),
             run_id, expected_version),
        )
        if changed.rowcount != 1:
            raise ReminderDeliveryReviewError("Stale linked-item review")
        insert_activity(
            conn, actor=actor.strip(), surface="dashboard", touch_project=False,
            action_type="personal_reminder_linked_item_reviewed", object_ref=run_id,
            summary="Operator reviewed confirmed reminder linked-item failure",
            metadata_json=json.dumps({
                "decision": decision, "outcome": outcome,
                "evidence_ref": evidence_ref.strip(), "note": note.strip(),
                "original_error": row["last_error"], "work_item_id": work_item_id,
                "chat_id": expected_chat_id,
                "bot_fingerprint": expected_bot_fingerprint,
            }),
        )
        return {
            "run_id": run_id, "schedule_id": row["schedule_id"],
            "state": "sent", "linked_item_review_outcome": outcome,
        }
