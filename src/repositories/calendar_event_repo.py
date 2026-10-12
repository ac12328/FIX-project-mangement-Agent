"""Persistence for the read-only Microsoft Graph calendar cache."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from src.models.calendar_event import CalendarEvent


def upsert_calendar_event(conn: sqlite3.Connection, event: CalendarEvent) -> None:
    conn.execute(
        """INSERT INTO calendar_events (
               calendar_event_id, operator_id, calendar_id, subject, start_at,
               end_at, original_timezone, location, organizer_email,
               attendees_json, is_online_meeting, join_url, show_as,
               sensitivity, body_preview, last_modified_at, source_etag,
               raw_hash, synced_at, cancelled
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(calendar_event_id) DO UPDATE SET
               operator_id = excluded.operator_id,
               calendar_id = excluded.calendar_id,
               subject = excluded.subject,
               start_at = excluded.start_at,
               end_at = excluded.end_at,
               original_timezone = excluded.original_timezone,
               location = excluded.location,
               organizer_email = excluded.organizer_email,
               attendees_json = excluded.attendees_json,
               is_online_meeting = excluded.is_online_meeting,
               join_url = excluded.join_url,
               show_as = excluded.show_as,
               sensitivity = excluded.sensitivity,
               body_preview = excluded.body_preview,
               last_modified_at = excluded.last_modified_at,
               source_etag = excluded.source_etag,
               raw_hash = excluded.raw_hash,
               synced_at = excluded.synced_at,
               cancelled = excluded.cancelled""",
        (
            event.calendar_event_id,
            event.operator_id,
            event.calendar_id,
            event.subject,
            event.start_at,
            event.end_at,
            event.original_timezone,
            event.location,
            event.organizer_email,
            json.dumps(event.attendees, ensure_ascii=False),
            int(event.is_online_meeting),
            event.join_url,
            event.show_as,
            event.sensitivity,
            event.body_preview,
            event.last_modified_at,
            event.source_etag,
            event.raw_hash,
            event.synced_at,
            int(event.cancelled),
        ),
    )


def get_calendar_event(
    conn: sqlite3.Connection,
    calendar_event_id: str,
    operator_id: str = "default_operator",
) -> CalendarEvent | None:
    row = conn.execute(
        """SELECT * FROM calendar_events
           WHERE calendar_event_id = ? AND operator_id = ?""",
        (calendar_event_id, operator_id),
    ).fetchone()
    return _row(row) if row else None


def list_calendar_events(
    conn: sqlite3.Connection,
    operator_id: str,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    include_cancelled: bool = False,
    limit: int = 100,
) -> list[CalendarEvent]:
    clauses = ["operator_id = ?"]
    params: list = [operator_id]
    if start_at:
        clauses.append("end_at >= ?")
        params.append(start_at)
    if end_at:
        clauses.append("start_at <= ?")
        params.append(end_at)
    if not include_cancelled:
        clauses.append("cancelled = 0")
    params.append(max(1, min(limit, 500)))
    rows = conn.execute(
        f"""SELECT * FROM calendar_events
            WHERE {' AND '.join(clauses)}
            ORDER BY start_at ASC LIMIT ?""",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def mark_missing_events_cancelled(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    calendar_id: str,
    window_start: str,
    window_end: str,
    present_ids: set[str],
) -> int:
    rows = conn.execute(
        """SELECT calendar_event_id FROM calendar_events
           WHERE operator_id = ? AND calendar_id = ?
             AND end_at >= ? AND start_at <= ? AND cancelled = 0""",
        (operator_id, calendar_id, window_start, window_end),
    ).fetchall()
    missing = [row["calendar_event_id"] for row in rows if row["calendar_event_id"] not in present_ids]
    if not missing:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """UPDATE calendar_events SET cancelled = 1, synced_at = ?
           WHERE calendar_event_id = ?""",
        [(now, event_id) for event_id in missing],
    )
    return len(missing)


def record_calendar_sync_success(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    calendar_id: str,
    synced_at: str,
) -> None:
    conn.execute(
        """INSERT INTO calendar_sync_state (
               operator_id, calendar_id, provider, last_success_at,
               last_error_at, last_error, updated_at
           ) VALUES (?, ?, 'msgraph', ?, NULL, NULL, ?)
           ON CONFLICT(operator_id, calendar_id, provider) DO UPDATE SET
               last_success_at = excluded.last_success_at,
               last_error_at = NULL,
               last_error = NULL,
               updated_at = excluded.updated_at""",
        (operator_id, calendar_id, synced_at, synced_at),
    )


def record_calendar_sync_error(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    calendar_id: str,
    error: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO calendar_sync_state (
               operator_id, calendar_id, provider, last_error_at,
               last_error, updated_at
           ) VALUES (?, ?, 'msgraph', ?, ?, ?)
           ON CONFLICT(operator_id, calendar_id, provider) DO UPDATE SET
               last_error_at = excluded.last_error_at,
               last_error = excluded.last_error,
               updated_at = excluded.updated_at""",
        (operator_id, calendar_id, now, error[:1000], now),
    )


def _row(row: sqlite3.Row) -> CalendarEvent:
    data = dict(row)
    data["attendees"] = json.loads(data.pop("attendees_json") or "[]")
    data["is_online_meeting"] = bool(data["is_online_meeting"])
    data["cancelled"] = bool(data["cancelled"])
    return CalendarEvent(**data)
