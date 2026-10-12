"""Repository functions for inbound_replies table (Blueprint Section 27.3)."""

import json as _json
import sqlite3
import uuid
from datetime import datetime, timezone

from src.models.inbound_reply import InboundReply


def insert_inbound_reply(conn: sqlite3.Connection, reply: InboundReply) -> str:
    """Insert an InboundReply record. Returns reply_id."""
    requested_docs_json = (
        _json.dumps(reply.requested_docs) if reply.requested_docs else None
    )
    conn.execute(
        """INSERT INTO inbound_replies
           (reply_id, thread_id, project_id, from_contact_id, received_at,
            subject, body_text, original_draft_id, summary, suggested_action,
            confidence, requested_docs_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reply.reply_id,
            reply.thread_id,
            reply.project_id,
            reply.from_contact_id,
            reply.received_at,
            reply.subject,
            reply.body_text,
            reply.original_draft_id,
            reply.summary,
            reply.suggested_action,
            reply.confidence,
            requested_docs_json,
        ),
    )
    return reply.reply_id


def get_inbound_replies(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int = 10,
) -> list[dict]:
    """Get recent inbound replies for a project, newest first."""
    rows = conn.execute(
        """SELECT reply_id, thread_id, project_id, from_contact_id, received_at,
                  subject, body_text, original_draft_id, summary, suggested_action,
                  confidence, requested_docs_json
           FROM inbound_replies
           WHERE project_id = ?
           ORDER BY received_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def make_reply_id() -> str:
    """Generate a unique reply ID."""
    return f"ir_{uuid.uuid4().hex[:16]}"


def get_inbound_reply_by_id(conn: sqlite3.Connection, reply_id: str) -> dict | None:
    """Return a single inbound reply by reply_id, or None if not found."""
    row = conn.execute(
        "SELECT reply_id, thread_id, project_id, from_contact_id, received_at, "
        "subject, body_text, original_draft_id, summary, suggested_action, confidence, "
        "requested_docs_json "
        "FROM inbound_replies WHERE reply_id = ?",
        (reply_id,),
    ).fetchone()
    return dict(row) if row is not None else None
