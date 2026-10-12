"""Committed inbound information delivery, without replaying uncertain Telegram sends.

Only future project-update presentations use this ledger. It is independent of
review/undo cards, which require a proposed update and their own outbox.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
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


def enqueue_presentation(
    conn: sqlite3.Connection, *, email_id: str, project_id: str,
    kind: str, payload: dict[str, Any],
) -> bool:
    """Store immutable content and recipient identity in the business transaction."""
    if kind not in {"info_card", "plain"} or not email_id or not project_id:
        raise ValueError("invalid inbound presentation identity")
    if payload.get("project_id") != project_id:
        raise ValueError("presentation scope mismatch")
    encoded = json.dumps({**payload, "email_id": email_id}, ensure_ascii=False, sort_keys=True)
    prior = conn.execute(
        "SELECT project_id,kind,payload_json FROM inbound_presentation_outbox WHERE email_id=?",
        (email_id,),
    ).fetchone()
    if prior:
        if tuple(prior) != (project_id, kind, encoded):
            raise ValueError("inbound presentation identity collision")
        return False
    chat_id, fingerprint = _destination()
    bound = bool(chat_id and fingerprint)
    if not bound:
        chat_id = fingerprint = None
    now = _now().isoformat()
    conn.execute("""INSERT INTO inbound_presentation_outbox
        (email_id,project_id,kind,payload_json,chat_id,bot_fingerprint,
         state,available_at,last_error,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (email_id, project_id, kind, encoded, chat_id, fingerprint,
         "pending" if bound else "blocked", now,
         None if bound else "recipient_unbound", now, now),
    )
    return True



def block_presentation_for_reconciliation(conn: sqlite3.Connection, email_id: str) -> bool:
    """Suppress a success notice when the enclosing route cannot be certified."""
    return bool(conn.execute("""UPDATE inbound_presentation_outbox
        SET state='blocked',last_error='route_requires_reconciliation',updated_at=?
        WHERE email_id=? AND state IN ('pending','blocked')""",
        (_now().isoformat(), email_id)).rowcount)


def recover_expired_presentations(conn: sqlite3.Connection) -> int:
    """A lease expiry may follow an accepted Telegram send: never replay it."""
    now = _now().isoformat()
    return conn.execute("""UPDATE inbound_presentation_outbox
        SET state='uncertain',last_error='delivery_lease_expired',
            lease_token=NULL,lease_until=NULL,updated_at=?
        WHERE state='sending' AND lease_until<=?""", (now, now)).rowcount


@contextmanager
def _write_scope(conn: sqlite3.Connection | None):
    if conn is None:
        from src.db.connection import scoped_write
        with scoped_write() as owned:
            yield owned
    else:
        if conn.in_transaction:
            raise ValueError("presentation dispatch requires a committed caller transaction")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _send_info(payload: dict[str, Any], chat_id: int, fingerprint: str) -> dict:
    from src.email.analysis_notice import EmailAnalysisNotice
    from src.telegram.review_push import push_email_info_card_sync

    if payload.get("combined_advisory"):
        from src.db.connection import scoped_read
        from src.email.advisory_delivery_policy import read_advisory_context
        with scoped_read() as db:
            current = read_advisory_context(db, payload["project_id"])
            group = payload.get("combined_delivery_group")
            claims = [db.execute(f"SELECT * FROM {table} WHERE email_id=?", (payload.get("email_id"),)).fetchone()
                      for table in ("project_advisory_outbox", "inbound_presentation_outbox")]
            if (not group or any(row is None or row["state"] != "sending" or row["lease_token"] != group
                    or (row["project_id"], row["chat_id"], row["bot_fingerprint"]) != (payload["project_id"], chat_id, fingerprint)
                    for row in claims)
                    or current.pending_review_ids or current.snapshot_json != payload.get("advisory_snapshot")):
                return {"sent": False, "error": "project_state_changed"}
    return push_email_info_card_sync(
        None, project_id=payload["project_id"], summary=payload["summary"],
        suggested_action=payload.get("suggested_action") or "",
        sender_email=payload.get("sender_email"), subject=payload.get("subject"),
        client_name=payload.get("client_name"),
        notice=EmailAnalysisNotice.model_validate(payload["notice"]),
        chat_id=chat_id, expected_bot_fingerprint=fingerprint,
        require_persistence=True, source_email_id=payload.get("email_id"),
        combined_advisory=bool(payload.get("combined_advisory")),
        advisory_fingerprint=payload.get("advisory_fingerprint"),
        combined_delivery_group=payload.get("combined_delivery_group"),
        advisory_urgent=bool(payload.get("advisory_urgent")),
    )


def _send_plain(payload: dict[str, Any], chat_id: int, fingerprint: str) -> dict:
    import requests
    from config.settings import CONFIG
    from src.telegram.i18n import t

    token = str(CONFIG.telegram.bot_token or "")
    if not token or hashlib.sha256(token.encode()).hexdigest() != fingerprint:
        return {"sent": False, "error": "bot_identity_changed"}
    # Older committed intents may predate the frozen text field.
    text = payload.get("text") or t("policy.email_update_result").format(
        project_id=payload["project_id"], result=payload["result"],
    )[:4096]
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=10,
    )
    # A missing or malformed receipt is not proof that Telegram did not send.
    if not response.ok:
        return {"sent": False, "error": "delivery_unconfirmed"}
    result = response.json()
    message_id = result.get("result", {}).get("message_id") if result.get("ok") else None
    return {"sent": bool(message_id), "telegram_message_id": message_id,
            "error": None if message_id else "delivery_unconfirmed"}


def _send(kind: str, payload: dict, chat_id: int, fingerprint: str) -> dict:
    if kind == "info_card":
        return _send_info(payload, chat_id, fingerprint)
    return _send_plain(payload, chat_id, fingerprint)


def _imap_route_released(conn: sqlite3.Connection, email_id: str) -> bool:
    row = conn.execute(
        "SELECT source_type,route_state FROM inbound_email_work WHERE email_id=?",
        (email_id,),
    ).fetchone()
    return row is None or row[0] != "imap" or row[1] == "completed"


def deliver_presentation(
    email_id: str, *, conn: sqlite3.Connection | None = None,
    sender: Callable[[str, dict, int, str], dict] | None = None,
) -> str:
    """Claim before I/O; only known pre-send refusal can return to pending."""
    from src.email.combined_advisory_delivery import try_combined_presentation
    combined = try_combined_presentation(email_id, conn=conn, sender=sender)
    if combined is not None:
        return combined
    token = uuid4().hex
    now = _now()
    with _write_scope(conn) as db:
        recover_expired_presentations(db)
        row = db.execute(
            "SELECT * FROM inbound_presentation_outbox WHERE email_id=?", (email_id,),
        ).fetchone()
        if row is None:
            return "absent"
        if row["state"] != "pending":
            return row["state"]
        if row["available_at"] > now.isoformat():
            return "pending"
        # The final IMAP receipt and this eligibility predicate become visible
        # together on commit. A committed policy intent alone is not a route.
        if not _imap_route_released(db, email_id):
            return "pending"
        current_chat, current_fingerprint = _destination()
        if (current_chat != row["chat_id"] or current_fingerprint != row["bot_fingerprint"]):
            db.execute("""UPDATE inbound_presentation_outbox
                SET state='blocked',last_error='recipient_changed',updated_at=?
                WHERE email_id=? AND state='pending'""", (now.isoformat(), email_id))
            return "blocked"
        changed = db.execute("""UPDATE inbound_presentation_outbox
            SET state='sending',lease_token=?,lease_until=?,attempts=attempts+1,delivery_payload_json=NULL,
                updated_at=? WHERE email_id=? AND state='pending'
                AND NOT EXISTS (SELECT 1 FROM inbound_email_work AS route
                    WHERE route.email_id=inbound_presentation_outbox.email_id
                      AND route.source_type='imap' AND route.route_state!='completed')""",
            (token, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), email_id),
        ).rowcount
        if changed != 1:
            return "pending"
        kind, payload = row["kind"], json.loads(row["payload_json"])
        chat_id, fingerprint = row["chat_id"], row["bot_fingerprint"]
    try:
        result = (sender or _send)(kind, payload, chat_id, fingerprint)
        if not isinstance(result, dict):
            result = {}
    except Exception:
        logger.exception("Inbound presentation outcome unknown for %s", email_id)
        result = {}
    message_id = result.get("telegram_message_id")
    if result.get("sent") and message_id is not None:
        state, error = "sent", None
    elif result.get("error") in {"budget_exhausted", "telegram_not_configured"}:
        state, error = "pending", str(result["error"])
    else:
        state, error = "uncertain", str(result.get("error") or "delivery_unconfirmed")
    if result.get("error") in {"bot_identity_changed", "recipient_changed"}:
        state = "blocked"
    try:
        with _write_scope(conn) as db:
            changed = db.execute("""UPDATE inbound_presentation_outbox
                SET state=?,available_at=?,telegram_message_id=?,last_error=?,
                    lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE email_id=? AND state='sending' AND lease_token=?""",
                (state, (now + timedelta(minutes=5)).isoformat(),
                 str(message_id) if state == "sent" else None, error,
                 _now().isoformat(), email_id, token),
            ).rowcount
            if changed != 1:
                return "uncertain"
    except Exception:
        logger.exception("Inbound presentation receipt failed for %s", email_id)
        return "uncertain"
    return state


def process_pending_presentations(*, limit: int = 10) -> list[str]:
    from src.db.connection import scoped_write

    with scoped_write() as conn:
        recover_expired_presentations(conn)
        rows = conn.execute("""SELECT email_id FROM inbound_presentation_outbox
            WHERE state='pending' AND available_at<=?
              AND NOT EXISTS (SELECT 1 FROM inbound_email_work AS route
                  WHERE route.email_id=inbound_presentation_outbox.email_id
                    AND route.source_type='imap' AND route.route_state!='completed')
            ORDER BY available_at,email_id LIMIT ?""",
            (_now().isoformat(), max(0, min(limit, 50))),
        ).fetchall()
    return [deliver_presentation(row[0]) for row in rows]
