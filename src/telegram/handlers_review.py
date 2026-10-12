"""Handlers for Review Card (A) and Triage Card (B)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from src.services.review_service import review_approve, review_reject
from src.telegram._cb_helpers import extract_link, extract_message_metadata
from src.telegram.i18n import t
from src.telegram.operator_messages import applied_receipt, review_failure
from src.telegram.session import create_session, set_session_data


def handle_review(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Handle Review Card (A)."""
    if not project_id:
        return {"text": t("review.no_project")}

    review_link = extract_link(message_context, "proposed_update")
    if review_link is None:
        metadata = extract_message_metadata(message_context)
        if metadata.get("review_update_id"):
            review_link = {"object_ref": metadata["review_update_id"]}
        elif (message_context or {}).get("telegram_message_id") is not None:
            # A persisted card without an exact target cannot authorize the latest
            # unrelated proposal. Direct legacy command calls remain supported.
            return {"text": t("review.approved_no_changes").format(project_id=project_id),
                    "project_id": project_id, "_error": True}

    if action == "APPROVE":
        row = _get_review_target(
            conn,
            project_id,
            review_link["object_ref"] if review_link else None,
        )
        if not row:
            return {
                "text": t("review.approved_no_changes").format(project_id=project_id),
                "project_id": project_id,
            }

        update_id = row["update_id"]
        result = review_approve(
            conn,
            update_id,
            actor=operator_id,
            surface="telegram",
            commit=post_commit is None,
            post_commit=post_commit,
        )
        if result.success:
            applied = ", ".join(result.applied_fields.keys())
            return {
                "text": applied_receipt(project_id, result.applied_fields, current_next_action=(
                    conn.execute("SELECT next_action FROM projects WHERE project_id=?", (project_id,)).fetchone()[0]
                ), advisory_refresh=True),
                "project_id": project_id,
                "object_ref": update_id,
            }
        return {
            "_error": True,
            "text": review_failure(result.message),
            "project_id": project_id,
            "object_ref": update_id,
        }

    if action == "REJECT":
        row = _get_review_target(
            conn,
            project_id,
            review_link["object_ref"] if review_link else None,
        )
        if review_link and row is None:
            return {"text": t("review.approved_no_changes").format(project_id=project_id), "project_id": project_id}
        update_id = row["update_id"] if row else "unlinked"
        result = review_reject(
            conn,
            update_id,
            reason="",
            tags=[],
            actor=operator_id,
            surface="telegram",
            fallback_project_id=project_id if row is None else None,
            commit=post_commit is None,
            post_commit=post_commit,
        )
        if not result.success:
            return {
                "text": result.message,
                "project_id": project_id,
                "object_ref": update_id,
            }
        return {
            "text": t("review.rejected").format(project_id=project_id),
            "project_id": project_id,
            "object_ref": update_id,
        }

    if action in ("MODIFY", "EDIT"):  # EDIT kept for backward-compat
        if review_link and _get_review_target(conn, project_id, review_link["object_ref"]) is None:
            return {"text": t("review.approved_no_changes").format(project_id=project_id), "project_id": project_id}
        from src.project_state.observability import project_state_metrics

        project_state_metrics.record_review("edited")
        session_id = create_session(
            conn,
            "review_edit",
            originating_ref=review_link["object_ref"] if review_link else project_id,
            operator_id=operator_id,
            commit=post_commit is None,
        )
        set_session_data(
            conn, session_id, {"project_id": project_id}, commit=post_commit is None,
        )
        return {
            "text": t("review.edit_mode").format(project_id=project_id),
            "project_id": project_id,
            "session_id": session_id,
            "object_ref": review_link["object_ref"] if review_link else project_id,
            "object_links": [
                {"object_type": "telegram_session", "object_ref": session_id, "project_id": project_id},
            ],
        }

    return {"text": t("review.generic_action").format(action=action), "project_id": project_id}


def _get_review_target(
    conn: sqlite3.Connection,
    project_id: str,
    update_id: str | None,
) -> sqlite3.Row | None:
    """Resolve the exact proposed update targeted by a review card."""
    if update_id:
        row = conn.execute(
            """SELECT update_id
               FROM proposed_updates
               WHERE update_id = ? AND project_id = ? AND decision_outcome = 'needs_review'""",
            (update_id, project_id),
        ).fetchone()
        # An old card must never approve a different, newer pending update.
        return row

    return conn.execute(
        """SELECT update_id, proposed_changes, confidence, risk_level
           FROM proposed_updates
           WHERE project_id = ? AND decision_outcome = 'needs_review'
           ORDER BY created_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def handle_triage(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle Triage Card (B) actions."""
    if action == "ASSIGN":
        email_link = extract_link(message_context, "email")
        from src.email.triage_conversation import freeze_assignment_scope, assignment_prompt
        scope = freeze_assignment_scope(conn, email_link["object_ref"], message_context or {}) if email_link else None
        metadata = extract_message_metadata(message_context)
        if metadata.get("triage_destination_scope") and scope is None:
            from src.telegram.operator_messages import en
            return {"_error": True, "text": "Classification scope changed; open the latest source card."
                    if en() else "分類範圍已改變，請查看最新郵件通知。"}
        session_id = create_session(
            conn,
            "triage_assign",
            originating_ref=email_link["object_ref"] if email_link else None,
            operator_id=operator_id,
        )
        data = {"project_id": project_id}
        if scope:
            data["triage_assignment_scope"] = scope.model_dump()
        set_session_data(conn, session_id, data)
        return {
            "text": assignment_prompt(conn, scope) if scope else t("triage.send_project_id"),
            "project_id": project_id,
            "session_id": session_id,
            "object_ref": email_link["object_ref"] if email_link else None,
            "object_links": [
                {"object_type": "telegram_session", "object_ref": session_id, "project_id": project_id},
            ],
        }
    if action == "DETAILS":
        from src.telegram.email_details import email_notification_details
        return email_notification_details(conn, message_context, project_id)
    return {"text": t("triage.generic_action").format(action=action), "project_id": project_id}
