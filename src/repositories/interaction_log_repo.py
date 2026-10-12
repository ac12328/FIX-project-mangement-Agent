"""
Repository for operator_interaction_log — audit trail for card push/response.
"""

import sqlite3
from datetime import datetime, timezone


def insert_card_push(
    conn: sqlite3.Connection,
    *,
    card_type: str,
    card_profile: str | None,
    project_id: str | None,
    telegram_message_id: int | None,
    pushed_at: str | None = None,
) -> int:
    """Insert a record when a card is pushed to the operator."""
    now = pushed_at or datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO operator_interaction_log
           (card_type, card_profile, project_id, pushed_at, telegram_message_id)
           VALUES (?, ?, ?, ?, ?)""",
        (card_type, card_profile, project_id, now, telegram_message_id),
    )
    return cursor.lastrowid


def record_operator_response(
    conn: sqlite3.Connection,
    *,
    telegram_message_id: int,
    operator_response: str,
    responded_at: str | None = None,
) -> int:
    """Update the interaction log when the operator responds to a card."""
    now = responded_at or datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """UPDATE operator_interaction_log
           SET operator_response = ?, responded_at = ?
           WHERE telegram_message_id = ?
             AND operator_response IS NULL""",
        (operator_response, now, telegram_message_id),
    )
    return cursor.rowcount


def get_interaction_log(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    *,
    limit: int = 50,
) -> list[dict]:
    """Fetch recent interaction log entries, optionally filtered by project."""
    if project_id:
        rows = conn.execute(
            """SELECT * FROM operator_interaction_log
               WHERE project_id = ?
               ORDER BY pushed_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM operator_interaction_log
               ORDER BY pushed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
