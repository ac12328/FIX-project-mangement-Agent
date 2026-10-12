"""
Telegram callback query router for AgentFIXBPRO.
Routes card button presses to handler functions in sub-modules.
"""

from __future__ import annotations

import logging
import sqlite3
import asyncio
import threading
from contextlib import nullcontext
from collections.abc import Callable
from typing import ContextManager

from config.settings import CONFIG
from src.telegram.persistence import find_message_context, record_callback_action

# Handler sub-modules
from src.telegram.handlers_review import handle_review, handle_triage
from src.telegram.handlers_draft import handle_draft, handle_inbound_reply, handle_style_pref
from src.telegram.handlers_project import handle_identity, handle_ambiguous, handle_new_project
from src.telegram.handlers_ops import handle_sla, handle_followup, handle_brief, handle_inactivity, handle_error
from src.telegram.handlers_ticket import handle_attach
from src.telegram.handlers_query import handle_query_clarification
from src.telegram.handlers_batch import handle_batch
from src.telegram.handlers_advisory import handle_project_advisory, handle_support_advisory
from src.telegram import card_dispatcher
from src.telegram.i18n import t

logger = logging.getLogger(__name__)


def handle_callback(
    conn: sqlite3.Connection,
    data: str,
    *,
    chat_id: int | None = None,
    telegram_message_id: int | None = None,
    operator_id: str = "default_operator",
    post_commit: list[Callable[[], None]] | None = None,
    audit_write_scope: Callable[[], ContextManager[sqlite3.Connection]] | None = None,
    owned_draft_scope: bool = False,
    draft_scope_start_changes: int | None = None,
) -> dict | None:
    """Route a callback query to the appropriate handler via card_dispatcher."""
    message_context = None
    if chat_id is not None and telegram_message_id is not None:
        message_context = find_message_context(
            conn, chat_id=chat_id, telegram_message_id=telegram_message_id
        )

    parts = data.split(":", 2)
    profile = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else ""
    resolved_project_id = (parts[2] if len(parts) > 2 else None) or _extract_project_id_from_context(message_context)
    # Per-item BATCH callbacks carry "session_id:idx" as entity_id. Strip the
    # trailing :idx so the activity-log project_id is not polluted with a
    # composite string. handle_batch itself uses partition(":") to split.
    if profile == "BATCH" and resolved_project_id and ":" in resolved_project_id:
        resolved_project_id = resolved_project_id.partition(":")[0]

    response = card_dispatcher.dispatch(
        conn,
        data,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        operator_id=operator_id,
        message_context=message_context,
        post_commit=post_commit,
        owned_draft_scope=owned_draft_scope,
        draft_scope_start_changes=draft_scope_start_changes,
    )

    try:
        # Error cards show recovery information. Never replay an unrelated last
        # chat message or claim a scheduler retry was started by this callback.
        if profile == "N" and action == "RETRY" and response and "retry_started" not in response:
            from src.telegram.error_notices import error_notice
            import json
            try:
                metadata = json.loads((message_context or {}).get("metadata_json") or "{}")
            except (ValueError, TypeError):
                metadata = {}
            task = metadata.get("task") or (response.get("retry_context") or {}).get("task_type") or "unknown"
            response["text"] = error_notice(task, metadata.get("error_class", ""), reference=metadata.get("trace_id"))
        if chat_id is not None and telegram_message_id is not None:
            from src.telegram._cb_helpers import extract_object_ref
            with (audit_write_scope() if audit_write_scope else nullcontext(conn)) as audit_conn:
                record_callback_action(
                    audit_conn,
                    chat_id=chat_id,
                    telegram_message_id=telegram_message_id,
                    callback_data=data,
                    project_id=(response or {}).get("project_id", resolved_project_id),
                    session_id=(response or {}).get("session_id"),
                    operator_id=operator_id,
                    object_ref=(response or {}).get("object_ref") or extract_object_ref(message_context),
                    metadata={"profile": profile, "action": action},
                    commit=post_commit is None and audit_write_scope is None,
                )
    except Exception:
        logger.exception("Post-dispatch callback action record failed: %s", data)

    return response


def _retry_last_message(conn: sqlite3.Connection, chat_id: int, *, before_message_id: int | None = None) -> None:
    """Re-run agent_respond on the inbound message that triggered the error."""
    if before_message_id:
        row = conn.execute(
            """SELECT text FROM telegram_messages
               WHERE chat_id = ? AND direction = 'inbound'
                 AND text IS NOT NULL AND text != ''
                 AND telegram_message_id < ?
               ORDER BY telegram_message_id DESC LIMIT 1""",
            (chat_id, before_message_id),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT text FROM telegram_messages
               WHERE chat_id = ? AND direction = 'inbound'
                 AND text IS NOT NULL AND text != ''
               ORDER BY id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
    if not row or not row["text"]:
        logger.warning("N:RETRY — no prior inbound message found for chat_id=%s", chat_id)
        return

    original_text = str(row["text"])
    token = CONFIG.telegram.bot_token

    def _run() -> None:
        from src.telegram.agent_chat import agent_respond
        from telegram import Bot
        try:
            # agent_respond manages its own scoped_write() per tool call internally.
            # Do NOT wrap it in scoped_write() — that holds the write lock for the
            # entire multi-round LLM loop (30-180s), starving email poll and other jobs.
            result_text = agent_respond(None, original_text, chat_id)

            async def _send() -> None:
                from src.telegram.message_delivery import send_formatted_text

                bot = Bot(token=token)
                await send_formatted_text(
                    bot,
                    chat_id=chat_id,
                    text=result_text or t("callback.retry_no_response"),
                )
            asyncio.run(_send())
        except Exception:
            logger.exception("N:RETRY background re-run failed for chat_id=%s", chat_id)

    threading.Thread(target=_run, daemon=True).start()


def _extract_project_id_from_context(message_context: dict | None) -> str | None:
    if not message_context:
        return None
    if message_context.get("project_id"):
        return message_context["project_id"]
    for link in message_context.get("links", []):
        if link.get("project_id"):
            return link["project_id"]
    return None


_HANDLERS = {
    "A": handle_review,
    "B": handle_triage,
    "TK": handle_triage,
    "C": handle_draft,
    "D": handle_draft,
    "E": handle_identity,
    "F": handle_new_project,       # Profile F: New Project Intake Card
    "G": handle_followup,
    "H": handle_ambiguous,
    "I": handle_brief,
    "J": handle_inactivity,
    "K": handle_query_clarification,  # Profile K: Query Clarification Card
    "L": handle_inbound_reply,
    "M": handle_sla,                   # Profile M: SLA Alert Card
    "N": handle_error,                 # Profile N: LLM Error Card
    "O": handle_project_advisory,        # Profile O: Project Advisory Card
    "P": handle_support_advisory,        # Profile P: Support Advisory Card
    "PREF": handle_style_pref,         # Style preference save (YES/NO after EDIT+SEND)
    "IA": handle_inactivity,  # Inactivity Alert (Section 34)
    "BATCH": handle_batch,             # Batch Update card (WP-19)
    "ATTACH": handle_attach,           # Attach email to ticket (Profile L)
}
