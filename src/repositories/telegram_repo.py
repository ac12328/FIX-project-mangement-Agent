"""
Telegram persistence repository.
Stores raw Telegram messages plus message-to-object linkage.
"""

import json
import sqlite3
from datetime import datetime, timezone


def insert_telegram_message(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
    direction: str,
    message_kind: str = "text",
    text: str | None = None,
    callback_data: str | None = None,
    reply_to_telegram_message_id: int | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    operator_id: str = "default_operator",
    metadata_json: str | None = None,
) -> int:
    """Insert or update one Telegram message row. Returns the row ID."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO telegram_messages
           (chat_id, telegram_message_id, direction, message_kind, text,
            callback_data, reply_to_telegram_message_id, project_id, session_id,
            operator_id, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(chat_id, telegram_message_id)
           DO UPDATE SET
               direction = excluded.direction,
               message_kind = excluded.message_kind,
               text = COALESCE(excluded.text, telegram_messages.text),
               callback_data = COALESCE(excluded.callback_data, telegram_messages.callback_data),
               reply_to_telegram_message_id = COALESCE(
                   excluded.reply_to_telegram_message_id,
                   telegram_messages.reply_to_telegram_message_id
               ),
               project_id = COALESCE(excluded.project_id, telegram_messages.project_id),
               session_id = COALESCE(excluded.session_id, telegram_messages.session_id),
               operator_id = COALESCE(excluded.operator_id, telegram_messages.operator_id),
               metadata_json = COALESCE(excluded.metadata_json, telegram_messages.metadata_json)
           RETURNING id""",
        (
            chat_id,
            telegram_message_id,
            direction,
            message_kind,
            text,
            callback_data,
            reply_to_telegram_message_id,
            project_id,
            session_id,
            operator_id,
            metadata_json,
            now,
        ),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def insert_message_object_link(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
    object_type: str,
    object_ref: str,
    relation_type: str = "primary",
    project_id: str | None = None,
) -> int:
    """Insert one message-object mapping row. Returns the row ID."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO message_object_map
           (chat_id, telegram_message_id, object_type, object_ref, relation_type, project_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(chat_id, telegram_message_id, object_type, object_ref, relation_type)
           DO UPDATE SET project_id = COALESCE(excluded.project_id, message_object_map.project_id)
           RETURNING id""",
        (chat_id, telegram_message_id, object_type, object_ref, relation_type, project_id, now),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def get_message_links(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
) -> list[dict]:
    """Return all object mappings for one Telegram message."""
    rows = conn.execute(
        """SELECT * FROM message_object_map
           WHERE chat_id = ? AND telegram_message_id = ?
           ORDER BY id ASC""",
        (chat_id, telegram_message_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_message_context(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    telegram_message_id: int,
) -> dict | None:
    """Return one Telegram message row with all linked objects."""
    row = conn.execute(
        """SELECT * FROM telegram_messages
           WHERE chat_id = ? AND telegram_message_id = ?""",
        (chat_id, telegram_message_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["links"] = get_message_links(
        conn,
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
    )
    if result.get("metadata_json"):
        try:
            result["metadata"] = json.loads(result["metadata_json"])
        except json.JSONDecodeError:
            result["metadata"] = None
    return result


def list_project_telegram_messages(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    object_type: str | None = None,
    object_ref: str | None = None,
) -> list[dict]:
    """List Telegram messages linked to a project, newest first."""
    params: list = [project_id]
    exists_filters = ["map.chat_id = m.chat_id", "map.telegram_message_id = m.telegram_message_id", "map.project_id = ?"]
    if object_type:
        exists_filters.append("map.object_type = ?")
        params.append(object_type)
    if object_ref:
        exists_filters.append("map.object_ref = ?")
        params.append(object_ref)

    direct_filters = ["m.project_id = ?"]
    direct_params: list = [project_id]
    if object_type or object_ref:
        direct_filters = ["1 = 0"]
        direct_params = []

    query = f"""
        SELECT DISTINCT m.*
        FROM telegram_messages m
        WHERE ({' AND '.join(direct_filters)})
           OR EXISTS (
                SELECT 1
                FROM message_object_map map
                WHERE {' AND '.join(exists_filters)}
           )
        ORDER BY m.created_at DESC
        LIMIT ?
    """
    rows = conn.execute(query, tuple(direct_params + params + [limit])).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["links"] = get_message_links(
            conn,
            chat_id=item["chat_id"],
            telegram_message_id=item["telegram_message_id"],
        )
        if item.get("metadata_json"):
            try:
                item["metadata"] = json.loads(item["metadata_json"])
            except json.JSONDecodeError:
                item["metadata"] = None
        results.append(item)
    return results
