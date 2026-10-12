"""Handlers for Staged Update Card (CU) confirmation and cancellation."""

from __future__ import annotations

from collections.abc import Callable
import sqlite3

from src.repositories.activity_log_repo import insert_activity
from src.services.project_update_commands import publish_confirmed_project_update
from src.tools.executor import execute_tool


def _tool_failure(result: object, project_id: str) -> dict | None:
    """Interpret structured tool receipts, not human-readable text prefixes."""
    if not isinstance(result, dict):
        raise RuntimeError("Staged update returned no structured receipt")
    if result.get("side_effect_status") == "may_have_occurred":
        # The gateway may report a late timeout after a handler staged writes.
        # The callback savepoint must discard them instead of committing an
        # unacknowledged update without its audit and post-commit effects.
        raise RuntimeError("Staged update outcome is uncertain")
    if result.get("ok") is True:
        return None
    text = str(result.get("text") or result.get("error") or "Update did not complete.")
    response = {"text": text, "project_id": project_id}
    if result.get("error") != "no_pending_update":
        response["_error"] = True
    return response


def bound_staged_operation_project(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    chat_id: int | None,
    telegram_message_id: int | None,
) -> str | None:
    """Resolve only the operation attached to this delivered Telegram card."""
    if not operation_id or chat_id is None or telegram_message_id is None:
        return None
    row = conn.execute(
        "SELECT card.project_id,card.chat_id,card.telegram_message_id,"
        "card.state,op.operation_type FROM staged_update_card_outbox AS card "
        "JOIN pending_operations AS op ON op.operation_id=card.operation_id "
        "WHERE card.operation_id=?", (operation_id,),
    ).fetchone()
    if (not row or row[3] != "sent" or row[4] != "project_update"
            or str(row[1]) != str(chat_id)
            or row[2] != telegram_message_id):
        return None
    return str(row[0])


def handle_staged_update(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Stage command and audit together; the callback owner commits and runs effects."""
    if not project_id:
        return {"text": "缺少 project ID，無法處理暫存更新。"}

    operation_id = None
    if project_id.startswith("@"):
        operation_id = project_id[1:]
        bound_project = bound_staged_operation_project(
            conn, operation_id,
            chat_id=(message_context or {}).get("chat_id"),
            telegram_message_id=(message_context or {}).get("telegram_message_id"),
        )
        if bound_project is None:
            return {
                "_error": True,
                "text": "This confirmation card is not bound to the current message. "
                        "Re-check the pending update in its original chat.",
            }
        project_id = bound_project

    context = {
        "operator_id": operator_id,
        "chat_id": (message_context or {}).get("chat_id"),
    }
    if action == "CONFIRM":
        result = execute_tool(
            conn, "staged", "confirm_update",
            {"project_id": project_id, "operation_id": operation_id,
             "commit": False, "publish_event": False},
            execution_context=context,
        )
        failure = _tool_failure(result, project_id)
        if failure is not None:
            return failure
        if result.get("outcome") not in {"accepted", "no_change"}:
            return {"_error": True, "text": "Update outcome is unknown.", "project_id": project_id}
        text = str(result.get("text") or "")
        insert_activity(
            conn,
            actor=operator_id,
            action_type="staged_update_confirmed",
            project_id=project_id,
            surface="telegram",
            summary=f"Operator confirmed staged update for {project_id}",
        )
        if result["outcome"] == "accepted" and post_commit is not None:
            applied_fields = dict(result.get("applied_fields") or {})
            surface = str(result.get("surface") or "telegram")
            post_commit.append(
                lambda: publish_confirmed_project_update(
                    project_id, applied_fields, surface=surface,
                )
            )
        return {"text": f"✅ 已確認更新 {project_id}：\n{text[:500]}", "project_id": project_id}

    if action == "CANCEL":
        result = execute_tool(
            conn, "staged", "cancel_update",
            {"project_id": project_id, "operation_id": operation_id, "commit": False},
            execution_context=context,
        )
        failure = _tool_failure(result, project_id)
        if failure is not None:
            return failure
        if result.get("outcome") != "cancelled":
            return {"_error": True, "text": "Cancellation outcome is unknown.", "project_id": project_id}
        text = str(result.get("text") or "")
        insert_activity(
            conn,
            actor=operator_id,
            action_type="staged_update_cancelled",
            project_id=project_id,
            surface="telegram",
            summary=f"Operator cancelled staged update for {project_id}",
        )
        return {"text": f"已取消 {project_id} 暫存更新。\n{text[:300]}", "project_id": project_id}

    return {"text": f"未知操作：{action}"}
