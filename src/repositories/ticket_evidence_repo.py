"""
Ticket evidence repository.
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum

from src.models.enums import TicketEvidenceType
from src.models.support_ticket import TicketEvidence


def _as_evidence_type_str(evidence_type: TicketEvidenceType | str) -> str:
    """Normalize enum or string to DB-stored evidence_type value."""
    if isinstance(evidence_type, Enum):
        return str(evidence_type.value)
    return str(evidence_type)


def insert_ticket_evidence(
    conn: sqlite3.Connection,
    ticket_id: str,
    evidence_type: TicketEvidenceType | str,
    content_summary: str,
    *,
    evidence_id: str | None = None,
    content_text: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Insert evidence for a ticket and refresh parent ticket timestamps.

    Side-effects on the parent ``support_tickets`` row:
      - ``updated_at`` bumped so dashboard lists sort by latest activity
      - ``ai_summary_json`` / ``reply_draft_text`` cleared so next summary recomputes
    Returns the evidence_id.
    """
    if evidence_id is None:
        evidence_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    evidence_type_str = _as_evidence_type_str(evidence_type)
    conn.execute(
        """INSERT INTO ticket_evidence
           (evidence_id, ticket_id, evidence_type, content_summary,
            content_text, source_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (evidence_id, ticket_id, evidence_type_str, content_summary,
         content_text, source_ref, now),
    )
    conn.execute(
        """UPDATE support_tickets
           SET updated_at = ?,
               ai_summary_json = NULL,
               reply_draft_text = NULL
           WHERE ticket_id = ?""",
        (now, ticket_id),
    )
    return evidence_id


def get_evidence_for_ticket(
    conn: sqlite3.Connection, ticket_id: str
) -> list[TicketEvidence]:
    """Get all evidence for a ticket, newest first."""
    rows = conn.execute(
        """SELECT * FROM ticket_evidence
           WHERE ticket_id = ?
           ORDER BY created_at DESC""",
        (ticket_id,),
    ).fetchall()
    return [TicketEvidence(**dict(r)) for r in rows]


def search_ticket_evidence(
    conn: sqlite3.Connection,
    query: str,
    *,
    ticket_id: str | None = None,
    evidence_type: TicketEvidenceType | str | None = None,
    limit: int = 8,
) -> list[TicketEvidence]:
    """Search ticket evidence by keyword in summary or body text."""
    q = f"%{query.strip()}%"
    sql = [
        "SELECT * FROM ticket_evidence",
        "WHERE (content_summary LIKE ? OR IFNULL(content_text, '') LIKE ?)",
    ]
    params: list = [q, q]
    if ticket_id:
        sql.append("AND ticket_id = ?")
        params.append(ticket_id)
    if evidence_type:
        sql.append("AND evidence_type = ?")
        params.append(_as_evidence_type_str(evidence_type))
    sql.append("ORDER BY created_at DESC LIMIT ?")
    params.append(max(1, min(limit, 25)))
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [TicketEvidence(**dict(r)) for r in rows]


def get_evidence_by_id(
    conn: sqlite3.Connection, evidence_id: str
) -> TicketEvidence | None:
    """Get a single evidence item by ID."""
    row = conn.execute(
        "SELECT * FROM ticket_evidence WHERE evidence_id = ?", (evidence_id,)
    ).fetchone()
    if row is None:
        return None
    return TicketEvidence(**dict(row))
