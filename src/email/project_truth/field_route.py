"""One source-bound field decision, including evidence, under a caller-owned boundary."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.db.borrowed_transaction import BorrowedTransactionConnection
from src.db.savepoint import savepoint
from src.email.project_truth.contracts import FieldRouteCommandReceipt
from src.email.update_outcomes import COMMITTED_UPDATE_OUTCOMES


class _UnpersistedFieldDecision(Exception):
    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__(outcome)


def _attributable_update_id(
    conn: Any, *, project_id: str, source_ref: str, evidence_id: str,
    outcome: str, after_rowid: int,
) -> str | None:
    """Find one new audit row tied to this command's unique source evidence.

    The legacy router returns only a string. Never infer its effect from a
    project-wide latest row, or fabricate an ID when an adapter omits its audit.
    """
    review = outcome.startswith("review_")
    rows = conn.execute(
        "SELECT update_id, proposed_changes, decision_outcome, task_type, source_type, source_ref "
        "FROM proposed_updates WHERE rowid > ? AND project_id = ?",
        (after_rowid, project_id),
    ).fetchall()
    matches: list[str] = []
    for row in rows:
        try:
            changes = json.loads(row["proposed_changes"])
        except (TypeError, ValueError):
            continue
        if not isinstance(changes, list) or not changes or not all(
            isinstance(change, dict) and change.get("evidence_ref") == evidence_id
            for change in changes
        ):
            continue
        if row["decision_outcome"] != ("needs_review" if review else "accepted"):
            continue
        if review:
            if row["source_type"] != "email" or row["source_ref"] != source_ref:
                continue
        elif row["task_type"] != (
            "direct_write" if outcome == "direct_write" else "truth_write_reasoning"
        ):
            continue
        if row["update_id"]:
            matches.append(row["update_id"])
    return matches[0] if len(matches) == 1 else None


def route_serve_fields(
    conn: Any, result: Any, *, proposed_fields: dict[str, str], confidence: float,
    sender_email: str, subject: str, body: str, summary: str,
    suggested_action: str, has_attachments: bool,
    post_commit: list[Callable[[], None]] | None,
) -> FieldRouteCommandReceipt:
    """Route a field command without committing unrelated caller work.

    The result describes effects in the caller's *pending* transaction, not
    committed external delivery. Unknown audit IDs require reconciliation.
    """
    from src.email.reply_analyser import route_proposed_field_updates
    from src.email.sender_trust import evidence_summary_for_sender
    from src.repositories.evidence_repo import insert_evidence

    source_body = result.case.newest_authored_body if result.case is not None else str(body or "")
    assessment = result.recommendation.assessment
    callbacks: list[Callable[[], None]] = []
    fields = tuple(sorted(proposed_fields.items()))

    def receipt(outcome: str, status: str, *, evidence_id: str | None = None,
                update_id: str | None = None) -> FieldRouteCommandReceipt:
        return FieldRouteCommandReceipt(
            project_id=result.project_id, source_ref=result.source_ref,
            write_action=outcome, status=status, proposed_fields=fields,
            evidence_id=evidence_id, update_id=update_id,
            review_id=update_id if outcome.startswith("review_") else None,
        )

    try:
        with savepoint(conn, ensure_transaction=True):
            borrowed = BorrowedTransactionConnection(conn)
            before_rowid = conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM proposed_updates WHERE project_id = ?",
                (result.project_id,),
            ).fetchone()[0]
            evidence_id = insert_evidence(
                borrowed, project_id=result.project_id, evidence_type="email",
                content_summary=evidence_summary_for_sender(sender_email, subject or ""),
                evidence_at=datetime.now(timezone.utc).isoformat(),
                content_text=source_body, source_ref=result.source_ref,
            )
            outcome = route_proposed_field_updates(
                borrowed, project_id=result.project_id, proposed_fields=proposed_fields,
                confidence=confidence, evidence_ref=evidence_id,
                actor="email_project_truth_b",
                reason=f"Evidence-grounded inbound email from {sender_email}",
                summary=summary, suggested_action=suggested_action,
                sender_email=sender_email, email_body=source_body,
                email_intent="project_update", has_attachments=has_attachments,
                subject=subject, assessment=assessment.json_payload(),
                policy_version=assessment.policy_version,
                source_type=assessment.source_type.value,
                source_ref=result.source_ref, post_commit=callbacks,
                raise_on_error=True,
            )
            if outcome in {
                "skipped", "below_review_threshold", "assessment_required",
                "review_persistence_failed",
            }:
                raise _UnpersistedFieldDecision(outcome)
            if outcome not in COMMITTED_UPDATE_OUTCOMES | {
                "review_card", "review_notification_pending", "review_notification_unknown",
            }:
                raise ValueError(f"Unknown email field route outcome: {outcome!r}")
            update_id = _attributable_update_id(
                conn, project_id=result.project_id, source_ref=result.source_ref,
                evidence_id=evidence_id, outcome=outcome, after_rowid=before_rowid,
            )
    except _UnpersistedFieldDecision as rejected:
        return receipt(rejected.outcome,
                       "incomplete" if rejected.outcome == "review_persistence_failed" else "not_applied")
    if post_commit is not None:
        post_commit.extend(callbacks)
    return receipt(
        outcome, "persisted_in_caller_transaction" if update_id else "incomplete",
        evidence_id=evidence_id, update_id=update_id,
    )
