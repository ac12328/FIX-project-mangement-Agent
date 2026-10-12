"""Committed support-email work: recoverable card delivery and draft scheduling.

A Telegram send with no confirmed receipt is quarantined, not automatically retried.
The caller must commit the ticket and this row together before this worker runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.email.inbound_work import InboundRouteClaim
from src.models.support_ticket import SupportNotification

logger = logging.getLogger(__name__)
_LEASE = timedelta(minutes=5)
_RETRY = timedelta(minutes=5)
_DRAFT_LEASE = timedelta(minutes=30)


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


def bind_support_route_source(
    conn: sqlite3.Connection, *, email_id: str,
    route_claim: InboundRouteClaim | None, direct_support: bool = False,
) -> str:
    """Bind an effect to a verified route, or to an explicitly direct caller."""
    row = conn.execute(
        "SELECT source_type,route_state,lease_token,"
        "lease_expires_at > datetime('now') FROM inbound_email_work WHERE email_id=?",
        (email_id,),
    ).fetchone()
    if route_claim is None:
        if not direct_support or row is not None:
            raise RuntimeError("Support route requires a claim or explicit direct source")
        return "direct"
    if direct_support or route_claim.email_id != email_id:
        raise ValueError("Support route claim does not match its source")
    if (row is None or row[0] not in {"imap", "graph"} or row[1] != "routing"
            or row[2] != route_claim.token or not row[3]):
        raise RuntimeError("Support route claim is missing, stale, or expired")
    return str(row[0])


def enqueue_support_effects(
    conn: sqlite3.Connection, *, email_id: str, notification: SupportNotification,
    is_followup: bool, send_card: bool,
    route_claim: InboundRouteClaim | None, direct_support: bool = False,
) -> None:
    """Add post-commit work to the ticket transaction without external effects."""
    route_source_type = bind_support_route_source(
        conn, email_id=email_id, route_claim=route_claim, direct_support=direct_support,
    )
    destination = _destination() if send_card else None
    now = _now().isoformat()
    conn.execute(
        """INSERT INTO support_email_effects
           (email_id,ticket_id,notification_json,is_followup,chat_id,bot_fingerprint,
            route_source_type,card_state,draft_state,available_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (email_id, notification.ticket_id, notification.model_dump_json(),
         int(is_followup), destination[0] if destination else None,
         destination[1] if destination else None, route_source_type,
         "pending" if destination else "none",
         "none" if is_followup else "pending", now, now, now),
    )


def enqueue_support_digest(
    conn: sqlite3.Connection, *, email_id: str, route_claim: InboundRouteClaim | None,
    category: str, title: str, body: str, project_id: str | None,
    direct_support: bool = False,
    metadata: dict,
) -> None:
    """Stage claimed-route digests until a successful final route receipt."""
    from src.repositories.digest_queue_repo import enqueue_digest_item

    source = bind_support_route_source(
        conn, email_id=email_id, route_claim=route_claim, direct_support=direct_support,
    )
    metadata = {**metadata, "email_id": email_id}
    if source == "direct":
        enqueue_digest_item(conn, category=category, title=title, body=body,
                            project_id=project_id, metadata=metadata, commit=False)
        return
    assert route_claim is not None
    row = conn.execute(
        "SELECT source_type,route_state,lease_token,checkpoint_json "
        "FROM inbound_email_work WHERE email_id=?", (email_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Support digest route claim is missing")
    if row[1] != "routing" or row[2] != route_claim.token:
        raise RuntimeError("Support digest route claim is stale")
    checkpoints = json.loads(row[3])
    if not isinstance(checkpoints, dict):
        raise RuntimeError("Invalid support route checkpoint")
    intents = checkpoints.get("support_digest_intents", [])
    if not isinstance(intents, list):
        raise RuntimeError("Invalid support digest intents")
    checkpoints["support_digest_intents"] = [*intents, {
        "category": category, "title": title, "body": body,
        "project_id": project_id, "metadata": metadata,
    }]
    changed = conn.execute(
        """UPDATE inbound_email_work SET checkpoint_json=?,updated_at=datetime('now')
           WHERE email_id=? AND source_type=? AND route_state='routing'
             AND lease_token=? AND lease_expires_at > datetime('now')
             AND checkpoint_json=?""",
        (json.dumps(checkpoints, ensure_ascii=False, sort_keys=True),
         email_id, source, route_claim.token, row[3]),
    )
    if changed.rowcount != 1:
        raise RuntimeError("Support digest route claim expired")


def release_support_route_digests(
    conn: sqlite3.Connection, email_id: str, token: str, *, source_type: str,
) -> None:
    """Enqueue only a claimed IMAP/Graph route's successful final receipt."""
    if source_type not in {"imap", "graph"}:
        raise ValueError("Support digest release requires a claimed route source")
    from src.repositories.digest_queue_repo import enqueue_digest_item

    row = conn.execute(
        """SELECT checkpoint_json FROM inbound_email_work
           WHERE email_id=? AND source_type=? AND route_state='routing'
             AND lease_token=? AND lease_expires_at > datetime('now')""",
        (email_id, source_type, token),
    ).fetchone()
    if row is None:
        raise RuntimeError("Final support digest route claim is stale")
    checkpoints = json.loads(row[0])
    if not isinstance(checkpoints, dict):
        raise RuntimeError("Invalid support route checkpoint")
    intents = checkpoints.get("support_digest_intents", [])
    if not isinstance(intents, list):
        raise RuntimeError("Invalid support digest intents")
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != {
            "category", "title", "body", "project_id", "metadata",
        }:
            raise RuntimeError("Invalid support digest intent")
        enqueue_digest_item(conn, **intent, commit=False)


def release_imap_support_digests(
    conn: sqlite3.Connection, email_id: str, token: str,
) -> None:
    """Keep the established IMAP final-receipt call site compatible."""
    release_support_route_digests(conn, email_id, token, source_type="imap")


def recover_expired_support_cards(conn: sqlite3.Connection) -> int:
    """An expired claim may have reached Telegram; never replay it."""
    now = _now().isoformat()
    changed = conn.execute(
        """UPDATE support_email_effects SET card_state='uncertain',
           last_error='delivery_lease_expired',lease_token=NULL,lease_until=NULL,updated_at=?
           WHERE card_state='sending' AND lease_until<=?""", (now, now),
    )
    return changed.rowcount


# Use the persisted effect source, not the continued existence of a route row.
# This predicate is repeated at the atomic card/draft claims to close the gap
# between selecting due work and claiming it in a separate transaction.
_RELEASED_SOURCE_SQL = """(
    (support_email_effects.route_source_type='direct' AND NOT EXISTS (
        SELECT 1 FROM inbound_email_work AS route
        WHERE route.email_id=support_email_effects.email_id))
    OR (support_email_effects.route_source_type IN ('imap','graph') AND EXISTS (
        SELECT 1 FROM inbound_email_work AS route
        WHERE route.email_id=support_email_effects.email_id
          AND route.source_type=support_email_effects.route_source_type
          AND route.route_state='completed'))
)"""


def _support_route_released(conn: sqlite3.Connection, email_id: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM support_email_effects WHERE email_id=? AND {_RELEASED_SOURCE_SQL}",
        (email_id,),
    ).fetchone() is not None


def deliver_support_card(email_id: str) -> str:
    """Claim and commit before network I/O, then persist the result separately."""
    from src.db.connection import scoped_write
    from src.telegram.advisory_push import push_support_notify_card_sync

    now = _now()
    lease = uuid4().hex
    with scoped_write() as conn:
        recover_expired_support_cards(conn)
        row = conn.execute(
            "SELECT * FROM support_email_effects WHERE email_id=?", (email_id,),
        ).fetchone()
        if row is None:
            return "missing"
        row = dict(row)
        if row["card_state"] != "pending" or row["available_at"] > now.isoformat():
            return row["card_state"]
        if not _support_route_released(conn, email_id):
            return "pending"
        if _destination() != (row["chat_id"], row["bot_fingerprint"]):
            conn.execute(
                """UPDATE support_email_effects SET last_error='recipient_changed',
                   available_at=?,updated_at=? WHERE email_id=?""",
                ((now + _RETRY).isoformat(), now.isoformat(), email_id),
            )
            return "pending"
        changed = conn.execute(
            f"""UPDATE support_email_effects SET card_state='sending',attempts=attempts+1,
               lease_token=?,lease_until=?,updated_at=? WHERE email_id=? AND card_state='pending'
               AND {_RELEASED_SOURCE_SQL}""",
            (lease, (now + _LEASE).isoformat(), now.isoformat(), email_id),
        )
        if changed.rowcount != 1:
            return "pending"

    try:
        payload = SupportNotification(**json.loads(row["notification_json"]))
        result = push_support_notify_card_sync(
            None, ticket_id=row["ticket_id"], title=payload.title, body=payload.body,
            project_id=payload.project_id, is_followup=bool(row["is_followup"]),
            chat_id=row["chat_id"], expected_bot_fingerprint=row["bot_fingerprint"],
            email_id=email_id,
        )
        if result.get("sent") and result.get("telegram_message_id") is not None:
            state, error = "sent", None
        elif result.get("error") in {"telegram_not_configured", "bot_identity_changed",
                                   "recipient_changed", "budget_exhausted"}:
            state, error = "pending", result["error"]
        else:
            state, error = "uncertain", "delivery_unconfirmed"
    except Exception:
        logger.exception("Support card delivery unconfirmed for %s", email_id)
        result, state, error = {}, "uncertain", "delivery_unconfirmed"
    try:
        with scoped_write() as conn:
            changed = conn.execute(
                """UPDATE support_email_effects SET card_state=?,last_error=?,
                   available_at=?,telegram_message_id=?,lease_token=NULL,lease_until=NULL,
                   updated_at=? WHERE email_id=? AND card_state='sending' AND lease_token=?""",
                (state, error, (_now() + _RETRY).isoformat(),
                 str(result["telegram_message_id"]) if state == "sent" else None,
                 _now().isoformat(), email_id, lease),
            )
            if changed.rowcount != 1:
                return "uncertain"
    except Exception:
        logger.exception("Could not persist support card receipt for %s", email_id)
        return "uncertain"
    return state


def recover_expired_support_drafts(conn: sqlite3.Connection) -> int:
    """An expired draft may be recomputed; stale workers cannot overwrite it."""
    now = _now().isoformat()
    # A human-authored draft also finishes the work; never replace it with LLM text.
    conn.execute(
        """UPDATE support_email_effects SET draft_state='none',
           draft_lease_token=NULL,draft_lease_until=NULL,draft_last_error=NULL,updated_at=?
           WHERE draft_state IN ('pending','scheduled') AND EXISTS (
               SELECT 1 FROM support_tickets t WHERE t.ticket_id=support_email_effects.ticket_id
               AND t.reply_draft_text IS NOT NULL)""",
        (now,),
    )
    changed = conn.execute(
        """UPDATE support_email_effects SET draft_state='pending',
           draft_lease_token=NULL,draft_lease_until=NULL,draft_available_at=?,
           draft_last_error='draft_lease_expired',updated_at=?
           WHERE draft_state='scheduled' AND draft_lease_until<=?""",
        (now, now, now),
    )
    return changed.rowcount


def complete_support_draft(email_id: str, lease: str, result: dict | None) -> None:
    """Commit a draft only if this worker still owns the durable claim."""
    from src.db.connection import scoped_write

    now = _now().isoformat()
    draft = result.get("reply_draft") if isinstance(result, dict) else None
    with scoped_write() as conn:
        row = conn.execute(
            """SELECT ticket_id FROM support_email_effects WHERE email_id=?
               AND draft_state='scheduled' AND draft_lease_token=?""",
            (email_id, lease),
        ).fetchone()
        if row is None or not _support_route_released(conn, email_id):
            return
        already_drafted = conn.execute(
            "SELECT reply_draft_text FROM support_tickets WHERE ticket_id=?", (row["ticket_id"],),
        ).fetchone()
        done = bool(draft) or (already_drafted is not None and already_drafted[0] is not None)
        if draft:
            conn.execute(
                """UPDATE support_tickets SET reply_draft_text=?,draft_generated_at=?,updated_at=?
                   WHERE ticket_id=? AND reply_draft_text IS NULL""",
                (draft, now, now, row["ticket_id"]),
            )
        conn.execute(
            """UPDATE support_email_effects SET draft_state=?,draft_lease_token=NULL,
               draft_lease_until=NULL,draft_available_at=?,draft_last_error=?,updated_at=?
               WHERE email_id=? AND draft_state='scheduled' AND draft_lease_token=?""",
            ("none" if done else "pending",
             now if done else (_now() + _RETRY).isoformat(),
             None if done else "draft_generation_failed", now, email_id, lease),
        )
    if draft:
        try:
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(
                event_type="ticket_updated", detail={"ticket_id": row["ticket_id"]},
                surface="email",
            ))
        except Exception:
            logger.debug("Support draft refresh event failed", exc_info=True)


def process_support_effects(*, email_id: str | None = None, limit: int = 10) -> None:
    """Run only after commit; periodic polling recovers abandoned draft claims."""
    from src.db.connection import scoped_write
    from src.triage.support_draft import schedule_draft_generation

    with scoped_write() as conn:
        recover_expired_support_cards(conn)
        recover_expired_support_drafts(conn)
        if email_id is not None:
            rows = conn.execute(
                "SELECT email_id,ticket_id,card_state,draft_state FROM support_email_effects WHERE email_id=?",
                (email_id,),
            ).fetchall()
            rows = [row for row in rows if _support_route_released(conn, row["email_id"])]
        else:
            rows = conn.execute(
                f"""SELECT email_id,ticket_id,card_state,draft_state FROM support_email_effects
                   WHERE ((card_state='pending' AND available_at<=?)
                      OR (draft_state='pending' AND (draft_available_at IS NULL OR draft_available_at<=?)))
                     AND {_RELEASED_SOURCE_SQL}
                   ORDER BY created_at LIMIT ?""",
                (_now().isoformat(), _now().isoformat(), max(0, min(limit, 50))),
            ).fetchall()
    for row in rows:
        if row["draft_state"] == "pending":
            lease = uuid4().hex
            now = _now()
            try:
                with scoped_write() as conn:
                    changed = conn.execute(
                        f"""UPDATE support_email_effects SET draft_state='scheduled',
                           draft_lease_token=?,draft_lease_until=?,draft_last_error=NULL,updated_at=?
                           WHERE email_id=? AND draft_state='pending'
                             AND (draft_available_at IS NULL OR draft_available_at<=?)
                             AND {_RELEASED_SOURCE_SQL}""",
                        (lease, (now + _DRAFT_LEASE).isoformat(), now.isoformat(),
                         row["email_id"], now.isoformat()),
                    )
                if changed.rowcount:
                    schedule_draft_generation(
                        row["ticket_id"],
                        on_complete=lambda result, eid=row["email_id"], token=lease:
                            complete_support_draft(eid, token, result),
                    )
            except Exception:
                logger.exception("Could not schedule support draft for %s", row["email_id"])
                with scoped_write() as conn:
                    conn.execute(
                        """UPDATE support_email_effects SET draft_state='pending',
                           draft_lease_token=NULL,draft_lease_until=NULL,
                           draft_available_at=?,draft_last_error='draft_launch_failed',updated_at=?
                           WHERE email_id=? AND draft_state='scheduled' AND draft_lease_token=?""",
                        ((_now() + _RETRY).isoformat(), _now().isoformat(), row["email_id"], lease),
                    )
        if row["card_state"] == "pending":
            deliver_support_card(row["email_id"])
        try:
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(
                event_type="ticket_updated", detail={"ticket_id": row["ticket_id"]},
                surface="email",
            ))
        except Exception:
            logger.debug("Support ticket refresh event failed", exc_info=True)
