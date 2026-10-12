"""
Advisory suggestion helper — appends context-aware advisory to chat responses.

Replaces the inline `_maybe_suggest()` in `agent_chat.py` and adds
support ticket awareness + advisory engine integration.
"""

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_STALE_EXTERNAL_DAYS = 3


def maybe_suggest_advisory(
    conn: sqlite3.Connection,
    project_id: str | None,
    response_text: str,
) -> str:
    """Append proactive suggestions when relevant. Never raises.

    Checks:
      1. Stale External project → suggest follow-up
      2. Pending drafts → remind operator
      3. Open support tickets linked to project → show advisory summary
    """
    if not project_id:
        return response_text
    lowered = (response_text or "").casefold()
    draft_just_created = (
        "draft_" in lowered
        and any(marker in lowered for marker in ("created", "saved", "??", "??"))
    )
    if draft_just_created:
        return response_text
    try:
        suggestions = _gather_suggestions(conn, project_id)
        if suggestions:
            response_text += "\n\n" + "\n".join(suggestions)
    except Exception:
        logger.debug("Advisory suggestions failed for %s", project_id, exc_info=True)
    return response_text


def _gather_suggestions(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Gather suggestion lines for a project."""
    from src.repositories.project_repo import get_project
    from src.email.draft_session import get_pending_drafts
    from src.utils.calendar import business_days_between

    project = get_project(conn, project_id)
    if not project:
        return []

    suggestions: list[str] = []

    # Skip generic stale chase when M8 sign-off ready (PM handling complete)
    try:
        from src.agent.progress_advisory import is_m8_signoff_ready
        if is_m8_signoff_ready(project):
            suggestions.extend(_ticket_suggestions(conn, project_id))
            pending = get_pending_drafts(conn, project_id=project_id)
            if pending:
                suggestions.append(
                    f"-- {len(pending)} pending draft(s) for {project_id}. /pending to review."
                )
            return suggestions
    except Exception:
        pass

    # 1. Stale External project → suggest follow-up
    if project.ball_with.value == "External" and project.updated_at:
        updated = project.updated_at
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        days = business_days_between(updated, datetime.now(timezone.utc))
        if days >= _STALE_EXTERNAL_DAYS:
            suggestions.append(
                f"-- {project_id} has been waiting {days} business days. "
                "Want me to draft a follow-up?"
            )

    # 2. Pending drafts
    pending = get_pending_drafts(conn, project_id=project_id)
    if pending:
        suggestions.append(
            f"-- {len(pending)} pending draft(s) for {project_id}. /pending to review."
        )

    # 3. Open support tickets linked to this project
    suggestions.extend(_ticket_suggestions(conn, project_id))

    return suggestions


def _ticket_suggestions(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Check for open tickets and add advisory summary if found."""
    try:
        from src.repositories.ticket_repo import get_open_tickets_for_project
        from src.utils.enum_helpers import enum_val
        tickets = get_open_tickets_for_project(conn, project_id)
        if not tickets:
            return []
        lines: list[str] = []
        for t in tickets[:3]:
            sev = enum_val(t.severity)
            cat = enum_val(t.category)
            lines.append(
                f"-- Open ticket {t.ticket_id} ({sev} {cat}): {t.title[:60]}"
            )
        return lines
    except Exception:
        logger.debug("Ticket suggestion lookup failed for %s", project_id, exc_info=True)
        return []
