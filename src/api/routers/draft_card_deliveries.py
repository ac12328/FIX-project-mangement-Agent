"""Authenticated Admin-only review of uncertain operational draft cards."""

from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.services.draft_card_delivery_review import (
    DraftCardDeliveryReviewError, attachment_preparation_reviews,
    cancel_unverified_draft_card, confirm_uncertain_draft_card_sent,
    uncertain_draft_card_deliveries,
)
from src.events.bus import Event, event_bus

router = APIRouter(tags=["draft-card-deliveries"])
logger = logging.getLogger(__name__)


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class DraftCardDeliveryStatus(BaseModel):
    draft_id: str
    project_id: str | None
    chat_id: int
    state: Literal["uncertain", "sent"]
    attempts: int
    telegram_message_ids: list[int] | None = None
    last_error: str | None
    created_at: str
    updated_at: str


class DraftCardDeliveryReviewItem(BaseModel):
    draft_id: str
    project_id: str | None
    to_email: str
    subject: str
    chat_id: int | None
    bot_fingerprint: str | None
    state: Literal["uncertain"]
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


class ConfirmDraftCardDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempts: int = Field(ge=1, strict=True)
    expected_chat_id: int = Field(gt=0, strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    telegram_message_ids: list[int] = Field(min_length=1, max_length=20)
    evidence_ref: str = Field(min_length=1, max_length=2000)


@router.get("/draft-card-deliveries/uncertain", response_model=list[DraftCardDeliveryReviewItem])
def list_uncertain(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return uncertain_draft_card_deliveries(conn, limit=limit)


@router.post("/draft-card-deliveries/{draft_id}/confirm-sent", response_model=DraftCardDeliveryStatus)
def confirm_sent(
    draft_id: str, body: ConfirmDraftCardDeliveryRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_uncertain_draft_card_sent(
            conn, draft_id=draft_id, actor=actor.user_id,
            expected_updated_at=body.expected_updated_at,
            expected_attempts=body.expected_attempts,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            telegram_message_ids=body.telegram_message_ids,
            evidence_ref=body.evidence_ref,
        )
    except KeyError:
        raise HTTPException(404, "Delivery not found") from None
    except DraftCardDeliveryReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    conn.commit()
    return result


class DraftCardPreparationReviewItem(BaseModel):
    draft_id: str
    project_id: str | None
    draft_status: str
    to_email: str
    subject: str
    chat_id: int | None
    bot_fingerprint: str | None
    state: Literal["pending"]
    attachment_state: Literal["needs_review"]
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


class CancelUnverifiedDraftCardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_to_email: str = Field(min_length=1, max_length=500)
    expected_subject: str = Field(min_length=1, max_length=5000)
    expected_chat_id: StrictInt | None
    expected_bot_fingerprint: str | None = Field(
        pattern=r"^[0-9a-f]{64}$",
    )
    acknowledge_no_card: Literal[True]
    evidence_ref: str = Field(min_length=1, max_length=2000)


class CancelledDraftCardPreparation(BaseModel):
    draft_id: str
    project_id: str | None
    draft_status: str
    state: Literal["cancelled"]
    attachment_state: Literal["needs_review"]
    updated_at: str


@router.get(
    "/draft-card-deliveries/attachment-preparation",
    response_model=list[DraftCardPreparationReviewItem],
)
def list_attachment_preparations(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return attachment_preparation_reviews(conn, limit=limit)


@router.post(
    "/draft-card-deliveries/{draft_id}/cancel-unverified",
    response_model=CancelledDraftCardPreparation,
)
def cancel_unverified(
    draft_id: str, body: CancelUnverifiedDraftCardRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = cancel_unverified_draft_card(
            conn, draft_id=draft_id, actor=actor.user_id,
            expected_updated_at=body.expected_updated_at,
            expected_to_email=body.expected_to_email,
            expected_subject=body.expected_subject,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            evidence_ref=body.evidence_ref,
            acknowledge_no_card=body.acknowledge_no_card,
        )
    except KeyError:
        raise HTTPException(404, "Draft card not found") from None
    except DraftCardDeliveryReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    # A successful response must represent durable card suppression plus audit.
    conn.commit()
    try:
        event_bus.publish(Event(
            event_type="draft_card_preparation_reconciled",
            project_id=result["project_id"],
            detail={"draft_id": draft_id, "state": "cancelled"}, surface="dashboard",
        ))
    except Exception:
        logger.exception("Could not publish draft card preparation reconciliation for %s", draft_id)
    return result
