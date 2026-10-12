"""Offload large tool results to SQLite thread_artifacts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

# Raw offload in core_loop (non-digest paths) — keep high to avoid breaking tool replay.
ARTIFACT_THRESHOLD_CHARS = 4000
# Digest pipeline uses a lower threshold in tool_digest.OFFLOAD_THRESHOLD_CHARS.


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> dict | None:
    """Load full artifact payload by id."""
    row = conn.execute(
        """
        SELECT artifact_id, tool_name, project_id, content_json, char_count, created_at
        FROM thread_artifacts WHERE artifact_id = ?
        """,
        (artifact_id,),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def store_artifact(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    thread_id: str,
    tool_name: str,
    content: str,
    project_id: str | None = None,
) -> str:
    """Persist content and return artifact_id."""
    artifact_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO thread_artifacts (
            artifact_id, operator_id, thread_id, tool_name,
            project_id, content_json, char_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            operator_id,
            thread_id,
            tool_name,
            project_id,
            content,
            len(content),
            now,
        ),
    )
    return artifact_id


def maybe_offload(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    thread_id: str,
    tool_name: str,
    content: str,
    project_id: str | None = None,
) -> str:
    """Return original content or compact JSON pointer."""
    if not content or len(content) <= ARTIFACT_THRESHOLD_CHARS:
        return content
    artifact_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO thread_artifacts (
            artifact_id, operator_id, thread_id, tool_name,
            project_id, content_json, char_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            operator_id,
            thread_id,
            tool_name,
            project_id,
            content,
            len(content),
            now,
        ),
    )
    pointer = {
        "artifact_id": artifact_id,
        "tool": tool_name,
        "chars": len(content),
        "project_id": project_id,
    }
    return json.dumps(pointer, ensure_ascii=False)