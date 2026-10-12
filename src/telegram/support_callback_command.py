"""Transaction-owned mutations for a persisted Profile P support card."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from src.auth.rbac import P_BUSINESS_WRITE, permissions_for_role
from src.auth.repository import get_user
from src.db.savepoint import savepoint
from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity
from src.repositories.ticket_repo import get_ticket, update_ticket_fields
from src.telegram._cb_helpers import extract_message_metadata
from src.telegram.i18n import t
from src.telegram.persistence import find_message_context

logger = logging.getLogger(__name__)

# The selector intentionally does not offer the outreach-only category.
SELECTABLE_CATEGORIES = (
    "fix_session", "network", "trade", "post_trade", "cert", "config",
    "access", "monitoring", "other",
)
_UNAVAILABLE = "Support card or ticket unavailable. No operation was executed."


class _UnavailableCard(Exception):
    pass


def _bound_ticket(conn: sqlite3.Connection, message_context: dict | None,
                  operator_id: str):
    """Reread card, actor, and ticket in the same writer used for the command."""
    chat_id = (message_context or {}).get("chat_id")
    message_id = (message_context or {}).get("telegram_message_id")
    if chat_id is None or message_id is None:
        raise _UnavailableCard
    card = find_message_context(
        conn, chat_id=chat_id, telegram_message_id=message_id,
    )
    metadata = extract_message_metadata(card)
    if not card or card.get("direction") != "outbound" or (
        card.get("message_kind") != "card"
        or metadata.get("card_profile") != "P"
        or not metadata.get("ticket_id")
    ):
        raise _UnavailableCard
    actor = get_user(conn, operator_id)
    if not actor or actor["status"] != "active" or (
        P_BUSINESS_WRITE not in permissions_for_role(actor["role"])
    ):
        raise _UnavailableCard
    ticket = get_ticket(conn, metadata["ticket_id"])
    if ticket is None or ticket.status in {"resolved", "closed"}:
        raise _UnavailableCard
    card_project = card.get("project_id")
    if card_project is not None and card_project != ticket.related_project_id:
        raise _UnavailableCard
    metadata_project = metadata.get("project_id")
    if metadata_project is not None and metadata_project != ticket.related_project_id:
        raise _UnavailableCard
    return card, metadata, ticket


def _valid_target(card: dict, metadata: dict, ticket, action: str,
                  callback_entity: str | None) -> bool:
    if action == "ESCALATE":
        team = metadata.get("escalation_team")
        return callback_entity == card.get("project_id") and (
            isinstance(team, str) and bool(team.strip())
        )
    if action == "RECLASS_SELECT":
        ticket_id, separator, category = (callback_entity or "").partition(":")
        return bool(separator) and ticket_id == ticket.ticket_id and (
            category in SELECTABLE_CATEGORIES
        )
    return False


def support_callback_denial(conn: sqlite3.Connection, action: str,
                            callback_entity: str | None, message_context: dict | None,
                            operator_id: str) -> str | None:
    """Check the persisted card before an irreversible Telegram card edit."""
    try:
        card, metadata, ticket = _bound_ticket(conn, message_context, operator_id)
        return None if _valid_target(card, metadata, ticket, action, callback_entity) else _UNAVAILABLE
    except _UnavailableCard:
        return _UNAVAILABLE


def apply_support_ticket_action(
    conn: sqlite3.Connection,
    action: str,
    callback_entity: str | None,
    *,
    message_context: dict | None,
    operator_id: str,
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Revalidate and atomically mutate one ticket; caller owns callback commit."""
    try:
        with savepoint(conn, ensure_transaction=True):
            card, metadata, ticket = _bound_ticket(conn, message_context, operator_id)
            if not _valid_target(card, metadata, ticket, action, callback_entity):
                raise _UnavailableCard
            if action == "ESCALATE":
                target = metadata["escalation_team"]
                fields = {"escalated_to": target}
                activity = "support_escalated"
                event_type = "support_escalated"
                detail = {"ticket_id": ticket.ticket_id, "escalation_team": target}
                summary = f"Ticket {ticket.ticket_id} escalated to {target}"
                text = t("advisory.escalated_to").format(escalation_team=target)
            elif action == "RECLASS_SELECT":
                ticket_id, _, category = (callback_entity or "").partition(":")
                fields = {"category": category}
                activity = "support_reclassified"
                event_type = "support_reclassified"
                detail = {"ticket_id": ticket_id, "new_category": category}
                summary = f"Ticket {ticket_id} reclassified to {category}"
                label = category.replace("_", " ").title()
                text = t("advisory.reclassified").format(ticket_id=ticket_id, label=label)
            else:
                raise _UnavailableCard

            field, value = next(iter(fields.items()))
            changed = getattr(ticket, field) != value
            if changed:
                updated = update_ticket_fields(conn, ticket.ticket_id, **fields)
                if updated is None:
                    raise _UnavailableCard
                insert_activity(
                    conn, actor=operator_id, action_type=activity,
                    project_id=ticket.related_project_id, surface="telegram",
                    object_ref=ticket.ticket_id, summary=summary,
                )
        if post_commit is None:
            conn.commit()
        if changed:
            event = Event(
                event_type=event_type, project_id=ticket.related_project_id,
                detail=detail, surface="telegram",
            )
            if post_commit is None:
                try:
                    event_bus.publish(event)
                except Exception:
                    logger.exception("Post-commit support event failed: %s", action)
            else:
                post_commit.append(lambda event=event: event_bus.publish(event))
        return {"text": text, "project_id": ticket.related_project_id,
                "object_ref": ticket.ticket_id}
    except _UnavailableCard:
        return {"_error": True, "text": _UNAVAILABLE}
    except Exception:
        logger.exception("Support card command failed: %s", action)
        return {"_error": True, "text": "Support action could not be confirmed. Check the ticket before retrying."}
