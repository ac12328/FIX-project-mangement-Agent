"""Commit-bound, suggestion-deduplicated delivery of contact identity cards.

An unknown Telegram outcome stays uncertain; a worker may only claim a
committed pending row. No network I/O occurs under a database writer lease.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _destination() -> tuple[int | None, str | None]:
    from config.settings import CONFIG

    telegram = CONFIG.telegram
    allowed = list(telegram.allowed_chat_ids or [])
    chat_id = telegram.chat_id or (allowed[0] if allowed else None)
    token = str(telegram.bot_token or "")
    return (int(chat_id) if chat_id else None,
            hashlib.sha256(token.encode()).hexdigest() if token else None)


def enqueue_contact_identity_card(conn: sqlite3.Connection, suggestion_id: str) -> bool:
    """Record the first card intent in the same transaction as its suggestion."""
    from src.telegram.cards import build_card, format_card_text
    from src.telegram.i18n import t
    from src.triage.contact_learning import _build_identity_body, get_contact_suggestion

    suggestion = get_contact_suggestion(conn, suggestion_id)
    if suggestion is None or suggestion["status"] != "pending":
        return False
    if suggestion.get("telegram_message_id") or suggestion.get("last_pushed_at"):
        return False
    existing = conn.execute(
        "SELECT 1 FROM contact_identity_card_outbox WHERE suggestion_id=?", (suggestion_id,),
    ).fetchone()
    if existing:
        return False
    chat_id, fingerprint = _destination()
    bound = bool(chat_id and fingerprint)
    card = build_card("E", t("contact.identity_title"), _build_identity_body(suggestion))
    now = _now().isoformat()
    conn.execute("""INSERT INTO contact_identity_card_outbox
        (suggestion_id,source_email_id,project_id,chat_id,bot_fingerprint,card_text,
         state,last_error,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            suggestion_id, suggestion.get("email_id"), suggestion.get("project_id"),
            chat_id if bound else None, fingerprint if bound else None,
            format_card_text(card), "pending" if bound else "blocked",
            None if bound else "recipient_unbound", now, now,
        ))
    return True


@contextmanager
def _write_scope(conn: sqlite3.Connection | None):
    if conn is None:
        from src.db.connection import scoped_write
        with scoped_write() as owned:
            yield owned
    else:
        if conn.in_transaction:
            raise ValueError("identity card dispatch requires a committed transaction")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def recover_expired_identity_cards(conn: sqlite3.Connection) -> int:
    """A timed-out claim may have sent; do not replay it."""
    now = _now().isoformat()
    return conn.execute("""UPDATE contact_identity_card_outbox
        SET state='uncertain',last_error='delivery_lease_expired',
            lease_token=NULL,lease_until=NULL,updated_at=?
        WHERE state='sending' AND lease_until<=?""", (now, now)).rowcount


async def _send_async(chat_id: int, suggestion_id: str, text: str,
                      source_email_id: str | None, project_id: str | None,
                      bot_token: str, bot_fingerprint: str) -> dict[str, Any]:
    from telegram import Bot
    from src.telegram.cards import build_card
    from src.telegram.i18n import t
    from src.telegram.markup import card_to_markup
    from src.telegram.runtime import send_persisted_message

    card = build_card("E", t("contact.identity_title"), "")
    for row in card.buttons:
        for button in row:
            action = button.label.upper().replace(" ", "_")
            button.callback_data = f"E:{action}:{suggestion_id}"
    links = [{"object_type": "contact_suggestion", "object_ref": suggestion_id,
              "project_id": project_id}]
    if source_email_id:
        links.append({"object_type": "email", "object_ref": source_email_id,
                      "project_id": project_id})
    ids = await send_persisted_message(
        Bot(token=bot_token), None, chat_id=chat_id, text=text,
        project_id=project_id, message_kind="card", reply_markup=card_to_markup(card),
        object_links=links, require_persistence=True,
        metadata={"card_profile": "E", "contact_suggestion_id": suggestion_id,
                  "source_email_id": source_email_id,
                  "bot_fingerprint": bot_fingerprint},
    )
    return {"sent": bool(ids), "telegram_message_id": ids[-1] if ids else None}


def _send(chat_id: int, fingerprint: str, suggestion_id: str, text: str,
          source_email_id: str | None, project_id: str | None) -> dict[str, Any]:
    from config.settings import CONFIG
    from src.db.connection import scoped_read

    with scoped_read() as read_conn:
        suggestion = read_conn.execute(
            "SELECT status,telegram_message_id,last_pushed_at "
            "FROM contact_suggestions WHERE suggestion_id=?", (suggestion_id,),
        ).fetchone()
    if suggestion is None or suggestion["status"] != "pending" or suggestion["telegram_message_id"] or suggestion["last_pushed_at"]:
        return {"sent": False, "error": "suggestion_resolved"}

    telegram = CONFIG.telegram
    token = str(telegram.bot_token or "")
    allowed = list(telegram.allowed_chat_ids or [])
    current_chat = telegram.chat_id or (allowed[0] if allowed else None)
    if not token or current_chat != chat_id or hashlib.sha256(token.encode()).hexdigest() != fingerprint:
        return {"sent": False, "error": "recipient_changed"}

    def run() -> dict[str, Any]:
        return asyncio.run(_send_async(chat_id, suggestion_id, text,
                                       source_email_id, project_id, token, fingerprint))

    from src.utils.async_helpers import has_running_loop
    if not has_running_loop():
        return run()
    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            result.update(run())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True, name="tg-contact-card")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result


def deliver_contact_identity_card(
    suggestion_id: str, *, conn: sqlite3.Connection | None = None,
    sender: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Claim once, deliver outside writer, atomically record both local receipts."""
    token = uuid4().hex
    with _write_scope(conn) as db:
        recover_expired_identity_cards(db)
        row = db.execute("""SELECT o.*,s.status AS suggestion_status,
                s.telegram_message_id AS suggestion_message_id,s.last_pushed_at
            FROM contact_identity_card_outbox o
            JOIN contact_suggestions s USING(suggestion_id)
            WHERE o.suggestion_id=?""", (suggestion_id,)).fetchone()
        if row is None:
            return "absent"
        if row["state"] != "pending":
            return row["state"]
        if row["suggestion_status"] != "pending" or row["suggestion_message_id"] or row["last_pushed_at"]:
            db.execute("""UPDATE contact_identity_card_outbox
                SET state='skipped',last_error='suggestion_resolved_or_sent',updated_at=?
                WHERE suggestion_id=? AND state='pending'""", (_now().isoformat(), suggestion_id))
            return "skipped"
        chat_id, fingerprint = row["chat_id"], row["bot_fingerprint"]
        if not chat_id or not fingerprint or _destination() != (chat_id, fingerprint):
            db.execute("""UPDATE contact_identity_card_outbox
                SET state='blocked',last_error='recipient_changed',updated_at=?
                WHERE suggestion_id=? AND state='pending'""", (_now().isoformat(), suggestion_id))
            return "blocked"
        now = _now()
        changed = db.execute("""UPDATE contact_identity_card_outbox
            SET state='sending',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=?
            WHERE suggestion_id=? AND state='pending'""", (
                token, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), suggestion_id,
            )).rowcount
        if changed != 1:
            return "claimed_elsewhere"
        text, source_email_id, project_id = row["card_text"], row["source_email_id"], row["project_id"]
    # A committed claim is the sole permission to perform a remote send.
    try:
        result = (sender or _send)(chat_id, fingerprint, suggestion_id, text,
                                   source_email_id, project_id)
        if not isinstance(result, dict):
            result = {}
    except Exception:
        logger.exception("Contact identity card delivery outcome unknown for %s", suggestion_id)
        result = {}
    message_id = result.get("telegram_message_id")
    sent = result.get("sent") is True and type(message_id) is int and message_id > 0
    blocked = result.get("error") in {"recipient_changed", "bot_identity_changed"}
    skipped = result.get("error") == "suggestion_resolved"
    state = "sent" if sent else "blocked" if blocked else "skipped" if skipped else "uncertain"
    error = None if sent else str(result.get("error") or "delivery_unconfirmed")
    try:
        with _write_scope(conn) as db:
            changed = db.execute("""UPDATE contact_identity_card_outbox
                SET state=?,telegram_message_id=?,last_error=?,lease_token=NULL,
                    lease_until=NULL,updated_at=?
                WHERE suggestion_id=? AND state='sending' AND lease_token=?""", (
                    state, message_id if sent else None, error, _now().isoformat(),
                    suggestion_id, token,
                )).rowcount
            if changed != 1:
                return "uncertain"
            if sent:
                db.execute("""UPDATE contact_suggestions
                    SET telegram_message_id=?,last_pushed_at=?,updated_at=?
                    WHERE suggestion_id=? AND telegram_message_id IS NULL""", (
                        str(message_id), _now().isoformat(), _now().isoformat(), suggestion_id,
                    ))
    except Exception:
        logger.exception("Contact identity card receipt failed for %s", suggestion_id)
        return "uncertain"
    return state


def process_pending_contact_identity_cards(*, limit: int = 10) -> list[str]:
    from src.db.connection import scoped_write

    with scoped_write() as conn:
        recover_expired_identity_cards(conn)
        rows = conn.execute("""SELECT suggestion_id FROM contact_identity_card_outbox
            WHERE state='pending' ORDER BY created_at,suggestion_id LIMIT ?""",
            (max(0, min(limit, 50)),)).fetchall()
    return [deliver_contact_identity_card(row[0]) for row in rows]
