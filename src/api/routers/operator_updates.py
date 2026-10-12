"""HTTP surface for Workspace operator project updates."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.api.schemas.operator_updates import (
    OperatorUpdateCardResponse,
    OperatorUpdateConfirmRequest,
    OperatorUpdateDomainErrorResponse,
    OperatorUpdatePermissionErrorResponse,
    OperatorUpdateValidationErrorResponse,
    OperatorUpdateReceipt,
    OperatorUpdateStageRequest,
)
from src.auth.rbac import P_BUSINESS_WRITE, require_permission
from src.services.operator_project_update import (
    OperatorUpdateError,
    OperatorUpdateTerminalError,
    cancel_operator_update,
    confirm_operator_update,
    get_operator_update_for_actor,
    preview_operator_update,
    record_committed_terminal_conflict,
    stage_operator_update,
)

logger = logging.getLogger("agentfixbpro.api.operator_updates")

router = APIRouter(tags=["operator-updates"])

_OPERATOR_UPDATE_RESPONSES = {
    403: {
        "model": OperatorUpdateDomainErrorResponse
        | OperatorUpdatePermissionErrorResponse
    },
    404: {"model": OperatorUpdateDomainErrorResponse},
    409: {"model": OperatorUpdateDomainErrorResponse},
    422: {
        "model": OperatorUpdateDomainErrorResponse
        | OperatorUpdateValidationErrorResponse
    },
}

_HTTP_BY_CODE = {
    "flag_disabled": 409,
    "unbound_dashboard_actor": 403,
    "permission_required": 403,
    "project_not_found": 404,
    "pending_operation_not_found": 404,
    "pending_operation_conflict": 409,
    "four_eyes_required": 409,
    "pending_operation_not_pending": 409,
    "pending_operation_expired": 409,
    "field_not_in_v1": 422,
    "transition_not_in_v1": 422,
    "invalid_transition": 422,
    "reason_required": 422,
    "evidence_required": 422,
    "vague_next_action": 422,
    "project_outside_workspace_scope": 422,
}


def _raise(error: OperatorUpdateError) -> None:
    status = _HTTP_BY_CODE.get(error.code, 400)
    raise HTTPException(status, {"code": error.code, "message": error.message})


def _commit_terminal_update(
    conn: sqlite3.Connection,
    error: OperatorUpdateTerminalError,
) -> None:
    """Preserve a terminal receipt, including a commit-then-raise outcome."""
    try:
        conn.commit()
    except Exception:
        record_committed_terminal_conflict(conn, error)
        raise
    record_committed_terminal_conflict(conn, error)
    _raise(error)


def _card(operation) -> OperatorUpdateCardResponse:
    payload = operation.payload or {}
    return OperatorUpdateCardResponse(
        operation_id=operation.operation_id,
        status=operation.status,
        kind=str(payload.get("kind") or "operator_override"),
        high_risk=bool(payload.get("high_risk")),
        expires_at=operation.expires_at,
        base_version=operation.base_version,
        part={
            "type": "operator_update",
            "operation_id": operation.operation_id,
            "status": "pending" if operation.status == "pending" else operation.status,
            "project_id": operation.project_id,
            "kind": payload.get("kind"),
            "fields": payload.get("fields") or {},
            "missing_criteria": payload.get("missing_criteria") or [],
            "reason": payload.get("reason") or "",
            "transition": payload.get("transition"),
            "base_version": operation.base_version,
            "expires_at": operation.expires_at,
        },
        missing_criteria=list(payload.get("missing_criteria") or []),
        current_values=dict(payload.get("old_values") or {}),
        fields=dict(payload.get("fields") or {}),
        reason=str(payload.get("reason") or ""),
        transition=str(payload.get("transition") or "adjacent"),
    )


@router.post(
    "/projects/{project_id}/operator-updates",
    response_model=OperatorUpdateCardResponse,
    responses=_OPERATOR_UPDATE_RESPONSES,
)
def stage_update(
    project_id: str,
    body: OperatorUpdateStageRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> OperatorUpdateCardResponse:
    require_permission(actor, P_BUSINESS_WRITE)
    post_commit = []
    try:
        proposal = preview_operator_update(
            conn,
            project_id=project_id,
            fields=body.fields,
            reason=body.reason,
            evidence_ref=body.evidence_ref,
        )
        operation = stage_operator_update(
            conn,
            actor=actor.user_id,
            thread_id=body.thread_id,
            proposal=proposal,
            reason=body.reason,
            evidence_ref=body.evidence_ref,
            post_commit=post_commit,
        )
    except OperatorUpdateError as exc:
        _raise(exc)
    card = _card(operation)
    conn.commit()
    for effect in post_commit:
        try:
            effect()
        except Exception:
            logger.exception(
                "Post-commit Workspace staging effect failed for %s; staging is committed",
                operation.operation_id,
            )
    return card


@router.post(
    "/projects/{project_id}/operator-updates/{operation_id}/confirm",
    response_model=OperatorUpdateReceipt,
    response_model_exclude_none=True,
    responses=_OPERATOR_UPDATE_RESPONSES,
)
def confirm_update(
    project_id: str,
    operation_id: str,
    body: OperatorUpdateConfirmRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> OperatorUpdateReceipt:
    require_permission(actor, P_BUSINESS_WRITE)
    post_commit = []
    try:
        result = confirm_operator_update(
            conn,
            project_id=project_id,
            actor=actor.user_id,
            thread_id=body.thread_id,
            operation_id=operation_id,
            post_commit=post_commit,
            reason=body.reason,
            evidence_ref=body.evidence_ref,
        )
    except OperatorUpdateTerminalError as exc:
        # Preserve a reviewed expiry/supersession before returning its conflict.
        _commit_terminal_update(conn, exc)
    except OperatorUpdateError as exc:
        _raise(exc)
    receipt = OperatorUpdateReceipt.model_validate(
        {
            **result,
            "committed": result.get("outcome") in {"accepted", "no_change"},
        }
    )
    conn.commit()
    for effect in post_commit:
        try:
            effect()
        except Exception:
            logger.exception(
                "Post-commit Workspace update effect failed for %s; business write is committed",
                operation_id,
            )
    return receipt


@router.post(
    "/projects/{project_id}/operator-updates/{operation_id}/cancel",
    response_model=OperatorUpdateReceipt,
    response_model_exclude_none=True,
    responses=_OPERATOR_UPDATE_RESPONSES,
)
def cancel_update(
    project_id: str,
    operation_id: str,
    body: OperatorUpdateConfirmRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> OperatorUpdateReceipt:
    require_permission(actor, P_BUSINESS_WRITE)
    try:
        result = cancel_operator_update(
            conn,
            project_id=project_id,
            actor=actor.user_id,
            thread_id=body.thread_id,
            operation_id=operation_id,
        )
    except OperatorUpdateTerminalError as exc:
        # The expired operation and its Workspace part are one durable receipt.
        _commit_terminal_update(conn, exc)
    except OperatorUpdateError as exc:
        _raise(exc)
    receipt = OperatorUpdateReceipt.model_validate({**result, "committed": True})
    conn.commit()
    return receipt


@router.get(
    "/projects/{project_id}/operator-updates/{operation_id}",
    response_model=OperatorUpdateCardResponse,
    responses=_OPERATOR_UPDATE_RESPONSES,
)
def get_update(
    project_id: str,
    operation_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> OperatorUpdateCardResponse:
    require_permission(actor, P_BUSINESS_WRITE)
    try:
        operation = get_operator_update_for_actor(
            conn,
            project_id=project_id,
            operation_id=operation_id,
            actor=actor.user_id,
        )
    except OperatorUpdateError as exc:
        _raise(exc)
    return _card(operation)
