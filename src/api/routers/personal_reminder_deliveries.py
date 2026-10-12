"""Authenticated admin reconciliation of personal Telegram reminder sends."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.events.bus import Event, event_bus
from src.services.personal_reminder_delivery import (
    ReminderDeliveryReviewError, reconcile_reminder_delivery,
    review_linked_reminder_work_item,
    reviewable_reminder_deliveries,
)

router = APIRouter(tags=["personal-reminder-deliveries"])


def _require_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class ReminderDeliveryReviewItem(BaseModel):
    run_id: str
    schedule_id: str
    scheduled_for: str
    title: str
    operator_id: str
    chat_id: int | None
    bot_fingerprint: str | None
    text: str | None
    state: Literal["sending", "uncertain", "unrecorded", "sent"]
    review_version: int
    last_error: str | None
    linked_item_review_outcome: str | None = None
    created_at: str
    updated_at: str


class ReconcileReminderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, strict=True)
    expected_chat_id: int = Field(strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    decision: Literal["confirm_sent", "confirm_not_sent"]
    telegram_message_ids: list[StrictInt] | None = Field(default=None, min_length=1, max_length=100)
    evidence_ref: str = Field(min_length=1, max_length=2000)


class ReminderDeliveryResolution(BaseModel):
    run_id: str
    schedule_id: str
    state: Literal["sent", "not_sent"]


@router.get(
    "/personal-reminder-deliveries/review",
    response_model=list[ReminderDeliveryReviewItem],
)
def list_reviewable(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_admin(actor)
    return reviewable_reminder_deliveries(conn, limit=limit)


@router.post(
    "/personal-reminder-deliveries/{run_id}/reconcile",
    response_model=ReminderDeliveryResolution,
)
def reconcile(
    run_id: str, body: ReconcileReminderRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_admin(actor)
    try:
        result = reconcile_reminder_delivery(
            conn, run_id=run_id, actor=actor.user_id,
            expected_version=body.expected_version,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            decision=body.decision,
            telegram_message_ids=body.telegram_message_ids,
            evidence_ref=body.evidence_ref,
        )
    except KeyError:
        raise HTTPException(404, "Reminder receipt not found") from None
    except ReminderDeliveryReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    conn.commit()
    event_bus.publish(Event(
        event_type="personal_reminder_delivery_reviewed", surface="dashboard",
        detail={"run_id": run_id, "decision": body.decision},
    ))
    return result


class LinkedItemReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, strict=True)
    expected_chat_id: int = Field(strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    decision: Literal["retry_resolution", "acknowledge_manual_followup"]
    evidence_ref: str = Field(min_length=1, max_length=2000)
    note: str = Field(min_length=1, max_length=2000)


class LinkedItemReviewResolution(BaseModel):
    run_id: str
    schedule_id: str
    state: Literal["sent"]
    linked_item_review_outcome: Literal["resolved", "acknowledged_manual_followup"]


@router.post(
    "/personal-reminder-deliveries/{run_id}/linked-item-review",
    response_model=LinkedItemReviewResolution,
)
def review_linked_item(
    run_id: str, body: LinkedItemReviewRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_admin(actor)
    try:
        result = review_linked_reminder_work_item(
            conn, run_id=run_id, actor=actor.user_id,
            expected_version=body.expected_version,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            decision=body.decision, evidence_ref=body.evidence_ref, note=body.note,
        )
    except KeyError:
        raise HTTPException(404, "Reminder receipt not found") from None
    except ReminderDeliveryReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    conn.commit()
    event_bus.publish(Event(
        event_type="personal_reminder_linked_item_reviewed", surface="dashboard",
        detail={"run_id": run_id, "decision": body.decision},
    ))
    return result
