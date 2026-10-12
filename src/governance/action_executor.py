"""Execute approved action intents through deterministic application services."""

from __future__ import annotations

import sqlite3

from config.settings import CONFIG
from src.email.draft_session import get_draft, update_draft_status
from src.email.sending import email_config_dict, imap_config_dict
from src.email.draft_send_command import queue_approved_draft_send
from src.services.draft_approval_command import (
    DraftApprovalReceipt,
    dispatch_committed_draft_approval,
)
from src.governance.action_service import (
    approve_action_intent,
    begin_action_execution,
    complete_action_execution,
    fail_action_execution,
)
from src.events.bus import Event, event_bus
from src.governance.email_actions import email_send_payload
from src.governance.calendar_actions import _event_version
from src.governance.project_completion_actions import (
    PROJECT_COMPLETION_ACTION,
    build_project_completion_payload,
    get_project_completion_row,
)
from src.models.action_intent import ActionIntent
from src.repositories.action_intent_repo import get_action_intent, update_action_status


class ActionCommitUncertain(RuntimeError):
    """The durable outcome of an email approval/queue commit is unknown."""


def _commit_email_approval(conn: sqlite3.Connection) -> None:
    """Never dispatch when the approval/queue commit acknowledgement is lost."""
    try:
        conn.commit()
    except Exception as exc:
        # A failed acknowledgement does not establish whether SQLite committed.
        # Discard any still-open local transaction without reading its dirty rows.
        if conn.in_transaction:
            try:
                conn.rollback()
            except Exception:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
        raise ActionCommitUncertain(
            "Approval/queue outcome unconfirmed; manual reconciliation required; "
            "do not retry until the committed state has been verified"
        ) from exc


def execute_approved_action(
    conn: sqlite3.Connection, action_id: str, *, approved_by: str | None = None
) -> ActionIntent:
    """Execute an approved action using its registered deterministic handler."""
    intent = get_action_intent(conn, action_id)
    if intent is None:
        raise ValueError(f"Action intent {action_id} not found")
    if intent.action_type == "email_send":
        return _execute_email_send(conn, intent, approved_by=approved_by)
    if intent.action_type in {"calendar_create", "calendar_update", "calendar_cancel"}:
        return _execute_calendar_action(conn, intent)
    if intent.action_type == "assistant_project_update":
        return _execute_assistant_project_update(conn, intent)
    if intent.action_type == PROJECT_COMPLETION_ACTION:
        return _execute_project_completion(conn, intent)
    raise ValueError(f"Unsupported action type: {intent.action_type}")


def _execute_email_send(
    conn: sqlite3.Connection, intent: ActionIntent, *, approved_by: str | None = None
) -> ActionIntent:
    """Commit the approved delivery intent before calling the email provider.

    A caller-owned transaction cannot safely be sent here: dispatch must not
    commit unrelated caller edits. When an approver is supplied, approval and
    the action/draft/outbox intent share this short transaction.
    """
    if conn.in_transaction:
        raise ValueError(
            "Email execution requires a committed approval and no caller-owned "
            "transaction; commit or roll back pending edits before dispatch"
        )
    draft_id = intent.target_id
    email_config = email_config_dict(CONFIG)
    conn.execute("BEGIN IMMEDIATE")
    try:
        draft = get_draft(conn, draft_id)
        if draft is None:
            raise ValueError(f"Draft {draft_id} not found")
        if approved_by is not None:
            intent = approve_action_intent(conn, intent.action_id, approved_by=approved_by)
        payload, version = email_send_payload(conn, draft_id)
        begin_action_execution(
            conn,
            intent.action_id,
            current_payload=payload,
            current_target_version=version,
        )
        conn.execute("SAVEPOINT approved_email_queue")
        try:
            if draft["status"] in ("created", "send_failed"):
                update_draft_status(conn, draft_id, "in_review", commit=False)
            current = get_draft(conn, draft_id)
            if current and current["status"] == "in_review":
                update_draft_status(conn, draft_id, "approved", commit=False)
            queued, rejection = queue_approved_draft_send(
                conn,
                draft_id,
                email_config,
                actor_user_id=intent.operator_id,
                action_intent=intent.model_dump(),
                commit=False,
            )
            if rejection is not None:
                raise ValueError(rejection.error or "Draft delivery intent was rejected")
            assert queued is not None
            if (queued["snapshot"].get("outbox_meta") or {}).get("action_id") != intent.action_id:
                raise ValueError("Existing delivery intent belongs to another approval")
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT approved_email_queue")
            conn.execute("RELEASE SAVEPOINT approved_email_queue")
            failed = fail_action_execution(conn, intent.action_id, error=str(exc))
            _commit_email_approval(conn)
            return failed
        conn.execute("RELEASE SAVEPOINT approved_email_queue")
        _commit_email_approval(conn)
    except ActionCommitUncertain:
        raise
    except Exception:
        # Persist stale/expired approval invalidation, but not a partial queue.
        if conn.in_transaction:
            if (current_intent := get_action_intent(conn, intent.action_id)) and current_intent.status in {"invalidated", "expired"}:
                _commit_email_approval(conn)
            else:
                conn.rollback()
        raise

    # Claim/delivery runs only after the action, draft and immutable outbox are
    # committed. The shared receipt command quarantines an ambiguous inline
    # claim before action bookkeeping reads the durable outbox state.
    dispatch_committed_draft_approval(
        conn,
        DraftApprovalReceipt("queued", outbox_id=queued["outbox_id"]),
        email_config=email_config,
        imap_config=imap_config_dict(CONFIG),
    )
    # The provider call's return is not the authoritative receipt: another
    # worker may have claimed this same committed intent in the meantime.
    # In-flight work stays executing until its durable terminal outbox state
    # can be reconciled; a transient "claimed by another worker" is not failure.
    receipt = conn.execute(
        "SELECT status, provider_message_id, last_error FROM draft_outbox "
        "WHERE outbox_id=?", (queued["outbox_id"],),
    ).fetchone()
    if receipt is None:
        raise RuntimeError("Committed email delivery intent disappeared")
    current_action = get_action_intent(conn, intent.action_id)
    assert current_action is not None
    if current_action.status != "executing":
        # A worker may have reconciled the committed receipt while the inline
        # caller was returning from dispatch. Never apply a second transition.
        if current_action.status == {
            "sent": "executed", "uncertain": "uncertain", "cancelled": "uncertain",
            "failed": "failed", "needs_attention": "failed",
        }.get(receipt["status"]):
            return current_action
        raise RuntimeError("Action and delivery receipts have conflicting terminal states")
    if receipt["status"] in {"pending", "undoable", "scheduled", "sending"}:
        return current_action
    if receipt["status"] == "sent":
        completed = complete_action_execution(
            conn, intent.action_id, result_ref=receipt["provider_message_id"],
        )
    elif receipt["status"] in {"uncertain", "cancelled"}:
        update_action_status(
            conn,
            intent.action_id,
            "uncertain",
            error=receipt["last_error"] or "Delivery outcome unconfirmed; manual reconciliation required",
        )
        completed = get_action_intent(conn, intent.action_id)
        assert completed is not None
    elif receipt["status"] in {"failed", "needs_attention"}:
        completed = fail_action_execution(
            conn, intent.action_id, error=receipt["last_error"] or "Email send failed",
        )
    else:
        raise RuntimeError(f"Unexpected email delivery state: {receipt['status']}")
    conn.commit()
    return completed



def _execute_project_completion(
    conn: sqlite3.Connection, intent: ActionIntent
) -> ActionIntent:
    """Complete an M8 project after a version-bound Approval Center sign-off."""
    from src.write_layer.write_helpers import safe_write_project

    project_id = intent.target_id
    row = get_project_completion_row(conn, project_id)
    if row is None:
        update_action_status(
            conn,
            intent.action_id,
            "invalidated",
            error=f"Project {project_id} no longer exists",
        )
        conn.commit()
        raise ValueError(f"Project {project_id} not found")

    try:
        begin_action_execution(
            conn,
            intent.action_id,
            current_payload=build_project_completion_payload(row),
            current_target_version=str(row["updated_at"]),
        )
    except Exception:
        # Persist stale/expired approval state before the API returns a conflict.
        conn.commit()
        raise

    try:
        result = safe_write_project(
            conn,
            project_id,
            {
                "project_status": "Completed",
                "ball_with": "Internal",
                "next_action": "",
            },
            actor=intent.approved_by or intent.operator_id,
            operator_confirmed=True,
            reason="Completion confirmed in Approval Center",
            surface="dashboard",
            commit=False,
            publish_event=False,
            completion_approval_authorized=True,
        )
        if result.outcome not in {"accepted", "no_change"}:
            error = result.rejection_reason or result.needs_review_reason or result.outcome
            raise ValueError(f"Project completion was not applied: {error}")

        completed = complete_action_execution(
            conn,
            intent.action_id,
            result_ref=project_id,
        )
        conn.commit()
    except Exception as exc:
        # Keep project completion and approval status atomic. If the write layer
        # rolled back, record a deterministic failed action in a fresh transaction.
        conn.rollback()
        current = get_action_intent(conn, intent.action_id)
        if current is not None and current.status != "invalidated":
            update_action_status(
                conn,
                intent.action_id,
                "failed",
                approved_by=intent.approved_by,
                approved_at=intent.approved_at,
                error=str(exc),
            )
            conn.commit()
        raise

    event_bus.publish(
        Event(
            event_type="project_updated",
            project_id=project_id,
            detail={"fields": ["project_status", "ball_with", "next_action"]},
            surface="dashboard",
        )
    )
    return completed


def _execute_assistant_project_update(
    conn: sqlite3.Connection, intent: ActionIntent
) -> ActionIntent:
    """Apply an approved AI Workspace project update through the safe write layer."""
    from src.write_layer.write_helpers import safe_write_project

    project_id = str(intent.payload.get("project_id") or intent.target_id)
    fields = intent.payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("Assistant project update payload has no fields")
    begin_action_execution(
        conn,
        intent.action_id,
        current_payload=intent.payload,
        current_target_version=None,
    )
    try:
        result = safe_write_project(
            conn,
            project_id,
            dict(fields),
            actor="operator",
            operator_confirmed=True,
            evidence_ref=intent.payload.get("evidence_ref"),
            reason=str(intent.payload.get("reason") or "Approved AI Workspace action"),
            surface="ai_workspace",
        )
        if result.outcome not in {"accepted", "no_change"}:
            error = result.rejection_reason or result.needs_review_reason or result.outcome
            fail_action_execution(conn, intent.action_id, error=str(error))
            raise ValueError(f"Project update was not applied: {error}")
        return complete_action_execution(
            conn,
            intent.action_id,
            result_ref=project_id,
        )
    except Exception as exc:
        current = get_action_intent(conn, intent.action_id)
        if current is not None and current.status == "executing":
            fail_action_execution(conn, intent.action_id, error=str(exc))
        raise

def _execute_calendar_action(
    conn: sqlite3.Connection, intent: ActionIntent
) -> ActionIntent:
    if not (
        CONFIG.personal_assistant.enabled
        and CONFIG.personal_assistant.calendar_write_enabled
        and CONFIG.msgraph.enabled
        and CONFIG.msgraph.calendar_enabled
    ):
        raise ValueError("Calendar write is disabled")
    from src.email.graph_client import GraphClient
    from src.scheduling.calendar_sync import normalize_graph_event
    from src.repositories.calendar_event_repo import upsert_calendar_event
    from datetime import datetime, timezone

    graph = GraphClient(
        tenant_id=CONFIG.msgraph.tenant_id,
        client_id=CONFIG.msgraph.client_id,
        client_secret=CONFIG.msgraph.client_secret,
        user_email=CONFIG.msgraph.user_email,
    )
    operation = intent.payload.get("operation")
    calendar_id = intent.payload.get("calendar_id") or CONFIG.msgraph.calendar_id
    current_version = None
    if operation in {"update", "cancel"}:
        current = graph.fetch_calendar_event(
            calendar_id=calendar_id,
            event_id=intent.target_id,
        )
        current_version = _event_version(
            current.get("@odata.etag"),
            current.get("lastModifiedDateTime"),
        )
    begin_action_execution(
        conn,
        intent.action_id,
        current_payload=intent.payload,
        current_target_version=current_version,
    )
    try:
        if operation == "create":
            result = graph.create_calendar_event(
                calendar_id=calendar_id,
                payload=intent.payload["event"],
            )
            event_id = result.get("id") or intent.target_id
            if result.get("id"):
                upsert_calendar_event(conn, normalize_graph_event(
                    result,
                    operator_id=intent.operator_id,
                    calendar_id=calendar_id,
                    synced_at=datetime.now(timezone.utc).isoformat(),
                ))
        elif operation == "update":
            result = graph.update_calendar_event(
                calendar_id=calendar_id,
                event_id=intent.target_id,
                payload=intent.payload["event"],
            )
            event_id = intent.target_id
            if result.get("id"):
                upsert_calendar_event(conn, normalize_graph_event(
                    result,
                    operator_id=intent.operator_id,
                    calendar_id=calendar_id,
                    synced_at=datetime.now(timezone.utc).isoformat(),
                ))
        elif operation == "cancel":
            graph.cancel_calendar_event(
                calendar_id=calendar_id,
                event_id=intent.target_id,
            )
            conn.execute(
                """UPDATE calendar_events
                   SET cancelled = 1, synced_at = ?
                   WHERE calendar_event_id = ? AND operator_id = ?""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    intent.target_id,
                    intent.operator_id,
                ),
            )
            event_id = intent.target_id
        else:
            raise ValueError(f"Unsupported calendar operation: {operation}")
        conn.commit()
        return complete_action_execution(
            conn,
            intent.action_id,
            result_ref=event_id,
        )
    except Exception as exc:
        fail_action_execution(conn, intent.action_id, error=str(exc))
        raise
