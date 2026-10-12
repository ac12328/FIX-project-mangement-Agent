"""Handlers for support ticket evidence read tools."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.repositories.ticket_evidence_repo import (
    get_evidence_for_ticket,
    search_ticket_evidence,
)
from src.repositories.ticket_repo import get_ticket
_DEFAULT_EVIDENCE_LIMIT = 10
_DEFAULT_SEARCH_LIMIT = 8
_MAX_CONTENT_CHARS = 4000


def _format_evidence_item(item) -> dict[str, Any]:
    text = item.content_text or ""
    if len(text) > _MAX_CONTENT_CHARS:
        text = text[:_MAX_CONTENT_CHARS] + "\n[truncated]"
    return {
        "evidence_id": item.evidence_id,
        "ticket_id": item.ticket_id,
        "evidence_type": item.evidence_type,
        "content_summary": item.content_summary,
        "content_text": text or None,
        "source_ref": item.source_ref,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def chat_get_ticket_evidence(conn: sqlite3.Connection, args: dict) -> dict:
    ticket_id = (args.get("ticket_id") or "").strip()
    if not ticket_id:
        return {"text": "ticket_id is required."}

    ticket = get_ticket(conn, ticket_id)
    if not ticket:
        return {"text": f"Ticket {ticket_id} not found."}

    limit = int(args.get("limit") or _DEFAULT_EVIDENCE_LIMIT)
    evidence_type = (args.get("evidence_type") or "").strip() or None
    items = get_evidence_for_ticket(conn, ticket_id)
    if evidence_type:
        items = [
            e for e in items
            if e.evidence_type == evidence_type
        ]
    items = items[:limit]

    if not items:
        return {
            "text": f"No evidence for {ticket_id}" + (f" (type={evidence_type})" if evidence_type else "") + ".",
            "ticket_id": ticket_id,
            "items": [],
        }

    formatted = [_format_evidence_item(e) for e in items]
    lines = [f"Evidence for {ticket_id} ({len(formatted)} item(s)):"]
    for row in formatted:
        lines.append(
            f"- [{row['evidence_type']}] {row['content_summary'][:120]}"
            + (f"\n  {row['content_text'][:300]}" if row.get("content_text") else "")
        )
    return {"text": "\n".join(lines), "ticket_id": ticket_id, "items": formatted}


def chat_search_ticket_evidence(conn: sqlite3.Connection, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": "query is required."}

    limit = int(args.get("limit") or _DEFAULT_SEARCH_LIMIT)
    ticket_id = (args.get("ticket_id") or "").strip() or None
    evidence_type = (args.get("evidence_type") or "").strip() or None

    hits = search_ticket_evidence(
        conn, query, ticket_id=ticket_id, evidence_type=evidence_type, limit=limit,
    )
    if not hits:
        return {"text": f"No ticket evidence matched '{query}'.", "items": []}

    formatted = [_format_evidence_item(e) for e in hits]
    lines = [f"Ticket evidence matches for '{query}' ({len(formatted)}):"]
    for row in formatted:
        lines.append(f"- {row['ticket_id']} [{row['evidence_type']}] {row['content_summary'][:100]}")
    return {"text": "\n".join(lines), "items": formatted}


EVIDENCE_TOOL_HANDLERS = {
    "get_ticket_evidence": chat_get_ticket_evidence,
    "search_ticket_evidence": chat_search_ticket_evidence,
}