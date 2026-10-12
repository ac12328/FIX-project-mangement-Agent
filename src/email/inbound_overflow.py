"""Persist IMAP overflow cards before Telegram I/O without replaying uncertain sends."""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence

from src.email.inbound_work import RouteDeliveryIntent


def enqueue_overflow_item(
    conn: sqlite3.Connection, email_id: str, token: str, chat_id: int, summary: str,
) -> None:
    """Insert only while the source's route lease is held; caller commits with finish."""
    if not chat_id or not summary:
        raise ValueError("Overflow item requires a recipient and summary")
    inserted = conn.execute("""INSERT INTO inbound_overflow_items(email_id, chat_id, summary)
        SELECT email_id, ?, ? FROM inbound_email_work
        WHERE email_id=? AND source_type='imap' AND route_state='routing'
          AND lease_token=?""", (chat_id, summary, email_id, token)).rowcount
    if inserted != 1:
        raise RuntimeError(f"Inbound overflow enqueue lost route lease for {email_id}")


def pending_overflow_items(
    conn: sqlite3.Connection, chat_id: int, *, limit: int = 100,
) -> list[tuple[str, str]]:
    """Pending means no external send has begun, including prior poll cycles."""
    return [tuple(row) for row in conn.execute("""SELECT email_id, summary
        FROM inbound_overflow_items WHERE chat_id=? AND batch_id IS NULL
        ORDER BY created_at, email_id LIMIT ?""", (chat_id, limit))]


def claim_overflow_batch(
    conn: sqlite3.Connection, chat_id: int,
    items: Sequence[tuple[str, str]], text: str,
) -> str:
    """Claim the precise items with a committed recipient/content identity."""
    if not items or len({email_id for email_id, _ in items}) != len(items):
        raise ValueError("Overflow batch needs unique source items")
    intent = RouteDeliveryIntent.for_message(chat_id, text)
    batch_id = uuid.uuid4().hex
    conn.execute("""INSERT INTO inbound_overflow_batches
        (batch_id, chat_id, text, content_sha256, state)
        VALUES (?, ?, ?, ?, 'sending')""",
        (batch_id, chat_id, text, intent.content_sha256))
    for email_id, _ in items:
        updated = conn.execute("""UPDATE inbound_overflow_items
            SET batch_id=? WHERE email_id=? AND chat_id=? AND batch_id IS NULL""",
            (batch_id, email_id, chat_id)).rowcount
        if updated != 1:
            raise RuntimeError(f"Overflow item changed before claim: {email_id}")
    return batch_id


def record_overflow_result(
    conn: sqlite3.Connection, batch_id: str, message_ids: list[int],
) -> None:
    """Record local accepted-send IDs; an absent receipt remains uncertain."""
    if (not message_ids or len(set(message_ids)) != len(message_ids)
            or any(type(mid) is not int or mid <= 0 for mid in message_ids)):
        raise ValueError("Overflow card needs locally recorded Telegram IDs")
    updated = conn.execute("""UPDATE inbound_overflow_batches
        SET state='locally_recorded', message_ids_json=?,
            review_version=review_version+1, updated_at=datetime('now')
        WHERE batch_id=? AND state='sending'
          AND NOT EXISTS (SELECT 1 FROM inbound_overflow_resolutions WHERE batch_id=?)""",
        (json.dumps(message_ids), batch_id, batch_id)).rowcount
    if updated != 1:
        raise RuntimeError(f"Overflow batch receipt lost for {batch_id}")
