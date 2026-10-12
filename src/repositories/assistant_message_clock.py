"""Monotonic UTC timestamps for ordered messages within a serialized thread write."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def next_message_timestamp(conn: sqlite3.Connection, thread_id: str) -> str:
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT created_at FROM assistant_messages WHERE thread_id=? "
        "ORDER BY datetime(created_at) DESC, created_at DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if row:
        try:
            previous = datetime.fromisoformat(row[0])
            previous = (
                previous.replace(tzinfo=timezone.utc)
                if previous.tzinfo is None
                else previous.astimezone(timezone.utc)
            )
            now = max(now, previous + timedelta(microseconds=1))
        except (TypeError, ValueError):
            pass
    return now.isoformat(sep=" ", timespec="microseconds")
