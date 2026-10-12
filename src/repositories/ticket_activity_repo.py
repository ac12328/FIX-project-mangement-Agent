"""Build a merged activity timeline for a support ticket."""

import re
import sqlite3

from src.repositories.ticket_repo import get_ticket


_DOT_COLORS = {
    "email_sent": "blue",
    "email_received": "blue",
    "note_added": "amber",
    "status_change": "amber",
    "ai_action": "purple",
    "ticket_created": "purple",
    "system": "gray",
}


def get_ticket_activity(
    conn: sqlite3.Connection, ticket_id: str, limit: int = 50
) -> list[dict]:
    """Build a newest-first activity timeline for a support ticket."""
    entries: list[dict] = []

    rows = conn.execute(
        """SELECT evidence_id, evidence_type, content_summary, created_at
           FROM ticket_evidence
           WHERE ticket_id = ?
           ORDER BY created_at DESC""",
        (ticket_id,),
    ).fetchall()

    for row in rows:
        d = dict(row)
        etype = d["evidence_type"]
        summary = d["content_summary"] or ""

        if etype == "email":
            is_outbound = summary.lower().startswith("outbound")
            event_type = "email_sent" if is_outbound else "email_received"
            actor = "You" if is_outbound else _extract_sender(summary)
            display = _clean_summary(summary)
        elif etype == "note":
            event_type = "note_added"
            actor = "Operator"
            display = summary
        elif etype == "resolution":
            event_type = "status_change"
            actor = "System"
            display = f"Resolution recorded: {summary[:100]}"
        else:
            event_type = "system"
            actor = "System"
            display = summary

        entries.append({
            "timestamp": d["created_at"] or "",
            "actor": actor,
            "event_type": event_type,
            "summary": display,
            "dot_color": _DOT_COLORS.get(event_type, "gray"),
        })

    ticket = get_ticket(conn, ticket_id)
    if ticket:
        created = ticket.created_at
        ts = created.isoformat() if hasattr(created, "isoformat") else str(created) if created else ""
        source = ticket.source_type if isinstance(ticket.source_type, str) else ticket.source_type.value if ticket.source_type else "unknown"
        entries.append({
            "timestamp": ts,
            "actor": "System",
            "event_type": "ticket_created",
            "summary": f"Ticket created from {source} — {ticket.title[:80]}",
            "dot_color": _DOT_COLORS["ticket_created"],
        })

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return entries[:limit]


def _extract_sender(summary: str) -> str:
    match = re.search(r"from\s+(\S+@\S+?)(?::|$)", summary, re.IGNORECASE)
    return match.group(1) if match else "Unknown"


def _clean_summary(summary: str) -> str:
    cleaned = re.sub(
        r"^(?:Follow-up|Outbound|Inbound)\s+email(?:\s+from\s+\S+@\S+)?:\s*",
        "", summary, flags=re.IGNORECASE,
    )
    return cleaned[:200] if cleaned else summary[:200]
