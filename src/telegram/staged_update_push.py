"""Commit-bound CU cards: claim once, send without a writer lease, quarantine ambiguity."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import sqlite3
from typing import Any
from uuid import uuid4

from telegram import Bot

from config.settings import CONFIG
from src.db.connection import scoped_write
from src.repositories.working_memory_repo import get_memory
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.utils.notification_budget import reserve_notification_attempt
from src.utils.push_guard import URGENCY_URGENT

logger = logging.getLogger(__name__)
_PENDING_KEY = "pending_chat_update"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _destination(chat_id: int | None = None) -> tuple[int | None, str | None, str]:
    token = str(CONFIG.telegram.bot_token or "")
    chosen = chat_id or CONFIG.telegram.chat_id
    return (int(chosen) if chosen else None,
            hashlib.sha256(token.encode()).hexdigest() if token else None, token)


def enqueue_staged_update_card(
    conn: sqlite3.Connection, *, project_id: str, chat_id: int | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Persist the frozen operation/card identity in the stage owner's transaction."""
    mem = get_memory(conn, project_id, _PENDING_KEY)
    if not mem:
        return {"queued": False, "error": "no_pending_update"}
    try:
        staged = json.loads(mem["memory_value"])
        operation_id = str(staged.get("operation_id") or "").strip()
        if not operation_id:
            return {"queued": False, "error": "no_typed_operation"}
        fields = staged.get("fields") or {}
        if not isinstance(fields, dict):
            return {"queued": False, "error": "invalid_pending_update"}
    except (ValueError, TypeError, AttributeError):
        return {"queued": False, "error": "invalid_pending_update"}
    op = conn.execute(
        "SELECT project_id,chat_id,status,expires_at,operation_type FROM pending_operations "
        "WHERE operation_id=?", (operation_id,),
    ).fetchone()
    recipient, fingerprint, _ = _destination(chat_id)
    if (not op or op[0] != project_id or op[2] != "pending"
            or op[3] <= _now() or op[4] != "project_update"):
        return {"queued": False, "error": "operation_not_pending"}
    if not recipient or not fingerprint:
        return {"queued": False, "error": "telegram_not_configured"}
    if op[1] != str(recipient):
        return {"queued": False, "error": "recipient_mismatch"}
    body_lines = [summary or t("staged_update.card_body").format(project_id=project_id)]
    body_lines.extend(f"  • {key} → {value}" for key, value in fields.items())
    body = "\n".join(body_lines)
    existing = conn.execute(
        "SELECT project_id,chat_id,bot_fingerprint,body FROM staged_update_card_outbox "
        "WHERE operation_id=?", (operation_id,),
    ).fetchone()
    if existing:
        if tuple(existing[:3]) != (project_id, recipient, fingerprint):
            raise ValueError("Staged card operation identity collision")
        # A duplicate request may have different summary prose; keep the first
        # committed card snapshot instead of mutating an already pending send.
        return {"queued": True, "operation_id": operation_id}
    now = _now()
    conn.execute(
        "INSERT INTO staged_update_card_outbox "
        "(operation_id,project_id,chat_id,bot_fingerprint,body,available_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (operation_id, project_id, recipient, fingerprint, body, now, now, now),
    )
    return {"queued": True, "operation_id": operation_id}


def push_staged_update_card_sync(
    conn: sqlite3.Connection, *, project_id: str, chat_id: int | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Compatibility entry: queues a card; never sends inside the caller's transaction."""
    return enqueue_staged_update_card(
        conn, project_id=project_id, chat_id=chat_id, summary=summary,
    )


def recover_expired_staged_update_cards(conn: sqlite3.Connection) -> int:
    """An expired in-flight send may have succeeded remotely: never replay."""
    now = _now()
    return conn.execute(
        "UPDATE staged_update_card_outbox SET state='uncertain',"
        "last_error='delivery_lease_expired',lease_token=NULL,lease_until=NULL,updated_at=? "
        "WHERE state='sending' AND lease_until<=?", (now, now),
    ).rowcount


def deliver_staged_update_card(operation_id: str, *, sender=None) -> str:
    """Claim a committed intent before remote I/O; only a positive receipt is sent."""
    lease, now = uuid4().hex, _now()
    with scoped_write() as conn:
        recover_expired_staged_update_cards(conn)
        row = conn.execute(
            "SELECT * FROM staged_update_card_outbox WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return "missing"
        row = dict(row)
        if row["state"] != "pending":
            return row["state"]
        operation = conn.execute(
            "SELECT status,expires_at FROM pending_operations WHERE operation_id=? "
            "AND project_id=?", (operation_id, row["project_id"]),
        ).fetchone()
        if not operation or operation[0] != "pending" or operation[1] <= now:
            conn.execute(
                "UPDATE staged_update_card_outbox SET state='cancelled',"
                "last_error='operation_resolved',updated_at=? WHERE operation_id=?",
                (now, operation_id),
            )
            return "cancelled"
        if row["available_at"] > now:
            return "pending"
        _, fingerprint, token = _destination()
        allowed = {int(chat) for chat in (CONFIG.telegram.allowed_chat_ids or [])}
        if CONFIG.telegram.chat_id:
            allowed.add(int(CONFIG.telegram.chat_id))
        if not token or fingerprint != row["bot_fingerprint"] or row["chat_id"] not in allowed:
            refusal = "recipient_changed"
        elif not reserve_notification_attempt(conn, URGENCY_URGENT):
            refusal = "budget_exhausted"
        else:
            refusal = None
        if refusal:
            conn.execute(
                "UPDATE staged_update_card_outbox SET last_error=?,available_at=?,updated_at=? "
                "WHERE operation_id=? AND state='pending'",
                (refusal, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                 now, operation_id),
            )
            return "pending"
        conn.execute(
            "UPDATE staged_update_card_outbox SET state='sending',attempts=attempts+1,"
            "lease_token=?,lease_until=?,updated_at=? WHERE operation_id=? AND state='pending'",
            (lease, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             now, operation_id),
        )
    if sender is None:
        sender = send_claimed_staged_update_card_sync
    try:
        result = sender(
            operation_id=operation_id, project_id=row["project_id"],
            chat_id=row["chat_id"], body=row["body"], token=token,
        )
        message_id = result.get("telegram_message_id")
        if result.get("sent") is True and type(message_id) is int and message_id > 0:
            state, error = "sent", None
        else:
            state, error = "uncertain", "delivery_unconfirmed"
    except Exception:
        logger.exception("Staged update card delivery uncertain: %s", operation_id)
        message_id, state, error = None, "uncertain", "delivery_unconfirmed"
    try:
        with scoped_write() as conn:
            changed = conn.execute(
                "UPDATE staged_update_card_outbox SET state=?,telegram_message_id=?,"
                "last_error=?,lease_token=NULL,lease_until=NULL,updated_at=? "
                "WHERE operation_id=? AND state='sending' AND lease_token=?",
                (state, message_id if state == "sent" else None, error,
                 _now(), operation_id, lease),
            ).rowcount
        return state if changed == 1 else "uncertain"
    except Exception:
        logger.exception("Could not finalize staged update card receipt: %s", operation_id)
        return "uncertain"


def process_pending_staged_update_cards(*, limit: int = 10) -> list[str]:
    with scoped_write() as conn:
        recover_expired_staged_update_cards(conn)
        rows = conn.execute(
            "SELECT operation_id FROM staged_update_card_outbox WHERE state='pending' "
            "AND available_at<=? ORDER BY available_at,operation_id LIMIT ?",
            (_now(), max(0, min(limit, 50))),
        ).fetchall()
        operation_ids = [row[0] for row in rows]
    return [deliver_staged_update_card(operation_id) for operation_id in operation_ids]


def send_claimed_staged_update_card_sync(
    *, operation_id: str, project_id: str, chat_id: int, body: str, token: str,
) -> dict[str, Any]:
    """Send after claim, then require local message audit before positive receipt."""
    async def send() -> list[int]:
        # Bind each button to the frozen operation, not just its project. An old
        # card must never confirm a later proposal for the same project.
        card = build_card(
            "CU", t("staged_update.card_title").format(project_id=project_id),
            body, project_id=project_id,
            extra_buttons=[[
                ("CONFIRM", f"CU:CONFIRM:@{operation_id}"),
                ("CANCEL", f"CU:CANCEL:@{operation_id}"),
            ]],
        )
        return await send_persisted_message(
            Bot(token=token), None, chat_id=chat_id, text=format_card_text(card),
            project_id=project_id, message_kind="staged_update_card",
            reply_markup=card_to_markup(card), format_mode="plain",
            require_persistence=True,
            metadata={"operation_id": operation_id, "card_profile": "CU"},
        )

    message_ids = asyncio.run(send())
    if not message_ids or any(type(mid) is not int or mid <= 0 for mid in message_ids):
        return {"sent": False}
    # The keyboard is on the last chunk; strict audit covered every chunk.
    return {"sent": True, "telegram_message_id": message_ids[-1]}
