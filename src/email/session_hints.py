"""Harness evidence enrichment: session connectivity hints from project emails.

General (all projects): when status is served, pull Host/Port/CompID from recent
linked mail so the agent does not rely only on sparse project columns.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

# Host / IP (prefer private ranges and common FIX host lines)
_HOST_RE = re.compile(
    r"(?i)\b(?:host|ip|uat\s*host|prod\s*host|server)\s*[:=]\s*"
    r"(\d{1,3}(?:\.\d{1,3}){3})"
)
_IP_FALLBACK_RE = re.compile(
    r"\b((?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3})\b"
)
_PORT_RE = re.compile(
    r"(?i)\b(?:port|uat\s*port|prod\s*port)\s*[:=]?\s*(\d{2,5})\b"
)
_SENDER_RE = re.compile(
    r"(?i)\b(?:sender(?:comp)?id|sender\s*id|sendercompid)\s*[:=]\s*"
    r"([A-Za-z0-9._\-]{3,32})"
)
_TARGET_RE = re.compile(
    r"(?i)\b(?:target(?:comp)?id|target\s*id|targetcompid)\s*[:=]\s*"
    r"([A-Za-z0-9._\-]{3,32})"
)
# CompIDs that look like session names (e.g. OSFIXT04S) without labels
_COMPID_LOOSE_RE = re.compile(
    r"\b([A-Z]{2,6}FIX[A-Z0-9]{2,10})\b"
)


def extract_session_hints_from_text(text: str) -> dict[str, str]:
    """Pull session fields from free text (subject + body)."""
    if not text:
        return {}
    out: dict[str, str] = {}
    m = _HOST_RE.search(text)
    if m:
        out["host"] = m.group(1)
    else:
        m2 = _IP_FALLBACK_RE.search(text)
        if m2:
            out["host"] = m2.group(1)
    m = _PORT_RE.search(text)
    if m:
        out["port"] = m.group(1)
    m = _SENDER_RE.search(text)
    if m:
        out["sender_comp_id"] = m.group(1)
    m = _TARGET_RE.search(text)
    if m:
        out["target_comp_id"] = m.group(1)
    if "sender_comp_id" not in out:
        m = _COMPID_LOOSE_RE.search(text)
        if m:
            out["sender_comp_id"] = m.group(1)
    return out


def load_recent_email_session_hints(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    email_limit: int = 8,
    body_chars: int = 2500,
) -> dict[str, Any]:
    """Scan recent project emails (FK or subject tag) for session params.

    Returns dict with ``hints`` (merged fields), ``source_subject``,
    ``source_sender``, ``source_date``, and ``evidence_lines``.
    """
    tag = f"%{project_id}%"
    rows = conn.execute(
        """
        SELECT email_id, subject, sender_email, processed_at, body_snippet, direction
        FROM processed_emails
        WHERE project_id = ?
           OR subject LIKE ?
           OR COALESCE(body_snippet, '') LIKE ?
        ORDER BY processed_at DESC
        LIMIT ?
        """,
        (project_id, tag, tag, email_limit),
    ).fetchall()

    merged: dict[str, str] = {}
    evidence: list[str] = []
    source_subject: str | None = None
    source_sender: str | None = None
    source_date: str | None = None

    for row in rows:
        r = dict(row)
        blob = f"{r.get('subject') or ''}\n{r.get('body_snippet') or ''}"
        if body_chars and len(blob) > body_chars:
            blob = blob[:body_chars]
        found = extract_session_hints_from_text(blob)
        if not found:
            continue
        # Prefer first (newest) value for each key; keep scanning to fill gaps
        for k, v in found.items():
            if k not in merged:
                merged[k] = v
                evidence.append(
                    f"{k}={v} from {(r.get('processed_at') or '')[:10]} "
                    f"{r.get('sender_email') or ''}"
                )
        if source_subject is None and found:
            source_subject = r.get("subject")
            source_sender = r.get("sender_email")
            source_date = (r.get("processed_at") or "")[:10]
        if len(merged) >= 4:
            break

    return {
        "hints": merged,
        "source_subject": source_subject,
        "source_sender": source_sender,
        "source_date": source_date,
        "evidence_lines": evidence[:6],
        "emails_scanned": len(rows),
    }


def format_session_hints_block(payload: dict[str, Any]) -> str | None:
    """Render operator-facing English lines for status tool output."""
    hints = payload.get("hints") or {}
    if not hints:
        return None
    lines = ["Session params from recent email (do not invent beyond these):"]
    label = {
        "host": "Host",
        "port": "Port",
        "sender_comp_id": "SenderCompID / SenderID",
        "target_comp_id": "TargetCompID",
    }
    for key in ("host", "port", "sender_comp_id", "target_comp_id"):
        if key in hints:
            lines.append(f"  - {label[key]}: {hints[key]}")
    if payload.get("source_date") or payload.get("source_subject"):
        lines.append(
            f"  - Source: {(payload.get('source_date') or '')} "
            f"{(payload.get('source_sender') or '')} | "
            f"{(payload.get('source_subject') or '')[:80]}"
        )
    return "\n".join(lines)
