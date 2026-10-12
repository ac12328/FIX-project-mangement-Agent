"""Typed read-only calendar event cached from Microsoft Graph."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CalendarEvent(BaseModel):
    calendar_event_id: str
    operator_id: str = "default_operator"
    calendar_id: str
    subject: str
    start_at: str
    end_at: str
    original_timezone: str | None = None
    location: str | None = None
    organizer_email: str | None = None
    attendees: list[dict[str, Any]] = Field(default_factory=list)
    is_online_meeting: bool = False
    join_url: str | None = None
    show_as: str | None = None
    sensitivity: str | None = None
    body_preview: str | None = None
    last_modified_at: str | None = None
    source_etag: str | None = None
    raw_hash: str
    synced_at: str
    cancelled: bool = False
