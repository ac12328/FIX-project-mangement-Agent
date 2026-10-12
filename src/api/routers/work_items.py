"""Personal task, commitment, waiting-for, and reminder API."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import defaults
from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_conn
from src.db.savepoint import savepoint
from src.repositories.personal_work_item_repo import (
    create_personal_work_item,
    list_personal_work_items,
)
from src.services.personal_work_item_service import change_personal_work_item

router = APIRouter(tags=["work-items"])


class PersonalWorkItemCreate(BaseModel):
    kind: str
    title: str
    description: str | None = None
    priority: str = "medium"
    owner_type: str = "operator"
    owner_ref: str | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    due_at: str | None = None
    remind_at: str | None = None
    source_type: str = "manual"
    source_ref: str | None = None
    evidence_text: str | None = None
    confidence: float = 1.0
    review_status: str | None = None


class SnoozeRequest(BaseModel):
    snoozed_until: str


class ResolutionRequest(BaseModel):
    resolution_note: str | None = None


def _require_feature() -> None:
    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_WORK_ITEMS_ENABLED
    ):
        raise HTTPException(404, "Personal work items are disabled")


@router.get("/work-items")
def list_items(
    kind: str | None = None,
    status: str | None = "open",
    review_status: str | None = None,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[dict]:
    _require_feature()
    return [
        item.model_dump()
        for item in list_personal_work_items(
            conn,
            actor.operator_id,
            kind=kind,
            status=status,
            review_status=review_status,
        )
    ]


@router.post("/work-items")
def create_item(
    body: PersonalWorkItemCreate,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_feature()
    try:
        with savepoint(conn, ensure_transaction=True):
            item = create_personal_work_item(
                conn,
                operator_id=actor.operator_id,
                commit=False,
                **body.model_dump(),
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return item.model_dump()


@router.post("/work-items/{work_item_id}/complete")
def complete_item(
    work_item_id: str,
    body: ResolutionRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    return _change_item(
        conn, actor, work_item_id, "complete", resolution_note=body.resolution_note
    )


@router.post("/work-items/{work_item_id}/cancel")
def cancel_item(
    work_item_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    return _change_item(conn, actor, work_item_id, "cancel")


@router.post("/work-items/{work_item_id}/snooze")
def snooze_item(
    work_item_id: str,
    body: SnoozeRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    return _change_item(
        conn, actor, work_item_id, "snooze", snoozed_until=body.snoozed_until
    )


@router.post("/work-items/{work_item_id}/confirm")
def confirm_item(
    work_item_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    return _change_item(conn, actor, work_item_id, "confirm")


@router.post("/work-items/{work_item_id}/reject")
def reject_item(
    work_item_id: str,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    return _change_item(conn, actor, work_item_id, "reject")


def _change_item(
    conn: sqlite3.Connection,
    actor: CurrentActor,
    work_item_id: str,
    action: str,
    *,
    snoozed_until: str | None = None,
    resolution_note: str | None = None,
) -> dict:
    _require_feature()
    try:
        item = change_personal_work_item(
            conn,
            operator_id=actor.operator_id,
            work_item_id=work_item_id,
            action=action,
            snoozed_until=snoozed_until,
            resolution_note=resolution_note,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if item is None:
        raise HTTPException(404, "Work item not found")
    return item.model_dump()
