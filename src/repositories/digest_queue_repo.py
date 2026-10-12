"""Repository for the digest notification queue."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def enqueue_digest_item(
    conn: sqlite3.Connection,
    *,
    category: str,
    title: str,
    body: str,
    project_id: str | None = None,
    metadata: dict | None = None,
    commit: bool = True,
) -> int:
    """Insert one item into the digest queue. Returns row id."""
    now = datetime.now(timezone.utc).isoformat()
    metadata = dict(metadata or {})
    # These presentation groups reuse the existing DB enum. No production
    # migration or loss of compatibility with older digest readers is needed.
    if category in {"email_information", "system_maintenance"}:
        metadata["notification_kind"] = category
        if category == "email_information":
            metadata["action_required"] = False
        category = "email_deferred"
    metadata_json = json.dumps(metadata, ensure_ascii=False)
    cursor = conn.execute(
        """INSERT INTO digest_queue (category, title, body, project_id, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (category, title, body, project_id, metadata_json, now),
    )
    if commit:
        conn.commit()
    return cursor.lastrowid


def get_pending_items(
    conn: sqlite3.Connection,
    *,
    categories: tuple[str, ...] | None = None,
) -> list[dict]:
    """Fetch all items where delivered_at IS NULL, ordered by category then created_at."""
    if categories:
        placeholders = ",".join("?" for _ in categories)
        rows = conn.execute(
            f"""SELECT id, category, title, body, project_id, metadata_json, created_at
                FROM digest_queue
                WHERE delivered_at IS NULL AND category IN ({placeholders})
                ORDER BY category, created_at""",
            categories,
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, category, title, body, project_id, metadata_json, created_at
               FROM digest_queue
               WHERE delivered_at IS NULL
               ORDER BY category, created_at""",
        ).fetchall()
    return [dict(r) for r in rows]


def mark_delivered(
    conn: sqlite3.Connection,
    item_ids: list[int],
    batch_id: str,
) -> None:
    """Mark items as delivered in a digest batch."""
    if not item_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" for _ in item_ids)
    conn.execute(
        f"""UPDATE digest_queue
            SET delivered_at = ?, digest_batch_id = ?
            WHERE id IN ({placeholders})""",
        [now, batch_id] + item_ids,
    )
    conn.commit()


def count_pending(conn: sqlite3.Connection) -> int:
    """Return count of undelivered items."""
    row = conn.execute(
        "SELECT COUNT(*) FROM digest_queue WHERE delivered_at IS NULL"
    ).fetchone()
    return row[0] if row else 0
