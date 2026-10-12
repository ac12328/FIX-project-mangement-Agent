"""Dashboard-only, evidence-backed project closing endpoints."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_BUSINESS_WRITE, P_PROJECT_CLOSE, require_permission
from src.portfolio.project_closing import (
    ClosingValidationError,
    advance_project_to_m8,
    close_project,
    confirm_checklist_item,
    dismiss_checklist_item,
    get_closing_status,
    initiate_closing,
)

router = APIRouter(tags=["closing"])


class AdvanceToM8Request(BaseModel):
    evidence_ref: str = Field(min_length=1)
    expected_project_updated_at: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    evidence_ref: str | None = None
    note: str | None = None
    manual_attestation: bool = False


class DismissRequest(BaseModel):
    reason: str = Field(min_length=1)


class CloseRequest(BaseModel):
    expected_project_updated_at: str = Field(min_length=1)
    confirm_terminal: bool
    sign_off_note: str = Field(min_length=1)
    acknowledgements: list[str] = Field(default_factory=list)


def _require_dashboard_closer(actor: CurrentActor) -> None:
    if actor.surface != "dashboard":
        raise HTTPException(403, "Final closing workflow is available in Dashboard only")
    require_permission(actor, P_BUSINESS_WRITE)
    require_permission(actor, P_PROJECT_CLOSE)


def _status_payload(conn: sqlite3.Connection, project_id: str, actor: CurrentActor) -> dict:
    status = get_closing_status(conn, project_id)
    if status is None:
        raise HTTPException(404, "Project not found")
    payload = status.to_dict()
    payload["can_manage"] = (
        actor.surface == "dashboard"
        and P_BUSINESS_WRITE in actor.permissions
        and P_PROJECT_CLOSE in actor.permissions
    )
    return payload


@router.get("/projects/{project_id}/closing")
def get_project_closing(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    actor: CurrentActor = Depends(get_current_actor),
):
    """Return server-calculated closing readiness and checklist state."""
    return _status_payload(conn, project_id, actor)


@router.post("/projects/{project_id}/closing/advance-to-m8")
def advance_to_m8(
    project_id: str,
    body: AdvanceToM8Request,
    conn: sqlite3.Connection = Depends(get_write_conn),
    actor: CurrentActor = Depends(get_write_actor),
):
    """Perform a separate evidence-backed M7 -> M8 transition."""
    _require_dashboard_closer(actor)
    try:
        ok, message = advance_project_to_m8(
            conn,
            project_id,
            actor=actor.user_id,
            evidence_ref=body.evidence_ref.strip(),
            expected_project_updated_at=body.expected_project_updated_at,
            reason=body.reason.strip(),
            surface=actor.surface,
        )
    except ClosingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not ok:
        code = 409 if "changed after" in message else 422
        raise HTTPException(code, message)
    return {"success": True, "message": message, "project_id": project_id}


@router.post("/projects/{project_id}/closing/initiate", status_code=201)
def initiate_project_closing(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_write_conn),
    actor: CurrentActor = Depends(get_write_actor),
):
    """Initiate the closing checklist for an operationally ready M8 project."""
    _require_dashboard_closer(actor)
    try:
        status = initiate_closing(conn, project_id, initiated_by=actor.user_id)
    except ClosingValidationError as exc:
        if str(exc) == "Project not found":
            raise HTTPException(404, str(exc)) from exc
        raise HTTPException(422, str(exc)) from exc
    return {
        "message": "Closing checklist initiated.",
        "project_id": project_id,
        "checklist": status.checklist,
    }


@router.post("/projects/{project_id}/closing/{item_id}/confirm")
def confirm_item(
    project_id: str,
    item_id: str,
    body: ConfirmRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
    actor: CurrentActor = Depends(get_write_actor),
):
    """Confirm a checklist item using evidence or manual attestation."""
    _require_dashboard_closer(actor)
    try:
        ok = confirm_checklist_item(
            conn,
            project_id,
            item_id,
            evidence_ref=body.evidence_ref,
            resolution_note=body.note,
            manual_attestation=body.manual_attestation,
            resolved_by=actor.user_id,
        )
    except ClosingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Item not found or already finalized")
    return {"success": True, "item_id": item_id, "status": "confirmed"}


@router.post("/projects/{project_id}/closing/{item_id}/dismiss")
def dismiss_item(
    project_id: str,
    item_id: str,
    body: DismissRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
    actor: CurrentActor = Depends(get_write_actor),
):
    """Dismiss an optional checklist item with a nonblank reason."""
    _require_dashboard_closer(actor)
    try:
        ok = dismiss_checklist_item(
            conn,
            project_id,
            item_id,
            reason=body.reason,
            resolved_by=actor.user_id,
        )
    except ClosingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Item not found or already finalized")
    return {"success": True, "item_id": item_id, "status": "dismissed"}


@router.post("/projects/{project_id}/close")
def complete_project_close(
    project_id: str,
    body: CloseRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
    actor: CurrentActor = Depends(get_write_actor),
):
    """Perform explicit terminal sign-off after a fresh readiness check."""
    _require_dashboard_closer(actor)
    if body.confirm_terminal is not True:
        raise HTTPException(422, "Explicit terminal confirmation is required")
    ok, message = close_project(
        conn,
        project_id,
        actor=actor.user_id,
        surface=actor.surface,
        expected_project_updated_at=body.expected_project_updated_at,
        sign_off_note=body.sign_off_note,
        acknowledgements=body.acknowledgements,
    )
    if not ok:
        code = 409 if "changed after" in message else 422
        raise HTTPException(code, message)
    return {"success": True, "message": message, "project_id": project_id}
