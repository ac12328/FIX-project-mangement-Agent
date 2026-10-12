"""Recover an orphan Workspace reconciliation turn from its durable receipt."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.assistant.service import ExecutionResult
from src.project_reconciliation.repository import get_operation_by_commit_request_key
from src.project_reconciliation.service_support import stored_result
from src.project_reconciliation.surface import format_reconciliation_result


def committed_receipt_for_turn(
    conn: sqlite3.Connection, *, turn: dict[str, Any],
) -> ExecutionResult | None:
    """Return only a verified receipt belonging to the exact thread and owner."""
    if turn.get("execution_status") != "running":
        return None
    turn_id = str(turn.get("turn_id") or "")
    thread_id = str(turn.get("thread_id") or "")
    if not turn_id or not thread_id:
        return None
    request_key = f"project-reconcile:workspace:{turn_id}"
    # Historical operations without an explicit committing-turn binding are
    # ambiguous: the original request may have been reviewed and committed by
    # a later turn. Never infer receipt ownership from the creation key alone.
    operation = get_operation_by_commit_request_key(conn, request_key)
    if operation is None:
        return None
    if (operation.get("operation_type") != "project_email_reconciliation"
            or operation.get("trusted_surface") != "ai_workspace"):
        return None
    owner = conn.execute(
        "SELECT owner_user_id FROM assistant_threads WHERE thread_id=?", (thread_id,),
    ).fetchone()
    if (
        owner is None
        or operation.get("chat_id") != thread_id
        or operation.get("operator_id") != owner["owner_user_id"]
        or operation.get("phase") != "committed"
    ):
        return None
    receipt = stored_result(operation)
    if (
        receipt is None or not receipt.committed or receipt.status != "committed"
        or receipt.operation_id != operation.get("operation_id")
        or receipt.project_id != operation.get("resource_key")
    ):
        return None
    text = format_reconciliation_result(receipt)
    return ExecutionResult(
        text=text,
        parts=[
            {"type": "text", "text": text},
            {
                "type": "tool_result", "tool": "project_reconcile",
                "status": "completed", "text": f"DB receipt confirmed; Operation: {receipt.operation_id}",
            },
        ],
    )