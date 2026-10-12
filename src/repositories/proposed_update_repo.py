"""
Proposed updates repository — audit trail of all Write Layer decisions.
"""

import json
import sqlite3
from datetime import datetime, timezone

from src.models.proposed_update import ProposedUpdate, WriteDecision


def store_proposed_update(
    conn: sqlite3.Connection,
    update: ProposedUpdate,
    decision: WriteDecision,
) -> None:
    """Store a ProposedUpdate and its decision outcome."""
    now = datetime.now(timezone.utc).isoformat()
    changes_json = json.dumps(
        [c.model_dump() for c in update.proposed_changes]
    )
    rejection_json = json.dumps(decision.rejection_reasons) if decision.rejection_reasons else None
    assessment_json = (
        json.dumps(update.assessment, ensure_ascii=False)
        if update.assessment is not None else None
    )

    conn.execute(
        """INSERT INTO proposed_updates
           (update_id, project_id, proposed_changes, task_type,
            confidence, risk_level, requires_operator_review,
            decision_outcome, rejection_reasons, assessment_json,
            policy_version, source_type, source_ref, decided_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            update.update_id,
            update.project_id,
            changes_json,
            update.task_type.value,
            update.confidence,
            update.risk_level.value,
            1 if update.requires_operator_review else 0,
            decision.outcome.value,
            rejection_json,
            assessment_json,
            update.policy_version,
            update.source_type,
            update.source_ref,
            now,
            now,
        ),
    )


def get_proposed_updates(
    conn: sqlite3.Connection, project_id: str, limit: int = 50
) -> list[dict]:
    """Get proposed updates for a project, newest first."""
    rows = conn.execute(
        """SELECT * FROM proposed_updates
           WHERE project_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        raw_assessment = item.get("assessment_json")
        if raw_assessment:
            try:
                item["assessment"] = json.loads(raw_assessment)
            except (TypeError, json.JSONDecodeError):
                item["assessment"] = None
        else:
            item["assessment"] = None
        results.append(item)
    return results
