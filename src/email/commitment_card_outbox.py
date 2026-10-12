"""Reply-linked commitment cards: durable intent, short claims, no blind replay."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import sqlite3
from uuid import uuid4

from config.settings import CONFIG

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _destination() -> tuple[int | None, str | None]:
    token = str(CONFIG.telegram.bot_token or "")
    allowed = list(CONFIG.telegram.allowed_chat_ids or [])
    chat_id = CONFIG.telegram.chat_id or (allowed[0] if allowed else None)
    return (int(chat_id) if chat_id else None,
            hashlib.sha256(token.encode()).hexdigest() if token else None)


@contextmanager
def _db(conn: sqlite3.Connection | None):
    if conn is not None:
        yield conn
    else:
        from src.db.connection import scoped_write
        with scoped_write() as scoped:
            yield scoped


def enqueue_commitment_card(
    conn: sqlite3.Connection, *, reply_id: str, project_id: str,
    commitment_text: str, due_date: str,
) -> None:
    """Record delivery intent in the same transaction as its inbound reply."""
    chat_id, fingerprint = _destination()
    now = _now()
    existing = conn.execute(
        "SELECT project_id,commitment_text,due_date FROM email_commitment_card_outbox "
        "WHERE reply_id=?", (reply_id,),
    ).fetchone()
    if existing:
        if tuple(existing) != (project_id, commitment_text, due_date):
            raise ValueError("Commitment card identity collision")
        return
    conn.execute(
        "INSERT INTO email_commitment_card_outbox "
        "(reply_id,project_id,commitment_text,due_date,chat_id,bot_fingerprint,"
        "available_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (reply_id, project_id, commitment_text, due_date, chat_id, fingerprint,
         now, now, now),
    )


def recover_expired_commitment_cards(conn: sqlite3.Connection) -> int:
    """An expired claim might have reached Telegram; require reconciliation."""
    now = _now()
    return conn.execute(
        "UPDATE email_commitment_card_outbox SET state='uncertain',"
        "last_error='delivery_lease_expired',lease_token=NULL,lease_until=NULL,updated_at=? "
        "WHERE state='sending' AND lease_until<=?", (now, now),
    ).rowcount


def deliver_commitment_card(
    reply_id: str, *, conn: sqlite3.Connection | None = None, sender=None,
) -> str:
    """Claim before send; only proven pre-send refusals return to pending."""
    token = uuid4().hex
    now = _now()
    with _db(conn) as db:
        if conn is not None and db.in_transaction:
            raise ValueError("Commitment card delivery requires a committed reply")
        recover_expired_commitment_cards(db)
        row = db.execute(
            "SELECT * FROM email_commitment_card_outbox WHERE reply_id=?", (reply_id,),
        ).fetchone()
        if row is None:
            db.commit()
            return "missing"
        row = dict(row)
        if row["state"] != "pending":
            db.commit()
            return row["state"]
        current_chat, current_fingerprint = _destination()
        if not row["chat_id"] or not row["bot_fingerprint"]:
            refusal = "recipient_unbound"
        elif current_chat != row["chat_id"] or current_fingerprint != row["bot_fingerprint"]:
            refusal = "recipient_changed"
        else:
            refusal = None
        if refusal:
            db.execute(
                "UPDATE email_commitment_card_outbox SET last_error=?,available_at=?,"
                "updated_at=? WHERE reply_id=? AND state='pending'",
                (refusal, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                 now, reply_id),
            )
            db.commit()
            return "pending"
        claimed = db.execute(
            "UPDATE email_commitment_card_outbox SET state='sending',lease_token=?,"
            "lease_until=?,attempts=attempts+1,updated_at=? WHERE reply_id=? "
            "AND state='pending' AND available_at<=?",
            (token, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             now, reply_id, now),
        ).rowcount
        db.commit()
    if claimed != 1:
        return "pending"

    if sender is None:
        from src.telegram.card_push import push_commitment_card_sync
        sender = push_commitment_card_sync
    try:
        result = sender(
            None, project_id=row["project_id"],
            commitment_text=row["commitment_text"], due_date=row["due_date"],
            reply_id=reply_id, chat_id=row["chat_id"],
            expected_bot_fingerprint=row["bot_fingerprint"],
        )
        if result.get("sent") is True and result.get("telegram_message_id") is not None:
            state, error = "sent", None
        elif result.get("sent") is False and result.get("error") in {
            "telegram_not_configured", "budget_exhausted", "bot_identity_changed",
        }:
            state, error = "pending", result["error"]
        else:
            state, error = "uncertain", "delivery_unconfirmed"
    except Exception:
        logger.exception("Commitment card delivery uncertain for %s", reply_id)
        result = {}
        state, error = "uncertain", "delivery_unconfirmed"

    try:
        with _db(conn) as db:
            updated = db.execute(
                "UPDATE email_commitment_card_outbox SET state=?,telegram_message_id=?,"
                "last_error=?,available_at=?,lease_token=NULL,lease_until=NULL,updated_at=? "
                "WHERE reply_id=? AND state='sending' AND lease_token=?",
                (state, str(result["telegram_message_id"]) if state == "sent" else None,
                 error, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                 _now(), reply_id, token),
            ).rowcount
            db.commit()
            return state if updated == 1 else "uncertain"
    except Exception:
        logger.exception("Could not finalize commitment card receipt for %s", reply_id)
        return "uncertain"


def process_pending_commitment_cards(*, limit: int = 10) -> list[str]:
    with _db(None) as conn:
        recover_expired_commitment_cards(conn)
        rows = conn.execute(
            "SELECT reply_id FROM email_commitment_card_outbox "
            "WHERE state='pending' AND available_at<=? "
            "ORDER BY available_at,reply_id LIMIT ?", (_now(), max(0, min(limit, 50))),
        ).fetchall()
    return [deliver_commitment_card(row[0]) for row in rows]
