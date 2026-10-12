"""Source integration boundaries: evidence first, durable job second."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.learning.jobs.outbox_repo import enqueue_job


def enqueue_post_send_learning(conn: sqlite3.Connection, draft: dict[str, Any]) -> str | None:
    """Capture the sent draft revision and conditionally schedule extraction."""
    from src.learning.governance_flags import capture_enabled, outbox_v2_enabled
    from src.learning.ingest.post_send import capture_sent_draft_edit
    if not capture_enabled():
        return None

    history_value = draft.get("draft_history")
    try:
        history = json.loads(history_value) if isinstance(history_value, str) else (history_value or [])
    except (TypeError, json.JSONDecodeError):
        history = []
    original = str((history[0].get("body") if history and isinstance(history[0], dict) else "") or "")
    sent = str(draft.get("current_draft_text") or "")
    if not original or not sent:
        return None
    draft_id = str(draft.get("draft_id") or "")
    revision = conn.execute(
        """SELECT output_revision_id,operator_id FROM output_revisions
           WHERE surface='draft' AND artifact_type='draft' AND artifact_id=? AND revision=?""",
        (draft_id, int(draft.get("draft_version") or 1)),
    ).fetchone()
    if not revision:
        from src.memory.output_revision_repo import create_output_revision
        operator_id = str(draft.get("operator_id") or "default_operator")
        revision_id = create_output_revision(
            conn, operator_id=operator_id, surface="draft", artifact_type="draft",
            artifact_id=draft_id, revision=int(draft.get("draft_version") or 1),
            trace_id=None, content=sent,
        )
        revision = {"output_revision_id": revision_id, "operator_id": operator_id}
    observation = capture_sent_draft_edit(
        conn,
        source_id=f"sent:{draft_id}",
        operator_id=revision["operator_id"],
        artifact_id=draft_id,
        before_text=original,
        after_text=sent,
        project_id=draft.get("project_id"),
        output_revision_id=revision["output_revision_id"],
    )
    if not outbox_v2_enabled():
        return observation.observation_id
    return enqueue_job(
        conn,
        observation_id=observation.observation_id,
        job_type="post_send_extraction",
        extractor_version="post-send-v2",
        payload={"draft_id": draft_id},
    )


def enqueue_wiki_learning_items(conn: sqlite3.Connection, items: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Capture Wiki inputs; return counts and a safe cursor for the contiguous batch."""
    from src.learning.governance_flags import capture_enabled, outbox_v2_enabled
    from src.learning.ingest.manual import capture_wiki_ingestion

    if not capture_enabled():
        return {"captured": 0, "enqueued": 0, "cursor": None}
    captured = enqueued = 0
    cursor: str | None = None
    for category in ("evidence", "learned_rules", "feedback"):
        for item in items.get(category, []):
            source_ref = str(
                item.get("evidence_id") or item.get("rule_id") or item.get("feedback_id")
                or item.get("id") or ""
            )
            if not source_ref:
                continue
            text = str(
                item.get("content_text") or item.get("rule") or item.get("operator_note")
                or item.get("summary") or source_ref
            )
            before_observations = conn.total_changes
            observation = capture_wiki_ingestion(
                conn,
                source_id=f"wiki-extraction:{category}:{source_ref}",
                operator_id="default_operator",
                page_path=f"pending/{category}/{source_ref}",
                text=text,
                project_id=item.get("project_id") or item.get("source_project_id"),
            )
            if conn.total_changes > before_observations:
                captured += 1
            if outbox_v2_enabled():
                before_jobs = conn.total_changes
                enqueue_job(
                    conn,
                    observation_id=observation.observation_id,
                    job_type="wiki_extraction",
                    extractor_version="wiki-extraction-v1",
                    payload={"category": category, "source_ref": source_ref},
                )
                if conn.total_changes > before_jobs:
                    enqueued += 1
            created_at = str(item.get("created_at") or "")
            if created_at and (cursor is None or created_at > cursor):
                cursor = created_at
    return {"captured": captured, "enqueued": enqueued, "cursor": cursor}
