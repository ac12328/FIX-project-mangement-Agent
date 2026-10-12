"""Link emails to unambiguous known project IDs in email subjects.

Idempotent helpers used at ingest and for repair of unlinked rows.
"""

from __future__ import annotations

import re
import sqlite3

# Prefer verified bracketed project IDs.
_BRACKET_PROJECT_RE = re.compile(
    r"\[([A-Za-z][A-Za-z0-9_-]{1,31})\]",
)

# Known project ids loaded once per call from DB for membership check
def extract_bracket_project_ids(subject: str | None) -> list[str]:
    if not subject:
        return []
    return [m.group(1).upper() for m in _BRACKET_PROJECT_RE.finditer(subject)]


def resolve_project_id_from_subject(
    conn: sqlite3.Connection,
    subject: str | None,
    *,
    existing_project_id: str | None = None,
) -> str | None:
    """Return one unambiguous known project ID from the subject, or keep existing.

    Only links when the extracted id exists in ``projects`` (not ``_system``).
    If *existing_project_id* is already set, it wins (no override).
    """
    if existing_project_id and existing_project_id != "_system":
        return existing_project_id

    # Match complete known IDs, never partial prefixes.
    # Multiple project references are ambiguous and must not be guessed.
    candidates = {
        str(row[0])
        for row in conn.execute("SELECT project_id FROM projects WHERE project_id != '_system'")
        if re.search(
            r"(?<![A-Za-z0-9_-])" + re.escape(str(row[0])) + r"(?![A-Za-z0-9_-])",
            subject or "",
            re.IGNORECASE,
        )
    }
    return next(iter(candidates)) if len(candidates) == 1 else existing_project_id


def relink_unlinked_emails_by_subject_tag(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
) -> int:
    """Set project_id on unlinked emails whose subject tags a known project.

    Returns number of rows updated.
    """
    rows = conn.execute(
        """
        SELECT email_id, subject FROM processed_emails
        WHERE project_id IS NULL
          AND subject LIKE '%[%]%'
        ORDER BY processed_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    updated = 0
    for row in rows:
        email_id = row["email_id"] if hasattr(row, "keys") else row[0]
        subject = row["subject"] if hasattr(row, "keys") else row[1]
        pid = resolve_project_id_from_subject(conn, subject, existing_project_id=None)
        if not pid:
            continue
        cur = conn.execute(
            "UPDATE processed_emails SET project_id = ? WHERE email_id = ? AND project_id IS NULL",
            (pid, email_id),
        )
        updated += cur.rowcount
    return updated
