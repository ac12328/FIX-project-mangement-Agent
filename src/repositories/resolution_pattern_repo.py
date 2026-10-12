"""
Resolution pattern repository — stores learned resolution blueprints.
"""

import json
import sqlite3
from datetime import datetime, timezone

from src.models.resolution_pattern import ResolutionPattern


def insert_pattern(
    conn: sqlite3.Connection,
    pattern_id: str,
    *,
    category: str | None = None,
    subcategory: str | None = None,
    trigger_keywords: list[str] | None = None,
    diagnosis_steps: list[str] | None = None,
    resolution_action: str | None = None,
    escalation_target: str | None = None,
    typical_draft_template: str | None = None,
    source: str = "operator",
    confidence: float = 0.5,
) -> ResolutionPattern:
    """Insert a new resolution pattern. Returns the created pattern."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO resolution_patterns
           (pattern_id, category, subcategory, trigger_keywords, diagnosis_steps,
            resolution_action, escalation_target, typical_draft_template, source,
            times_used, times_confirmed, confidence, last_used_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pattern_id, category, subcategory,
         json.dumps(trigger_keywords or []),
         json.dumps(diagnosis_steps or []),
         resolution_action, escalation_target, typical_draft_template,
         source, 1, 0, confidence, now, now, now),
    )
    return get_pattern(conn, pattern_id)  # type: ignore[return-value]


def get_pattern(
    conn: sqlite3.Connection, pattern_id: str
) -> ResolutionPattern | None:
    """Get a pattern by ID."""
    row = conn.execute(
        "SELECT * FROM resolution_patterns WHERE pattern_id = ?", (pattern_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_pattern(row)


def get_patterns_by_category(
    conn: sqlite3.Connection, category: str, limit: int = 20
) -> list[ResolutionPattern]:
    """Get patterns filtered by category, highest confidence first."""
    rows = conn.execute(
        """SELECT * FROM resolution_patterns
           WHERE category = ?
           ORDER BY confidence DESC, times_confirmed DESC
           LIMIT ?""",
        (category, limit),
    ).fetchall()
    return [_row_to_pattern(r) for r in rows]


def search_patterns(
    conn: sqlite3.Connection, keywords: list[str], limit: int = 10
) -> list[ResolutionPattern]:
    """Search patterns whose trigger_keywords overlap with given keywords.

    Returns patterns sorted by overlap count (best match first).
    """
    rows = conn.execute(
        "SELECT * FROM resolution_patterns ORDER BY confidence DESC LIMIT 200"
    ).fetchall()
    scored: list[tuple[int, ResolutionPattern]] = []
    kw_lower = {k.lower() for k in keywords}
    for r in rows:
        pattern = _row_to_pattern(r)
        stored_kw = {k.lower() for k in pattern.trigger_keywords}
        overlap = len(kw_lower & stored_kw)
        if overlap > 0:
            scored.append((overlap, pattern))
    scored.sort(key=lambda x: (-x[0], -x[1].confidence))
    return [p for _, p in scored[:limit]]


def increment_usage(conn: sqlite3.Connection, pattern_id: str) -> None:
    """Increment times_used and update last_used_at."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE resolution_patterns
           SET times_used = times_used + 1, last_used_at = ?, updated_at = ?
           WHERE pattern_id = ?""",
        (now, now, pattern_id),
    )


def confirm_pattern(conn: sqlite3.Connection, pattern_id: str) -> None:
    """Increment times_confirmed and boost confidence (cap at 1.0)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE resolution_patterns
           SET times_confirmed = times_confirmed + 1,
               confidence = MIN(1.0, confidence + 0.05),
               updated_at = ?
           WHERE pattern_id = ?""",
        (now, pattern_id),
    )


def get_similar_patterns(
    conn: sqlite3.Connection,
    category: str | None = None,
    keywords: list[str] | None = None,
    limit: int = 5,
) -> list[ResolutionPattern]:
    """Find similar patterns by category and/or keyword overlap."""
    if category:
        candidates = get_patterns_by_category(conn, category, limit=50)
    else:
        rows = conn.execute("SELECT * FROM resolution_patterns").fetchall()
        candidates = [_row_to_pattern(r) for r in rows]

    if not keywords:
        return candidates[:limit]

    kw_lower = {k.lower() for k in keywords}
    scored = []
    for p in candidates:
        stored_kw = {k.lower() for k in p.trigger_keywords}
        overlap = len(kw_lower & stored_kw)
        scored.append((overlap, p.confidence, p))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [p for _, _, p in scored[:limit]]


def _row_to_pattern(row: sqlite3.Row) -> ResolutionPattern:
    """Convert a DB row to ResolutionPattern, parsing JSON fields."""
    d = dict(row)
    for field in ("trigger_keywords", "diagnosis_steps"):
        val = d.get(field)
        if isinstance(val, str):
            d[field] = json.loads(val)
        elif val is None:
            d[field] = []
    return ResolutionPattern(**d)
