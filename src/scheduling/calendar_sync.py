"""Microsoft Graph calendar synchronization into the local read-only cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import CONFIG
from src.email.graph_client import GraphClient
from src.auth.principals import SHARED_MAILBOX_PRINCIPAL
from src.models.calendar_event import CalendarEvent
from src.repositories.calendar_event_repo import (
    mark_missing_events_cancelled,
    record_calendar_sync_error,
    record_calendar_sync_success,
    upsert_calendar_event,
)


def sync_calendar_events(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    client: GraphClient | None = None,
) -> dict:
    cfg = CONFIG.msgraph
    if not (cfg.enabled and cfg.calendar_enabled):
        raise ValueError("Microsoft Graph calendar sync is disabled")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = current - timedelta(days=max(0, cfg.calendar_past_days))
    window_end = current + timedelta(days=max(1, cfg.calendar_future_days))
    graph = client or GraphClient(
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        user_email=cfg.user_email,
    )
    try:
        raw_events = graph.fetch_calendar_view(
            calendar_id=cfg.calendar_id,
            start_datetime=window_start.isoformat(),
            end_datetime=window_end.isoformat(),
        )
        synced_at = datetime.now(timezone.utc).isoformat()
        events = [
            normalize_graph_event(
                raw,
                operator_id=SHARED_MAILBOX_PRINCIPAL,
                calendar_id=cfg.calendar_id,
                synced_at=synced_at,
            )
            for raw in raw_events
            if raw.get("id")
        ]
        for event in events:
            upsert_calendar_event(conn, event)
        cancelled = mark_missing_events_cancelled(
            conn,
            operator_id=SHARED_MAILBOX_PRINCIPAL,
            calendar_id=cfg.calendar_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            present_ids={event.calendar_event_id for event in events},
        )
        record_calendar_sync_success(
            conn,
            operator_id=SHARED_MAILBOX_PRINCIPAL,
            calendar_id=cfg.calendar_id,
            synced_at=synced_at,
        )
        conn.commit()
        return {
            "synced": len(events),
            "cancelled": cancelled,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "synced_at": synced_at,
        }
    except Exception as exc:
        record_calendar_sync_error(
            conn,
            operator_id=SHARED_MAILBOX_PRINCIPAL,
            calendar_id=cfg.calendar_id,
            error=str(exc),
        )
        conn.commit()
        raise


def normalize_graph_event(
    raw: dict,
    *,
    operator_id: str,
    calendar_id: str,
    synced_at: str,
) -> CalendarEvent:
    start_at, original_timezone = _graph_datetime(raw.get("start") or {})
    end_at, _ = _graph_datetime(raw.get("end") or {})
    organizer = (
        raw.get("organizer", {}).get("emailAddress", {}).get("address")
    )
    attendees = []
    for attendee in raw.get("attendees") or []:
        address = attendee.get("emailAddress") or {}
        if not address.get("address"):
            continue
        attendees.append({
            "name": address.get("name"),
            "email": address["address"],
            "type": attendee.get("type"),
            "response": (attendee.get("status") or {}).get("response"),
        })
    location = (raw.get("location") or {}).get("displayName") or None
    join_url = (
        (raw.get("onlineMeeting") or {}).get("joinUrl")
        or raw.get("onlineMeetingUrl")
    )
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CalendarEvent(
        calendar_event_id=raw["id"],
        operator_id=operator_id,
        calendar_id=calendar_id,
        subject=raw.get("subject") or "(No title)",
        start_at=start_at,
        end_at=end_at,
        original_timezone=original_timezone,
        location=location,
        organizer_email=organizer,
        attendees=attendees,
        is_online_meeting=bool(raw.get("isOnlineMeeting")),
        join_url=join_url,
        show_as=raw.get("showAs"),
        sensitivity=raw.get("sensitivity"),
        body_preview=raw.get("bodyPreview"),
        last_modified_at=raw.get("lastModifiedDateTime"),
        source_etag=raw.get("@odata.etag"),
        raw_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        synced_at=synced_at,
        cancelled=bool(raw.get("isCancelled")),
    )


def _graph_datetime(value: dict) -> tuple[str, str | None]:
    raw = value.get("dateTime")
    if not raw:
        raise ValueError("Calendar event is missing dateTime")
    timezone_name = value.get("timeZone") or "UTC"
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(), timezone_name
