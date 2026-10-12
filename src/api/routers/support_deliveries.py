"""Admin-only review of support Telegram cards with uncertain remote outcomes."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.services.support_delivery_review import (
    SupportDeliveryReviewError,
    confirm_uncertain_support_sent,
    uncertain_support_deliveries,
)

router = APIRouter(tags=["support-deliveries"])


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class SupportDeliveryStatus(BaseModel):
    email_id: str
    ticket_id: str
    project_id: str | None
    chat_id: int
    state: Literal["uncertain", "sent"]
    attempts: int
    telegram_message_id: str | None
    last_error: str | None
    created_at: str
    updated_at: str


class SupportDeliveryReviewItem(SupportDeliveryStatus):
    bot_fingerprint: str
    title: str
    body: str
    is_followup: bool


class ConfirmSupportDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_id: str = Field(min_length=1, max_length=1000)
    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempts: int = Field(ge=1, strict=True)
    expected_chat_id: int = Field(strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    telegram_message_id: int = Field(gt=0, strict=True)
    evidence_ref: str = Field(min_length=1, max_length=2000)


@router.get("/support-deliveries/uncertain", response_model=list[SupportDeliveryReviewItem])
def list_uncertain_deliveries(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return uncertain_support_deliveries(conn, limit=limit)


@router.post("/support-deliveries/confirm-sent", response_model=SupportDeliveryStatus)
def confirm_sent(
    body: ConfirmSupportDeliveryRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_uncertain_support_sent(
            conn, email_id=body.email_id, actor=actor.user_id,
            expected_updated_at=body.expected_updated_at,
            expected_attempts=body.expected_attempts,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            telegram_message_id=body.telegram_message_id,
            evidence_ref=body.evidence_ref,
        )
    except KeyError:
        raise HTTPException(404, "Delivery not found") from None
    except SupportDeliveryReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    # Never claim success until both the decision and activity audit are durable.
    conn.commit()
    return result
