"""Committed operational draft cards with no automatic replay after uncertain sends."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import sqlite3
from uuid import uuid4

from config.settings import CONFIG
from src.db.connection import scoped_write
from src.utils.notification_budget import reserve_notification_attempt
from src.utils.push_guard import URGENCY_NORMAL

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _destination(chat_id: int | None = None) -> tuple[int | None, str | None, str]:
    token = str(CONFIG.telegram.bot_token or "")
    allowed = list(CONFIG.telegram.allowed_chat_ids or [])
    chosen = chat_id or CONFIG.telegram.chat_id or (allowed[0] if allowed else None)
    return (int(chosen) if chosen else None,
            hashlib.sha256(token.encode()).hexdigest() if token else None, token)


def enqueue_draft_card(
    conn: sqlite3.Connection, *, draft_id: str, to_email: str, subject: str,
    chat_id: int | None = None, attachments_pending: bool = False,
) -> None:
    """Persist destination-bound intent in the caller's draft transaction."""
    recipient, fingerprint, _ = _destination(chat_id)
    existing = conn.execute(
        "SELECT to_email,subject,chat_id,bot_fingerprint FROM draft_card_outbox "
        "WHERE draft_id=?", (draft_id,),
    ).fetchone()
    if existing:
        if tuple(existing) != (to_email, subject, recipient, fingerprint):
            raise ValueError("Draft card identity collision")
        return
    now = _now()
    conn.execute(
        "INSERT INTO draft_card_outbox "
        "(draft_id,to_email,subject,chat_id,bot_fingerprint,attachment_state,"
        "available_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (draft_id, to_email, subject, recipient, fingerprint,
         "preparing" if attachments_pending else "ready", now, now, now),
    )


def complete_draft_card_attachments(
    conn: sqlite3.Connection, draft_id: str, subject: str,
) -> None:
    """Release a prepared intent only after attachment coverage is known."""
    changed = conn.execute(
        "UPDATE draft_card_outbox SET subject=?,attachment_state='ready',updated_at=? "
        "WHERE draft_id=? AND state='pending' AND attachment_state='preparing'",
        (subject, _now(), draft_id),
    ).rowcount
    if changed != 1:
        raise ValueError("Draft card attachment preparation cannot be completed")


def recover_stale_attachment_preparations(conn: sqlite3.Connection) -> int:
    """Quarantine interrupted preparation; never send an unverified card."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    changed = conn.execute(
        "UPDATE draft_card_outbox SET attachment_state='needs_review',"
        "last_error='attachment_preparation_interrupted',updated_at=? "
        "WHERE state='pending' AND attachment_state='preparing' AND updated_at<=?",
        (_now(), cutoff),
    ).rowcount
    if changed:
        logger.error("%d draft cards need attachment reconciliation", changed)
    return changed


def recover_expired_draft_cards(conn: sqlite3.Connection) -> int:
    """An expired claim might have reached Telegram; do not resend it."""
    now = _now()
    return conn.execute(
        "UPDATE draft_card_outbox SET state='uncertain',"
        "last_error='delivery_lease_expired',lease_token=NULL,lease_until=NULL,updated_at=? "
        "WHERE state='sending' AND lease_until<=?", (now, now),
    ).rowcount


def deliver_draft_card(draft_id: str, *, sender=None) -> str:
    """Claim a committed intent before any model/Telegram I/O."""
    lease = uuid4().hex
    now = _now()
    with scoped_write() as conn:
        recover_expired_draft_cards(conn)
        row = conn.execute(
            "SELECT * FROM draft_card_outbox WHERE draft_id=?", (draft_id,),
        ).fetchone()
        if row is None:
            return "missing"
        row = dict(row)
        if row["state"] != "pending":
            return row["state"]
        if row["attachment_state"] != "ready":
            return row["attachment_state"]
        if row["available_at"] > now:
            return "pending"
        draft = conn.execute(
            "SELECT status FROM drafts WHERE draft_id=?", (draft_id,),
        ).fetchone()
        if draft is None or draft["status"] not in {"created", "in_review", "approved"}:
            conn.execute(
                "UPDATE draft_card_outbox SET state='cancelled',last_error='draft_not_reviewable',"
                "updated_at=? WHERE draft_id=? AND state='pending'", (now, draft_id),
            )
            return "cancelled"
        current_chat, current_fingerprint, token = _destination()
        allowed_chats = {int(chat) for chat in (CONFIG.telegram.allowed_chat_ids or [])}
        if current_chat:
            allowed_chats.add(current_chat)
        if not row["chat_id"] or not row["bot_fingerprint"]:
            refusal = "recipient_unbound"
        elif row["chat_id"] not in allowed_chats or current_fingerprint != row["bot_fingerprint"]:
            refusal = "recipient_changed"
        elif not reserve_notification_attempt(conn, URGENCY_NORMAL):
            refusal = "budget_exhausted"
        else:
            refusal = None
        if refusal:
            conn.execute(
                "UPDATE draft_card_outbox SET last_error=?,available_at=?,updated_at=? "
                "WHERE draft_id=? AND state='pending'",
                (refusal, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                 now, draft_id),
            )
            return "pending"
        conn.execute(
            "UPDATE draft_card_outbox SET state='sending',attempts=attempts+1,"
            "lease_token=?,lease_until=?,updated_at=? WHERE draft_id=? AND state='pending'",
            (lease, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             now, draft_id),
        )
    if sender is None:
        from src.telegram.card_push import send_claimed_operational_draft_card_sync
        sender = send_claimed_operational_draft_card_sync
    try:
        result = sender(
            draft_id=draft_id, to_email=row["to_email"], subject=row["subject"],
            chat_id=row["chat_id"], token=token,
        )
        ids = result.get("telegram_message_ids")
        if (result.get("sent") is True and isinstance(ids, list) and ids
                and all(type(mid) is int and mid > 0 for mid in ids)):
            state, error = "sent", None
        else:
            state, error = "uncertain", "delivery_unconfirmed"
    except Exception:
        logger.exception("Draft card delivery uncertain for %s", draft_id)
        ids = None
        state, error = "uncertain", "delivery_unconfirmed"
    try:
        with scoped_write() as conn:
            changed = conn.execute(
                "UPDATE draft_card_outbox SET state=?,telegram_message_ids_json=?,"
                "last_error=?,lease_token=NULL,lease_until=NULL,updated_at=? "
                "WHERE draft_id=? AND state='sending' AND lease_token=?",
                (state, json.dumps(ids) if state == "sent" else None, error,
                 _now(), draft_id, lease),
            ).rowcount
        return state if changed == 1 else "uncertain"
    except Exception:
        logger.exception("Could not finalize draft card receipt for %s", draft_id)
        return "uncertain"


def process_pending_draft_cards(*, limit: int = 10) -> list[str]:
    with scoped_write() as conn:
        recover_expired_draft_cards(conn)
        recover_stale_attachment_preparations(conn)
        rows = conn.execute(
            "SELECT draft_id FROM draft_card_outbox WHERE state='pending' "
            "AND attachment_state='ready' "
            "AND available_at<=? ORDER BY available_at,draft_id LIMIT ?",
            (_now(), max(0, min(limit, 50))),
        ).fetchall()
        draft_ids = [row[0] for row in rows]
    return [deliver_draft_card(draft_id) for draft_id in draft_ids]
