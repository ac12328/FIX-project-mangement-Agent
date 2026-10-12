"""
Episode repository — narrative timeline entries.
"""

import sqlite3
from datetime import datetime, timezone


def insert_episode(
    conn: sqlite3.Connection,
    project_id: str,
    ts: str,
    date: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    source_email: str | None = None,
    confidence: float = 1.0,
) -> int:
    """Insert an episode. Returns the row ID."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO episodes
           (project_id, ts, date, title, summary,
            source_email, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, ts, date, title, summary, source_email, confidence, now),
    )
    return cursor.lastrowid


def get_recent_episodes(
    conn: sqlite3.Connection, project_id: str, limit: int = 50
) -> list[dict]:
    """Get recent episodes for a project, newest first."""
    rows = conn.execute(
        """SELECT * FROM episodes
           WHERE project_id = ?
           ORDER BY ts DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
