"""
Response Pattern Memory service (Blueprint Section 21).
Learns and updates contact response preferences from email interactions.
"""

import sqlite3
import json
from datetime import datetime, timezone


def get_response_pattern(
    conn: sqlite3.Connection,
    contact_id: str,
    project_id: str | None = None,
    *, memory_request=None, for_context: bool = False,
) -> dict | None:
    """Get response pattern for a contact, optionally scoped to project."""
    from src.memory.projection_authority import response_projection_is_current
    authorized = set()
    if for_context and memory_request is not None:
        from src.memory.retriever import MemoryRetriever
        authorized = MemoryRetriever().authorized_versions(conn, memory_request)
    scopes = [project_id, None] if project_id else [None]
    for scope in scopes:
        rows = conn.execute(
            """SELECT * FROM response_pattern_memory
               WHERE contact_id=? AND project_id IS ? ORDER BY updated_at DESC,id DESC""",
            (contact_id, scope),
        ).fetchall()
        for row in rows:
            if not response_projection_is_current(conn, row):
                continue
            if for_context:
                owner = conn.execute("SELECT fact_id,fact_version,snapshot_json FROM memory_projection_ownership WHERE target='response_pattern' AND legacy_key=?",
                    (f"{row['contact_id']}|{row['project_id'] or '*'}",)).fetchone()
                if owner and json.loads(owner[2] or "{}").get("row_id") == row["id"] and (owner[0], owner[1]) not in authorized:
                    continue
            return dict(row)
    return None


def upsert_response_pattern(
    conn: sqlite3.Connection,
    contact_id: str,
    project_id: str | None = None,
    *,
    preferred_greeting: str | None = None,
    tone: str | None = None,
    response_length_preference: str | None = None,
    preferred_language: str | None = None,
    avoid_phrases: list[str] | None = None,
    positive_signals: list[str] | None = None,
    source: str = "operator",
    confidence: float = 0.5,
) -> None:
    """Insert or update a response pattern for a contact."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "response_pattern_memory")
    now = datetime.now(timezone.utc).isoformat()

    existing = None
    if project_id:
        existing = conn.execute(
            "SELECT id FROM response_pattern_memory WHERE contact_id = ? AND project_id = ?",
            (contact_id, project_id),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM response_pattern_memory WHERE contact_id = ? AND project_id IS NULL",
            (contact_id,),
        ).fetchone()

    avoid_json = json.dumps(avoid_phrases) if avoid_phrases is not None else None
    positive_json = json.dumps(positive_signals) if positive_signals is not None else None

    if existing:
        # Update existing record
        updates = {"updated_at": now, "source": source, "confidence": confidence}
        if preferred_greeting is not None:
            updates["preferred_greeting"] = preferred_greeting
        if tone is not None:
            updates["tone"] = tone
        if response_length_preference is not None:
            updates["response_length_preference"] = response_length_preference
        if preferred_language is not None:
            updates["preferred_language"] = preferred_language
        if avoid_json is not None:
            updates["avoid_phrases"] = avoid_json
        if positive_json is not None:
            updates["positive_signals"] = positive_json

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [dict(existing)["id"]]
        conn.execute(f"UPDATE response_pattern_memory SET {set_clause} WHERE id = ?", values)
    else:
        conn.execute(
            """INSERT INTO response_pattern_memory
               (contact_id, project_id, preferred_greeting, tone,
                response_length_preference, preferred_language,
                avoid_phrases, positive_signals, source, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, project_id, preferred_greeting,
             tone or "semi-formal",
             response_length_preference or "medium",
             preferred_language or "en",
             avoid_json or "[]", positive_json or "[]",
             source, confidence, now),
        )
    conn.commit()


def add_avoid_phrase(
    conn: sqlite3.Connection, contact_id: str, phrase: str,
    project_id: str | None = None,
) -> None:
    """Add a phrase to the avoid list for a contact."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "response_pattern_memory")
    pattern = get_response_pattern(conn, contact_id, project_id)
    if pattern is None:
        upsert_response_pattern(conn, contact_id, project_id, avoid_phrases=[phrase])
        return

    current = _parse_list(pattern.get("avoid_phrases"))
    if phrase not in current:
        current.append(phrase)
        row_id = pattern["id"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE response_pattern_memory SET avoid_phrases = ?, updated_at = ? WHERE id = ?",
            (json.dumps(current), now, row_id),
        )
        conn.commit()


def add_positive_signal(
    conn: sqlite3.Connection, contact_id: str, signal: str,
    project_id: str | None = None,
) -> None:
    """Add a positive signal for a contact."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "response_pattern_memory")
    pattern = get_response_pattern(conn, contact_id, project_id)
    if pattern is None:
        upsert_response_pattern(conn, contact_id, project_id, positive_signals=[signal])
        return

    current = _parse_list(pattern.get("positive_signals"))
    if signal not in current:
        current.append(signal)
        row_id = pattern["id"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE response_pattern_memory SET positive_signals = ?, updated_at = ? WHERE id = ?",
            (json.dumps(current), now, row_id),
        )
        conn.commit()


def _parse_list(raw: str | None) -> list[str]:
    """Parse a JSON list string."""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [s.strip() for s in raw.split(",") if s.strip()]
