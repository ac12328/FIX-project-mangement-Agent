"""Agent notes repository — char-bounded, LLM-curated personal memory for operator preferences."""

from __future__ import annotations

import sqlite3

from config.defaults import AGENT_NOTES_CHAR_LIMIT as CHAR_LIMIT


def list_notes(conn: sqlite3.Connection, *, note_type: str | None = None) -> list[dict]:
    """Return agent notes ordered by updated_at desc, optionally filtered by type."""
    if note_type:
        rows = conn.execute(
            "SELECT note_key, note_value, note_type, updated_at FROM agent_notes "
            "WHERE note_type = ? ORDER BY updated_at DESC",
            (note_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT note_key, note_value, note_type, updated_at FROM agent_notes ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_note(conn: sqlite3.Connection, key: str, value: str, *, note_type: str = "knowledge") -> str:
    """Add a new note. Returns error string if key exists or char limit exceeded."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "agent_notes")
    existing = conn.execute(
        "SELECT id FROM agent_notes WHERE note_key = ?", (key,)
    ).fetchone()
    if existing:
        return f"Key '{key}' already exists. Use replace to update it."

    current_total = total_chars(conn)
    if current_total + len(value) > CHAR_LIMIT:
        return (
            f"Over limit ({current_total + len(value)}/{CHAR_LIMIT}). "
            f"Remove or replace existing notes to make room."
        )

    conn.execute(
        "INSERT INTO agent_notes (note_key, note_value, note_type) VALUES (?, ?, ?)",
        (key, value, note_type),
    )
    conn.commit()
    new_total = total_chars(conn)
    return f"Added note '{key}' ({len(value)} chars). Total: {new_total}/{CHAR_LIMIT}"


def replace_note(conn: sqlite3.Connection, key: str, value: str, *, note_type: str | None = None) -> str:
    """Replace an existing note's value. Checks net char change against limit."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "agent_notes")
    row = conn.execute(
        "SELECT note_value, note_type FROM agent_notes WHERE note_key = ?", (key,)
    ).fetchone()
    if not row:
        return f"Note '{key}' not found. Use add to create it."

    old_len = len(row["note_value"] if isinstance(row, dict) else row[0])
    current_total = total_chars(conn)
    new_total = current_total - old_len + len(value)

    if new_total > CHAR_LIMIT:
        return (
            f"Over limit ({new_total}/{CHAR_LIMIT}). "
            f"Remove other notes to make room."
        )

    existing_type = row["note_type"] if isinstance(row, dict) else row[1]
    update_sql = "UPDATE agent_notes SET note_value = ?, updated_at = datetime('now')"
    params: list = [value]
    if note_type and note_type != existing_type:
        update_sql += ", note_type = ?"
        params.append(note_type)
    update_sql += " WHERE note_key = ?"
    params.append(key)

    conn.execute(update_sql, params)
    conn.commit()
    new_total = total_chars(conn)
    return f"Replaced note '{key}' ({len(value)} chars). Total: {new_total}/{CHAR_LIMIT}"


def remove_note(conn: sqlite3.Connection, key: str) -> str:
    """Delete a note to free space."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "agent_notes")
    cursor = conn.execute("DELETE FROM agent_notes WHERE note_key = ?", (key,))
    conn.commit()
    if cursor.rowcount == 0:
        return f"Note '{key}' not found."
    remaining = total_chars(conn)
    return f"Removed note '{key}'. Total: {remaining}/{CHAR_LIMIT}"


def total_chars(conn: sqlite3.Connection) -> int:
    """Return total character count across all note values."""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(note_value)), 0) AS total FROM agent_notes"
    ).fetchone()
    return row["total"] if isinstance(row, dict) else row[0]


