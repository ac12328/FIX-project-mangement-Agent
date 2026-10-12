"""Deterministic meeting preparation from cached, attributable context."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from src.models.meeting_prep import MeetingPrepSnapshot
from src.repositories.calendar_event_repo import get_calendar_event
from src.repositories.meeting_prep_repo import get_meeting_prep, save_meeting_prep
from src.repositories.operator_memory_repo import list_memories
from src.repositories.personal_work_item_repo import list_personal_work_items
from src.repositories.project_repo import get_project


def build_meeting_prep(
    conn: sqlite3.Connection,
    *,
    calendar_event_id: str,
    operator_id: str = "default_operator",
    calendar_operator_id: str | None = None,
    project_id: str | None = None,
    refresh: bool = False,
) -> MeetingPrepSnapshot:
    event = get_calendar_event(
        conn, calendar_event_id, calendar_operator_id or operator_id
    )
    if event is None or event.cancelled:
        raise ValueError("Calendar event not found")
    if not refresh:
        cached = get_meeting_prep(
            conn,
            calendar_event_id,
            operator_id,
            event_raw_hash=event.raw_hash,
        )
        if cached is not None:
            return cached

    linked_project_id = project_id or _resolve_project_id(conn, event.attendees, operator_id)
    project = get_project(conn, linked_project_id) if linked_project_id else None
    if project is not None and project.operator_id != operator_id:
        raise ValueError("Project not found")

    attendees = _attendee_context(conn, event.attendees)
    work_items = _related_work_items(conn, operator_id, linked_project_id, calendar_event_id)
    memories = _meeting_memories(conn, operator_id, calendar_event_id, linked_project_id, attendees)
    evidence = _recent_evidence(conn, linked_project_id)
    project_context = _project_context(project) if project else None
    agenda = _agenda(project_context, work_items)
    decisions = _open_decisions(project_context, work_items)
    talking_points = _talking_points(project_context, work_items, evidence)
    generated_at = datetime.now(timezone.utc).isoformat()

    snapshot = MeetingPrepSnapshot(
        prep_id=f"prep_{uuid.uuid4().hex[:12]}",
        operator_id=operator_id,
        calendar_event_id=calendar_event_id,
        event_raw_hash=event.raw_hash,
        project_id=linked_project_id,
        generated_at=generated_at,
        meeting=event.model_dump(),
        attendees=attendees,
        project_context=project_context,
        open_work_items=work_items,
        relevant_memories=memories,
        recent_evidence=evidence,
        agenda=agenda,
        open_decisions=decisions,
        talking_points=talking_points,
        provenance=_provenance(calendar_event_id, linked_project_id, attendees, evidence),
    )
    return save_meeting_prep(conn, snapshot)


def _resolve_project_id(
    conn: sqlite3.Connection,
    attendees: list[dict],
    operator_id: str,
) -> str | None:
    emails = [str(item.get("email") or "").lower() for item in attendees]
    emails = [email for email in emails if email]
    if not emails:
        return None
    placeholders = ",".join("?" for _ in emails)
    rows = conn.execute(
        f"""SELECT DISTINCT p.project_id
            FROM projects p
            LEFT JOIN contact_project_links l ON l.project_id = p.project_id
            LEFT JOIN contacts c ON c.contact_id = l.contact_id
            WHERE p.operator_id = ? AND (
                lower(p.client_email) IN ({placeholders})
                OR lower(c.primary_email) IN ({placeholders})
            )
            ORDER BY p.project_id""",
        [operator_id, *emails, *emails],
    ).fetchall()
    return rows[0]["project_id"] if len(rows) == 1 else None


def _attendee_context(conn: sqlite3.Connection, attendees: list[dict]) -> list[dict]:
    result = []
    for attendee in attendees:
        email = str(attendee.get("email") or "").lower()
        contact = conn.execute(
            """SELECT c.*, o.org_name
               FROM contacts c LEFT JOIN organisations o ON o.org_id = c.organisation_id
               WHERE lower(c.primary_email) = ? LIMIT 1""",
            (email,),
        ).fetchone() if email else None
        entry = dict(attendee)
        if contact:
            entry.update({
                "contact_id": contact["contact_id"],
                "display_name": contact["display_name"] or attendee.get("name"),
                "role": contact["role"],
                "organisation": contact["org_name"],
            })
        result.append(entry)
    return result


def _related_work_items(
    conn: sqlite3.Connection,
    operator_id: str,
    project_id: str | None,
    calendar_event_id: str,
) -> list[dict]:
    items = list_personal_work_items(conn, operator_id, status="open", limit=100)
    return [
        item.model_dump()
        for item in items
        if (
            item.related_entity_type == "calendar_event"
            and item.related_entity_id == calendar_event_id
        ) or (
            project_id
            and item.related_entity_type == "project"
            and item.related_entity_id == project_id
        )
    ][:20]


def _meeting_memories(
    conn: sqlite3.Connection,
    operator_id: str,
    calendar_event_id: str,
    project_id: str | None,
    attendees: list[dict],
) -> list[dict]:
    scopes = [("calendar_event", calendar_event_id)]
    if project_id:
        scopes.append(("project", project_id))
    scopes.extend(
        ("contact", attendee["contact_id"])
        for attendee in attendees
        if attendee.get("contact_id")
    )
    memories: list[dict] = []
    for scope, scope_id in scopes:
        memories.extend(list_memories(conn, operator_id, scope=scope, scope_id=scope_id))
    return memories[:20]


def _recent_evidence(conn: sqlite3.Connection, project_id: str | None) -> list[dict]:
    if not project_id:
        return []
    rows = conn.execute(
        """SELECT evidence_id, evidence_type, content_summary, source_ref, evidence_at
           FROM evidence_store WHERE project_id = ?
           ORDER BY evidence_at DESC LIMIT 10""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _project_context(project) -> dict:
    return {
        "project_id": project.project_id,
        "client_name": project.client_name,
        "project_status": project.project_status,
        "ball_with": project.ball_with,
        "current_milestone": project.current_milestone,
        "target_go_live": project.target_go_live,
        "next_action": project.next_action,
    }


def _agenda(project: dict | None, work_items: list[dict]) -> list[str]:
    agenda = []
    if project and project.get("next_action"):
        agenda.append(f"Review next action: {project['next_action']}")
    agenda.extend(f"Review open {item['kind']}: {item['title']}" for item in work_items[:5])
    return agenda or ["Confirm meeting objectives and desired outcomes"]


def _open_decisions(project: dict | None, work_items: list[dict]) -> list[str]:
    decisions = [
        item["title"] for item in work_items
        if item.get("review_status") == "proposed"
    ]
    if project and project.get("project_status") == "Blocked":
        decisions.append("Agree how to unblock the project")
    return decisions


def _talking_points(
    project: dict | None,
    work_items: list[dict],
    evidence: list[dict],
) -> list[str]:
    points = []
    if project:
        if project.get("current_milestone"):
            points.append(f"Current milestone: {project['current_milestone']}")
        if project.get("ball_with"):
            points.append(f"Current owner: {project['ball_with']}")
        if project.get("target_go_live"):
            points.append(f"Target go-live: {project['target_go_live']}")
    points.extend(f"Open item: {item['title']}" for item in work_items[:3])
    points.extend(f"Recent evidence: {item['content_summary']}" for item in evidence[:3])
    return points


def _provenance(
    calendar_event_id: str,
    project_id: str | None,
    attendees: list[dict],
    evidence: list[dict],
) -> list[dict[str, str]]:
    items = [{"source_type": "calendar_event", "source_ref": calendar_event_id}]
    if project_id:
        items.append({"source_type": "project", "source_ref": project_id})
    items.extend(
        {"source_type": "contact", "source_ref": item["contact_id"]}
        for item in attendees if item.get("contact_id")
    )
    items.extend(
        {"source_type": "evidence", "source_ref": item["evidence_id"]}
        for item in evidence
    )
    return items
