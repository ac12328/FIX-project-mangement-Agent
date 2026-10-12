"""Admin-only reconciliation for uncertain inbound email routes."""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_conn, get_current_actor, get_write_actor, get_write_conn
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.services.inbound_overflow_review import (
    OverflowReviewError, confirm_overflow_sent, list_unresolved_overflow,
)
from src.services.inbound_overflow_pending import (
    PendingOverflowError, list_other_recipient_items, rebind_pending_item,
)
from src.services.inbound_email_reconciliation import (
    InboundReconciliationError,
    confirm_inbound_route_completed,
    list_inbound_reconciliation,
)

router = APIRouter(tags=["inbound-email-work"])


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class InboundReviewItem(BaseModel):
    email_id: str
    source_type: str
    account_key: str | None
    mailbox: str | None
    uid_validity: int | None
    source_uid: int | None
    attempt_count: int
    route_state: Literal["needs_reconciliation"]
    last_error: str | None
    captured_at: str
    updated_at: str
    project_id: str | None
    ticket_id: str | None
    sender_email: str | None
    subject: str | None
    received_at: str | None
    body_snippet: str | None
    classification_type: str | None


class ConfirmInboundRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_id: str = Field(min_length=1, max_length=1000)
    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempt_count: int = Field(ge=0, strict=True)
    domain_evidence_ref: str = Field(min_length=1, max_length=2000)
    delivery_evidence_ref: str = Field(min_length=1, max_length=2000)


class ConfirmedInboundRoute(BaseModel):
    email_id: str
    route_state: Literal["completed"]
    attempt_count: int
    updated_at: str


@router.get("/inbound-email-work/reconciliation", response_model=list[InboundReviewItem])
def list_work_requiring_review(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return list_inbound_reconciliation(conn, limit=limit)


@router.post("/inbound-email-work/confirm-completed", response_model=ConfirmedInboundRoute)
def confirm_completed(
    body: ConfirmInboundRouteRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_inbound_route_completed(
            conn, email_id=body.email_id, actor=actor.user_id,
            expected_updated_at=body.expected_updated_at,
            expected_attempt_count=body.expected_attempt_count,
            domain_evidence_ref=body.domain_evidence_ref,
            delivery_evidence_ref=body.delivery_evidence_ref,
        )
    except KeyError:
        raise HTTPException(404, "Inbound email work not found") from None
    except InboundReconciliationError as exc:
        raise HTTPException(409, str(exc)) from None
    # Both the decision and activity audit must commit before reporting success.
    conn.commit()
    return result


class OverflowReviewItem(BaseModel):
    batch_id: str
    chat_id: int
    text: str
    content_sha256: str
    state: Literal["sending", "locally_recorded"]
    message_ids: list[int]
    local_receipts: list[dict]
    source_email_ids: list[str]
    review_version: int
    created_at: str
    updated_at: str


class ConfirmOverflowSentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=100)
    expected_review_version: int = Field(ge=0, strict=True)
    expected_state: Literal["sending", "locally_recorded"]
    expected_chat_id: int = Field(strict=True)
    expected_content_sha256: str = Field(min_length=64, max_length=64)
    telegram_message_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    evidence_ref: str = Field(min_length=1, max_length=2000)
    full_content_verified: bool = Field(strict=True)


class ConfirmedOverflowSent(BaseModel):
    batch_id: str
    decision: Literal["confirmed_sent"]
    review_version: int


@router.get("/inbound-email-work/overflow/reconciliation", response_model=list[OverflowReviewItem])
def list_overflow_review(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return list_unresolved_overflow(conn, limit=limit)


@router.post("/inbound-email-work/overflow/confirm-sent", response_model=ConfirmedOverflowSent)
def confirm_overflow_review(
    body: ConfirmOverflowSentRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = confirm_overflow_sent(
            conn, batch_id=body.batch_id, actor=actor.user_id,
            expected_review_version=body.expected_review_version,
            expected_state=body.expected_state, expected_chat_id=body.expected_chat_id,
            expected_content_sha256=body.expected_content_sha256,
            telegram_message_ids=body.telegram_message_ids,
            evidence_ref=body.evidence_ref,
            full_content_verified=body.full_content_verified,
        )
    except KeyError:
        raise HTTPException(404, "Overflow batch not found") from None
    except OverflowReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    conn.commit()
    return result


class PendingOverflowItem(BaseModel):
    email_id: str
    chat_id: int
    summary: str
    summary_sha256: str
    created_at: str
    route_state: str


class RebindPendingOverflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_id: str = Field(min_length=1, max_length=1000)
    expected_chat_id: int = Field(strict=True)
    expected_summary_sha256: str = Field(min_length=64, max_length=64)
    target_chat_id: int = Field(strict=True)
    evidence_ref: str = Field(min_length=1, max_length=2000)
    recipient_verified: bool = Field(strict=True)


class ReboundPendingOverflow(BaseModel):
    email_id: str
    old_chat_id: int
    chat_id: int
    summary_sha256: str


@router.get("/inbound-email-work/overflow/pending-other-recipient",
            response_model=list[PendingOverflowItem])
def list_pending_other_recipient(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return list_other_recipient_items(conn, current_chat_id=CONFIG.telegram.chat_id, limit=limit)


@router.post("/inbound-email-work/overflow/rebind-pending",
             response_model=ReboundPendingOverflow)
def rebind_pending_overflow(
    body: RebindPendingOverflowRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_review_admin(actor)
    try:
        result = rebind_pending_item(
            conn, email_id=body.email_id, actor=actor.user_id,
            expected_chat_id=body.expected_chat_id,
            expected_summary_sha256=body.expected_summary_sha256,
            target_chat_id=body.target_chat_id,
            current_chat_id=CONFIG.telegram.chat_id,
            evidence_ref=body.evidence_ref,
            recipient_verified=body.recipient_verified,
        )
    except KeyError:
        raise HTTPException(404, "Pending overflow source not found") from None
    except PendingOverflowError as exc:
        raise HTTPException(409, str(exc)) from None
    conn.commit()
    return result
