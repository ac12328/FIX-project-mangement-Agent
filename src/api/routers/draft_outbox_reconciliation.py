"""Admin-only review of indeterminate outbound draft delivery."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_scope
from src.api.routers.draft_workspace_common import publish_workspace_event
from src.auth.rbac import P_ADMIN_SECURITY, require_permission
from src.email.outbox_reconciliation import (
    DraftOutboxReconciliationError, list_uncertain_outbox, reconcile_uncertain_outbox,
)

router = APIRouter(tags=["draft-outbox-reconciliation"])


def _require_review_admin(actor: CurrentActor) -> None:
    if not CONFIG.dashboard_auth.enabled or actor.auth_method == "disabled":
        raise HTTPException(403, "Authenticated admin session required")
    require_permission(actor, P_ADMIN_SECURITY)


class ReconcileDraftOutboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed_sent", "confirmed_not_sent"]
    expected_updated_at: str = Field(min_length=1, max_length=100)
    expected_attempt_count: StrictInt = Field(ge=1)
    provider_evidence_ref: str = Field(min_length=1, max_length=2000)
    recipient_evidence_ref: str = Field(min_length=1, max_length=2000)
    full_recipient_set_verified: StrictBool
    provider_message_id: str | None = Field(default=None, max_length=1000)


class OutboxRecipientSet(BaseModel):
    to: list[str]
    cc: list[str]
    bcc: list[str]


class OutboxReviewItem(BaseModel):
    outbox_id: str
    draft_id: str
    status: Literal["uncertain"]
    created_by: str
    created_at: str
    updated_at: str
    attempt_count: int
    last_error: str | None
    provider_result: dict[str, Any] | None
    snapshot_hash: str
    subject: str | None
    recipients: OutboxRecipientSet | None
    review_blocked_reason: str | None = None


class OutboxReconcileResult(BaseModel):
    outbox_id: str
    draft_id: str
    status: Literal["sent", "failed"]
    updated_at: str
    attempts: int

@router.get("/draft-outbox/reconciliation", response_model=list[OutboxReviewItem])
def list_reconciliation(
    limit: int = Query(default=50, ge=1, le=100),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[dict]:
    _require_review_admin(actor)
    return list_uncertain_outbox(conn, limit=limit)


@router.post("/draft-outbox/{outbox_id}/reconcile", response_model=OutboxReconcileResult)
def reconcile_outbox(
    outbox_id: str, body: ReconcileDraftOutboxRequest,
    actor: CurrentActor = Depends(get_write_actor), write_scope=Depends(get_write_scope),
) -> dict:
    _require_review_admin(actor)
    try:
        with write_scope() as conn:
            result = reconcile_uncertain_outbox(
                conn, outbox_id=outbox_id, actor=actor.user_id,
                **body.model_dump(),
            )
    except KeyError:
        raise HTTPException(404, "Draft outbox record not found") from None
    except DraftOutboxReconciliationError as exc:
        raise HTTPException(409, str(exc)) from None
    publish_workspace_event(result["draft_id"], "draft_send_reconciled", {"outbox_id": outbox_id})
    return result
