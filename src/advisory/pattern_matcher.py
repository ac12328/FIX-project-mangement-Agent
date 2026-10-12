"""
Cross-ticket pattern matching — finds similar past tickets and enriches advisories.
"""

import logging
import sqlite3

from src.models.advisory import Advisory
from src.models.support_ticket import SupportTicket
from src.repositories.resolution_pattern_repo import get_similar_patterns
from src.utils.enum_helpers import enum_val

logger = logging.getLogger(__name__)


def find_similar_tickets(
    conn: sqlite3.Connection,
    ticket: SupportTicket,
    limit: int = 3,
) -> list[dict]:
    """Find past resolved tickets similar to the given ticket.

    Uses keyword overlap between the new ticket's description and resolved tickets'
    resolution text + descriptions.

    Returns list of dicts with ticket_id, title, resolution, similarity_score.
    """
    if not ticket.description:
        return []

    keywords = _extract_keywords(ticket.description)
    if not keywords:
        return []

    category = enum_val(ticket.category)

    # Search resolved tickets, prioritising same category
    rows = conn.execute(
        """SELECT ticket_id, title, description, resolution, category
           FROM support_tickets
           WHERE status IN ('resolved', 'closed')
             AND ticket_id != ?
           ORDER BY (CASE WHEN category = ? THEN 0 ELSE 1 END),
                    resolved_at DESC
           LIMIT 100""",
        (ticket.ticket_id, category),
    ).fetchall()

    scored: list[tuple[float, dict]] = []
    for row in rows:
        row_d = dict(row)
        row_cat = row_d.get("category", "")
        text_pool = " ".join(filter(None, [
            row_d.get("title", ""),
            row_d.get("description", ""),
            row_d.get("resolution", ""),
        ]))
        row_keywords = _extract_keywords(text_pool)
        if not row_keywords:
            continue

        overlap = len(keywords & row_keywords)
        if overlap == 0:
            continue

        # Boost score if same category
        category_bonus = 0.2 if row_cat == category else 0.0
        score = (overlap / max(len(keywords), 1)) + category_bonus
        score = min(score, 1.0)

        scored.append((score, {
            "ticket_id": row_d["ticket_id"],
            "title": row_d.get("title", ""),
            "resolution": (row_d.get("resolution") or "")[:200],
            "similarity_score": round(score, 2),
        }))

    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:limit]]


def enrich_advisory_with_patterns(
    conn: sqlite3.Connection,
    advisory: Advisory,
    ticket: SupportTicket | None = None,
    keywords: list[str] | None = None,
) -> Advisory:
    """Enrich an advisory with similar past resolution patterns and tickets.

    Modifies advisory.similar_past_cases in-place and returns it.
    """
    category = enum_val(ticket.category) if ticket else None

    # Add resolution pattern IDs
    kw_list = keywords or []
    if ticket and ticket.description and not kw_list:
        kw_list = list(_extract_keywords(ticket.description))[:20]

    patterns = get_similar_patterns(conn, category=category, keywords=kw_list, limit=3)
    pattern_ids = [p.pattern_id for p in patterns if p.confidence >= 0.5]

    # Add similar ticket IDs
    ticket_ids: list[str] = []
    if ticket:
        similar = find_similar_tickets(conn, ticket, limit=3)
        ticket_ids = [s["ticket_id"] for s in similar]

    # Merge, dedup
    existing = set(advisory.similar_past_cases)
    for ref in pattern_ids + ticket_ids:
        if ref not in existing:
            advisory.similar_past_cases.append(ref)
            existing.add(ref)

    return advisory


def _extract_keywords(text: str) -> set[str]:
    """Extract significant keywords (lowercase, > 3 chars) from text."""
    if not text:
        return set()
    return {w.lower() for w in text.split() if len(w) > 3}
