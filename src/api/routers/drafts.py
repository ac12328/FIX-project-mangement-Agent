"""Draft queue endpoints."""

from __future__ import annotations

import logging
import sqlite3
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_read_conn, get_write_conn
from src.api.schemas.common import ActionResult
from src.api.schemas.drafts import DraftResponse, DraftActionRequest, DraftActionResult
from src.email.draft_session import (
    create_draft,
    edit_draft,
    get_draft,
    get_pending_drafts,
    update_draft_status,
)
from src.events.bus import Event, event_bus

if TYPE_CHECKING:
    from src.services.draft_approval_command import DraftApprovalReceipt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drafts"])

# Production write scopes start BEGIN IMMEDIATE before yielding. Record their
# empty starting point to distinguish them from caller-owned dirty transactions.
_approval_scopes: dict[int, tuple[sqlite3.Connection, int]] = {}
_approval_scopes_lock = Lock()


class DraftEditRequest(BaseModel):
    body: str | None = None
    to_email: str | None = None
    cc: str | None = None


class DraftCreateRequest(BaseModel):
    """Body for POST /projects/{project_id}/drafts (Dashboard project page)."""
    draft_body: str
    subject: str | None = None
    task_type: str = "draft_email"


class StandaloneDraftCreateRequest(BaseModel):
    context: str
    to_email: str | None = None
    contact_id: str | None = None
    contact_name: str | None = None
    subject: str | None = None
    body: str | None = None
    cc: str | None = None
    purpose: str = "personal_email"
    related_project_id: str | None = None
    scheduled_send_at: str | None = None


@router.post("/drafts")
def create_standalone_draft(
    body: StandaloneDraftCreateRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Create a project-independent email draft for operator review."""
    from config import defaults
    from src.communications.compose_service import compose_email_draft

    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_UNIVERSAL_EMAIL_ENABLED
    ):
        raise HTTPException(404, "Universal email drafting is disabled")
    try:
        result = compose_email_draft(conn, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {
        "draft_id": result.draft_id,
        "to_email": result.to_email,
        "subject": result.subject,
        "contact_id": result.contact_id,
    }


@router.get("/drafts", response_model=list[DraftResponse])
def list_pending_drafts(conn: sqlite3.Connection = Depends(get_read_conn)):
    rows = get_pending_drafts(conn)
    return [DraftResponse(**r) for r in rows]


@router.get("/drafts/{draft_id}", response_model=DraftResponse)
def get_draft_detail(
    draft_id: str,
    conn: sqlite3.Connection = Depends(get_read_conn),
):
    draft = get_draft(conn, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    data = dict(draft)
    context_ref = data.get("context_packet_ref")
    if context_ref:
        row = conn.execute(
            "SELECT subject, sender_email, cc FROM processed_emails WHERE email_id = ?",
            (context_ref,),
        ).fetchone()
        if row:
            data["thread_subject"] = row["subject"]
            data["thread_sender"] = row["sender_email"]
            # Only inherit thread CC when replying to the same person
            draft_to = data.get("to_email") or ""
            thread_sender = row["sender_email"] or ""
            if draft_to.lower() == thread_sender.lower():
                data["thread_cc"] = row["cc"]
    return DraftResponse(**data)


@router.patch("/drafts/{draft_id}", response_model=DraftResponse)
def update_draft_body(
    draft_id: str,
    body: DraftEditRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Edit the body, to_email, and/or cc of a pending draft."""
    draft = get_draft(conn, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft["status"] in ("sent", "approved", "discarded"):
        raise HTTPException(422, "Cannot edit a draft in terminal state")

    # Validate all requested changes before writing anything. PATCH is
    # idempotent: sending the currently displayed values is a successful no-op.
    updates = {}
    if (
        body.to_email is not None
        and body.to_email != (draft.get("to_email") or "")
    ):
        from src.email.draft_safety import (
            is_standalone_trusted_internal_handoff,
            validate_recipient_for_project,
        )

        ok, error, _basis = validate_recipient_for_project(
            conn,
            project_id=draft.get("project_id"),
            to_email=body.to_email,
            allow_trusted_internal_handoff=is_standalone_trusted_internal_handoff(
                draft,
                to_email=body.to_email,
            ),
        )
        if not ok:
            raise HTTPException(422, error)
        updates["to_email"] = body.to_email
        context_ref = draft.get("context_packet_ref")
        if context_ref:
            source = conn.execute(
                "SELECT sender_email FROM processed_emails WHERE email_id = ?",
                (context_ref,),
            ).fetchone()
            source_email = (source["sender_email"] if source else "") or ""
            if source_email.strip().lower() != body.to_email.strip().lower():
                updates["context_packet_ref"] = None
                updates["cc"] = None
    if body.cc is not None and body.cc != (draft.get("cc") or ""):
        updates["cc"] = body.cc

    body_changed = (
        body.body is not None
        and body.body != (draft.get("current_draft_text") or "")
    )
    if body_changed:
        ok = edit_draft(conn, draft_id, new_body=body.body, commit=False)
        if not ok:
            raise HTTPException(422, "Could not update draft body")

    if updates:
        from config.settings import CONFIG
        from src.email.draft_workspace import (
            build_workspace_snapshot,
            initialize_draft_workspace,
            patch_workspace,
        )

        initialize_draft_workspace(
            conn,
            draft_id,
            actor_user_id=CONFIG.operator_id,
            commit=False,
        )
        snapshot = build_workspace_snapshot(conn, draft_id)
        envelope_patch = {}
        if "to_email" in updates:
            envelope_patch["to"] = updates["to_email"]
        if "cc" in updates:
            envelope_patch["cc"] = updates["cc"] or []
        workspace_patch = {"envelope": envelope_patch}
        if "context_packet_ref" in updates and updates["context_packet_ref"] is None:
            workspace_patch["delivery_context"] = {
                "delivery_mode": "new",
                "reply_to_email_id": None,
            }
        patch_workspace(
            conn,
            draft_id,
            workspace_patch,
            expected_revision=int(snapshot["revision"]["number"]),
            actor_user_id=CONFIG.operator_id,
            commit=False,
        )
        if "context_packet_ref" in updates:
            conn.execute(
                "UPDATE drafts SET context_packet_ref=? WHERE draft_id=?",
                (updates["context_packet_ref"], draft_id),
            )
    if body_changed or updates:
        conn.commit()

    updated = get_draft(conn, draft_id)
    return DraftResponse(**updated)


@router.post("/projects/{project_id}/drafts")
def create_project_draft(
    project_id: str,
    body: DraftCreateRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    """Create a draft scoped to a project (Dashboard project page 'Review & Send').

    P0-4: the project page 'Review & Send' button POSTed /api/projects/{id}/drafts,
    which did not exist, so the 404 was silently swallowed and the button did
    nothing. This endpoint creates the draft and returns {draft_id} so the
    frontend can navigate to the review page. Reuses create_draft (which commits
    and publishes the draft_created SSE event).
    """
    row = conn.execute(
        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    subject = (body.subject or "").strip() or f"Follow-up ({project_id})"
    try:
        draft_id = create_draft(
            conn,
            project_id=project_id,
            task_type=body.task_type,
            subject=subject,
            body=body.draft_body,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"draft_id": draft_id}


def _get_write_conn():
    """Get a short-lived write connection. Separate from get_conn so tests
    can override just the action endpoint's write path."""
    from src.db.connection import scoped_write
    with scoped_write() as conn:
        with _approval_scopes_lock:
            _approval_scopes[id(conn)] = (conn, conn.total_changes)
        try:
            yield conn
        finally:
            with _approval_scopes_lock:
                _approval_scopes.pop(id(conn), None)


def _approval_writer_is_clean(conn: sqlite3.Connection) -> bool:
    with _approval_scopes_lock:
        scope = _approval_scopes.get(id(conn))
    if scope is not None and scope[0] is conn:
        return conn.total_changes == scope[1]
    return not conn.in_transaction


def _queue_compatibility_scheduled_send(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
    commit: bool = True,
) -> ActionResult:
    """Route legacy send-later requests through the immutable outbox."""
    from src.email.draft_workspace import (
        build_workspace_snapshot,
        compute_send_affecting_hash,
        initialize_draft_workspace,
    )
    from src.email.outbox import queue_send_later
    from src.email.send_gate import SendGateBlockedError

    current = get_draft(conn, draft_id)
    scheduled_for = current.get("scheduled_send_at") if current else None
    if not scheduled_for:
        return ActionResult(success=False, message="Draft has no scheduled send time")
    try:
        initialize_draft_workspace(
            conn,
            draft_id,
            actor_user_id=actor_user_id,
            commit=False,
        )
        snapshot = build_workspace_snapshot(conn, draft_id)
        queue_send_later(
            conn,
            draft_id,
            actor_user_id=actor_user_id,
            expected_revision=snapshot["revision"]["number"],
            expected_hash=compute_send_affecting_hash(snapshot),
            scheduled_for=scheduled_for,
            commit=False,
        )
        if commit:
            conn.commit()
    except SendGateBlockedError as exc:
        return ActionResult(
            success=False,
            message=f"Draft safety check failed: {exc.result.to_dict()}",
        )
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        return ActionResult(success=False, message=f"Draft scheduling failed: {exc}")
    return ActionResult(
        success=True,
        message=f"Draft approved and scheduled for {scheduled_for}",
    )


def _legacy_immediate_receipt_result(receipt: DraftApprovalReceipt) -> ActionResult:
    """Preserve the legacy HTTP outcome while using the shared send command."""
    if receipt.state == "sent":
        return ActionResult(success=True, message="Draft approved and sent")
    if receipt.state == "queued":
        return ActionResult(success=False, message="Draft delivery queued; send not yet confirmed")
    if receipt.state == "in_progress":
        return ActionResult(success=False, message="Draft delivery in progress; do not retry")
    if receipt.state == "uncertain":
        return ActionResult(
            success=False, message="Delivery outcome unconfirmed; manual reconciliation required",
        )
    if receipt.state == "failed":
        return ActionResult(
            success=False, message=f"Draft approved but send failed: {receipt.error}",
        )
    return ActionResult(success=False, message=f"Draft send failed: {receipt.error}")


def _legacy_approve_and_send(
    wconn: sqlite3.Connection,
    draft_id: str,
    *,
    actor_user_id: str,
) -> ActionResult:
    """Commit approval and immutable outbox intent before synchronous dispatch."""
    from config.settings import CONFIG
    from src.email.draft_send_command import queue_approved_draft_send
    from src.email.sending import email_config_dict, imap_config_dict
    from src.services.draft_approval_command import (
        DraftApprovalReceipt, commit_immediate_draft_approval,
        dispatch_committed_draft_approval,
    )

    if not _approval_writer_is_clean(wconn):
        return ActionResult(
            success=False,
            message="Approval requires a clean write scope; uncommitted caller edits were not sent",
        )

    # Neither an accepted delivery nor an unknown outcome is a new command.
    previous = wconn.execute(
        "SELECT status FROM draft_outbox WHERE draft_id=? ORDER BY created_at DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    if previous and previous["status"] in ("sent", "sending", "uncertain", "needs_attention"):
        status = previous["status"]
        return ActionResult(
            success=False,
            message=(
                "Draft already sent" if status == "sent" else
                "Draft needs review before another send" if status == "needs_attention" else
                "Delivery outcome unconfirmed; manual reconciliation required"
            ),
        )

    try:
        email_config = email_config_dict(CONFIG)
        current = get_draft(wconn, draft_id)
        if (current and current["status"] in {"created", "in_review"}
                and not current.get("scheduled_send_at")):
            receipt = commit_immediate_draft_approval(
                wconn, draft_id, actor_user_id=actor_user_id,
                email_config=email_config, scope_start_changes=wconn.total_changes,
            )
            receipt = dispatch_committed_draft_approval(
                wconn, receipt, email_config=email_config,
                imap_config=imap_config_dict(CONFIG),
            )
            return _legacy_immediate_receipt_result(receipt)
        if not wconn.in_transaction:
            wconn.execute("BEGIN IMMEDIATE")
        committing = False
        try:
            current = get_draft(wconn, draft_id)
            if current and current["status"] in ("created", "send_failed"):
                update_draft_status(wconn, draft_id, "in_review", commit=False)
            current = get_draft(wconn, draft_id)
            if current and current["status"] == "in_review":
                update_draft_status(wconn, draft_id, "approved", commit=False)
                current = get_draft(wconn, draft_id)
            if not current or current["status"] != "approved":
                wconn.rollback()
                return ActionResult(success=False, message="Invalid status transition")
            if current.get("scheduled_send_at"):
                scheduled = _queue_compatibility_scheduled_send(
                    wconn, draft_id, actor_user_id=actor_user_id, commit=False,
                )
                if not scheduled.success:
                    wconn.rollback()
                    return scheduled
                committing = True
                wconn.commit()
                return scheduled

            queued, rejection = queue_approved_draft_send(
                wconn, draft_id, email_config, actor_user_id=actor_user_id,
                commit=False,
            )
            if rejection is not None:
                wconn.rollback()
                return ActionResult(success=False, message=f"Draft send failed: {rejection.error}")
            assert queued is not None
            # The approval, snapshot and outbox must all survive this commit.
            committing = True
            wconn.commit()
        except (sqlite3.Error, ValueError, RuntimeError) as exc:
            if wconn.in_transaction:
                wconn.rollback()
            if committing:
                logger.exception("Draft approval commit acknowledgement failed: %s", draft_id)
                return ActionResult(
                    success=False,
                    message="Approval/queue outcome unconfirmed; manual reconciliation required",
                )
            return ActionResult(success=False, message=f"Draft approval failed: {exc}")

        if queued["status"] == "uncertain":
            return ActionResult(
                success=False,
                message="Delivery outcome unconfirmed; manual reconciliation required",
            )
        receipt = dispatch_committed_draft_approval(
            wconn, DraftApprovalReceipt("queued", outbox_id=queued["outbox_id"]),
            email_config=email_config, imap_config=imap_config_dict(CONFIG),
        )
        return _legacy_immediate_receipt_result(receipt)
    finally:
        # A bulk request may issue another independent command in this scope.
        # The previous item must have ended its transaction before that happens.
        if not wconn.in_transaction:
            with _approval_scopes_lock:
                scope = _approval_scopes.get(id(wconn))
                if scope is not None and scope[0] is wconn:
                    _approval_scopes[id(wconn)] = (wconn, wconn.total_changes)


@router.post(
    "/drafts/{draft_id}/action",
    response_model=DraftActionResult,
    response_model_exclude_none=True,
)
def draft_action(
    draft_id: str,
    body: DraftActionRequest,
    actor: CurrentActor = Depends(get_current_actor),
    rconn: sqlite3.Connection = Depends(get_read_conn),
    wconn: sqlite3.Connection = Depends(_get_write_conn),
):
    """Approve+send or discard a draft.

    Uses a read connection for the initial lookup and a separate write
    connection for status transitions + sending, to minimise the time
    the write lock is held.
    """
    # Read phase ??no write lock needed
    draft = get_draft(rconn, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")

    if body.action == "approve":
        from config import defaults

        if not _approval_writer_is_clean(wconn):
            return ActionResult(
                success=False,
                message="Approval requires a clean write scope; uncommitted caller edits were not sent",
            )
        if not defaults.PERSONAL_ASSISTANT_ENABLED:
            return _legacy_approve_and_send(wconn, draft_id, actor_user_id=actor.user_id)

        from src.governance.email_actions import create_email_send_intent

        try:
            current = get_draft(wconn, draft_id)
            if current and current.get("scheduled_send_at"):
                return _legacy_approve_and_send(wconn, draft_id, actor_user_id=actor.user_id)
            if current and current["status"] in ("created", "send_failed"):
                update_draft_status(wconn, draft_id, "in_review")
            intent = create_email_send_intent(
                wconn,
                draft_id,
                operator_id=actor.user_id,
                surface="dashboard",
            )
            wconn.commit()
        except sqlite3.IntegrityError:
            row = wconn.execute(
                """SELECT action_id FROM action_intents
                   WHERE idempotency_key = ?""",
                (f"email_send:{draft_id}:v{draft.get('draft_version', 1)}",),
            ).fetchone()
            if not row:
                raise
            intent_id = row["action_id"]
        else:
            intent_id = intent.action_id

        event_bus.publish(Event(
            event_type="draft_approval_requested",
            project_id=draft.get("project_id"),
            detail={"draft_id": draft_id, "action_id": intent_id},
            surface="dashboard",
        ))
        return DraftActionResult(
            success=True,
            message=f"Draft queued for approval ({intent_id})",
            action_id=intent_id,
        )

    if body.action == "send":
        if not body.action_id:
            raise HTTPException(422, "action_id is required to send a draft")
        from src.governance.action_executor import (
            ActionCommitUncertain, execute_approved_action,
        )
        from src.repositories.action_intent_repo import get_action_intent

        if not _approval_writer_is_clean(wconn):
            raise HTTPException(409, "Send requires a clean write scope; uncommitted edits were not sent")

        intent = get_action_intent(wconn, body.action_id)
        if not intent or intent.action_type != "email_send" or intent.target_id != draft_id:
            raise HTTPException(409, "Approved email action does not match this draft")
        try:
            # The API owns this write scope. End its read transaction before
            # the email command starts its independent, durable queue intent.
            wconn.commit()
            executed = execute_approved_action(wconn, body.action_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ActionCommitUncertain as exc:
            return ActionResult(success=False, message=str(exc))

        if executed.status == "executing":
            return ActionResult(success=False, message="Draft delivery is in progress; do not retry")
        if executed.status == "executed":
            event_bus.publish(Event(
                event_type="draft_approved",
                project_id=draft.get("project_id"),
                detail={"draft_id": draft_id, "action_id": body.action_id},
                surface="dashboard",
            ))
            return ActionResult(success=True, message="Draft approved and sent")
        delivery = get_draft(wconn, draft_id)
        if delivery and delivery.get("delivery_status") == "uncertain":
            return ActionResult(
                success=False,
                message=f"Delivery outcome unconfirmed; manual reconciliation required: {executed.error}",
            )
        return ActionResult(success=False, message=f"Draft send failed: {executed.error}")

    if body.action == "discard":
        try:
            ok = update_draft_status(wconn, draft_id, "discarded")
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                return ActionResult(success=False, message="Database busy ??please try again in a few seconds.")
            raise

        if ok:
            event_bus.publish(Event(
                event_type="draft_discarded",
                project_id=draft.get("project_id"),
                detail={"draft_id": draft_id},
                surface="dashboard",
            ))
        return ActionResult(success=ok, message="Draft discarded" if ok else "Invalid transition")

    raise HTTPException(400, f"Unknown action: {body.action}")


class BulkDraftActionRequest(BaseModel):
    draft_ids: list[str] = Field(min_length=1, max_length=50)
    action: str  # "approve" | "discard"


@router.post("/drafts/bulk-action")
def bulk_draft_action(
    body: BulkDraftActionRequest,
    actor: CurrentActor = Depends(get_current_actor),
    rconn: sqlite3.Connection = Depends(get_read_conn),
    wconn: sqlite3.Connection = Depends(_get_write_conn),
):
    """Apply an action (approve+send or discard) to multiple drafts at once."""
    if body.action not in ("approve", "discard"):
        raise HTTPException(400, f"Unknown action: {body.action}")

    results: list[dict] = []

    for draft_id in body.draft_ids:
        try:
            draft = get_draft(rconn, draft_id)
            if not draft:
                results.append({"draft_id": draft_id, "success": False, "message": "Draft not found"})
                continue

            if body.action == "discard":
                ok = update_draft_status(wconn, draft_id, "discarded")
                if ok:
                    event_bus.publish(Event(
                        event_type="draft_discarded",
                        project_id=draft.get("project_id"),
                        detail={"draft_id": draft_id},
                        surface="dashboard",
                    ))
                results.append({"draft_id": draft_id, "success": ok, "message": "Draft discarded" if ok else "Invalid status transition"})

            elif body.action == "approve":
                from config.settings import CONFIG
                from src.email.sending import email_config_dict

                email_config = email_config_dict(CONFIG)
                if email_config.get("backend") == "smtp" and not email_config.get("smtp_host"):
                    results.append({"draft_id": draft_id, "success": False, "message": "SMTP not configured"})
                    continue

                result = _legacy_approve_and_send(
                    wconn, draft_id, actor_user_id=actor.user_id,
                )
                if result.success and "sent" in result.message:
                    event_bus.publish(Event(
                        event_type="draft_approved",
                        project_id=draft.get("project_id"),
                        detail={"draft_id": draft_id},
                        surface="dashboard",
                    ))
                results.append({
                    "draft_id": draft_id,
                    "success": result.success,
                    "message": result.message,
                })

        except Exception as exc:
            # A failed dispatch can leave an uncommitted provider receipt on
            # this shared writer. Never let the next item or scope exit commit it.
            if wconn.in_transaction:
                wconn.execute("ROLLBACK")
            logger.warning("bulk_draft_action: error processing %s: %s", draft_id, exc)
            results.append({"draft_id": draft_id, "success": False, "message": str(exc)})

    return {"results": results}
