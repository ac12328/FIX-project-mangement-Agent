"""Unified search endpoint — searches across multiple entity tables."""

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_read_conn

router = APIRouter(tags=["search"])


def _score(query_lower: str, value: str | None) -> float:
    """Return relevance score: 1.0 exact, 0.8 prefix, 0.5 contains, 0.0 miss."""
    if not value:
        return 0.0
    val_lower = value.lower()
    if val_lower == query_lower:
        return 1.0
    if val_lower.startswith(query_lower):
        return 0.8
    if query_lower in val_lower:
        return 0.5
    return 0.0


def _best_score(query_lower: str, *fields: str | None) -> float:
    """Return the highest score across multiple fields."""
    return max((_score(query_lower, f) for f in fields), default=0.0)


def _snippet(text: str | None, max_len: int = 120) -> str:
    """Return a short snippet of text."""
    if not text:
        return ""
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _search_projects(
    conn: sqlite3.Connection, pattern: str, query_lower: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT project_id, client_name, notes
        FROM projects
        WHERE project_id LIKE ? OR client_name LIKE ? OR notes LIKE ?
        """,
        (pattern, pattern, pattern),
    ).fetchall()
    results = []
    for r in rows:
        score = _best_score(query_lower, r["project_id"], r["client_name"])
        if score == 0.0:
            score = 0.5  # matched via notes
        results.append({
            "entity_type": "project",
            "id": r["project_id"],
            "title": r["client_name"] or r["project_id"],
            "snippet": _snippet(r["notes"]),
            "score": score,
        })
    return results


def _search_drafts(
    conn: sqlite3.Connection, pattern: str, query_lower: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT draft_id, subject, current_draft_text
        FROM drafts
        WHERE subject LIKE ? OR current_draft_text LIKE ?
        """,
        (pattern, pattern),
    ).fetchall()
    results = []
    for r in rows:
        score = _best_score(query_lower, r["subject"])
        if score == 0.0:
            score = 0.5  # matched via body
        results.append({
            "entity_type": "draft",
            "id": r["draft_id"],
            "title": r["subject"] or r["draft_id"],
            "snippet": _snippet(r["current_draft_text"]),
            "score": score,
        })
    return results


def _search_support_tickets(
    conn: sqlite3.Connection, pattern: str, query_lower: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ticket_id, title, description
        FROM support_tickets
        WHERE title LIKE ? OR description LIKE ?
        """,
        (pattern, pattern),
    ).fetchall()
    results = []
    for r in rows:
        score = _best_score(query_lower, r["title"])
        if score == 0.0:
            score = 0.5
        results.append({
            "entity_type": "support_ticket",
            "id": r["ticket_id"],
            "title": r["title"],
            "snippet": _snippet(r["description"]),
            "score": score,
        })
    return results


def _search_activity_log(
    conn: sqlite3.Connection, pattern: str, query_lower: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, project_id, summary
        FROM activity_log
        WHERE summary LIKE ?
        LIMIT 50
        """,
        (pattern,),
    ).fetchall()
    results = []
    for r in rows:
        results.append({
            "entity_type": "activity",
            "id": str(r["id"]),
            "title": f"Activity #{r['id']} ({r['project_id'] or 'global'})",
            "snippet": _snippet(r["summary"]),
            "score": 0.5,
        })
    return results


def _search_wiki_pages(
    conn: sqlite3.Connection, pattern: str, query_lower: str,
) -> list[dict]:
    """Search wiki_pages if the table exists."""
    try:
        rows = conn.execute(
            """
            SELECT id, title, content
            FROM wiki_pages
            WHERE content LIKE ? OR title LIKE ?
            LIMIT 50
            """,
            (pattern, pattern),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    results = []
    for r in rows:
        score = _best_score(query_lower, r["title"])
        if score == 0.0:
            score = 0.5
        results.append({
            "entity_type": "wiki",
            "id": str(r["id"]),
            "title": r["title"] or f"Wiki #{r['id']}",
            "snippet": _snippet(r["content"]),
            "score": score,
        })
    return results


@router.get("/search")
def unified_search(
    q: Optional[str] = Query(None, min_length=1),
    limit: int = Query(20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    """Search across projects, drafts, tickets, activity log, and wiki."""
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    pattern = f"%{q}%"
    query_lower = q.lower()

    all_results: list[dict] = []
    all_results.extend(_search_projects(conn, pattern, query_lower))
    all_results.extend(_search_drafts(conn, pattern, query_lower))
    all_results.extend(_search_support_tickets(conn, pattern, query_lower))
    all_results.extend(_search_activity_log(conn, pattern, query_lower))
    all_results.extend(_search_wiki_pages(conn, pattern, query_lower))

    # Sort by score descending, then truncate
    all_results.sort(key=lambda r: r["score"], reverse=True)
    return {"results": all_results[:limit]}
