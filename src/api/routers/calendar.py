"""Read-only calendar API backed by the local Microsoft Graph cache."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from config import defaults
from config.settings import CONFIG
from src.api.auth import CurrentActor
from src.auth.principals import SHARED_MAILBOX_PRINCIPAL
from src.api.deps import get_current_actor, get_read_conn, get_write_actor, get_write_conn
from src.repositories.calendar_event_repo import get_calendar_event, list_calendar_events
from src.repositories.meeting_prep_repo import get_meeting_prep
from src.scheduling.calendar_sync import sync_calendar_events

router = APIRouter(tags=["calendar"])


class MeetingPrepRequest(BaseModel):
    project_id: str | None = None
    refresh: bool = False


class MeetingIngestionRequest(BaseModel):
    source_text: str


class CalendarActionRequest(BaseModel):
    operation: str
    calendar_event_id: str | None = None
    event: dict = Field(default_factory=dict)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _require_feature(
    *,
    refresh: bool = False,
    meeting_prep: bool = False,
    meeting_ingestion: bool = False,
) -> None:
    if not (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED
    ):
        raise HTTPException(404, "Calendar read is disabled")
    if meeting_prep and not defaults.PERSONAL_ASSISTANT_MEETING_PREP_ENABLED:
        raise HTTPException(404, "Meeting preparation is disabled")
    if (
        meeting_ingestion
        and not defaults.PERSONAL_ASSISTANT_MEETING_INGESTION_ENABLED
    ):
        raise HTTPException(404, "Meeting ingestion is disabled")
    if refresh and not (CONFIG.msgraph.enabled and CONFIG.msgraph.calendar_enabled):
        raise HTTPException(409, "Microsoft Graph calendar sync is disabled")


@router.get("/calendar/events")
def list_events(
    start_at: str | None = None,
    end_at: str | None = None,
    days: int = Query(default=7, ge=1, le=90),
    include_cancelled: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[dict]:
    _require_feature()
    try:
        if start_at is None:
            start_at = datetime.now(timezone.utc).isoformat()
        else:
            start_at = _parse_timestamp(start_at).isoformat()
        if end_at is None:
            end_at = (_parse_timestamp(start_at) + timedelta(days=days)).isoformat()
        else:
            end_at = _parse_timestamp(end_at).isoformat()
        if end_at < start_at:
            raise ValueError("end_at must not be earlier than start_at")
        events = list_calendar_events(
            conn,
            SHARED_MAILBOX_PRINCIPAL,
            start_at=start_at,
            end_at=end_at,
            include_cancelled=include_cancelled,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(422, f"Invalid calendar time window: {exc}")
    return [event.model_dump() for event in events]


@router.get("/calendar/events/{calendar_event_id}")
def get_event(
    calendar_event_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict:
    _require_feature()
    event = get_calendar_event(conn, calendar_event_id, SHARED_MAILBOX_PRINCIPAL)
    if event is None:
        raise HTTPException(404, "Calendar event not found")
    return event.model_dump()


@router.get("/calendar/events/{calendar_event_id}/prep")
def get_meeting_preparation(
    calendar_event_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> dict:
    _require_feature(meeting_prep=True)
    event = get_calendar_event(conn, calendar_event_id, SHARED_MAILBOX_PRINCIPAL)
    if event is None:
        raise HTTPException(404, "Calendar event not found")
    snapshot = get_meeting_prep(conn, calendar_event_id, actor.operator_id)
    if snapshot is None:
        raise HTTPException(404, "Meeting preparation not found")
    return snapshot.model_dump()


@router.post("/calendar/events/{calendar_event_id}/prep")
def generate_meeting_preparation(
    calendar_event_id: str,
    body: MeetingPrepRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_feature(meeting_prep=True)
    from src.assistant.meeting_prep import build_meeting_prep

    try:
        snapshot = build_meeting_prep(
            conn,
            calendar_event_id=calendar_event_id,
            operator_id=actor.user_id,
            calendar_operator_id=SHARED_MAILBOX_PRINCIPAL,
            project_id=body.project_id,
            refresh=body.refresh,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return snapshot.model_dump()


@router.get("/calendar/events/{calendar_event_id}/ingestions")
def list_meeting_note_ingestions(
    calendar_event_id: str,
    actor: CurrentActor = Depends(get_current_actor),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[dict]:
    _require_feature(meeting_ingestion=True)
    if get_calendar_event(conn, calendar_event_id, SHARED_MAILBOX_PRINCIPAL) is None:
        raise HTTPException(404, "Calendar event not found")
    from src.repositories.meeting_ingestion_repo import list_meeting_ingestions

    return [
        item.model_dump()
        for item in list_meeting_ingestions(
            conn,
            operator_id=actor.user_id,
            calendar_event_id=calendar_event_id,
        )
    ]


@router.post("/calendar/events/{calendar_event_id}/ingestions")
def ingest_meeting_notes(
    calendar_event_id: str,
    body: MeetingIngestionRequest,
    actor: CurrentActor = Depends(get_write_actor),
) -> dict:
    _require_feature(meeting_ingestion=True)
    from src.assistant.meeting_ingestion import ingest_meeting_notes_scoped as ingest

    try:
        ingestion = ingest(
            calendar_event_id=calendar_event_id,
            source_text=body.source_text,
            operator_id=actor.user_id,
            calendar_operator_id=SHARED_MAILBOX_PRINCIPAL,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Meeting ingestion failed: {exc}")
    return ingestion.model_dump()


@router.post("/calendar/actions")
def propose_calendar_action(
    body: CalendarActionRequest,
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_feature()
    if not defaults.PERSONAL_ASSISTANT_CALENDAR_WRITE_ENABLED:
        raise HTTPException(404, "Calendar write is disabled")
    from src.governance.calendar_actions import create_calendar_action_intent

    try:
        intent = create_calendar_action_intent(
            conn,
            operation=body.operation,
            operator_id=actor.user_id,
            surface=actor.surface,
            calendar_event_id=body.calendar_event_id,
            event_payload=body.event,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return intent.model_dump()


@router.post("/calendar/refresh")
def refresh_calendar(
    actor: CurrentActor = Depends(get_write_actor),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    _require_feature(refresh=True)
    try:
        return sync_calendar_events(conn)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Calendar synchronization failed: {exc}")
