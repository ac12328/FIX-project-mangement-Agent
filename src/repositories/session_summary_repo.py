"""Repository for session_summaries table (WS-2 cross-session continuity)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


def save_session_summary(
    conn: sqlite3.Connection,
    chat_id: int,
    summary_text: str,
    *,
    projects_discussed: str | None = None,
    decisions_made: str | None = None,
    open_questions: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> str:
    """Insert a session summary. Returns the generated session_id."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = _gen_session_id()
    conn.execute(
        """INSERT INTO session_summaries
           (session_id, chat_id, started_at, ended_at, summary_text,
            projects_discussed, decisions_made, open_questions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            chat_id,
            started_at or now,
            ended_at or now,
            summary_text,
            projects_discussed,
            decisions_made,
            open_questions,
        ),
    )
    return session_id


def get_recent_summaries(
    conn: sqlite3.Connection,
    chat_id: int,
    limit: int = 3,
) -> list[dict]:
    """Return the most recent session summaries for a chat, newest first."""
    rows = conn.execute(
        """SELECT session_id, chat_id, started_at, ended_at, summary_text,
                  projects_discussed, decisions_made, open_questions, created_at
           FROM session_summaries
           WHERE chat_id = ?
           ORDER BY ended_at DESC, rowid DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def search_sessions(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """FTS5 search over session summaries. Returns top matches with snippets."""
    rows = conn.execute(
        """
        SELECT s.session_id, s.started_at, s.ended_at,
               snippet(session_summaries_fts, 0, '>>>', '<<<', '...', 30) AS snippet,
               s.projects_discussed, s.decisions_made
        FROM session_summaries_fts f
        JOIN session_summaries s ON f.rowid = s.rowid
        WHERE session_summaries_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _gen_session_id() -> str:
    return "sess_" + os.urandom(6).hex()
