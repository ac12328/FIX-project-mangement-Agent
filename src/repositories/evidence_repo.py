"""
Evidence store repository.
"""

import sqlite3
import uuid
from datetime import datetime, timezone

from src.models.evidence import Evidence


def insert_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    evidence_type: str,
    content_summary: str,
    evidence_at: str,
    content_text: str | None = None,
    source_ref: str | None = None,
    evidence_id: str | None = None,
) -> str:
    """Insert evidence into the store. Returns the evidence_id."""
    if evidence_id is None:
        evidence_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO evidence_store
           (evidence_id, project_id, evidence_type, content_summary,
            content_text, source_ref, evidence_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (evidence_id, project_id, evidence_type, content_summary,
         content_text, source_ref, evidence_at, now),
    )
    return evidence_id


def get_recent_evidence(
    conn: sqlite3.Connection, project_id: str, limit: int = 20
) -> list[Evidence]:
    """Get recent evidence for a project, newest first."""
    rows = conn.execute(
        """SELECT * FROM evidence_store
           WHERE project_id = ?
           ORDER BY evidence_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [Evidence(**dict(r)) for r in rows]


def get_evidence_by_id(
    conn: sqlite3.Connection, evidence_id: str
) -> Evidence | None:
    """Get a single evidence item by ID."""
    row = conn.execute(
        "SELECT * FROM evidence_store WHERE evidence_id = ?", (evidence_id,)
    ).fetchone()
    if row is None:
        return None
    return Evidence(**dict(row))


def evidence_exists(conn: sqlite3.Connection, evidence_id: str) -> bool:
    """Check if evidence exists by ID."""
    row = conn.execute(
        "SELECT 1 FROM evidence_store WHERE evidence_id = ?", (evidence_id,)
    ).fetchone()
    return row is not None
