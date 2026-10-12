"""Paginated route-family list queries for Dashboard UI API v2."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from config.defaults import ACTIVE_PROJECT_STATUSES
from src.api.auth import CurrentActor
from src.models.enums import ProjectStatus
from src.api.schemas.ui_v2 import (
    ActivityListItemReadModel,
    DraftListItemReadModel,
    FacetReadModel,
    PartyListItemReadModel,
    ProjectListItemReadModel,
    ReviewListItemReadModel,
    SupportListItemReadModel,
    TimelineListItemReadModel,
)
from src.api.ui_v2_common import _age_days, _facets, _project_health, can_execute
from src.api.ui_v2_go_live import project_go_live_dates

_HEALTH_ORDER = {"red": 0, "yellow": 1, "green": 2}

def list_projects(
    conn: sqlite3.Connection,
    *,
    q: str | None,
    status: str | None,
    ball_with: str | None,
    milestone: str | None,
    market: str | None,
    health: str | None,
    active_only: bool,
    sort: str,
    owner: str | None = None,
) -> tuple[list[ProjectListItemReadModel], list[FacetReadModel]]:
    clauses = ["project_id NOT LIKE char(92) || '_%' ESCAPE char(92)"]
    params: list[Any] = []
    if active_only:
        placeholders = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
        clauses.append(f"project_status IN ({placeholders})")
        params.extend(ACTIVE_PROJECT_STATUSES)
    for column, value in (
        ("project_status", status),
        ("ball_with", ball_with),
        ("current_milestone", milestone),
        ("market", market),
        ("operator_id", owner),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if q:
        clauses.append("(project_id LIKE ? OR client_name LIKE ? OR next_action LIKE ?)")
        pattern = f"%{q.strip()}%"
        params.extend([pattern, pattern, pattern])

    rows = conn.execute(
        f"""SELECT project_id, client_name, market, project_status, ball_with,
                    current_milestone, target_go_live, next_action, updated_at,
                    last_activity_at, created_at, operator_id,
                    (SELECT actual_go_live FROM project_go_live_confirmations g
                     WHERE g.project_id = projects.project_id) AS actual_go_live,
                    (SELECT evidence_id FROM project_go_live_confirmations g
                     WHERE g.project_id = projects.project_id) AS actual_go_live_evidence_id,
                    (SELECT source_quote FROM project_go_live_confirmations g
                     WHERE g.project_id = projects.project_id) AS actual_go_live_source_quote
             FROM projects
             WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchall()
    go_live_dates = project_go_live_dates(conn, rows)
    items: list[ProjectListItemReadModel] = []
    for row in rows:
        days_stale = _age_days(row["last_activity_at"] or row["updated_at"] or row["created_at"]) or 0
        item_health = _project_health(str(row["project_status"]), str(row["ball_with"]), days_stale)
        if health and (row["project_status"] in ProjectStatus.terminal_statuses() or item_health != health):
            continue
        items.append(
            ProjectListItemReadModel(
                **go_live_dates[str(row["project_id"])].model_dump(),
                project_id=str(row["project_id"]),
                client_name=row["client_name"],
                market=row["market"],
                project_status=str(row["project_status"]),
                ball_with=str(row["ball_with"]),
                current_milestone=row["current_milestone"],
                target_go_live=row["target_go_live"],
                actual_go_live=row["actual_go_live"],
                actual_go_live_evidence_id=row["actual_go_live_evidence_id"],
                actual_go_live_source_quote=row["actual_go_live_source_quote"],
                next_action=row["next_action"],
                updated_at=row["updated_at"],
                last_activity_at=row["last_activity_at"],
                operator_id=str(row["operator_id"] or "default_operator"),
                health=item_health,
                days_stale=days_stale,
            )
        )
    if sort == "name":
        items.sort(key=lambda item: ((item.client_name or item.project_id).lower(), item.project_id))
    elif sort == "updated":
        items.sort(key=lambda item: item.updated_at or "", reverse=True)
    elif sort == "go_live":
        items.sort(key=lambda item: (item.target_go_live or "9999-12-31", item.project_id))
    else:
        items.sort(key=lambda item: (
            item.project_status in ProjectStatus.terminal_statuses(),
            _HEALTH_ORDER[item.health],
            -item.days_stale if item.project_status not in ProjectStatus.terminal_statuses() else 0,
            item.project_id,
        ))
    facets = _facets(
        {
            "health": ("Health", (item.health for item in items if item.project_status not in ProjectStatus.terminal_statuses())),
            "status": ("Status", (item.project_status for item in items)),
            "ball_with": ("Ball with", (item.ball_with for item in items)),
            "market": ("Market", (item.market or "Unspecified" for item in items)),
        }
    )
    return items, facets


def _decode_domains(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in value.split(",") if part.strip()]


def list_parties(
    conn: sqlite3.Connection,
    *,
    org_type: str | None,
    q: str | None,
    sort: str,
) -> tuple[list[PartyListItemReadModel], list[FacetReadModel]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if org_type:
        clauses.append("o.org_type = ?")
        params.append(org_type)
    if q:
        pattern = f"%{q.strip()}%"
        clauses.append("(o.org_name LIKE ? OR o.domains LIKE ? OR o.notes LIKE ?)")
        params.extend([pattern, pattern, pattern])
    rows = conn.execute(
        f"""SELECT o.org_id, o.org_name, o.org_type, o.domains, o.created_at,
                    COUNT(DISTINCT pp.project_id) AS project_count,
                    COUNT(DISTINCT c.contact_id) AS contact_count
             FROM organisations o
             LEFT JOIN project_parties pp ON pp.org_id = o.org_id
             LEFT JOIN contacts c ON c.organisation_id = o.org_id
             WHERE {' AND '.join(clauses)}
             GROUP BY o.org_id, o.org_name, o.org_type, o.domains, o.created_at""",
        params,
    ).fetchall()
    items = [
        PartyListItemReadModel(
            org_id=str(row["org_id"]),
            org_name=str(row["org_name"]),
            org_type=str(row["org_type"] or "unknown"),
            domains=_decode_domains(row["domains"]),
            project_count=int(row["project_count"] or 0),
            contact_count=int(row["contact_count"] or 0),
            created_at=row["created_at"],
        )
        for row in rows
    ]
    if sort == "projects":
        items.sort(key=lambda item: (-item.project_count, item.org_name.lower()))
    elif sort == "recent":
        items.sort(key=lambda item: item.created_at or "", reverse=True)
    else:
        items.sort(key=lambda item: item.org_name.lower())
    return items, _facets({"org_type": ("Organisation type", (item.org_type for item in items))})


_ACTIVE_TICKET_STATUSES = ("open", "investigating", "waiting_external", "waiting_internal")


def _latest_ticket_mail(conn: sqlite3.Connection, ticket_ids: list[str]) -> dict[str, dict[str, Any]]:
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


def list_support(
    conn: sqlite3.Connection,
    actor: CurrentActor,
    *,
    q: str | None,
    status: str | None,
    severity: str | None,
    sort: str,
    queue: str | None = None,
    case_type: str | None = None,
    unidentified_only: bool = False,
    related_project_id: str | None = None,
) -> tuple[list[SupportListItemReadModel], list[FacetReadModel], dict[str, int]]:
    from src.support.pm_queue import (
        TicketQueueInput,
        derive_ticket_queue,
        queue_counts,
        strip_disclaimer,
    )

    clauses = ["1=1"]
    params: list[Any] = []
    restrict_active = status == "open" or not status
    if restrict_active:
        placeholders = ",".join("?" for _ in _ACTIVE_TICKET_STATUSES)
        clauses.append(f"s.status IN ({placeholders})")
        params.extend(_ACTIVE_TICKET_STATUSES)
    elif status:
        clauses.append("s.status = ?")
        params.append(status)
    if severity:
        clauses.append("s.severity = ?")
        params.append(severity)
    if related_project_id:
        clauses.append("s.related_project_id = ?")
        params.append(related_project_id)
    if q:
        pattern = f"%{q.strip()}%"
        clauses.append(
            "(s.ticket_id LIKE ? OR s.title LIKE ? OR s.description LIKE ? "
            "OR s.counterparty LIKE ? OR s.reporter_email LIKE ? OR p.client_name LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
    rows = conn.execute(
        f"""SELECT s.ticket_id, s.title, s.category, s.severity, s.status,
                    s.related_project_id, s.description, s.reporter_email,
                    COALESCE(o.org_name, s.counterparty) AS counterparty,
                    s.assigned_team, s.updated_at, p.client_name AS related_project_name
             FROM support_tickets s
             LEFT JOIN organisations o ON o.org_id = s.counterparty_org_id
             LEFT JOIN projects p ON p.project_id = s.related_project_id
             WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchall()
    latest = _latest_ticket_mail(conn, [str(row["ticket_id"]) for row in rows])
    executable = can_execute(actor)
    derived: list[tuple[str, SupportListItemReadModel]] = []
    count_pairs: list[tuple[str, object]] = []
    for row in rows:
        data = dict(row)
        mail = latest.get(str(data["ticket_id"]), {})
        last_preview = mail.get("content_text") or mail.get("content_summary")
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
                last_preview=last_preview,
                last_activity_at=mail.get("received_at") or mail.get("created_at") or data.get("updated_at"),
                updated_at=data.get("updated_at"),
            ),
            related_project_name=data.get("related_project_name"),
        )
        item = SupportListItemReadModel(
            ticket_id=str(data["ticket_id"]),
            title=data["title"],
            display_title=view.display_title,
            category=data["category"],
            severity=data["severity"],
            status=data["status"],
            related_project_id=data.get("related_project_id"),
            related_project_name=view.related_project_name,
            counterparty=data.get("counterparty"),
            party_name=view.party_name,
            reporter_email=view.reporter_email,
            account_hint=view.account_hint,
            assigned_team=data.get("assigned_team"),
            updated_at=data.get("updated_at"),
            last_activity_at=view.last_activity_at,
            last_sender=view.last_sender,
            last_subject=view.last_subject,
            last_preview=view.last_preview or strip_disclaimer(data.get("description"))[:180] or None,
            age_days=view.age_days,
            ball_with=view.ball_with,
            case_type=view.case_type,
            queue=view.queue,
            next_action=view.next_action,
            on_project=view.on_project,
            is_noise=view.is_noise,
            already_answered=view.already_answered,
            mixed_thread=view.mixed_thread,
            can_execute=executable,
        )
        derived.append((str(data["status"]), item))
        count_pairs.append((str(data["status"]), view))

    counts = queue_counts(count_pairs)
    items = [item for _, item in derived]
    if queue == "on_project":
        items = [item for item in items if item.on_project]
    elif queue == "all":
        pass
    elif queue:
        items = [item for item in items if item.queue == queue]
    elif status is None:
        items = [item for item in items if not item.is_noise]
    if case_type:
        items = [item for item in items if item.case_type == case_type]
    if unidentified_only:
        items = [item for item in items if not item.party_name]
    if sort == "updated":
        items.sort(key=lambda item: item.last_activity_at or item.updated_at or "", reverse=True)
    elif sort == "title":
        items.sort(key=lambda item: (item.display_title or item.title).lower())
    else:
        items.sort(key=lambda item: (item.age_days is None, -(item.age_days or 0), item.last_activity_at or ""))
    return items, _facets(
        {
            "queue": ("Queue", (item.queue for item in items)),
            "case_type": ("Type", (item.case_type for item in items)),
            "status": ("Status", (item.status for item in items)),
        }
    ), counts


def list_drafts(
    conn: sqlite3.Connection,
    actor: CurrentActor,
    *,
    status: str | None,
    q: str | None,
) -> tuple[list[DraftListItemReadModel], list[FacetReadModel]]:
    from config.defaults import ACTIVE_PROJECT_STATUSES

    params: list[Any] = []
    if status:
        clauses = ["d.status = ?"]
        params.append(status)
    else:
        clauses = ["d.status IN ('created', 'in_review', 'send_failed')"]
    placeholders = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
    clauses.append(f"(p.project_status IN ({placeholders}) OR d.project_id IS NULL)")
    params.extend(ACTIVE_PROJECT_STATUSES)
    if q:
        pattern = f"%{q.strip()}%"
        clauses.append(
            "(d.draft_id LIKE ? OR d.subject LIKE ? OR d.current_draft_text LIKE ? "
            "OR d.to_email LIKE ? OR d.task_type LIKE ? OR p.client_name LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
    rows = conn.execute(
        f"""SELECT d.draft_id, d.project_id, p.client_name AS project_label,
                    d.ticket_id, d.task_type, d.subject, d.current_draft_text,
                    d.draft_version, d.to_email, d.cc, d.status, d.review_state,
                    d.delivery_status, d.scheduled_send_at, d.created_at
             FROM drafts d
             LEFT JOIN projects p ON p.project_id = d.project_id
             WHERE {' AND '.join(clauses)}
             ORDER BY d.created_at DESC""",
        params,
    ).fetchall()
    executable = can_execute(actor)
    items = [DraftListItemReadModel(**dict(row), can_execute=executable) for row in rows]
    return items, _facets(
        {
            "status": ("Status", (item.status for item in items)),
            "task_type": ("Type", (item.task_type or "other" for item in items)),
            "review_state": ("Review state", (item.review_state or "not_requested" for item in items)),
        }
    )


def list_reviews(
    conn: sqlite3.Connection,
    actor: CurrentActor,
    *,
    risk: str | None,
    q: str | None,
) -> tuple[list[ReviewListItemReadModel], list[FacetReadModel]]:
    from config.defaults import ACTIVE_PROJECT_STATUSES

    placeholders = ",".join("?" for _ in ACTIVE_PROJECT_STATUSES)
    clauses = ["pu.decision_outcome = 'needs_review'", f"p.project_status IN ({placeholders})"]
    params: list[Any] = list(ACTIVE_PROJECT_STATUSES)
    if risk:
        clauses.append("pu.risk_level = ?")
        params.append(risk)
    if q:
        pattern = f"%{q.strip()}%"
        clauses.append(
            "(pu.update_id LIKE ? OR pu.task_type LIKE ? OR p.client_name LIKE ? "
            "OR pu.proposed_changes LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern])
    rows = conn.execute(
        f"""SELECT pu.update_id, pu.project_id, p.client_name AS project_label,
                    pu.proposed_changes, pu.task_type, pu.confidence, pu.risk_level,
                    pu.created_at, pu.assessment_json, pu.policy_version,
                    pu.source_type, pu.source_ref
             FROM proposed_updates pu
             JOIN projects p ON p.project_id = pu.project_id
             WHERE {' AND '.join(clauses)}
             ORDER BY pu.created_at DESC""",
        params,
    ).fetchall()
    from src.project_reconciliation.repository import proposed_changes_are_notes_only

    executable = can_execute(actor)
    items = []
    for row in rows:
        if (
            str(row["task_type"] or "") == "email_reconciliation"
            and proposed_changes_are_notes_only(row["proposed_changes"])
        ):
            continue
        items.append(ReviewListItemReadModel(**dict(row), can_execute=executable))
    return items, _facets(
        {
            "risk_level": ("Risk", (item.risk_level or "unknown" for item in items)),
            "task_type": ("Task type", (item.task_type or "unknown" for item in items)),
        }
    )


def list_activity(
    conn: sqlite3.Connection,
    *,
    action_type: str | None,
    project_id: str | None,
    q: str | None,
) -> tuple[list[ActivityListItemReadModel], list[FacetReadModel]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if action_type:
        clauses.append("action_type = ?")
        params.append(action_type)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if q:
        pattern = f"%{q.strip()}%"
        clauses.append("(summary LIKE ? OR actor LIKE ? OR object_ref LIKE ?)")
        params.extend([pattern, pattern, pattern])
    rows = conn.execute(
        f"""SELECT id, project_id, actor, surface, action_type, object_ref, summary, created_at
             FROM activity_log
             WHERE {' AND '.join(clauses)}
             ORDER BY created_at DESC""",
        params,
    ).fetchall()
    items = [ActivityListItemReadModel(**dict(row)) for row in rows]
    return items, _facets(
        {
            "action_type": ("Action type", (item.action_type for item in items)),
            "surface": ("Surface", (item.surface or "unknown" for item in items)),
        }
    )



def list_timeline(
    conn: sqlite3.Connection,
    *,
    q: str | None,
    health: str | None,
    schedule_status: str | None,
    sort: str,
) -> tuple[list[TimelineListItemReadModel], list[FacetReadModel]]:
    from src.api.routers.timeline import MILESTONE_RANK, build_timeline

    items = [TimelineListItemReadModel(**item) for item in build_timeline(conn)]
    if q:
        query = q.strip().lower()
        items = [
            item for item in items
            if query in item.project_id.lower()
            or query in (item.client_name or "").lower()
            or query in (item.current_milestone or "").lower()
        ]
    if health:
        items = [item for item in items if item.health == health]
    if schedule_status:
        items = [item for item in items if item.schedule_status == schedule_status]

    if sort == "go_live":
        date_status_order = {"overdue": 0, "due_soon": 0, "scheduled": 0, "invalid": 1, "no_date": 2}
        items.sort(
            key=lambda item: (
                date_status_order[item.schedule_status],
                item.target_go_live or "",
                item.project_id,
            )
        )
    elif sort == "risk":
        health_order = {"red": 0, "yellow": 1, "green": 2}
        schedule_order = {"overdue": 0, "due_soon": 1, "invalid": 2, "scheduled": 3, "no_date": 4}
        items.sort(key=lambda item: (health_order[item.health], schedule_order[item.schedule_status], item.days_to_go_live or 0, item.project_id))
    elif sort == "name":
        items.sort(key=lambda item: ((item.client_name or item.project_id).lower(), item.project_id))
    else:
        items.sort(key=lambda item: (-MILESTONE_RANK.get(item.current_milestone, 0), item.target_go_live is None, item.target_go_live or "", item.project_id))

    return items, _facets(
        {
            "health": ("Health", (item.health for item in items)),
            "schedule_status": ("Schedule", (item.schedule_status for item in items)),
            "ball_with": ("Ball with", (item.ball_with or "Unknown" for item in items)),
        }
    )
