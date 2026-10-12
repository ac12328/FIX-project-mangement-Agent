"""Authenticated Admin review of quarantined contact identity card sends."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.events.bus import Event, event_bus
from src.services.contact_identity_card_review import (
    ContactIdentityCardReviewError,
    confirm_uncertain_contact_identity_card_sent,
    list_unresolved_contact_identity_cards,
)

router = APIRouter(tags=["contact-identity-cards"])


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class ContactIdentityCardStatus(BaseModel):
    suggestion_id: str
    source_email_id: str | None
    project_id: str | None
    chat_id: int | None
    bot_fingerprint: str | None
    card_text: str
    state: Literal["uncertain", "blocked", "sent"]
    attempts: int
    telegram_message_id: int | None
    last_error: str | None
    created_at: str
    updated_at: str


class ConfirmContactIdentityCardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion_id: str = Field(min_length=1, max_length=1000)
    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempts: int = Field(ge=1, strict=True)
    expected_chat_id: int = Field(strict=True)
    expected_bot_fingerprint: str = Field(min_length=64, max_length=64)
    expected_card_text: str = Field(min_length=1)
    telegram_message_id: int = Field(gt=0, strict=True)
    evidence_ref: str = Field(min_length=1, max_length=2000)

    @field_validator("expected_chat_id")
    @classmethod
    def require_nonzero_chat(cls, value: int) -> int:
        if value == 0:
            raise ValueError("original chat ID must be nonzero")
        return value


@router.get("/contact-identity-cards/unresolved", response_model=list[ContactIdentityCardStatus])
def list_unresolved(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return list_unresolved_contact_identity_cards(conn, limit=limit)


@router.post("/contact-identity-cards/confirm-sent", response_model=ContactIdentityCardStatus)
def confirm_sent(
    body: ConfirmContactIdentityCardRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_uncertain_contact_identity_card_sent(
            conn, suggestion_id=body.suggestion_id, actor=actor.user_id,
            expected_updated_at=body.expected_updated_at,
            expected_attempts=body.expected_attempts,
            expected_chat_id=body.expected_chat_id,
            expected_bot_fingerprint=body.expected_bot_fingerprint,
            expected_card_text=body.expected_card_text,
            telegram_message_id=body.telegram_message_id,
            evidence_ref=body.evidence_ref,
        )
    except KeyError:
        raise HTTPException(404, "Delivery not found") from None
    except ContactIdentityCardReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    conn.commit()
    event_bus.publish(Event(
        event_type="contact_identity_card_reconciled", project_id=result["project_id"],
        detail={"suggestion_id": body.suggestion_id, "state": "sent"},
        surface="dashboard",
    ))
    return result


