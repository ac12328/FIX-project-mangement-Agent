"""Admin attestation of complete overflow sends, without Telegram replay."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class OverflowReviewError(ValueError):
    """The supplied external evidence does not resolve this batch safely."""


def list_unresolved_overflow(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """Expose immutable send intent and local receipts to an authenticated reviewer."""
    rows = conn.execute("""SELECT b.batch_id,b.chat_id,b.text,b.content_sha256,b.state,
        b.message_ids_json,b.review_version,b.created_at,b.updated_at
        FROM inbound_overflow_batches b
        LEFT JOIN inbound_overflow_resolutions r ON r.batch_id=b.batch_id
        WHERE r.batch_id IS NULL ORDER BY b.created_at,b.batch_id LIMIT ?""",
        (max(1, min(limit, 100)),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["message_ids"] = json.loads(item.pop("message_ids_json")) if item["message_ids_json"] else []
        item["source_email_ids"] = [r[0] for r in conn.execute(
            "SELECT email_id FROM inbound_overflow_items WHERE batch_id=? ORDER BY email_id",
            (item["batch_id"],))]
        item["local_receipts"] = [dict(r) for r in conn.execute("""SELECT telegram_message_id,text
            FROM telegram_messages WHERE chat_id=? AND direction='outbound'
              AND message_kind='card'
              AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json
                                    ELSE '{}' END, '$.overflow_batch_id')=?
            ORDER BY id""", (item["chat_id"], item["batch_id"]))]
        result.append(item)
    return result


def _check_receipts(
    conn: sqlite3.Connection, batch: sqlite3.Row, message_ids: list[int],
) -> None:
    chat_id, batch_id = batch["chat_id"], batch["batch_id"]
    local = conn.execute("""SELECT telegram_message_id FROM telegram_messages
        WHERE chat_id=? AND direction='outbound' AND message_kind='card'
          AND json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json
                                ELSE '{}' END, '$.overflow_batch_id')=?
        ORDER BY id""", (chat_id, batch_id)).fetchall()
    local_ids = [r[0] for r in local]
    if len(local_ids) != len(set(local_ids)) or not set(local_ids) <= set(message_ids):
        raise OverflowReviewError("external IDs conflict with local batch receipts")
    # A partially persisted multi-chunk send may leave a subsequence, never a different order.
    selected = [mid for mid in message_ids if mid in set(local_ids)]
    if selected != local_ids:
        raise OverflowReviewError("external chunk order conflicts with local receipts")
    if batch["state"] == "locally_recorded":
        try:
            recorded = json.loads(batch["message_ids_json"])
        except (TypeError, ValueError) as exc:
            raise OverflowReviewError("invalid locally recorded receipt") from exc
        if recorded != message_ids or local_ids != message_ids:
            raise OverflowReviewError("external IDs conflict with completed local receipt")
    for mid in message_ids:
        existing = conn.execute("""SELECT direction,message_kind,metadata_json FROM telegram_messages
            WHERE chat_id=? AND telegram_message_id=?""", (chat_id, mid)).fetchone()
        if existing:
            try:
                metadata = json.loads(existing["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if (existing["direction"] != "outbound" or existing["message_kind"] != "card"
                    or metadata.get("overflow_batch_id") != batch_id):
                raise OverflowReviewError("Telegram message ID belongs to another message")
        conflict = conn.execute("""SELECT 1 FROM inbound_overflow_resolutions r
            JOIN inbound_overflow_batches b ON b.batch_id=r.batch_id
            WHERE b.chat_id=? AND r.batch_id!=?
              AND EXISTS (SELECT 1 FROM json_each(r.message_ids_json) WHERE value=?)""",
            (chat_id, batch_id, mid)).fetchone()
        if conflict:
            raise OverflowReviewError("Telegram message ID belongs to another resolved batch")


def confirm_overflow_sent(
    conn: sqlite3.Connection, *, batch_id: str, actor: str,
    expected_review_version: int, expected_state: str, expected_chat_id: int,
    expected_content_sha256: str, telegram_message_ids: list[int], evidence_ref: str,
    full_content_verified: bool,
) -> dict[str, Any]:
    """Attest every externally inspected chunk; never infer non-delivery or resend."""
    from src.repositories.activity_log_repo import insert_activity

    if (not batch_id.strip() or not actor.strip() or not evidence_ref.strip()
            or len(expected_content_sha256) != 64 or full_content_verified is not True):
        raise OverflowReviewError("identity, independent evidence and full-content attestation required")
    if (type(expected_review_version) is not int or expected_review_version < 0
            or type(expected_chat_id) is not int or not expected_chat_id
            or expected_state not in ("sending", "locally_recorded")):
        raise OverflowReviewError("valid batch version and recipient required")
    if (not telegram_message_ids or len(telegram_message_ids) > 100
            or any(type(mid) is not int or mid <= 0 for mid in telegram_message_ids)
            or len(set(telegram_message_ids)) != len(telegram_message_ids)):
        raise OverflowReviewError("unique positive IDs for every verified chunk required")
    batch = conn.execute("SELECT * FROM inbound_overflow_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if batch is None:
        raise KeyError(batch_id)
    if (batch["review_version"] != expected_review_version or batch["state"] != expected_state
            or batch["chat_id"] != expected_chat_id
            or batch["content_sha256"] != expected_content_sha256
            or conn.execute("SELECT 1 FROM inbound_overflow_resolutions WHERE batch_id=?", (batch_id,)).fetchone()):
        raise OverflowReviewError("stale or already resolved overflow batch")
    _check_receipts(conn, batch, telegram_message_ids)
    source_ids = [row[0] for row in conn.execute(
        "SELECT email_id FROM inbound_overflow_items WHERE batch_id=? ORDER BY email_id", (batch_id,))]
    if not source_ids:
        raise OverflowReviewError("batch has no linked inbound sources")

    conn.execute("SAVEPOINT confirm_overflow_sent")
    try:
        changed = conn.execute("""UPDATE inbound_overflow_batches SET review_version=review_version+1,
            updated_at=datetime('now') WHERE batch_id=? AND review_version=? AND state=?
            AND chat_id=? AND content_sha256=?""", (batch_id, expected_review_version,
            expected_state, expected_chat_id, expected_content_sha256)).rowcount
        if changed != 1:
            raise OverflowReviewError("stale overflow batch review")
        conn.execute("""INSERT INTO inbound_overflow_resolutions
            (batch_id,decision,message_ids_json,evidence_ref,actor)
            VALUES (?,'confirmed_sent',?,?,?)""", (batch_id, json.dumps(telegram_message_ids),
            evidence_ref.strip(), actor.strip()))
        insert_activity(conn, actor=actor.strip(), action_type="inbound_overflow_reconciled",
            surface="dashboard", object_ref=batch_id, summary="Operator attested complete overflow card send",
            touch_project=False, metadata_json=json.dumps({
                "decision": "confirmed_sent", "chat_id": expected_chat_id,
                "content_sha256": expected_content_sha256, "message_ids": telegram_message_ids,
                "source_email_ids": source_ids, "evidence_ref": evidence_ref.strip(),
                "prior_state": expected_state,
            }, ensure_ascii=False))
        conn.execute("RELEASE SAVEPOINT confirm_overflow_sent")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT confirm_overflow_sent")
        conn.execute("RELEASE SAVEPOINT confirm_overflow_sent")
        raise
    return {"batch_id": batch_id, "decision": "confirmed_sent",
            "review_version": expected_review_version + 1}
