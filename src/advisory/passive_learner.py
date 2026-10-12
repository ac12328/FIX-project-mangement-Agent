"""
Passive learner — extracts patterns from sent emails linked to support tickets.

When an email is sent in the context of a support ticket, we extract:
  - CC patterns → potential escalation targets
  - Diagnostic questions asked → diagnosis steps
  - Resolution described → resolution pattern candidate
"""

import logging
import sqlite3

from src.repositories.resolution_pattern_repo import insert_pattern, get_similar_patterns
from src.repositories.ticket_repo import get_ticket, update_ticket_fields
from src.utils.enum_helpers import enum_val

logger = logging.getLogger(__name__)


def learn_from_sent_email(
    conn: sqlite3.Connection,
    draft: dict,
    ticket_id: str,
) -> dict:
    """Extract learning signals from a sent email linked to a support ticket.

    Args:
        conn: Database connection.
        draft: The draft dict (from get_draft).
        ticket_id: The support ticket this email relates to.

    Returns:
        Dict with keys: escalation_learned (bool), pattern_stored (bool), pattern_id (str|None).
    """
    result = {"escalation_learned": False, "pattern_stored": False, "pattern_id": None}

    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        return result

    body = draft.get("current_draft_text") or ""
    cc = draft.get("cc") or ""

    # 1. CC pattern → potential escalation target
    if cc and not ticket.escalated_to:
        # Store the CC as escalation hint
        update_ticket_fields(conn, ticket_id, escalated_to=cc.split(",")[0].strip())
        result["escalation_learned"] = True
        logger.debug("Passive learner: escalation target %s from CC for %s", cc, ticket_id)

    # 2. If body mentions resolution keywords, create a lightweight pattern
    if body and _looks_like_resolution(body):
        category = enum_val(ticket.category)
        # Check if a very similar pattern already exists
        keywords = _extract_action_keywords(body)
        existing = get_similar_patterns(conn, category=category, keywords=keywords, limit=1)
        if existing and existing[0].confidence >= 0.6:
            # Similar pattern exists — skip to avoid duplicates
            return result

        import uuid
        pattern_id = f"rp_{uuid.uuid4().hex[:10]}"
        pattern = insert_pattern(
            conn,
            pattern_id,
            category=category,
            trigger_keywords=keywords,
            resolution_action=_extract_resolution_summary(body),
            escalation_target=cc.split(",")[0].strip() if cc else None,
            source="auto_extracted",
            confidence=0.3,  # Low confidence — auto-extracted from email
        )
        result["pattern_stored"] = True
        result["pattern_id"] = pattern.pattern_id

        # Link pattern to ticket
        update_ticket_fields(conn, ticket_id, resolution_pattern_id=pattern_id)
        conn.commit()
        logger.info("Passive learner: stored pattern %s from email for %s", pattern_id, ticket_id)

    return result


def _looks_like_resolution(body: str) -> bool:
    """Heuristic: does the email body describe a resolution action?"""
    import re
    resolution_markers = [
        "resolved", "fixed", "restarted", "updated", "cleared",
        "corrected", "reset", "renewed", "applied", "patched",
        "restored", "reconfigured", "reconnected",
    ]
    body_lower = body.lower()
    return sum(1 for m in resolution_markers if re.search(rf"\b{m}\b", body_lower)) >= 2


def _extract_action_keywords(body: str) -> list[str]:
    """Extract significant keywords from email body for pattern matching."""
    words = [w.lower().strip(".,;:!?()[]") for w in body.split() if len(w) > 3]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:10]


def _extract_resolution_summary(body: str) -> str:
    """Extract a concise resolution summary from the email body."""
    # Take up to 500 chars, prefer first paragraph
    lines = body.strip().split("\n")
    summary_lines: list[str] = []
    total = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if summary_lines:
                break  # Stop at first blank line after content
            continue
        summary_lines.append(stripped)
        total += len(stripped)
        if total > 500:
            break
    return " ".join(summary_lines)[:500] if summary_lines else body[:500]
