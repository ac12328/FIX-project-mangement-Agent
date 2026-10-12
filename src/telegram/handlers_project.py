"""Handlers for Identity Card (E), Ambiguous Card (H), New Project Card (K)."""

from __future__ import annotations

import logging
import sqlite3

from src.events.bus import Event, event_bus
from src.repositories.activity_log_repo import insert_activity
from src.telegram._cb_helpers import extract_link, extract_message_metadata, extract_object_ref
from src.telegram.i18n import t
from src.telegram.session import create_session

logger = logging.getLogger(__name__)


def identity_callback_denial(
    conn: sqlite3.Connection,
    suggestion_id: str | None,
    message_context: dict | None,
) -> str | None:
    """Require the callback target to be the pending suggestion on this E card."""
    from src.triage.contact_learning import get_contact_suggestion

    unavailable = "Identity card unavailable. No action was taken."
    metadata = extract_message_metadata(message_context)
    links = [
        link for link in (message_context or {}).get("links", [])
        if link.get("object_type") == "contact_suggestion"
    ]
    link = links[0] if len(links) == 1 else None
    if not suggestion_id or not message_context or (
        message_context.get("direction") != "outbound"
        or message_context.get("message_kind") != "card"
        or metadata.get("card_profile") != "E"
        or metadata.get("contact_suggestion_id") != suggestion_id
        or not link or link.get("object_ref") != suggestion_id
    ):
        return unavailable
    suggestion = get_contact_suggestion(conn, suggestion_id)
    if not suggestion or suggestion["status"] != "pending" or (
        message_context.get("project_id") != suggestion["project_id"]
        or link.get("project_id") != suggestion["project_id"]
    ):
        return unavailable
    return None


def handle_identity(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle Identity Check Card (E) actions."""
    from src.triage.contact_learning import apply_contact_suggestion, get_contact_suggestion

    # Re-read by the callback's message identity; do not trust a caller-supplied
    # context after the entry preflight or when invoked through another router.
    from src.telegram.persistence import find_message_context

    chat_id = (message_context or {}).get("chat_id")
    telegram_message_id = (message_context or {}).get("telegram_message_id")
    persisted_context = (
        find_message_context(
            conn, chat_id=chat_id, telegram_message_id=telegram_message_id,
        ) if chat_id is not None and telegram_message_id is not None else None
    )
    denial = identity_callback_denial(conn, project_id, persisted_context)
    if denial:
        return {"text": denial, "_error": True}
    message_context = persisted_context

    contact_link = extract_link(message_context, "contact")
    suggestion_link = extract_link(message_context, "contact_suggestion")
    suggestion_id = project_id or (suggestion_link["object_ref"] if suggestion_link else None)
    contact_id = (contact_link["object_ref"] if contact_link else None)
    suggestion = get_contact_suggestion(conn, suggestion_id) if suggestion_id else None

    if action == "CONFIRM":
        if suggestion is not None:
            applied = apply_contact_suggestion(conn, suggestion_id, operator_id=operator_id)
            if applied is not None:
                insert_activity(
                    conn,
                    actor="operator",
                    action_type="identity_confirmed",
                    surface="telegram",
                    object_ref=suggestion_id,
                    project_id=suggestion.get("project_id"),
                    summary=f"Contact suggestion {suggestion_id} applied",
                )
                conn.commit()
                event_bus.publish(Event(
                    event_type="identity_confirmed",
                    detail={
                        "suggestion_id": suggestion_id,
                        "contact_id": applied["contact_id"],
                    },
                    project_id=suggestion.get("project_id"),
                    surface="telegram",
                ))
                return {
                    "text": t("identity.sender_confirmed_added").format(sender_email=suggestion['sender_email']),
                    "project_id": suggestion.get("project_id"),
                    "object_ref": suggestion_id,
                }
            return {
                "text": t("identity.suggestion_not_applied").format(suggestion_id=suggestion_id),
                "project_id": suggestion.get("project_id"),
                "object_ref": suggestion_id,
            }
        if contact_id:
            conn.execute(
                "UPDATE contacts SET confidence = 1.0 WHERE contact_id = ?",
                (contact_id,),
            )
            insert_activity(
                conn,
                actor="operator",
                action_type="identity_confirmed",
                surface="telegram",
                object_ref=contact_id,
                summary=f"Contact {contact_id} identity confirmed",
            )
            conn.commit()
            event_bus.publish(Event(
                event_type="identity_confirmed",
                detail={"contact_id": contact_id},
                surface="telegram",
            ))
        return {"text": t("identity.contact_confirmed").format(contact_id=contact_id or 'unknown'), "object_ref": contact_id or suggestion_id}

    if action == "MERGE":
        session_id = create_session(
            conn,
            "identity_merge",
            originating_ref=contact_id,
            operator_id=operator_id,
        )
        return {
            "text": t("identity.send_merge_id"),
            "session_id": session_id,
            "object_ref": contact_id,
            "object_links": [
                {"object_type": "telegram_session", "object_ref": session_id},
            ],
        }

    if action == "IGNORE":
        if contact_id:
            insert_activity(
                conn,
                actor="operator",
                action_type="identity_ignored",
                surface="telegram",
                object_ref=contact_id,
                summary=f"Contact {contact_id} identity check ignored",
            )
        conn.commit()
        return {"text": t("identity.contact_ignored").format(contact_id=contact_id or suggestion_id or 'unknown'), "object_ref": contact_id or suggestion_id}

    if action == "OVERRIDE":
        # Force-confirm as Vendor (cross-project party type)
        if suggestion is not None:
            applied = apply_contact_suggestion(
                conn, suggestion_id, operator_id=operator_id, org_type="vendor",
            )
            if applied is None:
                return {
                    "text": t("identity.suggestion_not_applied").format(
                        suggestion_id=suggestion_id
                    ),
                    "project_id": suggestion.get("project_id"),
                    "object_ref": suggestion_id,
                    "_error": True,
                }
            insert_activity(
                conn,
                actor="operator",
                action_type="identity_overridden",
                surface="telegram",
                object_ref=suggestion_id,
                project_id=suggestion.get("project_id"),
                summary=f"Contact suggestion {suggestion_id} forced as vendor",
            )
            conn.commit()
            return {
                "text": t("identity.sender_overridden").format(
                    sender_email=suggestion["sender_email"]
                ),
                "project_id": suggestion.get("project_id"),
                "object_ref": suggestion_id,
            }
        return {
            "text": t("identity.override_failed").format(
                contact_id=contact_id or suggestion_id
            ),
            "object_ref": contact_id or suggestion_id,
        }

    if action == "FLAG":
        # Flag for manual compliance review
        insert_activity(
            conn,
            actor="operator",
            action_type="identity_flagged",
            surface="telegram",
            object_ref=contact_id or suggestion_id,
            project_id=suggestion.get("project_id") if suggestion else None,
            summary=f"Contact {contact_id or suggestion_id} flagged for compliance review",
        )
        conn.commit()
        return {"text": t("identity.flagged").format(reference=contact_id or suggestion_id), "object_ref": contact_id or suggestion_id}

    return {"text": t("identity.generic_action").format(action=action), "object_ref": contact_id or suggestion_id}


def handle_ambiguous(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle Ambiguous Routing Card (H)."""
    if action:
        insert_activity(
            conn,
            actor="operator",
            action_type="email_routed",
            project_id=action,
            surface="telegram",
            summary=f"Operator manually routed email to {action}",
            object_ref=extract_object_ref(message_context),
        )
        conn.commit()
        return {"text": t("ambiguous.email_assigned").format(project_id=action), "project_id": action}
    return {"text": t("ambiguous.no_project")}


def handle_new_project(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle New Project Card (Profile F)."""
    from src.portfolio.project_intake import confirm_intake, create_intake_from_structured
    from src.tools.project_intake_handlers import clear_intake_proposal, load_intake_proposal

    if action in ("CREATE", "CONFIRM") and project_id:
        proposal = load_intake_proposal(conn, project_id)
        if proposal is None:
            proposal = create_intake_from_structured(project_id, project_id, source="telegram")
        ok, msg = confirm_intake(conn, proposal)
        if ok:
            clear_intake_proposal(conn, project_id)
            event_bus.publish(Event(
                event_type="project_created",
                project_id=project_id,
                surface="telegram",
            ))
        return {"text": msg, "project_id": project_id}

    if action == "DISCARD":
        if project_id:
            clear_intake_proposal(conn, project_id)
        return {"text": t("new_project.intake_discarded"), "project_id": project_id}

    return {"text": t("new_project.generic_action").format(action=action), "project_id": project_id}
