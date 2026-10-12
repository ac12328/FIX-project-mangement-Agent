"""Action intent approval and rejection endpoints."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_conn
from src.auth.rbac import P_BUSINESS_WRITE, P_PROJECT_CLOSE, require_permission
from src.governance.action_service import approve_action_intent, reject_action_intent
from src.governance.project_completion_actions import (
    PROJECT_COMPLETION_ACTION,
    create_project_completion_intent,
)
from src.models.action_intent import ActionIntent
from src.governance.four_eyes import team_four_eyes_enabled
from src.repositories.action_intent_repo import (
    get_action_intent,
    list_action_intents,
    list_pending_team_intents,
)

router = APIRouter(tags=["approvals"])


@router.post("/projects/{project_id}/completion-approval", status_code=201)
def request_project_completion(
    project_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    """Create the version-bound approval used to complete an M8 project."""
    _require_project_completion_access(actor)
    try:
        intent = create_project_completion_intent(
            conn,
            project_id,
            operator_id=actor.operator_id,
            surface=actor.surface,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "already" in str(exc).lower() else 422
        raise HTTPException(status_code, str(exc)) from exc
    conn.commit()
    return intent.model_dump()


@router.get("/approvals")
def list_approvals(
    status: str | None = None,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[dict]:
    items = (
        list_pending_team_intents(conn, status=status)
        if team_four_eyes_enabled()
        else list_action_intents(conn, actor.operator_id, status=status)
    )
    return [item.model_dump() for item in items]


@router.get("/approvals/{action_id}")
def get_approval(
    action_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict:
    intent = get_action_intent(conn, action_id)
    if intent is None:
        raise HTTPException(404, "Approval not found")
    if not team_four_eyes_enabled() and intent.operator_id != actor.operator_id:
        raise HTTPException(404, "Approval not found")
    return intent.model_dump()


@router.post("/approvals/{action_id}/approve")
def approve(
    action_id: str,
    execute: bool = False,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    owned_intent = _require_visible_intent(conn, action_id, actor.operator_id)
    _require_completion_permission_for_intent(actor, owned_intent)
    from src.governance.action_executor import ActionCommitUncertain

    try:
        if execute and owned_intent.action_type == "email_send":
            from src.governance.action_executor import execute_approved_action

            # The authenticated approver is bound inside the same short command
            # as draft status/history and the immutable outbox intent. Dispatch
            # happens only after that command commits.
            conn.commit()
            intent = execute_approved_action(
                conn, action_id, approved_by=actor.operator_id,
            )
        else:
            intent = approve_action_intent(conn, action_id, approved_by=actor.operator_id)
            if execute or intent.action_type == PROJECT_COMPLETION_ACTION:
                from src.governance.action_executor import execute_approved_action

                intent = execute_approved_action(conn, action_id)
    except ValueError as exc:
        conn.commit()
        raise HTTPException(409, str(exc)) from exc
    except ActionCommitUncertain as exc:
        raise HTTPException(409, str(exc)) from exc
    conn.commit()
    return intent.model_dump()


@router.post("/approvals/{action_id}/reject")
def reject(
    action_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    owned_intent = _require_visible_intent(conn, action_id, actor.operator_id)
    _require_completion_permission_for_intent(actor, owned_intent)
    try:
        intent = reject_action_intent(conn, action_id)
    except ValueError as exc:
        conn.commit()
        raise HTTPException(409, str(exc)) from exc
    conn.commit()
    return intent.model_dump()


def _require_owned_intent(
    conn: sqlite3.Connection, action_id: str, operator_id: str
) -> ActionIntent:
    intent = get_action_intent(conn, action_id)
    if intent is None or intent.operator_id != operator_id:
        raise HTTPException(404, "Approval not found")
    return intent


def _require_visible_intent(
    conn: sqlite3.Connection, action_id: str, operator_id: str
) -> ActionIntent:
    if not team_four_eyes_enabled():
        return _require_owned_intent(conn, action_id, operator_id)
    intent = get_action_intent(conn, action_id)
    if intent is None:
        raise HTTPException(404, "Approval not found")
    return intent


def _require_project_completion_access(actor: CurrentActor) -> None:
    if actor.surface != "dashboard":
        raise HTTPException(403, "Project completion is available in Dashboard only")
    require_permission(actor, P_BUSINESS_WRITE)
    require_permission(actor, P_PROJECT_CLOSE)


def _require_completion_permission_for_intent(
    actor: CurrentActor, intent: ActionIntent
) -> None:
    if intent.action_type == PROJECT_COMPLETION_ACTION:
        _require_project_completion_access(actor)
