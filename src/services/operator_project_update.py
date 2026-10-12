"""Stage / confirm / cancel Workspace operator project updates."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from src.db.savepoint import savepoint
from src.events.bus import Event, event_bus
from src.models.pending_operation import PendingOperation
from src.repositories.activity_log_repo import insert_activity
from src.repositories.assistant_repo import (
    bind_operator_update_part,
    patch_operator_update_part,
)
from src.repositories.evidence_repo import insert_evidence
from src.repositories.pending_operation_repo import (
    clear_request_key,
    get_operation,
    list_pending_operations,
    resolve_operation,
)
from src.services import operator_update_metrics as metrics
from src.services.operator_update_classifier import (
    WorkspaceWriteIntent,
    classify_workspace_operator_write,
)
from src.services.operator_update_models import (
    OVERRIDE_PREFIX,
    OperatorUpdateError,
    OperatorUpdateProposal,
    audit_reason,
    canonical_evidence_ref,
    is_usable_override_reason,
    request_key,
    require_bound_actor,
    require_flag,
)
from src.services.operator_update_preview import preview_operator_update
from src.services.project_update_commands import (
    cancel_project_update_command,
    confirm_project_update_command,
    stage_project_update_command,
)

logger = logging.getLogger("agentfixbpro.operator_update")


class OperatorUpdateTerminalError(OperatorUpdateError):
    """A terminal operation and Workspace part await the caller's commit.

    ``conflict_operation_id`` is set only for a pending-to-superseded
    transition. The caller records its metric explicitly after checking the
    durable state, including when commit raises after actually committing.
    """

    def __init__(
        self, code: str, message: str, status: str, *,
        conflict_operation_id: str | None = None,
    ):
        super().__init__(code, message)
        self.status = status
        self.conflict_operation_id = conflict_operation_id
        self._conflict_recorded = False


def record_committed_terminal_conflict(
    conn: sqlite3.Connection, error: OperatorUpdateTerminalError,
) -> None:
    """Count a conflict only when its newly terminal receipt is durable.

    In particular, an exception from commit alone proves neither success nor
    rollback. Reading the resulting row after the transaction ends handles a
    commit-then-raise wrapper without counting an uncommitted transition.
    """
    if error.conflict_operation_id is None or error._conflict_recorded:
        return
    try:
        if conn.in_transaction:
            return
        row = conn.execute(
            "SELECT status FROM pending_operations WHERE operation_id=?",
            (error.conflict_operation_id,),
        ).fetchone()
        if row is None or row[0] != "superseded":
            return
        metrics.increment("workspace_operator_update_conflict")
        error._conflict_recorded = True
    except Exception:
        logger.exception("Failed to record committed Workspace conflict metric")


__all__ = [
    "OperatorUpdateError",
    "OperatorUpdateProposal",
    "OperatorUpdateTerminalError",
    "record_committed_terminal_conflict",
    "WorkspaceWriteIntent",
    "cancel_operator_update",
    "classify_workspace_operator_write",
    "confirm_operator_update",
    "get_operator_update_for_actor",
    "is_usable_override_reason",
    "preview_operator_update",
    "stage_operator_update",
]


def _require_workspace_thread_owner(
    conn: sqlite3.Connection, *, thread_id: str, actor: str,
) -> None:
    """Do not attach an operation to another operator's existing thread.

    Older direct callers may stage against an as-yet uncreated thread; preserve
    that compatibility, but never modify a thread owned by a different actor.
    """
    row = conn.execute(
        "SELECT owner_user_id FROM assistant_threads WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    if row is not None and row["owner_user_id"] != actor:
        raise OperatorUpdateError(
            "permission_required", "Workspace thread belongs to another operator.",
        )



def get_operator_update_for_actor(
    conn: sqlite3.Connection, *, project_id: str, operation_id: str, actor: str,
) -> PendingOperation:
    """Read a staged card only for its bound operator and current thread owner."""
    require_bound_actor(conn, actor)
    operation = get_operation(conn, operation_id)
    if (
        operation is None
        or operation.project_id != project_id
        or operation.operation_type != "project_update"
    ):
        raise OperatorUpdateError(
            "pending_operation_not_found", "Operation was not found.",
        )
    if operation.operator_id != actor:
        raise OperatorUpdateError(
            "permission_required", "Operation belongs to another operator.",
        )
    _require_workspace_thread_owner(
        conn, thread_id=str(operation.chat_id), actor=actor,
    )
    return operation


def stage_operator_update(
    conn: sqlite3.Connection,
    *,
    actor: str,
    thread_id: str,
    proposal: OperatorUpdateProposal,
    reason: str,
    evidence_ref: str | None,
    post_commit: list[Callable[[], None]] | None = None,
) -> PendingOperation:
    require_flag()
    require_bound_actor(conn, actor)
    _require_workspace_thread_owner(conn, thread_id=thread_id, actor=actor)
    if proposal.kind == "operator_override" and not is_usable_override_reason(reason):
        raise OperatorUpdateError(
            "reason_required",
            "Override requires a specific reason (at least 20 characters).",
        )
    resolved_ref = (
        canonical_evidence_ref(conn, proposal.project_id, evidence_ref)
        if evidence_ref
        else proposal.evidence_ref
    )
    live = conn.execute(
        "SELECT updated_at FROM projects WHERE project_id=?",
        (proposal.project_id,),
    ).fetchone()
    base_version = str(live["updated_at"]) if live else proposal.base_version
    to_milestone = proposal.fields["current_milestone"]
    key = request_key(actor, thread_id, proposal.project_id, to_milestone, base_version)
    payload = {
        "fields": dict(proposal.fields),
        "old_values": {
            field: proposal.current_values.get(field) for field in proposal.fields
        },
        "reason": audit_reason(proposal.kind, reason),
        "evidence_ref": resolved_ref,
        "surface": "ai_workspace",
        "kind": proposal.kind,
        "transition": proposal.transition,
        "assessment_status": proposal.assessment_status,
        "missing_criteria": proposal.missing_criteria,
        "policy_version": proposal.policy_version,
        "decision": proposal.decision,
        "candidate_value": proposal.candidate_value,
        "next_action_source": "operator" if "next_action" in proposal.fields else None,
        "thread_id": thread_id,
        "high_risk": proposal.high_risk,
    }
    with savepoint(conn, ensure_transaction=True):
        previous = list_pending_operations(
            conn, operator_id=actor, chat_id=thread_id,
            project_id=proposal.project_id, operation_type="project_update",
            include_expired=True,
        )
        operation = stage_project_update_command(
            conn,
            operator_id=actor,
            chat_id=thread_id,
            project_id=proposal.project_id,
            payload=payload,
            base_version=base_version,
            request_key=key,
        )
        for old in previous:
            if old.operation_id == operation.operation_id:
                continue
            resolved = get_operation(conn, old.operation_id)
            if resolved is not None and resolved.status in {"superseded", "expired"}:
                patch_operator_update_part(
                    conn, thread_id=thread_id, operation_id=old.operation_id,
                    status=resolved.status, record_receipt=False,
                )
        bind_operator_update_part(
            conn,
            thread_id=thread_id,
            project_id=proposal.project_id,
            operation_id=operation.operation_id,
            status="pending",
            payload={
                "kind": proposal.kind,
                "fields": proposal.fields,
                "missing_criteria": proposal.missing_criteria,
                "reason": payload["reason"],
                "transition": proposal.transition,
                "base_version": operation.base_version,
                "expires_at": operation.expires_at,
                "high_risk": proposal.high_risk,
            },
        )
    # The caller owns the outer commit. Never count a savepoint that can still
    # be rolled back; direct connection callers without an owner omit the metric.
    if post_commit is not None:
        post_commit.append(lambda: _record_committed_stage(
            project_id=proposal.project_id,
            operation_id=operation.operation_id,
            actor=actor,
            kind=proposal.kind,
            transition=proposal.transition,
        ))
    return operation


def _record_committed_stage(
    *, project_id: str, operation_id: str, actor: str, kind: str, transition: str,
) -> None:
    try:
        metrics.increment(
            "workspace_operator_update_staged", kind=kind, transition=transition,
        )
    except Exception:
        logger.exception("Failed to record committed Workspace staging metric for %s", project_id)
    logger.info(
        "staged project_id=%s operation_id=%s actor=%s kind=%s",
        project_id, operation_id, actor, kind,
    )


def _prevalidate_confirm(
    conn: sqlite3.Connection,
    *,
    actor: str,
    thread_id: str,
    operation: PendingOperation,
    reason: str | None,
    evidence_ref: str | None,
) -> tuple[dict, str]:
    if operation.operation_type != "project_update":
        raise OperatorUpdateError(
            "pending_operation_not_found",
            "Operation is not a project update.",
        )
    if operation.status != "pending":
        raise OperatorUpdateError(
            "pending_operation_not_pending",
            f"Operation {operation.operation_id} is {operation.status}.",
        )
    if operation.operator_id != actor or str(operation.chat_id) != str(thread_id):
        raise OperatorUpdateError(
            "permission_required",
            "Only the proposing Editor can confirm this update.",
        )
    if operation.expires_at:
        now = datetime.now(timezone.utc).isoformat()
        if operation.expires_at <= now:
            resolve_operation(
                conn, operation.operation_id, status="expired", resolution="expired",
            )
            raise OperatorUpdateError(
                "pending_operation_not_pending",
                f"Operation {operation.operation_id} expired.",
            )
    payload = dict(operation.payload or {})
    project_id = operation.project_id or ""
    row = conn.execute(
        "SELECT updated_at FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise OperatorUpdateError("project_not_found", f"Project {project_id} not found.")
    if operation.base_version != row["updated_at"]:
        resolve_operation(
            conn, operation.operation_id,
            status="superseded", resolution="project changed after staging",
        )
        raise OperatorUpdateError(
            "pending_operation_conflict",
            f"{project_id} changed after this update was staged. Re-check and stage again.",
        )
    kind = payload.get("kind") or "operator_override"
    confirm_reason = (reason if reason is not None else payload.get("reason") or "")
    if str(confirm_reason).startswith(OVERRIDE_PREFIX):
        operator_text = str(confirm_reason)[len(OVERRIDE_PREFIX):]
    else:
        operator_text = str(confirm_reason)
    if kind == "operator_override" and not is_usable_override_reason(operator_text):
        raise OperatorUpdateError(
            "reason_required",
            "Override requires a specific reason (at least 20 characters).",
        )
    if reason is not None:
        payload["reason"] = audit_reason(kind, operator_text)
    resolved_ref = evidence_ref if evidence_ref is not None else payload.get("evidence_ref")
    if resolved_ref:
        payload["evidence_ref"] = canonical_evidence_ref(conn, project_id, str(resolved_ref))
    return payload, project_id


def confirm_operator_update(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: str,
    thread_id: str,
    operation_id: str,
    post_commit: list[Callable[[], None]],
    reason: str | None = None,
    evidence_ref: str | None = None,
) -> dict:
    """Stage one confirmation; the caller owns commit and post-commit effects."""
    require_flag()
    require_bound_actor(conn, actor)
    _require_workspace_thread_owner(conn, thread_id=thread_id, actor=actor)
    operation = get_operation(conn, operation_id)
    if operation is None:
        raise OperatorUpdateError(
            "pending_operation_not_found",
            f"Operation {operation_id} was not found.",
        )
    if operation.project_id != project_id or operation.operation_type != "project_update":
        raise OperatorUpdateError("pending_operation_not_found", "Operation project mismatch.")
    if operation.operator_id != actor or str(operation.chat_id) != str(thread_id):
        raise OperatorUpdateError("permission_required", "Operation owner or thread mismatch.")

    terminal_error: OperatorUpdateTerminalError | None = None
    with savepoint(conn, ensure_transaction=True):
        try:
            payload, project_id = _prevalidate_confirm(
                conn, actor=actor, thread_id=thread_id, operation=operation,
                reason=reason, evidence_ref=evidence_ref,
            )
        except OperatorUpdateError as exc:
            resolved = get_operation(conn, operation_id)
            if resolved is None or resolved.status not in {"superseded", "expired"}:
                raise
            patch_operator_update_part(
                conn, thread_id=thread_id, operation_id=operation_id,
                status=resolved.status, error=exc.message,
            )
            terminal_error = OperatorUpdateTerminalError(
                exc.code, exc.message, resolved.status,
                conflict_operation_id=(
                    operation_id
                    if exc.code == "pending_operation_conflict"
                    and operation.status == "pending"
                    and resolved.status == "superseded"
                    else None
                ),
            )

        if terminal_error is None:
            if not payload.get("evidence_ref"):
                note_id = insert_evidence(
                    conn,
                    project_id=project_id,
                    evidence_type="manual_note",
                    content_summary=payload.get("reason") or "Workspace operator override",
                    content_text=payload.get("reason") or "",
                    evidence_at=datetime.now(timezone.utc).isoformat(),
                    source_ref=f"operator-update:{operation_id}",
                )
                payload["evidence_ref"] = note_id
                conn.execute(
                    "UPDATE pending_operations SET payload_json=? WHERE operation_id=?",
                    (json.dumps(payload, ensure_ascii=False), operation_id),
                )

            command_result = confirm_project_update_command(
                conn,
                project_id=project_id,
                operator_id=actor,
                chat_id=thread_id,
                operation_id=operation_id,
                authority="owner",
                surface="ai_workspace",
                commit=False,
                publish_event=False,
            )
            if not command_result.ok:
                metrics.increment(
                    "workspace_operator_update_rejected",
                    code=str(command_result.error or command_result.outcome or "rejected"),
                )
                raise OperatorUpdateError(
                    str(command_result.error or "invalid_transition"),
                    command_result.message or "Update was not applied.",
                )
            result = command_result.to_dict()

            insert_activity(
                conn,
                actor=actor,
                action_type="staged_update_confirmed",
                project_id=project_id,
                surface="ai_workspace",
                object_ref=operation_id,
                summary=f"Workspace operator confirmed {project_id} milestone update",
                metadata_json=json.dumps({
                    "kind": payload.get("kind"),
                    "transition": payload.get("transition"),
                    "evidence_ref": payload.get("evidence_ref"),
                    "missing_criteria_ids": [
                        item.get("id") for item in (payload.get("missing_criteria") or [])
                    ],
                }),
            )
            patch_operator_update_part(
                conn,
                thread_id=thread_id,
                operation_id=operation_id,
                status="confirmed",
                outcome=str(result.get("outcome") or "accepted"),
            )
    if terminal_error is not None:
        raise terminal_error

    post_commit.append(lambda: _publish_confirmed_operator_update(
        project_id=project_id, operation_id=operation_id, actor=actor,
        payload=payload, result=result,
    ))
    return result


def _publish_confirmed_operator_update(
    *, project_id: str, operation_id: str, actor: str, payload: dict, result: dict,
) -> None:
    """Wake durable delivery and refresh Workspace only after the DB commit."""
    if (result.get("outcome") == "accepted"
            and (payload.get("fields") or {}).get("current_milestone") == "M8_GoLive"):
        try:
            from src.services.go_live_notice import schedule_go_live_telegram

            schedule_go_live_telegram(project_id)
        except Exception:
            logger.exception(
                "Failed to schedule committed Workspace go-live notice for %s",
                project_id,
            )
    if result.get("outcome") == "accepted":
        try:
            event_bus.publish(Event(
                event_type="project_updated",
                project_id=project_id,
                detail={"fields": list((payload.get("fields") or {}).keys())},
                surface="ai_workspace",
            ))
        except Exception:
            logger.exception("Failed to publish committed Workspace update for %s", project_id)
    try:
        metrics.increment(
            "workspace_operator_update_confirmed",
            kind=str(payload.get("kind") or "operator_override"),
            transition=str(payload.get("transition") or "adjacent"),
            outcome=str(result.get("outcome") or "accepted"),
        )
    except Exception:
        logger.exception("Failed to record committed Workspace update metric for %s", project_id)
    logger.info(
        "confirmed project_id=%s operation_id=%s actor=%s outcome=%s",
        project_id, operation_id, actor, result.get("outcome"),
    )


def cancel_operator_update(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: str,
    thread_id: str,
    operation_id: str,
) -> dict:
    require_flag()
    require_bound_actor(conn, actor)
    _require_workspace_thread_owner(conn, thread_id=thread_id, actor=actor)
    operation = get_operation(conn, operation_id)
    if operation is None:
        raise OperatorUpdateError(
            "pending_operation_not_found",
            f"Operation {operation_id} was not found.",
        )
    if operation.project_id != project_id or operation.operation_type != "project_update":
        raise OperatorUpdateError("pending_operation_not_found", "Operation project mismatch.")
    if operation.operator_id != actor or str(operation.chat_id) != str(thread_id):
        raise OperatorUpdateError("permission_required", "Operation owner or thread mismatch.")
    if operation.project_id is None:
        raise OperatorUpdateError(
            "pending_operation_not_found",
            f"Operation {operation_id} has no project.",
        )
    terminal_error: OperatorUpdateTerminalError | None = None
    with savepoint(conn, ensure_transaction=True):
        command_result = cancel_project_update_command(
            conn,
            project_id=operation.project_id,
            operator_id=actor,
            chat_id=thread_id,
            operation_id=operation_id,
            commit=False,
        )
        if not command_result.ok:
            resolved = get_operation(conn, operation_id)
            if command_result.error == "pending_operation_expired" and resolved is not None:
                patch_operator_update_part(
                    conn,
                    thread_id=thread_id,
                    operation_id=operation_id,
                    status="expired",
                    error=command_result.message,
                )
                terminal_error = OperatorUpdateTerminalError(
                    command_result.error, command_result.message, resolved.status,
                )
            else:
                raise OperatorUpdateError(
                    str(command_result.error or "pending_operation_not_pending"),
                    command_result.message or "Cancel failed.",
                )
        else:
            result = command_result.to_dict()
            clear_request_key(conn, operation_id)
            patch_operator_update_part(
                conn,
                thread_id=thread_id,
                operation_id=operation_id,
                status="cancelled",
                outcome="cancelled",
            )
    if terminal_error is not None:
        raise terminal_error
    logger.info(
        "staged cancellation project_id=%s operation_id=%s actor=%s",
        operation.project_id, operation_id, actor,
    )
    return result
