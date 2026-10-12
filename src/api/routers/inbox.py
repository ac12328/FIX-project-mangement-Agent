"""Actionable inbox intelligence API."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from config import defaults
from src.api.auth import CurrentActor
from src.auth.principals import SHARED_MAILBOX_PRINCIPAL
from src.api.deps import get_current_actor, get_read_conn, get_write_actor
from src.email.inbox_intelligence import (
    StaleInboxSourceError, analyze_inbox_email, backfill_inbox_insights,
)
from src.repositories.inbox_insight_repo import get_inbox_insight, list_inbox_insights

router = APIRouter(tags=["inbox"])


def _require_feature() -> None:
    if not defaults.PERSONAL_ASSISTANT_INBOX_INTELLIGENCE_ENABLED:
        raise HTTPException(404, "Inbox intelligence is disabled")


@router.get("/inbox/insights")
def list_insights(
    priority: str | None = None,
    action_required: bool | None = None,
    include_suppressed: bool = False,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[dict]:
    _require_feature()
    return [
        item.model_dump()
        for item in list_inbox_insights(
            conn,
            SHARED_MAILBOX_PRINCIPAL,
            priority=priority,
            action_required=action_required,
            include_suppressed=include_suppressed,
        )
    ]


@router.post("/inbox/insights/backfill")
def backfill_insights(
    limit: int = 1,
    actor: CurrentActor = Depends(get_write_actor),
) -> dict:
    _require_feature()
    return backfill_inbox_insights(limit=max(1, min(limit, 3)))


@router.get("/inbox/insights/{insight_id}")
def get_insight(
    insight_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict:
    _require_feature()
    insight = get_inbox_insight(conn, insight_id)
    if not insight or insight.operator_id != SHARED_MAILBOX_PRINCIPAL:
        raise HTTPException(404, "Inbox insight not found")
    return insight.model_dump()


@router.post("/inbox/emails/{email_id}/analyze")
def analyze_email(
    email_id: str,
    actor: CurrentActor = Depends(get_write_actor),
) -> dict:
    _require_feature()
    try:
        insight = analyze_inbox_email(
            None,
            email_id=email_id,
            operator_id=SHARED_MAILBOX_PRINCIPAL,
            work_item_operator_id=actor.user_id,
        )
    except StaleInboxSourceError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return insight.model_dump()
