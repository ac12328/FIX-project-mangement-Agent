"""
Operator Preference Store (Plan Step 3.13d, Blueprint Section 37).
Project-level + counterparty-level preferences: tone, language, push_style.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class OperatorPreference:
    id: int | None = None
    scope: str = "project"  # project | counterparty
    project_id: str | None = None
    counterparty_id: str | None = None
    tone: str = "semi-formal"
    language: str = "bilingual"
    push_style: str = "reactive"
    custom_notes: str | None = None
    operator_id: str = "default_operator"
    updated_at: str | None = None


def get_preference(
    conn: sqlite3.Connection,
    scope: str,
    *,
    project_id: str | None = None,
    counterparty_id: str | None = None,
) -> OperatorPreference | None:
    """Get operator preference by scope and ID."""
    if scope == "project" and project_id:
        row = conn.execute(
            "SELECT * FROM operator_preferences WHERE scope = 'project' AND project_id = ?",
            (project_id,),
        ).fetchone()
    elif scope == "counterparty" and counterparty_id:
        row = conn.execute(
            "SELECT * FROM operator_preferences WHERE scope = 'counterparty' AND counterparty_id = ?",
            (counterparty_id,),
        ).fetchone()
    else:
        return None

    if row is None:
        return None
    d = dict(row)
    return OperatorPreference(**d)


def upsert_preference(conn: sqlite3.Connection, pref: OperatorPreference) -> int:
    """
    Insert or update an operator preference.
    Returns the row ID.
    """
    now = datetime.now(timezone.utc).isoformat()

    existing = get_preference(
        conn,
        pref.scope,
        project_id=pref.project_id,
        counterparty_id=pref.counterparty_id,
    )

    if existing and existing.id is not None:
        conn.execute(
            """UPDATE operator_preferences
               SET tone = ?, language = ?, push_style = ?,
                   custom_notes = ?, operator_id = ?, updated_at = ?
               WHERE id = ?""",
            (pref.tone, pref.language, pref.push_style,
             pref.custom_notes, pref.operator_id, now, existing.id),
        )
        conn.commit()
        return existing.id

    cursor = conn.execute(
        """INSERT INTO operator_preferences
           (scope, project_id, counterparty_id, tone, language,
            push_style, custom_notes, operator_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pref.scope, pref.project_id, pref.counterparty_id,
         pref.tone, pref.language, pref.push_style,
         pref.custom_notes, pref.operator_id, now),
    )
    conn.commit()
    return cursor.lastrowid


def get_effective_preference(
    conn: sqlite3.Connection,
    project_id: str,
    counterparty_id: str | None = None,
) -> OperatorPreference:
    """
    Get the effective preference for a project+counterparty pair.
    Priority: project-specific > counterparty > defaults.
    """
    # Try project-specific first
    pref = get_preference(conn, "project", project_id=project_id)
    if pref:
        return pref

    # Try counterparty
    if counterparty_id:
        pref = get_preference(conn, "counterparty", counterparty_id=counterparty_id)
        if pref:
            return pref

    # Return defaults
    return OperatorPreference()


def list_preferences(conn: sqlite3.Connection) -> list[OperatorPreference]:
    """List all operator preferences."""
    rows = conn.execute(
        "SELECT * FROM operator_preferences ORDER BY scope, project_id, counterparty_id"
    ).fetchall()
    return [OperatorPreference(**dict(r)) for r in rows]


def delete_preference(
    conn: sqlite3.Connection,
    scope: str,
    *,
    project_id: str | None = None,
    counterparty_id: str | None = None,
) -> bool:
    """Delete an operator preference. Returns True if a row was deleted."""
    if scope == "project" and project_id:
        cursor = conn.execute(
            "DELETE FROM operator_preferences WHERE scope = 'project' AND project_id = ?",
            (project_id,),
        )
    elif scope == "counterparty" and counterparty_id:
        cursor = conn.execute(
            "DELETE FROM operator_preferences WHERE scope = 'counterparty' AND counterparty_id = ?",
            (counterparty_id,),
        )
    else:
        return False
    conn.commit()
    return cursor.rowcount > 0
