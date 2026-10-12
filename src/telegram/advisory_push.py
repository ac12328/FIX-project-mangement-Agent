"""
Push helpers for Profile O (Project Advisory) and Profile P (Support Advisory) cards.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from telegram import Bot

from config.settings import CONFIG
from src.models.advisory import Advisory
from src.telegram.cards import build_card, format_card_text
from src.telegram.card_push import _run_push_in_thread
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.runtime import send_persisted_message
from src.utils.notification_budget import should_push_with_budget, consume_notification
from src.utils.push_guard import URGENCY_NORMAL, URGENCY_URGENT
from src.utils.async_helpers import has_running_loop


def _format_advisory_body(advisory: Advisory) -> str:
    """Format an Advisory into a readable card body."""
    lines = []
    if advisory.situation_assessment:
        lines.append(t("push.advisory_situation").format(situation=advisory.situation_assessment))
    if advisory.next_steps:
        lines.append("")
        lines.append(t("push.advisory_next_steps"))
        for i, ns in enumerate(advisory.next_steps[:4], 1):
            priority_icon = {"now": "🔴", "soon": "🟡", "when_ready": "🟢"}.get(ns.priority, "⚪")
            lines.append(f"  {i}. {priority_icon} {ns.action}")
    if advisory.escalation:
        lines.append(f"\n{t('push.advisory_escalate').format(team=advisory.escalation.team, reason=advisory.escalation.reason[:80])}")
    if advisory.draft_suggestion:
        lines.append(f"\n{t('push.advisory_draft_suggestion').format(purpose=advisory.draft_suggestion.purpose[:80])}")
    if advisory.similar_past_cases:
        lines.append(f"\n{t('push.advisory_similar').format(cases=', '.join(advisory.similar_past_cases[:3]))}")
    from src.telegram.operator_messages import en
    lines.append("\nSuggestions only; not a confirmed cause or an executed action." if en()
                 else "\n以上是建議，不代表已確認原因或已執行操作。")
    return "\n".join(lines)


def push_project_advisory_card_sync(
    conn: sqlite3.Connection | None,
    *,
    advisory: Advisory,
    project_id: str,
    event_summary: str = "",
    chat_id: int | None = None,
    expected_bot_fingerprint: str | None = None,
    source_email_id: str | None = None,
) -> dict[str, Any]:
    """Push a committed advisory; use None to avoid holding a writer over I/O."""
    token = CONFIG.telegram.bot_token
    if expected_bot_fingerprint is not None:
        from src.email.project_advisory_outbox import _destination

        if _destination() != (chat_id, expected_bot_fingerprint):
            return {"telegram_message_id": None, "sent": False,
                    "error": "bot_identity_changed"}
    fallback = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback[0] if fallback else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    if conn is None:
        from src.db.connection import scoped_read
        with scoped_read() as read_conn:
            allowed = should_push_with_budget(read_conn, URGENCY_NORMAL)
    else:
        allowed = should_push_with_budget(conn, URGENCY_NORMAL)
    if not allowed:
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
    kwargs = dict(
        token=token, chat_id=resolved_chat_id, advisory=advisory,
        project_id=project_id, event_summary=event_summary,
        source_email_id=source_email_id,
    )
    if has_running_loop():
        result = _run_push_in_thread(lambda c: _push_project_advisory(c, **kwargs))
    else:
        result = asyncio.run(_push_project_advisory(conn, **kwargs))
    if result.get("sent"):
        if conn is None:
            from src.db.connection import scoped_write
            with scoped_write() as write_conn:
                consume_notification(write_conn, commit=False)
        else:
            consume_notification(conn)
    return result


async def _push_project_advisory(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    advisory: Advisory,
    project_id: str,
    event_summary: str,
    source_email_id: str | None = None,
) -> dict[str, Any]:
    bot = Bot(token=token)
    body = _format_advisory_body(advisory)
    card = build_card("O", t("push.advisory_project_title"), body, project_id=project_id)
    card.metadata = {"project_id": project_id, "confidence": advisory.confidence}
    metadata = {"card_profile": "O", "project_id": project_id}
    if source_email_id is not None:
        metadata["email_id"] = source_email_id
        metadata["bot_fingerprint"] = hashlib.sha256(str(token).encode()).hexdigest()
        metadata["advisory_fingerprint"] = hashlib.sha256(
            advisory.model_dump_json().encode("utf-8")
        ).hexdigest()
    message_ids = await send_persisted_message(
        bot, conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=project_id,
        message_kind="card",
        reply_markup=card_to_markup(card),
        metadata=metadata,
        require_persistence=source_email_id is not None,
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def push_support_advisory_card_sync(
    conn: sqlite3.Connection,
    *,
    advisory: Advisory,
    ticket_id: str,
    ticket_title: str = "",
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Synchronously push a Profile P Support Advisory Card."""
    token = CONFIG.telegram.bot_token
    fallback = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    resolved_chat_id = chat_id or CONFIG.telegram.chat_id or (fallback[0] if fallback else 0)
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    urgency = URGENCY_URGENT if advisory.escalation else URGENCY_NORMAL
    if not should_push_with_budget(conn, urgency):
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}
    esc_team = advisory.escalation.team if advisory.escalation else None
    kwargs = dict(
        token=token, chat_id=resolved_chat_id, advisory=advisory,
        ticket_id=ticket_id, ticket_title=ticket_title, escalation_team=esc_team,
    )
    if has_running_loop():
        result = _run_push_in_thread(lambda c: _push_support_advisory(c, **kwargs))
    else:
        result = asyncio.run(_push_support_advisory(conn, **kwargs))
    if result.get("sent"):
        consume_notification(conn)
    return result


async def _push_support_advisory(
    conn: sqlite3.Connection,
    *,
    token: str,
    chat_id: int,
    advisory: Advisory,
    ticket_id: str,
    ticket_title: str,
    escalation_team: str | None,
) -> dict[str, Any]:
    bot = Bot(token=token)
    header = f"🎫 `{ticket_id}`"
    if ticket_title:
        header = t("push.advisory_ticket_prefix").format(ticket_id=ticket_id, title=ticket_title[:60])
    body = header + "\n" + _format_advisory_body(advisory)
    card = build_card("P", t("push.advisory_support_title"), body)
    card.metadata = {
        "ticket_id": ticket_id,
        "escalation_team": escalation_team,
        "confidence": advisory.confidence,
    }
    message_ids = await send_persisted_message(
        bot, conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=None,
        message_kind="card",
        reply_markup=card_to_markup(card),
        metadata={"card_profile": "P", "ticket_id": ticket_id},
    )
    telegram_message_id = message_ids[-1] if message_ids else None
    return {
        "telegram_message_id": telegram_message_id,
        "sent": telegram_message_id is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def push_support_notify_card_sync(
    conn: sqlite3.Connection | None,
    *,
    ticket_id: str,
    title: str,
    body: str,
    project_id: str | None = None,
    is_followup: bool = False,
    chat_id: int | None = None,
    expected_bot_fingerprint: str | None = None,
    email_id: str | None = None,
) -> dict[str, Any]:
    """Send a committed support card without holding a write lease over I/O."""
    import hashlib
    from src.db.connection import scoped_read, scoped_write

    token = CONFIG.telegram.bot_token
    fallback = list(getattr(CONFIG.telegram, "allowed_chat_ids", []) or [])
    configured_chat_id = CONFIG.telegram.chat_id or (fallback[0] if fallback else 0)
    resolved_chat_id = chat_id or configured_chat_id
    if not token or not resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "telegram_not_configured"}
    if expected_bot_fingerprint and hashlib.sha256(str(token).encode()).hexdigest() != expected_bot_fingerprint:
        return {"telegram_message_id": None, "sent": False, "error": "bot_identity_changed"}
    if expected_bot_fingerprint and configured_chat_id != resolved_chat_id:
        return {"telegram_message_id": None, "sent": False, "error": "recipient_changed"}
    if conn is None:
        with scoped_read() as read_conn:
            allowed = should_push_with_budget(read_conn, URGENCY_URGENT)
    else:
        allowed = should_push_with_budget(conn, URGENCY_URGENT)
    if not allowed:
        return {"telegram_message_id": None, "sent": False, "error": "budget_exhausted"}

    kwargs = dict(
        token=token, chat_id=resolved_chat_id, ticket_id=ticket_id,
        title=title, body=body, project_id=project_id, is_followup=is_followup,
        email_id=email_id,
    )
    if has_running_loop():
        result = _run_push_in_thread(lambda c: _push_support_notify(c, **kwargs))
    else:
        result = asyncio.run(_push_support_notify(conn, **kwargs))
    if result.get("sent"):
        if conn is None:
            with scoped_write() as write_conn:
                consume_notification(write_conn, commit=False)
        else:
            consume_notification(conn)
    return result


async def _push_support_notify(
    conn: sqlite3.Connection | None,
    *,
    token: str,
    chat_id: int,
    ticket_id: str,
    title: str,
    body: str,
    project_id: str | None,
    is_followup: bool,
    email_id: str | None,
) -> dict[str, Any]:
    bot = Bot(token=token)
    # Follow-ups: TK view card; new tickets: P support card with action buttons
    profile = "TK" if is_followup else "P"
    card_title = title or (
        f"支援跟進：{ticket_id}" if is_followup else f"新支援工單：{ticket_id}"
    )
    extra = None
    if is_followup:
        extra = [[("DETAILS", f"TK:DETAILS:{ticket_id}"), ("DISMISS", f"TK:DISMISS:{ticket_id}")]]
    card = build_card(
        profile,
        card_title,
        body,
        project_id=project_id,
        extra_buttons=extra,
    )
    card.metadata = {"ticket_id": ticket_id, "is_followup": is_followup}
    message_ids = await send_persisted_message(
        bot,
        conn,
        chat_id=chat_id,
        text=format_card_text(card),
        project_id=project_id,
        message_kind="card",
        reply_markup=card_to_markup(card),
        metadata={
            "card_profile": profile,
            "ticket_id": ticket_id,
            "is_followup": is_followup,
            "email_summary": body,
            "email_id": email_id,
        },
    )
    mid = message_ids[-1] if message_ids else None
    if not isinstance(mid, int) or mid <= 0:
        mid = None
    return {
        "telegram_message_id": mid,
        "sent": mid is not None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
