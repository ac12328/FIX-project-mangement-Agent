"""Desk-queue snapshot for Telegram /brief and /support."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.support.pm_queue import (
    ACTIVE_TICKET_STATUSES,
    TicketQueueInput,
    TicketQueueView,
    derive_ticket_queue,
    queue_counts,
)

_NEXT_LABELS = {
    "reply": "Reply",
    "follow_up": "Follow up",
    "close_stale": "Close",
    "link_project": "Link project",
    "review": "Review",
}


@dataclass
class DeskBriefLine:
    ticket_id: str
    display_title: str
    party_name: str | None
    case_type: str
    next_action: str
    next_label: str
    age_days: int | None
    last_preview: str | None
    queue: str
    title: str


@dataclass
class DeskBriefSnapshot:
    needs: list[DeskBriefLine] = field(default_factory=list)
    needs_count: int = 0
    waiting_count: int = 0
    stale_count: int = 0
    noise_count: int = 0
    on_project_count: int = 0
    all_active: int = 0
    listed: list[DeskBriefLine] = field(default_factory=list)


def _latest_mail(conn: sqlite3.Connection, ticket_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ticket_ids:
        return {}
    placeholders = ",".join("?" for _ in ticket_ids)
    rows = conn.execute(
        f"""SELECT te.ticket_id, te.content_summary, te.content_text, te.created_at,
                   pe.sender_email, pe.subject, pe.received_at
            FROM ticket_evidence te
            LEFT JOIN processed_emails pe ON pe.email_id = te.source_ref
            WHERE te.ticket_id IN ({placeholders})
            ORDER BY te.created_at DESC, te.evidence_id DESC""",
        ticket_ids,
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticket_id = str(row["ticket_id"])
        if ticket_id not in latest:
            latest[ticket_id] = dict(row)
    return latest


def _line_from_view(ticket_id: str, title: str, view: TicketQueueView) -> DeskBriefLine:
    return DeskBriefLine(
        ticket_id=ticket_id,
        display_title=view.display_title or title,
        party_name=view.party_name,
        case_type=view.case_type,
        next_action=view.next_action,
        next_label=_NEXT_LABELS.get(view.next_action, view.next_action),
        age_days=view.age_days,
        last_preview=view.last_preview,
        queue=view.queue,
        title=title,
    )


def collect_desk_snapshot(
    conn: sqlite3.Connection,
    *,
    list_queue: str = "needs_me",
    limit: int = 5,
) -> DeskBriefSnapshot:
    placeholders = ",".join("?" for _ in ACTIVE_TICKET_STATUSES)
    rows = conn.execute(
        f"""SELECT s.ticket_id, s.title, s.category, s.severity, s.status,
                    s.related_project_id, s.description, s.reporter_email,
                    COALESCE(o.org_name, s.counterparty) AS counterparty,
                    s.updated_at
             FROM support_tickets s
             LEFT JOIN organisations o ON o.org_id = s.counterparty_org_id
             WHERE s.status IN ({placeholders})""",
        ACTIVE_TICKET_STATUSES,
    ).fetchall()
    latest = _latest_mail(conn, [str(row["ticket_id"]) for row in rows])
    pairs: list[tuple[str, TicketQueueView, DeskBriefLine]] = []
    for row in rows:
        data = dict(row)
        mail = latest.get(str(data["ticket_id"]), {})
        view = derive_ticket_queue(
            TicketQueueInput(
                ticket_id=str(data["ticket_id"]),
                title=data["title"],
                category=data["category"],
                severity=data["severity"],
                status=data["status"],
                description=data.get("description"),
                related_project_id=data.get("related_project_id"),
                counterparty=data.get("counterparty"),
                party_name=data.get("counterparty"),
                reporter_email=data.get("reporter_email"),
                last_sender=mail.get("sender_email"),
                last_subject=mail.get("subject"),
                last_preview=mail.get("content_text") or mail.get("content_summary"),
                last_activity_at=mail.get("received_at") or mail.get("created_at") or data.get("updated_at"),
                updated_at=data.get("updated_at"),
            ),
        )
        pairs.append((str(data["status"]), view, _line_from_view(str(data["ticket_id"]), data["title"], view)))
    counts = queue_counts((status, view) for status, view, _line in pairs)
    if list_queue == "all":
        listed = [line for _status, view, line in pairs if not view.is_noise]
    elif list_queue == "on_project":
        listed = [line for _status, view, line in pairs if view.on_project]
    else:
        listed = [line for _status, view, line in pairs if view.queue == list_queue]
    listed.sort(key=lambda item: (item.age_days is None, -(item.age_days or 0)))
    needs = [line for _status, view, line in pairs if view.queue == "needs_me"]
    needs.sort(key=lambda item: (item.age_days is None, -(item.age_days or 0)))
    return DeskBriefSnapshot(
        needs=needs[:limit],
        needs_count=len(needs),
        waiting_count=counts.get("waiting", 0),
        stale_count=counts.get("stale", 0),
        noise_count=counts.get("noise", 0),
        on_project_count=counts.get("on_project", 0),
        all_active=counts.get("all_active", 0),
        listed=listed[:limit],
    )


def format_desk_brief_section(snapshot: DeskBriefSnapshot, *, header: str, counts: str, hint: str, empty: str) -> list[str]:
    lines = [header, counts.format(
        needs=snapshot.needs_count,
        waiting=snapshot.waiting_count,
        stale=snapshot.stale_count,
    )]
    if snapshot.needs_count == 0 and snapshot.all_active == 0:
        lines.append(empty)
        return lines
    rows = snapshot.needs or snapshot.listed
    for index, item in enumerate(rows):
        prefix = "├" if index < len(rows) - 1 else "└"
        who = item.party_name or "Unidentified"
        lines.append(f"{prefix} **{item.ticket_id}** · {who} · {item.case_type}")
        lines.append(f"│  {item.display_title[:70]}")
        lines.append(f"│  → {item.next_label}" + (f" · {item.age_days}d" if item.age_days is not None else ""))
    if snapshot.stale_count:
        lines.append(hint.format(stale=snapshot.stale_count))
    return lines
