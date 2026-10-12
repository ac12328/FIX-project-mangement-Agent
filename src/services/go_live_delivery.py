"""Durable, at-most-one automatic attempt for an M8 Telegram notice.

An expired in-flight send is uncertain, never automatically retried: Telegram
cannot provide an idempotency key. A human must reconcile an uncertain row
against Telegram before deciding whether to retry it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from src.models.enums import Milestone
from src.utils.push_guard import URGENCY_URGENT

logger = logging.getLogger(__name__)
_LEASE = timedelta(minutes=5)
_RETRY = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _destination() -> tuple[int, str] | None:
    from config.settings import CONFIG

    telegram = CONFIG.telegram
    fallback = list(getattr(telegram, "allowed_chat_ids", []) or [])
    chat_id = telegram.chat_id or (fallback[0] if fallback else 0)
    token = str(telegram.bot_token or "")
    if not chat_id or not token:
        return None
    return int(chat_id), hashlib.sha256(token.encode()).hexdigest()


def enqueue_go_live_telegram(conn: sqlite3.Connection, project_id: str, text: str) -> bool:
    """Enqueue on the caller's M8 transaction, without committing or sending."""
    destination = _destination()
    if destination is None:
        return False
    chat_id, fingerprint = destination
    prior = conn.execute(
        "SELECT chat_id,bot_fingerprint,text FROM go_live_telegram_outbox WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if prior is not None:
        if (prior[0], prior[1], prior[2]) != (chat_id, fingerprint, text):
            raise ValueError("Go-live Telegram delivery identity collision")
        return False
    now = _now().isoformat()
    conn.execute(
        """INSERT INTO go_live_telegram_outbox
           (project_id,chat_id,bot_fingerprint,text,available_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (project_id, chat_id, fingerprint, text, now, now, now),
    )
    return True


def _notice_receipt(
    conn: sqlite3.Connection, project_id: str, chat_id: int,
) -> sqlite3.Row | None:
    from src.services.go_live_notice import GO_LIVE_NOTICE_KIND

    return conn.execute(
        """SELECT telegram_message_id FROM telegram_messages
           WHERE project_id=? AND chat_id=? AND direction='outbound'
             AND json_extract(metadata_json, '$.notice')=?
           ORDER BY (typeof(telegram_message_id)='integer' AND telegram_message_id>0) DESC,
                    created_at DESC, telegram_message_id DESC LIMIT 1""",
        (project_id, chat_id, GO_LIVE_NOTICE_KIND),
    ).fetchone()


def _positive_message_id(value: Any) -> str | None:
    """Only a real positive integer can identify a Telegram receipt."""
    return str(value) if type(value) is int and value > 0 else None


def _sent_message_id(conn: sqlite3.Connection, project_id: str, chat_id: int) -> str | None:
    row = _notice_receipt(conn, project_id, chat_id)
    return _positive_message_id(row[0]) if row is not None else None


def _normalize_legacy_sent_receipts(conn: sqlite3.Connection, now: str) -> int:
    """Expose old false-success rows to review without replaying or recharging."""
    rows = conn.execute(
        """SELECT project_id,chat_id FROM go_live_telegram_outbox
           WHERE state='sent' AND (telegram_message_id IS NULL
             OR telegram_message_id=''
             OR telegram_message_id GLOB '*[^0-9]*'
             OR telegram_message_id NOT GLOB '*[1-9]*')""",
    ).fetchall()
    for project_id, chat_id in rows:
        sent_id = _sent_message_id(conn, project_id, chat_id)
        conn.execute(
            """UPDATE go_live_telegram_outbox SET state=?,telegram_message_id=?,
               last_error=?,lease_token=NULL,lease_until=NULL,updated_at=?
               WHERE project_id=? AND state='sent'""",
            ("sent" if sent_id else "uncertain", sent_id,
             None if sent_id else "delivery_unconfirmed", now, project_id),
        )
        if sent_id is None:
            logger.warning("Legacy go-live Telegram receipt uncertain for %s; manual reconciliation required", project_id)
    return len(rows)


def recover_expired_go_live_deliveries(conn: sqlite3.Connection) -> int:
    """Reconcile stored sends; quarantine invalid legacy sent and expired claims."""
    now = _now().isoformat()
    normalized = _normalize_legacy_sent_receipts(conn, now)
    rows = conn.execute(
        """SELECT project_id,chat_id FROM go_live_telegram_outbox
           WHERE state='sending' AND lease_until<=?""",
        (now,),
    ).fetchall()
    for project_id, chat_id in rows:
        sent_id = _sent_message_id(conn, project_id, chat_id)
        conn.execute(
            """UPDATE go_live_telegram_outbox
               SET state=?, telegram_message_id=?, last_error=?, lease_token=NULL,
                   lease_until=NULL, updated_at=?
               WHERE project_id=? AND state='sending' AND lease_until<=?""",
            ("sent" if sent_id else "uncertain", sent_id,
             None if sent_id else "delivery_lease_expired", now, project_id, now),
        )
        if sent_id:
            from src.utils.notification_budget import consume_notification

            consume_notification(conn, commit=False)
        else:
            logger.warning("Go-live Telegram delivery uncertain for %s; manual reconciliation required", project_id)
    return normalized + len(rows)


def deliver_go_live_telegram(
    project_id: str,
    *,
    sender: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Claim, send outside any write transaction, then finalize the receipt."""
    from src.db.connection import scoped_write
    from src.utils.notification_budget import should_push_with_budget

    now = _now()
    lease_token = uuid4().hex
    with scoped_write() as db:
        recover_expired_go_live_deliveries(db)
        row = db.execute(
            "SELECT * FROM go_live_telegram_outbox WHERE project_id=?", (project_id,),
        ).fetchone()
        if row is None:
            return "missing"
        row = dict(row)
        if row["state"] != "pending" or row["available_at"] > now.isoformat():
            return row["state"]
        milestone = db.execute(
            "SELECT current_milestone FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if milestone is None or milestone[0] != Milestone.M8_GO_LIVE.value:
            db.execute(
                "UPDATE go_live_telegram_outbox SET state='cancelled',updated_at=? WHERE project_id=?",
                (now.isoformat(), project_id),
            )
            return "cancelled"
        prior_receipt = _notice_receipt(db, project_id, row["chat_id"])
        sent_id = _positive_message_id(prior_receipt[0]) if prior_receipt is not None else None
        if sent_id:
            db.execute(
                """UPDATE go_live_telegram_outbox SET state='sent',telegram_message_id=?,
                   updated_at=? WHERE project_id=?""",
                (sent_id, now.isoformat(), project_id),
            )
            return "sent"
        if prior_receipt is not None:
            # Legacy synthetic IDs indicate an unconfirmed remote attempt, not
            # permission to replay it or evidence that it succeeded.
            db.execute(
                """UPDATE go_live_telegram_outbox SET state='uncertain',
                   telegram_message_id=NULL,last_error='delivery_unconfirmed',
                   lease_token=NULL,lease_until=NULL,updated_at=? WHERE project_id=?""",
                (now.isoformat(), project_id),
            )
            return "uncertain"
        destination = _destination()
        if (destination is None or destination != (row["chat_id"], row["bot_fingerprint"])):
            error = "recipient_unbound" if destination is None else "recipient_changed"
        elif not should_push_with_budget(db, URGENCY_URGENT):
            error = "budget_exhausted"
        else:
            error = None
        if error:
            db.execute(
                """UPDATE go_live_telegram_outbox SET last_error=?,available_at=?,updated_at=?
                   WHERE project_id=? AND state='pending'""",
                (error, (now + _RETRY).isoformat(), now.isoformat(), project_id),
            )
            return "pending"
        changed = db.execute(
            """UPDATE go_live_telegram_outbox SET state='sending',lease_token=?,
               lease_until=?,attempts=attempts+1,updated_at=?
               WHERE project_id=? AND state='pending' AND available_at<=?""",
            (lease_token, (now + _LEASE).isoformat(), now.isoformat(),
             project_id, now.isoformat()),
        )
        if changed.rowcount != 1:
            return "pending"

    try:
        if sender is None:
            from src.services.go_live_notice import push_go_live_telegram_sync

            sender = push_go_live_telegram_sync
        result = sender(
            None, project_id, row["text"], chat_id=row["chat_id"],
            expected_bot_fingerprint=row["bot_fingerprint"],
        )
        if result.get("sent") is True and _positive_message_id(result.get("telegram_message_id")):
            state, error = "sent", None
        elif result.get("sent") is not True and result.get("error") in {"telegram_not_configured", "bot_identity_changed", "recipient_changed", "budget_exhausted"}:
            state, error = "pending", result["error"]
        elif result.get("error") == "already_sent":
            state, error = "sent", None
        else:
            state, error = "uncertain", "delivery_unconfirmed"
    except Exception:
        logger.exception("Go-live Telegram delivery uncertain for %s", project_id)
        result, state, error = {}, "uncertain", "delivery_unconfirmed"

    try:
        with scoped_write() as db:
            sent_id = None
            if state == "sent":
                sent_id = _positive_message_id(result.get("telegram_message_id"))
                if sent_id is None:
                    sent_id = _sent_message_id(db, project_id, row["chat_id"])
                if sent_id is None:
                    state, error = "uncertain", "delivery_unconfirmed"
            changed = db.execute(
                """UPDATE go_live_telegram_outbox SET state=?,available_at=?,
                   telegram_message_id=?,last_error=?,lease_token=NULL,lease_until=NULL,
                   updated_at=? WHERE project_id=? AND state='sending' AND lease_token=?""",
                (state, (_now() + _RETRY).isoformat(), sent_id, error,
                 _now().isoformat(), project_id, lease_token),
            )
            if changed.rowcount == 1 and state == "sent" and result.get("sent"):
                from src.utils.notification_budget import consume_notification

                consume_notification(db, commit=False)
            if changed.rowcount != 1:
                return "uncertain"
    except Exception:
        logger.exception("Could not finalize go-live Telegram delivery for %s", project_id)
        return "uncertain"
    if state == "uncertain":
        logger.warning("Go-live Telegram delivery uncertain for %s; manual reconciliation required", project_id)
    return state


def process_pending_go_live_telegram(*, limit: int = 10) -> list[str]:
    """Drain committed deliveries; restart recovery does not replay uncertain sends."""
    from src.db.connection import scoped_write

    with scoped_write() as conn:
        recover_expired_go_live_deliveries(conn)
        rows = conn.execute(
            """SELECT project_id FROM go_live_telegram_outbox
               WHERE state='pending' AND available_at<=?
               ORDER BY available_at,project_id LIMIT ?""",
            (_now().isoformat(), max(0, min(limit, 50))),
        ).fetchall()
    return [deliver_go_live_telegram(row[0]) for row in rows]

class GoLiveDeliveryReviewError(ValueError):
    """A manual delivery review cannot safely change this outbox row."""


def uncertain_go_live_deliveries(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """Expose review metadata, never the bot fingerprint or message text."""
    rows = conn.execute(
        """SELECT project_id,chat_id,bot_fingerprint,text,state,attempts,telegram_message_id,
                  last_error,created_at,updated_at
           FROM go_live_telegram_outbox WHERE state='uncertain'
           ORDER BY updated_at,project_id LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def confirm_uncertain_go_live_sent(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: str,
    expected_updated_at: str,
    expected_attempts: int,
    expected_chat_id: int,
    expected_bot_fingerprint: str,
    telegram_message_id: int,
    evidence_ref: str,
) -> dict[str, Any]:
    """Attest a positively verified receipt; never schedule a retry.

    The operator must independently verify the exact bot, chat, message and
    project text. This assertion is auditable, not remote proof of delivery.
    The caller owns the transaction; an audit failure rolls back the decision.
    """
    from src.repositories.activity_log_repo import insert_activity
    from src.utils.notification_budget import consume_notification

    if (not actor.strip() or not evidence_ref.strip() or not expected_updated_at.strip()
            or len(expected_bot_fingerprint) != 64):
        raise GoLiveDeliveryReviewError("actor, evidence reference and version required")
    if (not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool)
            or telegram_message_id <= 0):
        raise GoLiveDeliveryReviewError("positive Telegram message ID required")
    if (not isinstance(expected_chat_id, int) or isinstance(expected_chat_id, bool)
            or not isinstance(expected_attempts, int) or isinstance(expected_attempts, bool)
            or expected_attempts < 1):
        raise GoLiveDeliveryReviewError("valid destination and attempt version required")
    row = conn.execute(
        "SELECT * FROM go_live_telegram_outbox WHERE project_id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise KeyError(project_id)
    if (row["state"] != "uncertain" or row["updated_at"] != expected_updated_at
            or row["attempts"] != expected_attempts or row["chat_id"] != expected_chat_id
            or row["bot_fingerprint"] != expected_bot_fingerprint):
        raise GoLiveDeliveryReviewError("stale or non-reviewable delivery")
    milestone = conn.execute(
        "SELECT current_milestone FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if milestone is None or milestone[0] != Milestone.M8_GO_LIVE.value:
        raise GoLiveDeliveryReviewError("project is not at M8")
    stored_id = _sent_message_id(conn, project_id, expected_chat_id)
    if stored_id is not None and stored_id != str(telegram_message_id):
        raise GoLiveDeliveryReviewError("Telegram receipt conflicts with persisted message")
    now = _now().isoformat()
    changed = conn.execute(
        """UPDATE go_live_telegram_outbox SET state='sent',telegram_message_id=?,
                  last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=?
           WHERE project_id=? AND state='uncertain' AND updated_at=?
             AND attempts=? AND chat_id=? AND bot_fingerprint=?""",
        (str(telegram_message_id), now, project_id, expected_updated_at,
         expected_attempts, expected_chat_id, expected_bot_fingerprint),
    )
    if changed.rowcount != 1:
        raise GoLiveDeliveryReviewError("stale delivery review")
    insert_activity(
        conn, actor=actor.strip(), action_type="go_live_delivery_reconciled",
        project_id=project_id, surface="dashboard", object_ref=project_id,
        summary="Operator attested M8 Telegram delivery", touch_project=False,
        metadata_json=json.dumps({
            "decision": "confirm_sent", "evidence_ref": evidence_ref.strip(),
            "chat_id": expected_chat_id, "telegram_message_id": telegram_message_id,
            "attempts": expected_attempts, "previous_error": row["last_error"],
        }, ensure_ascii=False),
    )
    consume_notification(conn, commit=False)
    return {
        "project_id": project_id, "chat_id": expected_chat_id, "state": "sent",
        "attempts": expected_attempts, "telegram_message_id": str(telegram_message_id),
        "last_error": None, "created_at": row["created_at"], "updated_at": now,
    }
