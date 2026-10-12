"""Detect newer substantive evidence in the same persisted email thread."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from src.email.body_cleaner import clean_email_body

_REPLY_PREFIX = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.I)
_PROJECT_TAG = re.compile(r"\[[A-Z]{1,8}\d{4,}\]", re.I)


def find_newer_substantive_email(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_email_id: str,
) -> dict[str, Any] | None:
    """Return the earliest newer authored message from the source thread."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    received_expr = "received_at" if "received_at" in columns else "NULL"
    source = conn.execute(
        f"""SELECT email_id,subject,thread_id,thread_fp,
                   COALESCE({received_expr},processed_at) AS evidence_at
            FROM processed_emails WHERE project_id=? AND email_id=?""",
        (project_id, source_email_id),
    ).fetchone()
    if source is None:
        return None
    rows = conn.execute(
        f"""SELECT email_id,subject,body_snippet,thread_id,thread_fp,
                   COALESCE({received_expr},processed_at) AS evidence_at
            FROM processed_emails
            WHERE project_id=? AND email_id<>?
              AND COALESCE({received_expr},processed_at)>?
            ORDER BY COALESCE({received_expr},processed_at),email_id""",
        (project_id, source["email_id"], source["evidence_at"] or ""),
    ).fetchall()
    source_keys = {value for value in (source["thread_id"], source["thread_fp"]) if value}
    source_subject = _normalize_subject(source["subject"] or "")
    for raw in rows:
        candidate = dict(raw)
        candidate_keys = {
            value for value in (candidate.get("thread_id"), candidate.get("thread_fp")) if value
        }
        same_thread = (
            bool(source_keys.intersection(candidate_keys))
            if source_keys
            else bool(source_subject and _normalize_subject(candidate.get("subject") or "") == source_subject)
        )
        if not same_thread:
            continue
        authored = clean_email_body(candidate.get("body_snippet"))
        if not authored:
            continue
        candidate["cleaned_body"] = authored
        candidate["source_email_id"] = source["email_id"]
        return candidate
    return None


def _normalize_subject(subject: str) -> str:
    value = _REPLY_PREFIX.sub("", subject.strip())
    value = _PROJECT_TAG.sub("", value)
    return " ".join(value.casefold().split())
