"""Admin-only review of inbound presentation sends with uncertain outcomes."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.events.bus import Event, event_bus
from src.services.inbound_presentation_review import (
    InboundPresentationReviewError,
    confirm_uncertain_presentation_sent,
    list_unresolved_presentations,
)

router = APIRouter(tags=["inbound-presentations"])


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class InboundPresentationStatus(BaseModel):
    email_id: str
    project_id: str
    kind: Literal["info_card", "plain"]
    chat_id: int | None
    bot_fingerprint: str | None
    state: Literal["uncertain", "blocked", "sent"]
    attempts: int
    telegram_message_id: str | None
    last_error: str | None
    created_at: str
    updated_at: str


class InboundPresentationReviewItem(InboundPresentationStatus):
    payload: dict[str, Any]


class ConfirmInboundPresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_id: str = Field(min_length=1, max_length=1000)
    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempts: int = Field(ge=1, strict=True)
    expected_chat_id: int = Field(strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    telegram_message_id: int = Field(gt=0, strict=True)
    evidence_ref: str = Field(min_length=1, max_length=2000)


@router.get("/inbound-presentations/unresolved", response_model=list[InboundPresentationReviewItem])
def list_unresolved(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return list_unresolved_presentations(conn, limit=limit)


@router.post("/inbound-presentations/confirm-sent", response_model=InboundPresentationStatus)
def confirm_sent(
    body: ConfirmInboundPresentationRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_uncertain_presentation_sent(
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
    except InboundPresentationReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    # Never claim success until the decision and activity audit are durable.
    conn.commit()
    event_bus.publish(Event(
        event_type="inbound_presentation_reconciled", project_id=result["project_id"],
        detail={"email_id": body.email_id, "state": "sent"}, surface="dashboard",
    ))
    if result.get("payload", {}).get("combined_advisory"):
        event_bus.publish(Event(event_type="project_advisory_reconciled", project_id=result["project_id"],
            detail={"email_id": body.email_id, "state": "sent"}, surface="dashboard"))
    return result
