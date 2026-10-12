"""Deterministic personal brief aggregation across work and project domains."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from config import defaults
from src.portfolio.morning_brief import generate_morning_brief
from src.repositories.action_intent_repo import list_action_intents
from src.repositories.inbox_insight_repo import list_inbox_insights
from src.repositories.personal_work_item_repo import list_personal_work_items

SECTION_ORDER = (
    "needs_decision",
    "due_or_at_risk",
    "meetings_to_prepare",
    "waiting_on_others",
    "important_messages",
    "project_portfolio",
    "fyi",
)


def build_personal_brief(
    conn: sqlite3.Connection,
    operator_id: str = "default_operator",
) -> dict:
    now = datetime.now(timezone.utc)
    sections = {key: [] for key in SECTION_ORDER}

    for intent in list_action_intents(conn, operator_id, status="pending", limit=50):
        sections["needs_decision"].append({
            "id": intent.action_id,
            "type": "approval",
            "title": f"Approve {intent.action_type.replace('_', ' ')}",
            "reason": f"{intent.risk_class.capitalize()}-risk action needs approval",
            "due_at": intent.expires_at,
            "source_ref": intent.target_id,
            "suggested_action": "Review approval",
        })

    items = list_personal_work_items(conn, operator_id, status="open", limit=100)
    for item in items:
        entry = {
            "id": item.work_item_id,
            "type": item.kind,
            "title": item.title,
            "reason": _work_reason(item, now),
            "due_at": item.due_at or item.remind_at,
            "source_ref": item.source_ref,
            "evidence": item.evidence_text,
            "suggested_action": _suggested_action(item.kind),
            "review_status": item.review_status,
        }
        if item.review_status == "proposed":
            sections["needs_decision"].append(entry)
        elif item.kind == "waiting_for":
            sections["waiting_on_others"].append(entry)
        elif item.due_at or item.remind_at:
            sections["due_or_at_risk"].append(entry)
        else:
            sections["fyi"].append(entry)

    try:
        inbox_insights = list_inbox_insights(
            conn,
            operator_id,
            action_required=True,
            limit=50,
        )
    except sqlite3.OperationalError:
        inbox_insights = []
    for insight in inbox_insights:
        if insight.suppressed:
            continue
        sections["important_messages"].append({
            "id": insight.insight_id,
            "type": "inbox_insight",
            "title": insight.request_summary or f"Email {insight.email_id}",
            "reason": "; ".join(insight.priority_reasons) or "Action required",
            "due_at": insight.due_at,
            "source_ref": insight.email_id,
            "evidence": insight.evidence_spans,
            "suggested_action": insight.suggested_action or "Review email",
            "priority": insight.priority,
        })

    if (
        defaults.PERSONAL_ASSISTANT_ENABLED
        and defaults.PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED
    ):
        from src.repositories.calendar_event_repo import list_calendar_events

        meeting_window_end = (now + timedelta(days=2)).isoformat()
        try:
            meetings = list_calendar_events(
                conn,
                operator_id,
                start_at=now.isoformat(),
                end_at=meeting_window_end,
                limit=25,
            )
        except sqlite3.OperationalError:
            meetings = []
        for meeting in meetings:
            attendee_count = len(meeting.attendees)
            sections["meetings_to_prepare"].append({
                "id": meeting.calendar_event_id,
                "type": "calendar_event",
                "title": meeting.subject,
                "reason": _meeting_reason(meeting.start_at, attendee_count, now),
                "due_at": meeting.start_at,
                "source_ref": meeting.calendar_event_id,
                "suggested_action": "Prepare meeting",
                "location": meeting.location,
                "join_url": meeting.join_url,
                "organizer_email": meeting.organizer_email,
                "attendee_count": attendee_count,
            })

    project_brief = generate_morning_brief(conn, operator_id)
    for item in project_brief.red_items + project_brief.yellow_items:
        sections["project_portfolio"].append({
            "id": item.project_id,
            "type": "project",
            "title": item.client_name or item.project_id,
            "reason": item.urgency_reasons[0] if item.urgency_reasons else "Needs attention",
            "source_ref": item.project_id,
            "suggested_action": item.next_action or "Review project",
            "urgency": item.urgency,
            "score": item.urgency_score,
        })

    for entries in sections.values():
        entries.sort(key=lambda entry: (entry.get("due_at") or "9999", entry["title"]))

    return {
        "generated_at": now.isoformat(),
        "operator_id": operator_id,
        "sections": sections,
        "summary": {
            "needs_decision": len(sections["needs_decision"]),
            "due_or_at_risk": len(sections["due_or_at_risk"]),
            "meetings_to_prepare": len(sections["meetings_to_prepare"]),
            "waiting_on_others": len(sections["waiting_on_others"]),
            "project_alerts": len(sections["project_portfolio"]),
            "total_active_projects": project_brief.total_active,
        },
        "portfolio": {
            "generated_at": project_brief.generated_at,
            "total_active": project_brief.total_active,
            "action_items": [asdict(item) for item in project_brief.action_items],
        },
    }


def save_personal_brief_snapshot(
    conn: sqlite3.Connection, payload: dict, *, commit: bool = True
) -> str:
    snapshot_id = f"brief_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO personal_brief_snapshots
           (snapshot_id, operator_id, generated_at, payload_json)
           VALUES (?, ?, ?, ?)""",
        (
            snapshot_id,
            payload["operator_id"],
            payload["generated_at"],
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    if commit:
        conn.commit()
    return snapshot_id


def get_latest_personal_brief(
    conn: sqlite3.Connection, operator_id: str
) -> dict | None:
    row = conn.execute(
        """SELECT snapshot_id, payload_json, feedback_json
           FROM personal_brief_snapshots
           WHERE operator_id = ? ORDER BY generated_at DESC LIMIT 1""",
        (operator_id,),
    ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload_json"])
    payload["snapshot_id"] = row["snapshot_id"]
    payload["feedback"] = json.loads(row["feedback_json"] or "[]")
    return payload


def add_brief_feedback(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    item_id: str,
    feedback_type: str,
    commit: bool = True,
) -> bool:
    row = conn.execute(
        "SELECT feedback_json FROM personal_brief_snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if not row:
        return False
    feedback = json.loads(row["feedback_json"] or "[]")
    feedback.append({
        "item_id": item_id,
        "feedback_type": feedback_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    conn.execute(
        "UPDATE personal_brief_snapshots SET feedback_json = ? WHERE snapshot_id = ?",
        (json.dumps(feedback, ensure_ascii=False), snapshot_id),
    )
    if commit:
        conn.commit()
    return True


def format_personal_brief(payload: dict) -> str:
    labels = {
        "needs_decision": "Needs decision",
        "due_or_at_risk": "Due or at risk",
        "meetings_to_prepare": "Meetings to prepare",
        "waiting_on_others": "Waiting on others",
        "important_messages": "Important messages",
        "project_portfolio": "Project portfolio",
        "fyi": "FYI",
    }
    lines = ["🧭 Personal Work Brief"]
    for key in SECTION_ORDER:
        entries = payload["sections"][key]
        if not entries:
            continue
        lines.extend(["", f"**{labels[key]}**"])
        for entry in entries[:8]:
            lines.append(f"• {entry['title']} — {entry['reason']}")
    if len(lines) == 1:
        lines.extend(["", "No items need attention."])
    return "\n".join(lines)


def _meeting_reason(start_at: str, attendee_count: int, now: datetime) -> str:
    try:
        start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        delta = start.astimezone(timezone.utc) - now
        if delta <= timedelta(hours=2):
            timing = "Starts within 2 hours"
        elif start.date() == now.date():
            timing = "Later today"
        elif start.date() == (now + timedelta(days=1)).date():
            timing = "Tomorrow"
        else:
            timing = f"Starts {start_at}"
    except ValueError:
        timing = f"Starts {start_at}"
    attendees = f"{attendee_count} attendee" + ("" if attendee_count == 1 else "s")
    return f"{timing}; {attendees}"


def _work_reason(item, now: datetime) -> str:
    target = item.due_at or item.remind_at
    if not target:
        return "Open personal work item"
    try:
        parsed = datetime.fromisoformat(target.replace("Z", "+00:00"))
    except ValueError:
        return f"Scheduled for {target}"
    if parsed < now:
        return f"Overdue since {target}"
    return f"Due {target}"


def _suggested_action(kind: str) -> str:
    return {
        "task": "Complete task",
        "commitment": "Review commitment",
        "waiting_for": "Follow up",
        "reminder": "Review reminder",
    }.get(kind, "Review item")
