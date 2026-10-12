"""Operator-reviewed actual dates; never infer them from plans or closure timestamps.

The confirmation ledger leaves terminal project truth and aging untouched. Callers
own the scoped_write transaction and publish project_updated only after commit.
No general agent tool or automatic email write path is exposed by this service.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict

from src.repositories.activity_log_repo import insert_activity


class GoLiveConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    actual_go_live: date
    evidence_id: str
    source_quote: str
    expected_source_hash: str


def source_text(evidence: sqlite3.Row) -> str:
    # Summaries of emails/attachments are not original evidence. Manual notes are
    # intentionally allowed to store their original text in content_summary.
    if evidence["content_text"]:
        return str(evidence["content_text"])
    if evidence["evidence_type"] == "manual_note":
        return str(evidence["content_summary"] or "")
    return ""


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_confirmation(
    conn: sqlite3.Connection, confirmation: GoLiveConfirmation,
) -> None:
    project = conn.execute(
        "SELECT project_status FROM projects WHERE project_id=?",
        (confirmation.project_id,),
    ).fetchone()
    if not project or project[0] != "Completed" or confirmation.project_id.startswith("_"):
        raise ValueError("Backfill requires a completed, non-internal project")
    if confirmation.actual_go_live > datetime.now(timezone.utc).date():
        raise ValueError("An actual go-live date cannot be in the future")
    evidence = conn.execute(
        "SELECT * FROM evidence_store WHERE evidence_id=? AND project_id=?",
        (confirmation.evidence_id, confirmation.project_id),
    ).fetchone()
    if not evidence or evidence["evidence_type"] not in {"email", "manual_note", "attachment"}:
        raise ValueError("Project-scoped original evidence is required")
    text = source_text(evidence)
    if source_hash(text) != confirmation.expected_source_hash:
        raise ValueError("Source changed since review; review it again")
    quote = confirmation.source_quote.strip()
    if not quote or quote not in text or confirmation.actual_go_live.isoformat() not in quote:
        raise ValueError("Quote must contain the explicit ISO date and match original evidence")
    existing = conn.execute(
        "SELECT actual_go_live FROM project_go_live_confirmations WHERE project_id=?",
        (confirmation.project_id,),
    ).fetchone()
    if existing and existing[0] != confirmation.actual_go_live.isoformat():
        raise ValueError("A different actual date is already recorded; refusing to overwrite")


def record_confirmed_go_live(
    conn: sqlite3.Connection, confirmation: GoLiveConfirmation, *,
    actor: str, operator_reviewed: bool = False,
) -> bool:
    """Record one reviewed assertion and audit atomically in a caller transaction.

    operator_reviewed attests that the quoted date describes actual production
    activation, not a target, readiness, PCT completion, or the date of closure.
    This is a manual import boundary, not an automatic semantic classifier.
    """
    if not operator_reviewed or not actor.strip():
        raise ValueError("Explicit operator review and actor attribution are required")
    if not conn.in_transaction:
        raise ValueError("Use a scoped_write transaction")
    validate_confirmation(conn, confirmation)
    existing = conn.execute(
        "SELECT 1 FROM project_go_live_confirmations WHERE project_id=?",
        (confirmation.project_id,),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO project_go_live_confirmations
           (project_id,actual_go_live,evidence_id,source_quote,source_hash,recorded_by)
           VALUES (?,?,?,?,?,?)""",
        (confirmation.project_id, confirmation.actual_go_live.isoformat(),
         confirmation.evidence_id, confirmation.source_quote,
         confirmation.expected_source_hash, actor),
    )
    insert_activity(
        conn, actor=actor, action_type="actual_go_live_confirmed",
        project_id=confirmation.project_id, surface="system",
        object_ref=confirmation.evidence_id,
        summary=f"Actual go-live confirmed: {confirmation.actual_go_live.isoformat()}",
        metadata_json=json.dumps(confirmation.model_dump(mode="json")),
        touch_project=False,
    )
    return True
