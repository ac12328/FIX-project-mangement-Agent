"""
Event repository — project events (inbound/outbound activity spine).
"""

import sqlite3
from datetime import datetime, timezone


def insert_event(
    conn: sqlite3.Connection,
    project_id: str,
    event_type: str,
    event_at: str,
    event_direction: str | None = None,
    source_agent: str | None = None,
    source_surface: str | None = None,
    source_kind: str | None = None,
    email_id: str | None = None,
    subject: str | None = None,
    summary: str | None = None,
    participants_json: str | None = None,
    metadata_json: str | None = None,
) -> int:
    """Insert a project event. Returns the row ID."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO project_events
           (project_id, event_type, event_direction, event_at,
            source_agent, source_surface, source_kind, email_id,
            subject, summary, participants_json, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, event_type, event_direction, event_at,
         source_agent, source_surface, source_kind, email_id,
         subject, summary, participants_json, metadata_json, now),
    )
    return cursor.lastrowid


def get_recent_events(
    conn: sqlite3.Connection, project_id: str, limit: int = 50
) -> list[dict]:
    """Get recent events for a project, newest first."""
    rows = conn.execute(
        """SELECT * FROM project_events
           WHERE project_id = ?
           ORDER BY event_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
