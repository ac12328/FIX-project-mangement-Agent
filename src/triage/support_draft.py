"""
Background draft generation for support tickets.

Fires a single LLM call (merged advisory + reply draft) when a ticket is
created. Stores reply_draft_text on the ticket row asynchronously.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from src.llm.client import llm_call_structured

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "support" / "reply_draft.md"

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "escalation_team": {"type": ["string", "null"]},
        "reply_draft": {"type": "string"},
    },
    "required": ["assessment", "next_steps", "escalation_team", "reply_draft"],
}


def build_support_draft(ticket) -> dict | None:
    """Build draft content without holding a database write transaction."""
    try:
        prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
        description_snippet = (ticket.description or "")[:800]

        prompt = (
            prompt_template
            .replace("{{title}}", ticket.title or "")
            .replace("{{category}}", str(ticket.category.value) if hasattr(ticket.category, "value") else str(ticket.category or ""))
            .replace("{{severity}}", str(ticket.severity.value) if hasattr(ticket.severity, "value") else str(ticket.severity or "medium"))
            .replace("{{counterparty}}", ticket.counterparty or "Counterparty")
            .replace("{{fix_log_snippet}}", description_snippet)
            .replace("{{similar_cases}}", "None available.")
        )

        result = llm_call_structured(
            task="support_draft",
            messages=[{"role": "user", "content": prompt}],
            response_schema=_DRAFT_SCHEMA,
        )

        return result

    except Exception:
        logger.exception("Failed to generate support draft for %s", ticket.ticket_id)
        return None


def generate_support_draft(conn, ticket) -> dict | None:
    """Generate and store a ticket draft for legacy direct callers."""
    from src.repositories.ticket_repo import update_ticket_draft

    result = build_support_draft(ticket)
    try:
        if result and result.get("reply_draft"):
            update_ticket_draft(conn, ticket.ticket_id, result["reply_draft"])
        return result
    except Exception:
        logger.exception("Failed to store support draft for %s", ticket.ticket_id)
        return None


def schedule_draft_generation(
    ticket_id: str, *, on_complete: Callable[[dict | None], None] | None = None,
) -> None:
    """
    Fire background daemon thread to generate support draft.
    Returns immediately — draft is stored asynchronously.
    """
    def _worker():
        result = None
        try:
            from src.db.connection import scoped_read, scoped_write
            from src.repositories.ticket_repo import get_ticket

            if on_complete is None:
                with scoped_write() as bg_conn:
                    ticket = get_ticket(bg_conn, ticket_id)
                    if ticket:
                        generate_support_draft(bg_conn, ticket)
            else:
                with scoped_read() as bg_conn:
                    ticket = get_ticket(bg_conn, ticket_id)
                if ticket:
                    result = build_support_draft(ticket)
        except Exception:
            logger.exception("Background draft generation failed for %s", ticket_id)
        finally:
            if on_complete is not None:
                try:
                    on_complete(result)
                except Exception:
                    logger.exception("Could not record draft result for %s", ticket_id)

    thread = threading.Thread(
        target=_worker, daemon=True, name=f"support-draft-{ticket_id}"
    )
    thread.start()
