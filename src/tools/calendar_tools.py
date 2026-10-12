"""Read-only agent tools for cached calendar events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import defaults
from config.settings import CONFIG
from src.auth.principals import SHARED_MAILBOX_PRINCIPAL
from src.repositories.calendar_event_repo import get_calendar_event, list_calendar_events
from src.tools.contracts import ToolContract


LIST_CALENDAR_EVENTS = ToolContract(
    name="list_calendar_events",
    description=(
        "List the operator's cached calendar events for a UTC time window. "
        "Use this for upcoming meetings and schedule questions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "start_at": {"type": "string", "description": "ISO 8601 window start"},
            "end_at": {"type": "string", "description": "ISO 8601 window end"},
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
            "include_cancelled": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
)

PROPOSE_CALENDAR_ACTION = ToolContract(
    name="propose_calendar_action",
    description=(
        "Stage a calendar create, update, or cancel request in the Approval Center. "
        "This tool never changes Microsoft Graph directly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["create", "update", "cancel"]},
            "calendar_event_id": {"type": "string"},
            "event": {"type": "object"},
        },
        "required": ["operation"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    requires_operator_approval=True,
    audit_required=True,
)

INGEST_MEETING_NOTES = ToolContract(
    name="ingest_meeting_notes",
    description=(
        "Analyze operator-provided meeting notes and create evidence-backed proposed "
        "personal work items. No project truth is changed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_event_id": {"type": "string"},
            "source_text": {"type": "string"},
        },
        "required": ["calendar_event_id", "source_text"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    audit_required=True,
)

PREPARE_MEETING = ToolContract(
    name="prepare_meeting",
    description=(
        "Generate or retrieve an attributable preparation packet for a cached "
        "calendar event. Optionally provide an explicit linked project ID."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_event_id": {"type": "string"},
            "project_id": {"type": "string"},
            "refresh": {"type": "boolean"},
        },
        "required": ["calendar_event_id"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="write",
    audit_required=True,
)

GET_CALENDAR_EVENT = ToolContract(
    name="get_calendar_event",
    description="Get full cached details for one calendar event by event ID.",
    input_schema={
        "type": "object",
        "properties": {"calendar_event_id": {"type": "string"}},
        "required": ["calendar_event_id"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
)


def _enabled() -> bool:
    return (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def list_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled():
        return {"error": "Calendar read is disabled"}
    now = datetime.now(timezone.utc)
    try:
        start_at = (
            _parse_timestamp(args["start_at"]).isoformat()
            if args.get("start_at")
            else now.isoformat()
        )
        days = max(1, min(int(args.get("days", 7)), 90))
        end_at = (
            _parse_timestamp(args["end_at"]).isoformat()
            if args.get("end_at")
            else (_parse_timestamp(start_at) + timedelta(days=days)).isoformat()
        )
        if end_at < start_at:
            raise ValueError("end_at must not be earlier than start_at")
    except (TypeError, ValueError) as exc:
        return {"error": f"Invalid calendar time window: {exc}"}
    events = list_calendar_events(
        conn,
        SHARED_MAILBOX_PRINCIPAL,
        start_at=start_at,
        end_at=end_at,
        include_cancelled=bool(args.get("include_cancelled", False)),
        limit=max(1, min(int(args.get("limit", 50)), 100)),
    )
    return {
        "events": [event.model_dump() for event in events],
        "window_start": start_at,
        "window_end": end_at,
        "source": "local_calendar_cache",
    }


def get_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled():
        return {"error": "Calendar read is disabled"}
    event = get_calendar_event(
        conn,
        args["calendar_event_id"],
        SHARED_MAILBOX_PRINCIPAL,
    )
    if event is None:
        return {"error": "Calendar event not found"}
    return event.model_dump()


def prep_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled() or not defaults.PERSONAL_ASSISTANT_MEETING_PREP_ENABLED:
        return {"error": "Meeting preparation is disabled"}
    from src.assistant.meeting_prep import build_meeting_prep

    try:
        snapshot = build_meeting_prep(
            conn,
            calendar_event_id=args["calendar_event_id"],
            operator_id=str(args.get("_operator_id") or CONFIG.operator_id),
            calendar_operator_id=SHARED_MAILBOX_PRINCIPAL,
            project_id=args.get("project_id"),
            refresh=bool(args.get("refresh", False)),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return snapshot.model_dump()


def ingestion_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled() or not defaults.PERSONAL_ASSISTANT_MEETING_INGESTION_ENABLED:
        return {"error": "Meeting ingestion is disabled"}
    # A caller-owned writer cannot be released around an external LLM call.
    # Fail closed instead of committing or acquiring a nested write lease.
    if conn.in_transaction or not bool(conn.execute("PRAGMA query_only").fetchone()[0]):
        return {
            "error": "Meeting ingestion requires an idle read-only connection",
            "error_code": "meeting_ingestion_connection_ownership",
        }
    from contextlib import nullcontext
    from src.assistant.meeting_ingestion import ingest_meeting_notes_scoped
    from src.tools.executor import get_active_write_connection_factory

    try:
        ingestion = ingest_meeting_notes_scoped(
            calendar_event_id=args["calendar_event_id"],
            source_text=args["source_text"],
            operator_id=str(args.get("_operator_id") or CONFIG.operator_id),
            calendar_operator_id=SHARED_MAILBOX_PRINCIPAL,
            read_connection_factory=lambda: nullcontext(conn),
            write_connection_factory=get_active_write_connection_factory(),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return ingestion.model_dump()


def action_handler(conn: sqlite3.Connection, args: dict) -> dict:
    if not _enabled() or not defaults.PERSONAL_ASSISTANT_CALENDAR_WRITE_ENABLED:
        return {"error": "Calendar write is disabled"}
    from src.governance.calendar_actions import create_calendar_action_intent

    try:
        intent = create_calendar_action_intent(
            conn,
            operation=args["operation"],
            operator_id=str(args.get("_operator_id") or CONFIG.operator_id),
            surface="agent",
            calendar_event_id=args.get("calendar_event_id"),
            event_payload=args.get("event") or {},
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "action": "approval_required",
        "action_id": intent.action_id,
        "status": intent.status,
        "summary": f"Calendar {args['operation']} staged for Approval Center",
    }


CALENDAR_TOOL_HANDLERS: dict[str, Any] = {
    "list_calendar_events": list_handler,
    "get_calendar_event": get_handler,
    "prepare_meeting": prep_handler,
    "ingest_meeting_notes": ingestion_handler,
    "propose_calendar_action": action_handler,
}

CALENDAR_TOOLS = [
    LIST_CALENDAR_EVENTS,
    GET_CALENDAR_EVENT,
    PREPARE_MEETING,
    INGEST_MEETING_NOTES,
    PROPOSE_CALENDAR_ACTION,
]
