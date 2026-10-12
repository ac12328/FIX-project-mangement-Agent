"""Shared application commands for confirming and cancelling staged project updates.

Adapters translate and present; this module owns transactions, pending-operation
lifecycle and after-commit effects.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from src.db.savepoint import savepoint
from src.events.bus import Event, event_bus
from src.governance.four_eyes import (
    gate_high_risk_write,
    is_high_risk_project_fields,
    team_four_eyes_enabled,
)
from src.models.enums import Milestone
from src.models.pending_operation import PendingOperation
from src.repositories import pending_operation_repo
from src.repositories.working_memory_repo import delete_memory, get_memory
from src.write_layer import write_helpers
from src.write_layer.models import WriteResult

logger = logging.getLogger(__name__)

PENDING_CHAT_UPDATE_KEY = "pending_chat_update"
ConfirmationAuthority = Literal["owner", "four_eyes"]


@dataclass(frozen=True)
class ProjectUpdateCommandResult:
    """Surface-neutral receipt for a staged project update command."""

    ok: bool
    outcome: str
    project_id: str
    operation_id: str | None
    committed: bool = False
    error: str | None = None
    message: str = ""
    applied_fields: dict[str, Any] = field(default_factory=dict)
    write_result: WriteResult | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, text: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "outcome": self.outcome,
            "committed": self.committed,
            "operation_id": self.operation_id,
            "project_id": self.project_id,
            "text": self.message if text is None else text,
        }
        if self.error:
            result["error"] = self.error
        return result


def _error(
    code: str,
    message: str,
    *,
    project_id: str,
    operation_id: str | None,
    outcome: str = "rejected",
) -> ProjectUpdateCommandResult:
    return ProjectUpdateCommandResult(
        ok=False,
        outcome=outcome,
        error=code,
        message=message,
        project_id=project_id,
        operation_id=operation_id,
    )


def stage_project_update_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    operator_id: str,
    chat_id: str,
    payload: dict[str, Any],
    base_version: str | None,
    request_key: str | None = None,
) -> PendingOperation:
    """Stage one owned operation; the caller owns authorization and commit.
    Workspace requests reuse identical pending keys; keyless tools supersede proposals.
    Related surface records must be written in the caller's outer savepoint.
    """
    with savepoint(conn, ensure_transaction=True):
        existing = pending_operation_repo.get_operation_by_request_key(conn, request_key)
        if existing is not None and (
            existing.operator_id != operator_id
            or str(existing.chat_id) != str(chat_id)
            or existing.project_id != project_id
        ):
            raise ValueError("Request key belongs to a different operation owner")
        if existing is not None and existing.status == "pending":
            if pending_operation_repo.expire_operation(conn, existing.operation_id):
                existing = pending_operation_repo.get_operation(conn, existing.operation_id)
        if existing is not None and existing.status == "pending" and (
            existing.operator_id == operator_id
            and str(existing.chat_id) == str(chat_id)
            and existing.project_id == project_id
            and existing.base_version == base_version
            and (existing.payload or {}) == payload
        ):
            return existing
        if existing is not None:
            pending_operation_repo.clear_request_key(conn, existing.operation_id)
        return pending_operation_repo.stage_operation(
            conn,
            operation_type="project_update",
            operator_id=operator_id,
            chat_id=chat_id,
            project_id=project_id,
            payload=payload,
            base_version=base_version,
            request_key=request_key,
        )


def _delete_matching_typed_pending_memory(
    conn: sqlite3.Connection,
    project_id: str,
    operation_id: str,
) -> None:
    """Remove compatibility memory only when it points at this operation."""
    mem = get_memory(conn, project_id, PENDING_CHAT_UPDATE_KEY)
    if not mem:
        return
    try:
        staged = json.loads(mem["memory_value"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return
    if isinstance(staged, dict) and str(staged.get("operation_id") or "") == operation_id:
        delete_memory(conn, project_id, PENDING_CHAT_UPDATE_KEY)


def _validate_operation(
    conn: sqlite3.Connection,
    operation: PendingOperation,
    *,
    project_id: str,
    operator_id: str,
    chat_id: str,
    authority: ConfirmationAuthority,
    enforce_high_risk_review: bool,
) -> ProjectUpdateCommandResult | None:
    if operation.operation_type != "project_update":
        return _error(
            "pending_operation_type_mismatch",
            "The specified operation is not a staged project update.",
            project_id=project_id,
            operation_id=operation.operation_id,
        )

    fields = (operation.payload or {}).get("fields") or {}
    high_risk, _reason = is_high_risk_project_fields(conn, project_id, fields)
    if enforce_high_risk_review and team_four_eyes_enabled() and high_risk:
        if str(operation.chat_id) != str(chat_id):
            return _error(
                "pending_operation_scope_mismatch",
                "This pending update belongs to another conversation.",
                project_id=project_id,
                operation_id=operation.operation_id,
            )
        # Owner-scoped confirmation must not bypass the common review policy.
        if authority != "four_eyes" and operation.operator_id != operator_id:
            return _error(
                "pending_operation_scope_mismatch",
                "This pending update belongs to another conversation.",
                project_id=project_id,
                operation_id=operation.operation_id,
            )
        blocked = gate_high_risk_write(
            conn,
            project_id,
            fields,
            operation.operator_id,
            operator_id,
        )
        if blocked:
            return _error(
                "four_eyes_required",
                blocked,
                project_id=project_id,
                operation_id=operation.operation_id,
            )
    elif operation.operator_id != operator_id or str(operation.chat_id) != str(chat_id):
        return _error(
            "pending_operation_scope_mismatch",
            "This pending update belongs to another conversation.",
            project_id=project_id,
            operation_id=operation.operation_id,
        )

    if operation.project_id != project_id:
        return _error(
            "pending_operation_project_mismatch",
            f"Operation {operation.operation_id} belongs to {operation.project_id}, not {project_id}.",
            project_id=project_id,
            operation_id=operation.operation_id,
        )

    if operation.status == "pending" and pending_operation_repo.expire_operation(
        conn, operation.operation_id,
    ):
        operation = pending_operation_repo.get_operation(conn, operation.operation_id)

    if operation.status != "pending":
        _delete_matching_typed_pending_memory(conn, project_id, operation.operation_id)
        if operation.status == "expired":
            return _error(
                "pending_operation_expired",
                f"The staged update for {project_id} expired. Re-check and stage it again.",
                project_id=project_id,
                operation_id=operation.operation_id,
            )
        return _error(
            "pending_operation_not_pending",
            f"Operation {operation.operation_id} is already {operation.status}.",
            project_id=project_id,
            operation_id=operation.operation_id,
        )
    return None


def publish_confirmed_project_update(
    project_id: str,
    applied_fields: dict[str, Any],
    *,
    surface: str,
    publish_event: bool = True,
) -> None:
    """Run best-effort refresh and delivery wake-up only after owner commit."""
    if applied_fields.get("current_milestone") == Milestone.M8_GO_LIVE.value:
        try:
            from src.services.go_live_notice import schedule_go_live_telegram

            schedule_go_live_telegram(project_id)
        except Exception:
            logger.exception("Failed to schedule M8 go-live Telegram notice for %s", project_id)
    if publish_event:
        try:
            event_bus.publish(
                Event(
                    event_type="project_updated",
                    project_id=project_id,
                    detail={"fields": list(applied_fields)},
                    surface=surface,
                )
            )
        except Exception:
            logger.exception("Failed to publish committed project update for %s", project_id)


def confirm_project_update_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    operator_id: str,
    chat_id: str,
    operation_id: str,
    authority: ConfirmationAuthority,
    surface: str | None = None,
    commit: bool = True,
    publish_event: bool = True,
) -> ProjectUpdateCommandResult:
    """Confirm one typed staged update as a single database unit of work."""
    if publish_event and not commit:
        raise ValueError("publish_event=True requires commit=True")

    caller_in_transaction = conn.in_transaction
    operation = pending_operation_repo.get_operation(conn, operation_id)
    if operation is None:
        return _error(
            "pending_operation_not_found",
            f"Operation {operation_id} was not found.",
            project_id=project_id,
            operation_id=operation_id,
        )

    with savepoint(conn, ensure_transaction=True):
        validation_error = _validate_operation(
            conn,
            operation,
            project_id=project_id,
            operator_id=operator_id,
            chat_id=chat_id,
            authority=authority,
            enforce_high_risk_review=True,
        )
    if validation_error is not None:
        # A denied command must never commit unrelated caller work. A standalone
        # expiry/terminal-memory cleanup may still persist its own state.
        if commit and not caller_in_transaction:
            if validation_error.error in (
                "pending_operation_expired", "pending_operation_not_pending",
            ):
                conn.commit()
            else:
                conn.rollback()
        return validation_error

    row = conn.execute(
        "SELECT updated_at FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        with savepoint(conn, ensure_transaction=True):
            resolved = pending_operation_repo.resolve_operation(
                conn,
                operation.operation_id,
                status="failed",
                resolution="project missing",
            )
            if not resolved:
                raise RuntimeError(
                    f"Operation {operation.operation_id} could not be failed from its current state."
                )
            _delete_matching_typed_pending_memory(conn, project_id, operation.operation_id)
        if commit and not caller_in_transaction:
            conn.commit()
        return _error(
            "project_not_found",
            f"Project {project_id} not found.",
            project_id=project_id,
            operation_id=operation.operation_id,
        )

    if operation.base_version != row[0]:
        with savepoint(conn, ensure_transaction=True):
            resolved = pending_operation_repo.resolve_operation(
                conn,
                operation.operation_id,
                status="superseded",
                resolution="project changed after staging",
            )
            if not resolved:
                raise RuntimeError(
                    f"Operation {operation.operation_id} could not be superseded from its current state."
                )
            _delete_matching_typed_pending_memory(conn, project_id, operation.operation_id)
        if commit and not caller_in_transaction:
            conn.commit()
        return _error(
            "pending_operation_conflict",
            f"{project_id} changed after this update was staged. Re-check and stage again.",
            project_id=project_id,
            operation_id=operation.operation_id,
        )

    payload = dict(operation.payload or {})
    command_surface = str(surface or payload.get("surface") or "telegram")
    with savepoint(conn, ensure_transaction=True):
        write_result = write_helpers.safe_write_project(
            conn,
            project_id,
            payload["fields"],
            actor=operator_id,
            reason=payload.get("reason") or "operator confirmation",
            evidence_ref=payload.get("evidence_ref"),
            operator_confirmed=True,
            expected_updated_at=operation.base_version,
            surface=command_surface,
            commit=False,
            publish_event=False,
            schedule_notifications=False,
            rollback_on_error=False,
        )
        if commit and caller_in_transaction and write_result.outcome in ("accepted", "no_change"):
            raise ValueError("commit=True cannot own an existing transaction; use commit=False")
        if write_result.outcome in ("accepted", "no_change"):
            delete_memory(conn, project_id, PENDING_CHAT_UPDATE_KEY)
            resolved = pending_operation_repo.resolve_operation(
                conn,
                operation.operation_id,
                status="confirmed",
                resolution=write_result.outcome,
            )
        else:
            resolved = pending_operation_repo.resolve_operation(
                conn,
                operation.operation_id,
                status="failed",
                resolution=write_result.outcome,
            )
        if not resolved:
            raise RuntimeError(
                f"Operation {operation.operation_id} could not be resolved from its current state."
            )
    if commit and (
        write_result.outcome in ("accepted", "no_change") or not caller_in_transaction
    ):
        conn.commit()

    accepted = write_result.outcome == "accepted"
    if commit and accepted:
        publish_confirmed_project_update(
            project_id,
            dict(write_result.applied_fields),
            surface=command_surface,
            publish_event=publish_event,
        )

    reason = (
        write_result.rejection_reason
        or write_result.needs_review_reason
        or write_result.outcome
    )
    return ProjectUpdateCommandResult(
        ok=write_result.outcome in ("accepted", "no_change"),
        outcome=write_result.outcome,
        committed=commit and write_result.outcome in ("accepted", "no_change"),
        operation_id=operation.operation_id,
        project_id=project_id,
        message=reason,
        applied_fields=dict(write_result.applied_fields),
        write_result=write_result,
        payload=payload,
    )


def cancel_project_update_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    operator_id: str,
    chat_id: str,
    operation_id: str,
    commit: bool = True,
) -> ProjectUpdateCommandResult:
    """Cancel one owned typed staged update atomically."""
    caller_in_transaction = conn.in_transaction
    operation = pending_operation_repo.get_operation(conn, operation_id)
    if operation is None:
        return _error(
            "pending_operation_not_found",
            f"Operation {operation_id} was not found.",
            project_id=project_id,
            operation_id=operation_id,
        )

    with savepoint(conn, ensure_transaction=True):
        validation_error = _validate_operation(
            conn,
            operation,
            project_id=project_id,
            operator_id=operator_id,
            chat_id=chat_id,
            authority="owner",
            enforce_high_risk_review=False,
        )
    if validation_error is not None:
        # A denied command must never commit unrelated caller work. A standalone
        # expiry/terminal-memory cleanup may still persist its own state.
        if commit and not caller_in_transaction:
            if validation_error.error in (
                "pending_operation_expired", "pending_operation_not_pending",
            ):
                conn.commit()
            else:
                conn.rollback()
        return validation_error

    if commit and caller_in_transaction:
        raise ValueError("commit=True cannot own an existing transaction; use commit=False")

    with savepoint(conn, ensure_transaction=True):
        resolved = pending_operation_repo.resolve_operation(
            conn,
            operation.operation_id,
            status="cancelled",
            resolution="operator cancelled",
        )
        if not resolved:
            raise RuntimeError(
                f"Operation {operation.operation_id} could not be cancelled from its current state."
            )
        _delete_matching_typed_pending_memory(conn, project_id, operation.operation_id)
    if commit:
        conn.commit()

    return ProjectUpdateCommandResult(
        ok=True,
        outcome="cancelled",
        committed=commit,
        operation_id=operation.operation_id,
        project_id=project_id,
        message=f"Staged update for {project_id} discarded.",
        payload=dict(operation.payload or {}),
    )
