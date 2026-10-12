"""Executor handler for deterministic project reconciliation."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.project_reconciliation import ReconciliationMode, reconcile_project_from_emails
from src.project_reconciliation.repository import find_latest_owned_operation


def handle_project_reconcile(
    conn: sqlite3.Connection,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run a trusted, idempotent reconciliation request."""
    project_id = str(arguments.get("project_id") or "").strip().upper()
    mode = str(arguments.get("mode") or ReconciliationMode.PREVIEW.value).strip().lower()
    operator_id = str(arguments.get("_operator_id") or "default_operator")
    chat_id = str(arguments.get("chat_id") or "legacy")
    trusted_surface = str(arguments.get("_trusted_surface") or "internal")
    resume_operation_id = str(arguments.get("resume_operation_id") or "").strip() or None

    if mode == ReconciliationMode.COMMIT.value and resume_operation_id is None:
        latest = find_latest_owned_operation(
            conn,
            operator_id=operator_id,
            chat_id=chat_id,
            trusted_surface=trusted_surface,
            project_id=project_id,
        )
        if latest is not None:
            resume_operation_id = str(latest["operation_id"])

    result = reconcile_project_from_emails(
        conn,
        project_id=project_id,
        mode=mode,
        operator_id=operator_id,
        chat_id=chat_id,
        trusted_surface=trusted_surface,
        trusted_explicit_operator=bool(arguments.get("_trusted_explicit_operator")),
        request_key=str(arguments.get("_idempotency_key") or "").strip() or None,
        resume_operation_id=resume_operation_id,
        create_if_missing=bool(arguments.get("create_if_missing", False)),
        post_commit=arguments.get("_post_commit_effects"),
    )
    return result.model_dump(mode="json")


PROJECT_RECONCILIATION_TOOL_HANDLERS = {
    "project_reconcile": handle_project_reconcile,
}
