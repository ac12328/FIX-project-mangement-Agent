"""Shared helpers for the project reconciliation service."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.events.bus import Event, event_bus
from src.project_reconciliation.contracts import (
    FrozenEvidenceBundle,
    ReconciliationProposal,
    ReconciliationResult,
)
from src.project_reconciliation.repository import (
    bind_commit_request_key,
    supersede_other_owned_operations,
    update_operation,
)


def project_row(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id=?", (project_id,)
    ).fetchone()
    return dict(row) if row else None


def stored_result(operation: dict[str, Any]) -> ReconciliationResult | None:
    payload = operation.get("result") or {}
    if not payload:
        return None
    try:
        return ReconciliationResult.model_validate(payload)
    except Exception:
        return None


def make_result(
    *,
    status: str,
    outcome: str,
    project_id: str,
    operation_id: str,
    committed: bool = False,
    project_created: bool = False,
    applied_fields: dict[str, Any] | None = None,
    proposed_fields: dict[str, Any] | None = None,
    current_project: dict[str, Any] | None = None,
    bundle_hash: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed_update_id: str | None = None,
    error_code: str | None = None,
    error: str | None = None,
    text: str = "",
) -> ReconciliationResult:
    return ReconciliationResult(
        ok=error_code is None,
        status=status,
        outcome=outcome,
        committed=committed,
        project_id=project_id,
        operation_id=operation_id,
        project_created=project_created,
        applied_fields=applied_fields or {},
        proposed_fields=proposed_fields or {},
        current_project=current_project,
        bundle_hash=bundle_hash,
        evidence_refs=evidence_refs or [],
        proposed_update_id=proposed_update_id,
        error_code=error_code,
        error=error,
        text=text,
    )


def checkpoint_models(
    operation: dict[str, Any],
) -> tuple[FrozenEvidenceBundle | None, ReconciliationProposal | None]:
    checkpoint = operation.get("checkpoint") or {}
    try:
        bundle = FrozenEvidenceBundle.model_validate(checkpoint.get("bundle"))
        proposal = ReconciliationProposal.model_validate(checkpoint.get("proposal"))
    except Exception:
        return None, None
    return bundle, proposal


def operation_is_owned(
    operation: dict[str, Any],
    *,
    project_id: str,
    operator_id: str,
    chat_id: str,
    trusted_surface: str,
) -> bool:
    return (
        operation.get("resource_key") == project_id
        and str(operation.get("operator_id")) == str(operator_id)
        and str(operation.get("chat_id")) == str(chat_id)
        and operation.get("trusted_surface") == trusted_surface
    )




def publish_project_updated(
    *,
    project_id: str,
    operation_id: str,
    fields: list[str],
    surface: str,
) -> None:
    """Publish the post-commit project update event."""
    event_bus.publish(Event(
        event_type="project_updated",
        project_id=project_id,
        detail={
            "fields": fields,
            "operation_id": operation_id,
            "source": "email_reconciliation",
        },
        surface=surface,
    ))

def persist_result_and_supersede_siblings(
    conn: sqlite3.Connection,
    operation_id: str,
    result: ReconciliationResult,
    *,
    phase: str,
    operator_id: str,
    chat_id: str,
    project_id: str,
    proposed_update_id: str | None = None,
    commit_request_key: str | None = None,
) -> None:
    """Persist one authoritative result and retire competing review checkpoints."""
    update_operation(
        conn,
        operation_id,
        phase=phase,
        result=result.model_dump(mode="json"),
        project_id=project_id,
        proposed_update_id=proposed_update_id,
    )
    if commit_request_key is not None:
        bind_commit_request_key(conn, operation_id, commit_request_key)
    supersede_other_owned_operations(
        conn,
        operator_id=operator_id,
        chat_id=chat_id,
        project_id=project_id,
        winning_operation_id=operation_id,
    )
    conn.commit()

def persist_result(
    conn: sqlite3.Connection,
    operation_id: str,
    result: ReconciliationResult,
    *,
    phase: str,
    proposed_update_id: str | None = None,
    last_error: str | None = None,
) -> None:
    update_operation(
        conn,
        operation_id,
        phase=phase,
        result=result.model_dump(mode="json"),
        proposed_update_id=proposed_update_id,
        last_error=last_error,
    )
    conn.commit()
