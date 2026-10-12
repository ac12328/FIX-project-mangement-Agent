"""Synchronize a reconciliation checkpoint with its operator review outcome."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from src.project_reconciliation.contracts import ReconciliationResult
from src.project_reconciliation.repository import get_operation, update_operation
from src.project_reconciliation.service_support import checkpoint_models, project_row


def resolve_reconciliation_review(
    conn: sqlite3.Connection,
    review: Mapping[str, Any],
    *,
    accepted: bool,
    applied_fields: dict[str, Any] | None = None,
) -> None:
    """Update only the linked owned checkpoint, in the caller's review transaction."""
    operation_id = review.get("operation_id")
    if not operation_id:
        return
    operation = get_operation(conn, str(operation_id))
    project_id = str(review["project_id"])
    if (
        not operation
        or operation.get("operation_type") != "project_email_reconciliation"
        or operation.get("resource_key") != project_id
        or operation.get("proposed_update_id") != review.get("update_id")
        or operation.get("operator_id") != review.get("operator_id")
        or operation.get("phase") in {"committed", "no_change", "superseded"}
    ):
        return
    bundle, proposal = checkpoint_models(operation)
    status = "committed" if accepted else "superseded"
    result = ReconciliationResult(
        ok=True,
        status=status,
        outcome="accepted" if accepted else "rejected",
        committed=accepted,
        project_created=bool((operation.get("result") or {}).get("project_created")),
        project_id=project_id,
        operation_id=str(operation_id),
        applied_fields=applied_fields or {},
        proposed_fields=dict(proposal.fields) if proposal else {},
        current_project=project_row(conn, project_id),
        bundle_hash=bundle.bundle_hash if bundle else None,
        evidence_refs=proposal.evidence_refs if proposal else [],
        proposed_update_id=str(review["update_id"]),
        text=(
            f"{project_id} reconciliation was applied through operator review."
            if accepted
            else f"{project_id} reconciliation was rejected. Start a new assessment rather than resuming this checkpoint."
        ),
    )
    update_operation(
        conn,
        str(operation_id),
        phase=status,
        result=result.model_dump(mode="json"),
        project_id=project_id,
        proposed_update_id=str(review["update_id"]),
    )
