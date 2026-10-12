"""Admin-only review of M8 Telegram sends with uncertain remote outcomes."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.services.go_live_delivery import (
    GoLiveDeliveryReviewError,
    confirm_uncertain_go_live_sent,
    uncertain_go_live_deliveries,
)

router = APIRouter(tags=["go-live-deliveries"])


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class GoLiveDeliveryStatus(BaseModel):
    project_id: str
    chat_id: int
    state: Literal["uncertain", "sent"]
    attempts: int
    telegram_message_id: str | None
    last_error: str | None
    created_at: str
    updated_at: str


class GoLiveDeliveryReviewItem(GoLiveDeliveryStatus):
    bot_fingerprint: str
    text: str


class ConfirmGoLiveDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempts: int = Field(ge=1, strict=True)
    expected_chat_id: int = Field(strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    telegram_message_id: int = Field(gt=0, strict=True)
    evidence_ref: str = Field(min_length=1, max_length=2000)


@router.get("/go-live-deliveries/uncertain", response_model=list[GoLiveDeliveryReviewItem])
def list_uncertain_deliveries(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return uncertain_go_live_deliveries(conn, limit=limit)


@router.post(
    "/go-live-deliveries/{project_id}/confirm-sent",
    response_model=GoLiveDeliveryStatus,
)
def confirm_sent(
    project_id: str,
    body: ConfirmGoLiveDeliveryRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_uncertain_go_live_sent(
            conn, project_id=project_id, actor=actor.user_id,
            expected_updated_at=body.expected_updated_at,
            expected_attempts=body.expected_attempts,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            telegram_message_id=body.telegram_message_id,
            evidence_ref=body.evidence_ref,
        )
    except KeyError:
        raise HTTPException(404, "Delivery not found") from None
    except GoLiveDeliveryReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    # Do not report success before the decision and its audit row commit.
    conn.commit()
    return result
