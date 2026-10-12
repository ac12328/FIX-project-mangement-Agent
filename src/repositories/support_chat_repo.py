"""Repository for per-user support agent chat messages."""
from __future__ import annotations

import sqlite3
from uuid import uuid4

from config.settings import CONFIG


def _owner(owner_user_id: str | None) -> str:
    return owner_user_id or CONFIG.operator_id or "default_operator"


def get_chat_messages(
    conn: sqlite3.Connection,
    ticket_id: str,
    limit: int = 20,
    *,
    owner_user_id: str | None = None,
) -> list[dict]:
    """Return one user's latest ticket messages, oldest first."""
    rows = conn.execute(
        "SELECT id, turn_id, role, content, created_at FROM support_chat_messages "
        "WHERE ticket_id = ? AND owner_user_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (ticket_id, _owner(owner_user_id), limit),
    ).fetchall()
    messages = []
    for row in reversed(rows):
        item = dict(row)
        message_id = str(item.pop("id"))
        messages.append(
            {
                "message_id": message_id,
                "turn_id": item.pop("turn_id") or f"legacy-support-{message_id}",
                **item,
                "persisted": True,
            }
        )
    return messages


def save_chat_message(
    conn: sqlite3.Connection,
    ticket_id: str,
    role: str,
    content: str,
    *,
    owner_user_id: str | None = None,
    turn_id: str | None = None,
) -> int:
    """Insert a single owned chat message and return its stable integer id."""
    cursor = conn.execute(
        "INSERT INTO support_chat_messages "
        "(ticket_id, owner_user_id, turn_id, role, content) VALUES (?, ?, ?, ?, ?)",
        (
            ticket_id,
            _owner(owner_user_id),
            turn_id or uuid4().hex,
            role,
            content,
        ),
    )
    return int(cursor.lastrowid)
