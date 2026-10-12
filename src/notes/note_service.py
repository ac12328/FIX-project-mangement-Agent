"""
Meeting/Call Note service (Plan Step 2.15).
/note PROJECT-X client confirmed M3 done
Stores in Evidence Store (type=fact, source_ref=manual_note) + Working Memory.
Does NOT modify project truth directly.
"""

import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass

from src.repositories.evidence_repo import insert_evidence
from src.repositories.working_memory_repo import upsert_memory


@dataclass
class NoteResult:
    """Result of storing a note."""
    project_id: str
    evidence_id: str
    stored_in_memory: bool
    summary: str


def store_note(
    conn: sqlite3.Connection,
    project_id: str,
    note_text: str,
    *,
    source: str = "telegram",
) -> NoteResult:
    """
    Store a meeting/call note as evidence and in working memory.

    - Evidence: type=fact, source_ref=manual_note:{source}
    - Working Memory: key=latest_note (only refreshed on new notes; milestone/truth
      writes do not update this key — UI uses semantic staleness checks)
    - Does NOT modify project truth — milestone changes go through safe_write_project.
    """
    now = datetime.now(timezone.utc).isoformat()
    summary = _summarise(note_text)

    evidence_id = insert_evidence(
        conn,
        project_id=project_id,
        evidence_type="manual_note",
        content_summary=summary,
        content_text=note_text,
        source_ref=f"manual_note:{source}",
        evidence_at=now,
    )

    upsert_memory(conn, project_id, "latest_note", note_text)
    conn.commit()

    return NoteResult(
        project_id=project_id,
        evidence_id=evidence_id,
        stored_in_memory=True,
        summary=summary,
    )


def _summarise(text: str, max_len: int = 120) -> str:
    """Create a short summary from the note text."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3].rsplit(" ", 1)[0] + "..."
