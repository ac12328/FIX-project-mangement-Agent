"""Persistence for the wiki compiler queue."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_id() -> str:
    return "wcq_" + uuid.uuid4().hex[:20]


def enqueue_nugget(
    conn: sqlite3.Connection,
    *,
    entry_id: str,
    domain: str | None,
    topic: str,
    state: str,
    skip_reason: str | None = None,
) -> str | None:
    """Insert a queue row if this nugget is not already queued. Returns queue_id."""
    existing = conn.execute(
        "SELECT queue_id FROM wiki_compiler_queue WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    if existing:
        return existing[0]
    queue_id = _queue_id()
    conn.execute(
        """INSERT INTO wiki_compiler_queue
           (queue_id, entry_id, domain, topic, state, skip_reason, enqueued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (queue_id, entry_id, domain, topic, state, skip_reason, _now()),
    )
    return queue_id


def list_pending(conn: sqlite3.Connection, *, limit: int = 12) -> list[dict]:
    """Pending compile rows, preferring one topic so a batch stays coherent."""
    topic_row = conn.execute(
        """SELECT topic FROM wiki_compiler_queue
           WHERE state = 'pending'
           ORDER BY enqueued_at ASC LIMIT 1"""
    ).fetchone()
    if not topic_row:
        return []
    rows = conn.execute(
        """SELECT q.queue_id, q.entry_id, q.domain, q.topic,
                  e.title, e.content, e.knowledge_type, e.confidence,
                  e.visibility, e.source_emails
           FROM wiki_compiler_queue q
           JOIN email_knowledge_log e ON e.entry_id = q.entry_id
           WHERE q.state = 'pending' AND q.topic = ?
           ORDER BY q.enqueued_at ASC
           LIMIT ?""",
        (topic_row[0], limit),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_compiling(conn: sqlite3.Connection, queue_ids: list[str]) -> None:
    if not queue_ids:
        return
    placeholders = ",".join("?" * len(queue_ids))
    conn.execute(
        f"UPDATE wiki_compiler_queue SET state = 'compiling' WHERE queue_id IN ({placeholders})",
        queue_ids,
    )


def mark_batch_result(
    conn: sqlite3.Connection,
    queue_ids: list[str],
    *,
    state: str,
    compiled_path: str | None = None,
    error: str | None = None,
) -> None:
    if not queue_ids:
        return
    placeholders = ",".join("?" * len(queue_ids))
    conn.execute(
        f"""UPDATE wiki_compiler_queue
            SET state = ?, compiled_path = ?, error = ?, compiled_at = ?
            WHERE queue_id IN ({placeholders})""",
        (state, compiled_path, (error or "")[:2000] or None, _now(), *queue_ids),
    )


def set_nugget_wiki_path(
    conn: sqlite3.Connection, entry_ids: list[str], wiki_path: str | None
) -> None:
    if not entry_ids or not wiki_path:
        return
    placeholders = ",".join("?" * len(entry_ids))
    conn.execute(
        f"UPDATE email_knowledge_log SET wiki_path = ? WHERE entry_id IN ({placeholders})",
        (wiki_path, *entry_ids),
    )
