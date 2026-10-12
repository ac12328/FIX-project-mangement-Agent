"""
Callback handlers for Profile O (Project Advisory) and Profile P (Support Advisory) cards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity
from src.telegram._cb_helpers import extract_message_metadata
from src.telegram.i18n import t
from src.telegram.session import create_session, set_session_data

_log = logging.getLogger(__name__)

def _resolution_learning_delay_minutes() -> int:
    from config.settings import CONFIG
    return max(1, int(getattr(CONFIG.thresholds, "resolution_learning_delay_minutes", 30)))


# Backward-compat alias for tests and legacy imports.
RESOLUTION_LEARNING_TIMEOUT_MINUTES = 30


def _make_bg_conn() -> sqlite3.Connection:
    """Open a dedicated autocommit connection for background threads.

    Background threads run long-running LLM agent loops.  Using scoped_write()
    for these would leave an implicit write transaction open across LLM calls
    (which take 10-180 s), blocking every other writer (email_poll, scheduler
    jobs) and causing 'database is locked' errors.

    isolation_level=None = autocommit: each SQL statement commits immediately,
    so no write lock is ever held during network/LLM I/O.
    """
    from config.settings import DB_PATH
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _generate_draft_in_background(
    *,
    project_id: str,
    chat_id: int | None,
    telegram_message_id: int | None,
) -> None:
    """Generate a project email draft in background thread, then edit the card."""
    from config.settings import CONFIG

    def _run() -> None:
        from src.telegram.agent_chat import agent_respond

        token = CONFIG.telegram.bot_token
        try:
            bg_conn = _make_bg_conn()
            try:
                result_text = agent_respond(
                    bg_conn,
                    f"Please draft a follow-up email for project {project_id}",
                    chat_id or 0,
                )
            finally:
                bg_conn.close()
            if chat_id and telegram_message_id and token:
                from telegram import Bot

                preview = (result_text or t("advisory.draft_ready"))[:300]

                async def _edit() -> None:
                    bot = Bot(token=token)
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=telegram_message_id,
                            text=t("advisory.draft_generated").format(project_id=project_id, preview=preview),
                        )
                    except Exception:
                        pass

                asyncio.run(_edit())
        except Exception:
            _log.exception("Background draft generation failed for %s", project_id)
            if chat_id and telegram_message_id and token:
                from telegram import Bot

                async def _error() -> None:
                    bot = Bot(token=token)
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=telegram_message_id,
                            text=t("advisory.draft_generation_failed").format(project_id=project_id),
                        )
                    except Exception:
                        pass

                asyncio.run(_error())

    threading.Thread(target=_run, daemon=True, name="advisory-draft").start()


def _generate_reply_in_background(
    *,
    ticket_id: str,
    chat_id: int | None,
    telegram_message_id: int | None,
) -> None:
    """Generate a support ticket reply draft in background thread, then edit the card."""
    from config.settings import CONFIG

    def _run() -> None:
        from src.llm.client import llm_call_structured

        token = CONFIG.telegram.bot_token
        try:
            bg_conn = _make_bg_conn()
            try:
                row = bg_conn.execute(
                    "SELECT title, category, body_text FROM support_tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                if not row:
                    raise ValueError(f"Ticket {ticket_id} not found")
                prompt = (
                    f"Draft a professional reply to this support ticket.\n"
                    f"Title: {row['title']}\nCategory: {row['category']}\n"
                    f"Body: {(row['body_text'] or '')[:500]}"
                )
                result = llm_call_structured(
                    "support_reply_draft",
                    [{"role": "user", "content": prompt}],
                    {"draft": "string"},
                )
                draft_text = (result or {}).get("draft") or str(result or "")
                bg_conn.execute(
                    "UPDATE support_tickets SET reply_draft_text = ? WHERE ticket_id = ?",
                    (draft_text, ticket_id),
                )
                bg_conn.commit()
            finally:
                bg_conn.close()
            if chat_id and telegram_message_id and token:
                from telegram import Bot

                preview = (draft_text or t("advisory.draft_ready"))[:300]

                async def _edit() -> None:
                    bot = Bot(token=token)
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=telegram_message_id,
                            text=t("advisory.reply_draft_ready_for").format(ticket_id=ticket_id, preview=preview),
                        )
                    except Exception:
                        pass

                asyncio.run(_edit())
        except Exception:
            _log.exception("Background reply generation failed for %s", ticket_id)
            if chat_id and telegram_message_id and token:
                from telegram import Bot

                async def _error() -> None:
                    bot = Bot(token=token)
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=telegram_message_id,
                            text=t("advisory.reply_draft_failed").format(ticket_id=ticket_id),
                        )
                    except Exception:
                        pass

                asyncio.run(_error())

    threading.Thread(target=_run, daemon=True, name="advisory-reply").start()


def handle_project_advisory(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle Profile O (Project Advisory Card) actions.

    Actions: DRAFT_EMAIL, I_HANDLE, ASK_MORE
    """
    metadata = extract_message_metadata(message_context)
    if metadata.get("email_id") and project_id:
        from src.email.advisory_delivery_policy import advisory_card_is_current
        if not advisory_card_is_current(conn, email_id=metadata["email_id"],
                project_id=project_id, chat_id=(message_context or {}).get("chat_id"),
                bot_fingerprint=metadata.get("bot_fingerprint")):
            from src.telegram.operator_messages import en
            return {"_error": True, "project_id": project_id,
                    "text": ("This recommendation is outdated or unconfirmed. Review the latest project state before acting."
                             if en() else "這項建議已過期或尚未確認送達。請先核對最新專案狀態，再決定下一步。")}
    ticket_id = metadata.get("ticket_id")
    chat_id = (message_context or {}).get("chat_id")
    telegram_message_id = (message_context or {}).get("telegram_message_id")

    if action == "DRAFT_EMAIL":
        return _handle_draft_email(conn, project_id, metadata, operator_id,
                                   chat_id=chat_id, telegram_message_id=telegram_message_id)

    if action == "I_HANDLE":
        return _handle_i_handle(
            conn, project_id, ticket_id, "project", metadata, operator_id,
        )

    if action == "ASK_MORE":
        return _handle_ask_more(conn, project_id, "project", metadata, operator_id)

    return {"text": t("advisory.unknown_project_action").format(action=action)}


def handle_support_advisory(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    post_commit: list | None = None,
) -> dict:
    """Handle Profile P (Support Advisory Card) actions.

    Actions: DRAFT_REPLY, ESCALATE, RECLASSIFY, I_HANDLE
    """
    metadata = extract_message_metadata(message_context)
    ticket_id = metadata.get("ticket_id")
    chat_id = (message_context or {}).get("chat_id")
    telegram_message_id = (message_context or {}).get("telegram_message_id")

    if action == "DRAFT_REPLY":
        return _handle_draft_reply(conn, ticket_id, metadata, operator_id,
                                   chat_id=chat_id, telegram_message_id=telegram_message_id)

    if action == "ESCALATE":
        from src.telegram.support_callback_command import apply_support_ticket_action

        return apply_support_ticket_action(
            conn, action, project_id, message_context=message_context,
            operator_id=operator_id, post_commit=post_commit,
        )

    if action == "RECLASSIFY":
        return _handle_reclassify(conn, ticket_id, metadata, operator_id)

    if action == "RECLASS_SELECT":
        from src.telegram.support_callback_command import apply_support_ticket_action

        return apply_support_ticket_action(
            conn, action, project_id, message_context=message_context,
            operator_id=operator_id, post_commit=post_commit,
        )

    if action == "I_HANDLE":
        return _handle_i_handle(
            conn, project_id, ticket_id, "support", metadata, operator_id,
        )

    return {"text": t("advisory.unknown_support_action").format(action=action)}


# ---------------------------------------------------------------------------
# Shared: I HANDLE → creates resolution_learning session
# ---------------------------------------------------------------------------

def _handle_i_handle(
    conn: sqlite3.Connection,
    project_id: str | None,
    ticket_id: str | None,
    work_type: str,
    metadata: dict,
    operator_id: str,
) -> dict:
    """Create a resolution_learning session so the system asks the operator later."""
    ref = ticket_id or project_id or "unknown"
    session_id = create_session(
        conn,
        flow_type="resolution_learning",
        originating_ref=ref,
        operator_id=operator_id,
    )
    delay_minutes = _resolution_learning_delay_minutes()
    timeout_at = (
        datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
    ).isoformat()
    conn.execute(
        "UPDATE telegram_sessions SET timeout_at = ? WHERE session_id = ?",
        (timeout_at, session_id),
    )
    set_session_data(conn, session_id, {
        "work_type": work_type,
        "project_id": project_id,
        "ticket_id": ticket_id,
        "advisory_metadata": metadata,
    })
    conn.commit()

    insert_activity(
        conn, actor="operator", action_type="advisory_i_handle",
        project_id=project_id, surface="telegram",
        summary=f"Operator will handle {work_type} issue ({ref})",
    )
    conn.commit()

    event_bus.publish(Event(
        event_type="advisory_i_handle",
        project_id=project_id,
        detail={"session_id": session_id, "ticket_id": ticket_id, "work_type": work_type},
    ))

    return {
        "text": t("advisory.i_handle_ack").format(minutes=delay_minutes),
        "session_id": session_id,
        "project_id": project_id,
    }


# ---------------------------------------------------------------------------
# Profile O actions
# ---------------------------------------------------------------------------

def _handle_draft_email(
    conn: sqlite3.Connection,
    project_id: str | None,
    metadata: dict,
    operator_id: str,
    *,
    chat_id: int | None = None,
    telegram_message_id: int | None = None,
) -> dict:
    """Trigger draft creation for a project advisory action."""
    insert_activity(
        conn, actor="operator", action_type="advisory_draft_requested",
        project_id=project_id, surface="telegram",
        summary="Draft email requested from advisory card — generating in background",
    )
    conn.commit()
    if project_id:
        _generate_draft_in_background(
            project_id=project_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
        )
    return {
        "text": t("advisory.drafting_email"),
        "project_id": project_id,
    }


def _handle_ask_more(
    conn: sqlite3.Connection,
    project_id: str | None,
    work_type: str,
    metadata: dict,
    operator_id: str,
) -> dict:
    """Operator wants more detail from advisory engine."""
    insert_activity(
        conn, actor="operator", action_type="advisory_ask_more",
        project_id=project_id, surface="telegram",
        summary=f"Operator requested more detail on {work_type} advisory",
    )
    conn.commit()
    event_bus.publish(Event(
        event_type="advisory_ask_more",
        project_id=project_id,
        detail={"work_type": work_type, "metadata": metadata},
    ))
    return {
        "text": t("advisory.ask_more_ack"),
        "project_id": project_id,
    }


# ---------------------------------------------------------------------------
# Profile P actions
# ---------------------------------------------------------------------------

def _handle_draft_reply(
    conn: sqlite3.Connection,
    ticket_id: str | None,
    metadata: dict,
    operator_id: str,
    *,
    chat_id: int | None = None,
    telegram_message_id: int | None = None,
) -> dict:
    """Return stored reply draft or launch background generation."""
    insert_activity(
        conn, actor="operator", action_type="support_draft_requested",
        project_id=None, surface="telegram",
        summary=f"Draft reply requested for ticket {ticket_id}",
    )
    conn.commit()

    if ticket_id:
        from src.repositories.ticket_repo import get_ticket
        ticket = get_ticket(conn, ticket_id)
        if ticket and ticket.reply_draft_text:
            preview = ticket.reply_draft_text[:600]
            return {
                "text": t("advisory.stored_reply_draft").format(ticket_id=ticket_id, preview=preview),
            }
        # No stored draft — generate in background
        _generate_reply_in_background(
            ticket_id=ticket_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
        )
        return {"text": t("advisory.generating_reply").format(ticket_id=ticket_id)}

    return {"text": t("advisory.no_ticket_for_draft")}


from src.telegram.support_callback_command import SELECTABLE_CATEGORIES as _TICKET_CATEGORIES


def _handle_reclassify(
    conn: sqlite3.Connection,
    ticket_id: str | None,
    metadata: dict,
    operator_id: str,
) -> dict:
    """Show second-stage inline keyboard with category options."""
    insert_activity(
        conn, actor="operator", action_type="support_reclassify",
        project_id=None, surface="telegram",
        summary=f"Ticket {ticket_id} marked for reclassification",
    )
    conn.commit()

    from src.telegram.cards import build_card, format_card_text
    from src.telegram.markup import card_to_markup

    extra_buttons = []
    for cat in _TICKET_CATEGORIES:
        label = cat.replace("_", " ").title()
        callback = f"P:RECLASS_SELECT:{ticket_id}:{cat}" if ticket_id else f"P:RECLASS_SELECT:none:{cat}"
        extra_buttons.append([(label, callback)])

    card = build_card(
        "P",
        "Select Category",
        t("advisory.choose_category").format(ticket_id=ticket_id or 'this ticket'),
        extra_buttons=extra_buttons,
    )
    return {
        "text": format_card_text(card),
        "reply_markup": card_to_markup(card),
        "card": card,
    }
