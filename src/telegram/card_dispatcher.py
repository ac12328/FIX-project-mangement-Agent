"""
WS-4: Central card dispatcher.

Parses callback data, enriches message_context with chat_id + telegram_message_id,
and forwards to the appropriate handler. This ensures advisory handlers always
receive Telegram context for background card editing.

D4: Destructive actions (SEND, APPROVE) require a two-click confirmation flow.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
import sqlite3
from typing import Callable

from src.telegram.handlers_review import handle_review, handle_triage
from src.telegram.handlers_draft import handle_draft, handle_inbound_reply, handle_style_pref
from src.telegram.handlers_project import handle_identity, handle_ambiguous, handle_new_project
from src.telegram.handlers_ops import handle_sla, handle_followup, handle_brief, handle_inactivity, handle_error
from src.telegram.handlers_ticket import handle_attach
from src.telegram.handlers_query import handle_query_clarification
from src.telegram.handlers_batch import handle_batch
from src.telegram.handlers_advisory import handle_project_advisory, handle_support_advisory
from src.telegram.handlers_commitment import handle_commitment
from src.telegram.handlers_staged import handle_staged_update
from src.db.savepoint import savepoint
from src.telegram.callback_protocol import (
    confirm_required_actions,
    is_generic_cancel,
    parse_callback_data,
)
from src.telegram.i18n import t

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, Callable] = {
    "A": handle_review,
    "B": handle_triage,
    "TK": handle_triage,
    "C": handle_draft,
    "D": handle_draft,
    "E": handle_identity,
    "F": handle_new_project,
    "G": handle_followup,
    "H": handle_ambiguous,
    "I": handle_brief,
    "J": handle_inactivity,
    "K": handle_query_clarification,
    "L": handle_inbound_reply,
    "M": handle_sla,
    "N": handle_error,
    "O": handle_project_advisory,
    "P": handle_support_advisory,
    "Q": handle_commitment,
    "CP": handle_draft,
    "CU": handle_staged_update,
    "PREF": handle_style_pref,
    "IA": handle_inactivity,
    "BATCH": handle_batch,
    "ATTACH": handle_attach,
}

# Actions that require two-click confirmation (D4 undo mechanism).
# CU staged updates are already explicit confirm/cancel cards — single click only.
_CONFIRM_REQUIRED = confirm_required_actions()


def dispatch(
    conn: sqlite3.Connection,
    data: str,
    *,
    chat_id: int | None = None,
    telegram_message_id: int | None = None,
    operator_id: str = "default_operator",
    message_context: dict | None = None,
    post_commit: list[Callable[[], None]] | None = None,
    owned_draft_scope: bool = False,
    draft_scope_start_changes: int | None = None,
) -> dict | None:
    """
    Parse callback data and dispatch to the appropriate handler.

    Injects chat_id + telegram_message_id into message_context for handlers
    that launch background tasks (e.g. advisory draft generation).
    """
    parsed = parse_callback_data(data)
    if parsed is None:
        return {"text": "未知操作。請重試或使用 /help。"}

    profile = parsed.profile
    action = parsed.action
    entity_id = parsed.entity_id

    if is_generic_cancel(parsed):
        return {"text": t("dispatcher.action_cancelled")}

    handler = _HANDLERS.get(profile)
    if handler is None:
        return {"text": f"未知卡片類型：{profile}。請重試或使用 /help。"}

    enriched_context: dict = dict(message_context or {})
    if chat_id is not None:
        enriched_context["chat_id"] = chat_id
    if telegram_message_id is not None:
        enriched_context["telegram_message_id"] = telegram_message_id

    command_scope = profile == "CU" or (post_commit is not None and (
        profile == "A" or (
            profile in {"ATTACH", "Q"} and action in {"SAVE", "CONFIRM", "DISMISS"}
        ) or (
            profile == "P" and action in {"ESCALATE", "RECLASS_SELECT"}
        ) or (
            profile == "L" and action in {
                "CONFIRM_UPDATE", "EDIT", "UPDATE_PROJECT", "UNDO", "REJECT",
            }
        )
    ))
    effect_start = len(post_commit) if command_scope and post_commit is not None else 0
    try:
        scope = savepoint(conn, ensure_transaction=True) if command_scope else nullcontext()
        with scope:
            handler_kwargs = {"message_context": enriched_context, "operator_id": operator_id}
            if profile in {"A", "L", "CU", "ATTACH", "Q", "P"}:
                handler_kwargs["post_commit"] = post_commit
            if profile in {"C", "D", "CP"}:
                handler_kwargs["scope_start_changes"] = draft_scope_start_changes
            if owned_draft_scope and profile in {"G", "M"}:
                handler_kwargs["owned_draft_scope"] = True
            return handler(conn, action, entity_id, **handler_kwargs)
    except Exception:
        logger.exception("Callback handler error: %s", data)
        if command_scope:
            # The savepoint discarded the command; discard its deferred effects too.
            if post_commit is not None:
                del post_commit[effect_start:]
        else:
            try:
                conn.rollback()
            except Exception:
                pass
        # Tag the error so the three-phase card renderer can show ❌ instead
        # of ✅ when a destructive action (APPROVE, CONFIRM_ALL, etc.) fails.
        return {"_error": True, "text": "An error occurred processing that action."}
