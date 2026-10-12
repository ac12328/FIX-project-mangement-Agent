"""Canonical, approval-bound calendar action payloads."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from config.settings import CONFIG
from src.governance.action_service import create_action_intent
from src.models.action_intent import ActionIntent
from src.repositories.calendar_event_repo import get_calendar_event

_ALLOWED_FIELDS = {
    "subject",
    "start",
    "end",
    "body",
    "location",
    "attendees",
    "isOnlineMeeting",
    "onlineMeetingProvider",
    "showAs",
    "sensitivity",
}


def create_calendar_action_intent(
    conn: sqlite3.Connection,
    *,
    operation: str,
    operator_id: str,
    surface: str,
    calendar_event_id: str | None = None,
    event_payload: dict | None = None,
) -> ActionIntent:
    if operation not in {"create", "update", "cancel"}:
        raise ValueError(f"Unsupported calendar operation: {operation}")
    calendar_id = CONFIG.msgraph.calendar_id
    payload = _canonical_payload(operation, calendar_id, event_payload or {})
    target_version = None
    if operation == "create":
        _validate_event_payload(payload["event"], require_times=True)
        target_id = calendar_event_id or f"new_{uuid.uuid4().hex[:12]}"
    else:
        if not calendar_event_id:
            raise ValueError("calendar_event_id is required")
        event = get_calendar_event(conn, calendar_event_id, operator_id)
        if event is None or event.cancelled:
            raise ValueError("Calendar event not found")
        target_id = calendar_event_id
        target_version = _event_version(event.source_etag, event.last_modified_at)
        if operation == "update":
            _validate_event_payload(payload["event"], require_times=False)
            if not payload["event"]:
                raise ValueError("At least one calendar event field is required")
        else:
            payload.pop("event", None)
    return create_action_intent(
        conn,
        operator_id=operator_id,
        action_type=f"calendar_{operation}",
        risk_class="high",
        target_type="calendar_event",
        target_id=target_id,
        target_version=target_version,
        payload=payload,
        created_by_surface=surface,
        idempotency_key=(
            f"calendar:{operation}:{target_id}:{target_version or 'new'}:"
            f"{datetime.now(timezone.utc).date().isoformat()}"
        ),
    )


def _canonical_payload(operation: str, calendar_id: str, event: dict) -> dict:
    sanitized = {
        key: value for key, value in event.items()
        if key in _ALLOWED_FIELDS and value is not None
    }
    return {
        "operation": operation,
        "calendar_id": calendar_id,
        "event": sanitized,
    }


def _validate_event_payload(event: dict, *, require_times: bool) -> None:
    if require_times and not str(event.get("subject") or "").strip():
        raise ValueError("Calendar subject is required")
    if require_times and (not event.get("start") or not event.get("end")):
        raise ValueError("Calendar start and end are required")
    for key in ("start", "end"):
        value = event.get(key)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"Calendar {key} must be a Graph dateTime/timeZone object")


def _event_version(etag: str | None, last_modified_at: str | None) -> str | None:
    return etag or last_modified_at
