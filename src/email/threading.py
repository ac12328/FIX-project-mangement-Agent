"""
Email Reply Threading (Blueprint Section 27).
Tracks In-Reply-To and References headers to maintain email threads.
"""

import sqlite3
from datetime import datetime, timezone


def record_thread(
    conn: sqlite3.Connection,
    email_id: str,
    project_id: str | None,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    """Record email threading headers for future lookups."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO processed_emails
           (email_id, project_id, processed_at, received_at, result, thread_id, in_reply_to, references_chain)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (email_id, project_id, now, now, "threaded", thread_id, in_reply_to, references),
    )
    conn.commit()


def find_thread_project(
    conn: sqlite3.Connection,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str | None:
    """
    Find which project an email thread belongs to,
    using In-Reply-To or References headers.

    Returns project_id if found, None otherwise.
    """
    # Check In-Reply-To first (most specific)
    if in_reply_to:
        row = conn.execute(
            "SELECT project_id FROM processed_emails WHERE email_id = ?",
            (in_reply_to,),
        ).fetchone()
        if row and dict(row)["project_id"]:
            return dict(row)["project_id"]

    # Check References (may contain multiple Message-IDs)
    if references:
        # References header contains space-separated Message-IDs.
        # Do NOT strip angle brackets: stored email_id values keep their
        # brackets (from msg.get("Message-ID") verbatim), so stripping them
        # here makes the lookup never match.
        ref_ids = references.strip().split()
        for ref_id in reversed(ref_ids):  # Most recent first
            row = conn.execute(
                "SELECT project_id FROM processed_emails WHERE email_id = ?",
                (ref_id,),
            ).fetchone()
            if row and dict(row)["project_id"]:
                return dict(row)["project_id"]

    return None


def get_thread_history(
    conn: sqlite3.Connection, project_id: str, limit: int = 20
) -> list[dict]:
    """Get recent email thread entries for a project."""
    rows = conn.execute(
        """SELECT email_id, project_id, processed_at, result
           FROM processed_emails
           WHERE project_id = ?
           ORDER BY processed_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
