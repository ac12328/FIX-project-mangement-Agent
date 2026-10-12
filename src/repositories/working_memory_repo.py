"""
Working memory repository — persisted with 7-day staleness flag.
"""

import sqlite3
from datetime import datetime, timezone, timedelta

from config.defaults import WORKING_MEMORY_STALENESS_DAYS


def upsert_memory(
    conn: sqlite3.Connection,
    project_id: str,
    memory_key: str,
    memory_value: str | None,
) -> None:
    """Insert or update a working memory entry."""
    from src.memory.write_policy import warn_if_preference_in_working_memory

    warn_if_preference_in_working_memory(memory_key)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO working_memory (project_id, memory_key, memory_value, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(project_id, memory_key)
           DO UPDATE SET memory_value = excluded.memory_value, updated_at = excluded.updated_at""",
        (project_id, memory_key, memory_value, now),
    )


def get_memory(
    conn: sqlite3.Connection, project_id: str, memory_key: str
) -> dict | None:
    """Get a single memory entry with staleness flag."""
    row = conn.execute(
        "SELECT * FROM working_memory WHERE project_id = ? AND memory_key = ?",
        (project_id, memory_key),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["is_stale"] = _is_stale(d.get("updated_at"))
    return d


def delete_memory(
    conn: sqlite3.Connection, project_id: str, memory_key: str
) -> None:
    """Delete a single working memory entry."""
    conn.execute(
        "DELETE FROM working_memory WHERE project_id = ? AND memory_key = ?",
        (project_id, memory_key),
    )


def get_all_memory(
    conn: sqlite3.Connection, project_id: str
) -> list[dict]:
    """Get all working memory for a project, with staleness flags."""
    rows = conn.execute(
        "SELECT * FROM working_memory WHERE project_id = ? ORDER BY memory_key",
        (project_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_stale"] = _is_stale(d.get("updated_at"))
        result.append(d)
    return result


def _is_stale(updated_at: str | None) -> bool:
    """Check if a memory entry is stale (older than WORKING_MEMORY_STALENESS_DAYS)."""
    if not updated_at:
        return True
    try:
        updated = datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        threshold = datetime.now(timezone.utc) - timedelta(days=WORKING_MEMORY_STALENESS_DAYS)
        return updated < threshold
    except (ValueError, TypeError):
        return True
